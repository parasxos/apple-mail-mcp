"""Outgoing mail: compose RFC-822 messages and deliver them.

Why not Mail.app? Its AppleScript compose path wraps the body in a collapsed
`<blockquote class="Apple-Mail-URLShareWrapperClass">` that renders as an
*empty* message in Outlook/Exchange. Verified across six compose variants —
it is the OS, not our scripting. So we bypass Mail.app for sending and build
RFC-822 ourselves.

Delivery runs over an existing SSH session to a CERN host; if the session is
cold, a headless bootstrap script re-establishes it.

Safety: while `EMAIL_MCP_SEND_ALLOW_ALL` is off (the default), every
recipient must be on the allowlist — which defaults to *just the From:
address*. A mistake during the trial can therefore only reach Paris himself.
"""
from __future__ import annotations

import html as _html
import mimetypes
import re
import subprocess
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from pathlib import Path

from . import config
from .log import get_logger

_log = get_logger()


class SendError(Exception):
    """Raised for caller-fixable send failures (blocked recipient, dead
    transport, empty fields). The message is safe to surface verbatim."""


@dataclass
class SendResult:
    ok: bool
    message_id: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    subject: str = ""
    attachments: list[str] = field(default_factory=list)
    bootstrapped: bool = False
    error: str | None = None


# --------------------------------------------------------------------- #
# address handling                                                      #
# --------------------------------------------------------------------- #


def _split(addrs: str | list[str] | None) -> list[str]:
    """Normalise a recipient field (str with commas, or list) to a clean list
    of 'Name <addr>' / 'addr' entries, dropping empties."""
    if not addrs:
        return []
    if isinstance(addrs, str):
        parts = [a for _, a in getaddresses([addrs])]
        # getaddresses discards display names; re-run to keep them.
        pairs = getaddresses([addrs])
        return [formataddr(p) if p[0] else p[1] for p in pairs if p[1]]
    out: list[str] = []
    for item in addrs:
        for name, addr in getaddresses([item]):
            if addr:
                out.append(formataddr((name, addr)) if name else addr)
    return out


def _bare(addr: str) -> str:
    """Extract the lower-cased bare address from a 'Name <addr>' entry."""
    return parseaddr(addr)[1].strip().lower()


def _enforce_allowlist(recipients: list[str]) -> None:
    if config.send_allow_all():
        return
    allowed = config.send_allowlist()
    blocked = sorted({_bare(r) for r in recipients if _bare(r) not in allowed})
    if blocked:
        raise SendError(
            "Refusing to send: recipient(s) not on the allowlist — "
            f"{', '.join(blocked)}. Sending is restricted to "
            f"{', '.join(sorted(allowed))} until EMAIL_MCP_SEND_ALLOW_ALL=1 "
            "is set. (Trial-safety guard: mistakes can only reach you.)"
        )


# --------------------------------------------------------------------- #
# attachments                                                           #
# --------------------------------------------------------------------- #


def _attachment_paths(attachments: str | list[str] | None) -> list[Path]:
    """Normalise the attachments argument to a list of Paths.

    A bare string is ONE path — never comma-split (paths may legally
    contain commas); pass a list for multiple files.
    """
    if not attachments:
        return []
    items = [attachments] if isinstance(attachments, str) else list(attachments)
    return [Path(p).expanduser() for p in items if str(p).strip()]


def _load_attachments(
    attachments: str | list[str] | None,
) -> list[tuple[bytes, str, str, str]]:
    """Read attachment files, returning (data, maintype, subtype, filename)
    tuples ready for EmailMessage.add_attachment. Raises SendError with a
    caller-fixable message for missing files, directories, or a total size
    over the configured budget."""
    paths = _attachment_paths(attachments)
    if not paths:
        return []

    loaded: list[tuple[bytes, str, str, str]] = []
    total = 0
    for p in paths:
        if not p.exists():
            raise SendError(f"attachment not found: {p}")
        if p.is_dir():
            raise SendError(
                f"attachment is a directory: {p} — zip it first and attach "
                "the archive."
            )
        try:
            data = p.read_bytes()
        except OSError as e:
            raise SendError(f"cannot read attachment {p}: {e}") from e
        total += len(data)
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        loaded.append((data, maintype, subtype, p.name))

    budget = config.send_max_attach_mb()
    if total > budget * 1024 * 1024:
        raise SendError(
            f"attachments total {total / (1024 * 1024):.1f} MB, over the "
            f"{budget:g} MB budget (base64 adds ~33% on top; servers commonly "
            "reject large mail). Shrink the set, or raise "
            "EMAIL_MCP_MAX_ATTACH_MB if the recipient's server allows it."
        )
    return loaded


# --------------------------------------------------------------------- #
# MIME composition                                                      #
# --------------------------------------------------------------------- #


def _html_paras(text: str) -> str:
    return "".join(
        "<p>" + _html.escape(p).replace("\n", "<br>") + "</p>"
        for p in text.split("\n\n")
        if p.strip()
    )


def _html_body(text: str, quote_html: str = "") -> str:
    return f"<html><body>{_html_paras(text)}{quote_html}</body></html>"


