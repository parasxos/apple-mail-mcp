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
import subprocess
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr

from . import config


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
# MIME composition                                                      #
# --------------------------------------------------------------------- #


def _html_body(text: str) -> str:
    paras = "".join(
        "<p>" + _html.escape(p).replace("\n", "<br>") + "</p>"
        for p in text.split("\n\n")
        if p.strip()
    )
    return f"<html><body>{paras}</body></html>"


def compose(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
) -> EmailMessage:
    """Build a multipart/alternative message (plain + minimal HTML).

    The HTML part is a plain `<p>`-wrapped rendering — no Apple wrapper class,
    so it displays as normal body text everywhere.
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
    msg.set_content(body)
    msg.add_alternative(_html_body(body), subtype="html")
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
    try:
        proc = subprocess.run(
            ["ssh", "-O", "check",
             "-o", f"ControlPath={config.send_ssh_socket()}",
             f"{config.send_user()}@{config.send_host()}"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _bootstrap_master() -> bool:
    """Run the headless bootstrap to (re)establish the master socket."""
    cmd = config.send_bootstrap_cmd()
    if not cmd:
        return False
    try:
        subprocess.run(
            ["bash", cmd] if cmd.endswith(".sh") else ["bash", "-c", cmd],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return _socket_alive()


def _deliver(msg: EmailMessage) -> None:
    """Pipe the message to the remote delivery command on the SSH host.
    Raises SendError on delivery failure with the remote stderr attached."""
    raw = msg.as_bytes()
    remote = f"{config.send_delivery_cmd()} -t -i -f {config.send_from_addr()}"
    try:
        proc = subprocess.run(
            _ssh_base() + [remote],
            input=raw, capture_output=True, timeout=60,
        )
    except FileNotFoundError as e:
        raise SendError("ssh not found on PATH.") from e
    except subprocess.TimeoutExpired as e:
        raise SendError("delivery pipe timed out after 60s.") from e
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise SendError(
            f"delivery failed (exit {proc.returncode}): "
            f"{err or 'no stderr'}"
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
) -> SendResult:
    to_l, cc_l, bcc_l = _split(to), _split(cc), _split(bcc)
    if not to_l:
        raise SendError("`to` is required (no valid recipient address).")
    if not subject:
        raise SendError("`subject` is required.")
    if not body.strip():
        raise SendError("`body` is empty.")

    if config.send_bcc_self():
        self_addr = config.send_from_addr()
        if _bare(self_addr) not in {_bare(b) for b in bcc_l}:
            bcc_l.append(self_addr)

    _enforce_allowlist(to_l + cc_l + bcc_l)

    msg = compose(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc_l,
        in_reply_to=in_reply_to, references=references,
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
) -> SendResult:
    """Reply to message `id`, threading correctly (In-Reply-To / References /
    Re: subject). Defaults to replying to the original sender only; set
    reply_all=True to include the original To+Cc (minus your own address).
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

    return send_email(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc,
        in_reply_to=orig_msgid, references=orig_refs,
    )