# --------------------------------------------------------------------- #
# reply-history quoting                                                 #
# --------------------------------------------------------------------- #

_HTML_INNER_RE = re.compile(r"(?is)^.*?<body[^>]*>(.*)</body>.*$")
_TAG_BLOCK_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def _attribution(ref) -> str:
    """'On Mon, 13 Jul 2026 at 09:24, Name <addr> wrote:' — the original's
    UTC timestamp rendered in local time, matching what mail clients write."""
    stamp = ref.date.astimezone().strftime("%a, %d %b %Y at %H:%M")
    return f"On {stamp}, {ref.from_addr} wrote:"


def _strip_tags(html_doc: str) -> str:
    """Crude HTML→text for quoting when the original has no plain part."""
    text = _TAG_BLOCK_RE.sub("", html_doc)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _quote_plain(original_text: str, original_html: str, attribution: str) -> str:
    source = original_text.strip() or _strip_tags(original_html)
    quoted = "\n".join("> " + line for line in source.rstrip().splitlines())
    return f"{attribution}\n{quoted}" if quoted else attribution


def _quote_html(original_html: str, original_text: str, attribution: str) -> str:
    if original_html.strip():
        m = _HTML_INNER_RE.match(original_html)
        inner = m.group(1) if m else original_html
    else:
        inner = _html_paras(original_text)
    return (
        f"<div>{_html.escape(attribution)}</div>"
        '<blockquote type="cite" style="margin:0 0 0 0.8ex;'
        f'border-left:2px solid #cccccc;padding-left:1ex">{inner}</blockquote>'
    )


def compose(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
    quote_text: str = "",
    quote_html: str = "",
    attachments: list[tuple[bytes, str, str, str]] | None = None,
) -> EmailMessage:
    """Build a multipart/alternative message (plain + minimal HTML).

    The HTML part is a plain `<p>`-wrapped rendering — no Apple wrapper class,
    so it displays as normal body text everywhere. `quote_text`/`quote_html`
    carry an optional quoted-history block appended below the body (plain
    `>`-prefixed lines / a `<blockquote type="cite">`).

    `attachments` are pre-loaded (data, maintype, subtype, filename) tuples
    (see `_load_attachments`); adding one wraps the alternative pair in
    multipart/mixed, which is exactly the structure normal clients emit.
    """
    from_addr = config.send_from_addr()
    msg = EmailMessage()
    msg["From"] = formataddr((config.send_from_name(), from_addr))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = (references + " " + in_reply_to).strip()
        msg["References"] = refs
    msg.set_content(f"{body}\n\n{quote_text}\n" if quote_text else body)
    msg.add_alternative(_html_body(body, quote_html), subtype="html")
    for data, maintype, subtype, filename in attachments or []:
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename
        )
    return msg


# --------------------------------------------------------------------- #
# delivery                                                              #
# --------------------------------------------------------------------- #


def _ssh_base() -> list[str]:
    return [
        "ssh",
        "-o", f"ControlPath={config.send_ssh_socket()}",
        "-o", "BatchMode=yes",
        f"{config.send_user()}@{config.send_host()}",
    ]


def _socket_alive() -> bool:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check",
             "-o", f"ControlPath={config.send_ssh_socket()}",
             f"{config.send_user()}@{config.send_host()}"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log.warning(
            "socket check errored (%s) after %.1fs",
            type(e).__name__, time.monotonic() - t0,
        )
        return False
    alive = proc.returncode == 0
    _log.info(
        "socket check: %s (%.2fs)",
        "alive" if alive else "dead", time.monotonic() - t0,
    )
    return alive


def _kill_master() -> None:
    """Tear down the ControlMaster socket (best-effort). Used after a hung
    delivery pipe: a wedged master passes `-O check` yet stalls every
    channel, so killing it lets the next send bootstrap a fresh session."""
    try:
        subprocess.run(
            ["ssh", "-O", "exit",
             "-o", f"ControlPath={config.send_ssh_socket()}",
             f"{config.send_user()}@{config.send_host()}"],
            capture_output=True, text=True, timeout=10,
        )
        _log.warning("killed suspect ControlMaster socket after hung delivery")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log.warning("could not kill ControlMaster: %s", type(e).__name__)


def _bootstrap_master() -> bool:
    """Run the headless bootstrap to (re)establish the master socket."""
    cmd = config.send_bootstrap_cmd()
    if not cmd:
        _log.warning("no bootstrap command configured; cannot re-establish SSH")
        return False
    t0 = time.monotonic()
    _log.info("bootstrapping SSH master: %s", cmd)
    try:
        proc = subprocess.run(
            ["bash", cmd] if cmd.endswith(".sh") else ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        _log.error(
            "bootstrap errored (%s) after %.1fs",
            type(e).__name__, time.monotonic() - t0,
        )
        return False
    _log.info(
        "bootstrap exit %d (%.1fs)%s",
        proc.returncode, time.monotonic() - t0,
        "" if proc.returncode == 0
        else f" stderr: {(proc.stderr or '').strip()[:500]}",
    )
    return _socket_alive()


def _deliver(msg: EmailMessage) -> None:
    """Pipe the message to the remote delivery command on the SSH host.
    Raises SendError on delivery failure with the remote stderr attached."""
    raw = msg.as_bytes()
    remote = f"{config.send_delivery_cmd()} -t -i -f {config.send_from_addr()}"
    t0 = time.monotonic()
    _log.info(
        "deliver start: %s, %d bytes, to=%s",
        msg["Message-ID"], len(raw), msg["To"],
    )
    try:
        proc = subprocess.run(
            _ssh_base() + [remote],
            input=raw, capture_output=True, timeout=60,
        )
    except FileNotFoundError as e:
        _log.error("deliver failed: ssh not found on PATH")
        raise SendError("ssh not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        _log.error(
            "deliver timed out after %.0fs: %s — master passed check but the "
            "pipe hung; killing the socket so the next send re-bootstraps",
            time.monotonic() - t0, msg["Message-ID"],
        )
        _kill_master()
        raise SendError(
            "delivery pipe timed out after 60s — the SSH master looked alive "
            "but the session hung (stale ControlMaster). The socket has been "
            "reset; retry once and the send will re-bootstrap. Log: "
            f"{config.log_file() or 'disabled'}."
        ) from e
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        _log.error(
            "deliver failed (exit %d, %.1fs): %s",
            proc.returncode, time.monotonic() - t0, err[:500] or "no stderr",
        )
        raise SendError(
            f"delivery failed (exit {proc.returncode}): "
            f"{err or 'no stderr'}"
        )
    _log.info(
        "deliver ok: %s (%.1fs)", msg["Message-ID"], time.monotonic() - t0
    )


# --------------------------------------------------------------------- #
# public API                                                            #
# --------------------------------------------------------------------- #


def send_email(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
    quote_text: str = "",
    quote_html: str = "",
    attachments: str | list[str] | None = None,
) -> SendResult:
    to_l, cc_l, bcc_l = _split(to), _split(cc), _split(bcc)
    if not to_l:
        raise SendError("`to` is required (no valid recipient address).")
    if not subject:
        raise SendError("`subject` is required.")
    if not body.strip():
        raise SendError("`body` is empty.")

    attach_loaded = _load_attachments(attachments)

    if config.send_bcc_self():
        self_addr = config.send_from_addr()
        if _bare(self_addr) not in {_bare(b) for b in bcc_l}:
            bcc_l.append(self_addr)

    _enforce_allowlist(to_l + cc_l + bcc_l)

    msg = compose(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc_l,
        in_reply_to=in_reply_to, references=references,
        quote_text=quote_text, quote_html=quote_html,
        attachments=attach_loaded,
    )

    bootstrapped = False
    if not _socket_alive():
        bootstrapped = _bootstrap_master()
        if not _socket_alive():
            raise SendError(
                "No live SSH ControlMaster to "
                f"{config.send_user()}@{config.send_host()} and bootstrap "
                "failed. Establish the socket (2FA) then retry — e.g. run "
                f"{config.send_bootstrap_cmd()}."
            )

    _deliver(msg)
    return SendResult(
        ok=True,
        message_id=msg["Message-ID"],
        to=to_l, cc=cc_l, bcc=bcc_l,
        subject=subject,
        attachments=[fn for _, _, _, fn in attach_loaded],
        bootstrapped=bootstrapped,
    )


def reply_email(
    source,
    *,
    id: str,
    body: str,
    reply_all: bool = False,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    include_history: bool = True,
    attachments: str | list[str] | None = None,
) -> SendResult:
    """Reply to message `id`, threading correctly (In-Reply-To / References /
    Re: subject). Defaults to replying to the original sender only; set
    reply_all=True to include the original To+Cc (minus your own address).

    The original message is quoted below the reply (attribution line +
    `>`-prefixed plain text; `<blockquote type="cite">` in HTML), the way a
    normal mail client's Reply does. Pass include_history=False for a bare
    reply carrying only threading headers.
    """
    original = source.get(id)  # Email dataclass
    headers = original.headers
    orig_msgid = headers.get("Message-ID") or headers.get("Message-Id") or ""
    orig_refs = headers.get("References", "")
    orig_subject = original.ref.subject or headers.get("Subject", "")
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

    reply_to = headers.get("Reply-To") or original.ref.from_addr
    to_l = _split(reply_to)

    self_bare = _bare(config.send_from_addr())
    cc_l = _split(cc)
    if reply_all:
        extra = _split(original.ref.to) + _split(original.ref.cc)
        for a in extra:
            if _bare(a) != self_bare and _bare(a) not in {_bare(x) for x in to_l + cc_l}:
                cc_l.append(a)

    quote_text = quote_html = ""
    if include_history and (original.body_text.strip() or original.body_html.strip()):
        attribution = _attribution(original.ref)
        quote_text = _quote_plain(original.body_text, original.body_html, attribution)
        quote_html = _quote_html(original.body_html, original.body_text, attribution)

    return send_email(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc,
        in_reply_to=orig_msgid, references=orig_refs,
        quote_text=quote_text, quote_html=quote_html,
        attachments=attachments,
    )
