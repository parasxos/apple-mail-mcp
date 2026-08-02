"""Outgoing mail: compose RFC-822 messages and deliver them.

Why not Mail.app? Its AppleScript compose path wraps the body in a collapsed
`<blockquote class="Apple-Mail-URLShareWrapperClass">` that renders as an
*empty* message in Outlook/Exchange. Verified across six compose variants —
it is the OS, not our scripting. So we bypass Mail.app for sending and build
RFC-822 ourselves.

Delivery is routed by identity (see `email_mcp.identities`): the resolved
From: address names a `MailTransport` driver — `ssh_sendmail` (the original
CERN path), `smtp`, or `pipe` (see `email_mcp.transports`). With no
identities file a single default identity is synthesized from the env
getters, so the original env-only setup keeps working unchanged.

Safety: sending is unrestricted unless the identity DECLARES a restriction
— an `allowlist`, or `allow_all = false` (env: `EMAIL_MCP_SEND_ALLOWLIST`
/ `EMAIL_MCP_SEND_ALLOW_ALL=0`). An engaged guard restricts recipients to
the allowlist plus the identity's own address, so a trial mistake can only
reach the sender himself; day to day, the client's per-send permission
prompt is the checkpoint.
"""
from __future__ import annotations

import html as _html
import mimetypes
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
from pathlib import Path

from . import codes, config, identities, transports
from .log import get_logger
from .transports import SendError  # re-export: same class everywhere

_log = get_logger()


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


@dataclass
class DraftResult:
    """A draft filed in the identity's own Drafts folder — intent handed
    back, never executed (docs/draft-design.md). `draft_id` is the Graph
    message id (durable); `message_id` is the composer-minted Message-ID,
    the cross-store join key a local search can find once Mail syncs."""
    ok: bool
    draft_id: str
    message_id: str
    to: list[str]
    cc: list[str] = field(default_factory=list)
    subject: str = ""
    folder: str = "drafts"
    account: str = ""


# --------------------------------------------------------------------- #
# address handling                                                      #
# --------------------------------------------------------------------- #

# Header-injection fence: CR/LF/NUL anywhere in a header-bound value lets a
# caller smuggle extra headers (Bcc:, a second From:) into the wire format.
# Provenance rule: REFUSE what the model supplies (compose-time check below);
# SANITIZE what the store supplies (reply subjects are space-joined instead).
_CTL_RE = re.compile(r"[\r\n\x00]")


def _reject_header_injection(fields: dict[str, object]) -> None:
    """Refuse control characters in model-supplied header values."""
    for name, value in fields.items():
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item and _CTL_RE.search(str(item)):
                raise SendError(
                    f"header_injection: control character (CR/LF/NUL) in "
                    f"`{name}`: {str(item)!r} — headers are single-line; "
                    "put extra recipients in to/cc/bcc, extra text in body.",
                    code=codes.HEADER_INJECTION,
                )


def _validate_bare_addresses(field: str, addrs: list[str]) -> None:
    """Each split entry must contain a plausible bare address: exactly one
    @ with non-empty sides and no whitespace/control characters."""
    for entry in addrs:
        bare = parseaddr(entry)[1].strip()
        local, sep, domain = bare.partition("@")
        if (not sep or not local or not domain or "@" in domain
                or any(c.isspace() or ord(c) < 0x20 for c in bare)):
            raise SendError(
                f"invalid_recipient: {entry!r} in `{field}` is not a usable "
                "address (want user@domain, optionally as 'Name <user@domain>').",
                code=codes.INVALID_RECIPIENT,
            )


def _recipient_lists(
    to: str | list[str] | None,
    cc: str | list[str] | None,
    bcc: str | list[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    """to/cc/bcc → clean lists. Raw CR/LF/NUL is rejected BEFORE _split
    (getaddresses would degrade an injected line into extra recipients),
    then every split entry gets the bare-address sanity check."""
    out: list[list[str]] = []
    for name, raw in (("to", to), ("cc", cc), ("bcc", bcc)):
        items = [] if not raw else ([raw] if isinstance(raw, str) else list(raw))
        for item in items:
            if _CTL_RE.search(str(item)):
                raise SendError(
                    f"invalid_recipient: control character (CR/LF/NUL) in "
                    f"`{name}`: {str(item)!r} — addresses are single-line, "
                    "comma-separated.",
                    code=codes.INVALID_RECIPIENT,
                )
        split = _split(raw)
        _validate_bare_addresses(name, split)
        out.append(split)
    return out[0], out[1], out[2]


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


def _enforce_allowlist(recipients: list[str], ident: identities.Identity) -> None:
    # The identity OWNS the whole decision: env synthesis bakes
    # config.send_allow_all() into the synthesized identity, a TOML block
    # bakes its own declaration in at load. Consulting config here too was
    # a second spelling of the same rule.
    if ident.allow_all:
        return
    # An engaged guard always admits the identity's own address: bcc_self
    # rides on every send, and a guard that blocks even self-mail blocked
    # EVERYTHING for a TOML identity with allow_all = false and no list.
    allowed = ({a.strip().lower() for a in ident.allowlist}
               | {_bare(ident.from_addr)})
    blocked = sorted({_bare(r) for r in recipients if _bare(r) not in allowed})
    if blocked:
        raise SendError(
            f"Refusing to send as identity [{ident.name}]: recipient(s) not "
            f"on its allowlist — {', '.join(blocked)}. Sending is restricted "
            f"to {', '.join(sorted(allowed))} by this identity's declared "
            f"guard (allowlist / allow_all = false) — extend the allowlist, "
            f"or remove the declaration to lift it.",
            code=codes.RECIPIENT_NOT_ALLOWED,
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
            raise SendError(f"attachment not found: {p}",
                            code=codes.ATTACHMENT_NOT_FOUND)
        if p.is_dir():
            raise SendError(
                f"attachment is a directory: {p} — zip it first and attach "
                "the archive.",
                code=codes.ATTACHMENT_UNREADABLE,
            )
        try:
            data = p.read_bytes()
        except OSError as e:
            raise SendError(f"cannot read attachment {p}: {e}",
                            code=codes.ATTACHMENT_UNREADABLE) from e
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
            "EMAIL_MCP_MAX_ATTACH_MB if the recipient's server allows it.",
            code=codes.ATTACHMENTS_TOO_LARGE,
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
        # Read-side fence: quoted history must not carry live script/style
        # from a hostile original into the outgoing message.
        inner = _TAG_BLOCK_RE.sub("", inner)
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
    identity: identities.Identity | None = None,
) -> EmailMessage:
    """Build a multipart/alternative message (plain + minimal HTML).

    The HTML part is a plain `<p>`-wrapped rendering — no Apple wrapper class,
    so it displays as normal body text everywhere. `quote_text`/`quote_html`
    carry an optional quoted-history block appended below the body (plain
    `>`-prefixed lines / a `<blockquote type="cite">`).

    `identity` supplies the From: header and the Message-ID domain; None
    means the default identity (which, with no identities file, mirrors the
    env getters exactly as before).

    `attachments` are pre-loaded (data, maintype, subtype, filename) tuples
    (see `_load_attachments`); adding one wraps the alternative pair in
    multipart/mixed, which is exactly the structure normal clients emit.
    """
    ident = identity if identity is not None else identities.get(None)
    from_addr = ident.from_addr
    _reject_header_injection({
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
        "to": to,
        "cc": cc or [],
        "bcc": bcc or [],
        "from_addr": from_addr,
        "from_name": ident.from_name,
    })
    msg = EmailMessage()
    try:
        # Belt to the fence above: the stdlib refuses some malformed header
        # values with a bare ValueError — surface those as caller-fixable
        # SendErrors instead of a traceback on the MCP wire.
        msg["From"] = formataddr((ident.from_name, from_addr))
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
    except ValueError as e:
        raise SendError(f"invalid header content: {e}",
                        code=codes.INVALID_HEADER) from e
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
#
# The module-level functions below are thin delegates to the DEFAULT
# identity's transport. They are load-bearing seams: preflight() and
# deliver_for() route default-identity traffic THROUGH them, so a caller
# (or a test) replacing them intercepts the real flow. Named non-default
# identities go straight to their own transport.


def _default_transport():
    """A fresh transport for the current default identity (late-binding,
    like the identity loader itself)."""
    return transports.get_transport(identities.get(None))


def _is_default(ident: identities.Identity) -> bool:
    """True when `ident` is (or names) the file's default identity."""
    return ident.name == identities.get(None).name


def _socket_alive() -> bool:
    """Is the default transport's session ready? (Seam; the ssh-flavoured
    name is kept — drivers without a session fall back to ensure().)"""
    t = _default_transport()
    check = getattr(t, "socket_alive", None)
    return check() if check is not None else t.ensure()


def _kill_master() -> None:
    """Tear down the default transport's session, best-effort. (Seam.)"""
    t = _default_transport()
    kill = getattr(t, "kill_master", None)
    if kill is not None:
        kill()


def _bootstrap_master() -> bool:
    """(Re)establish the default transport's session headlessly. (Seam.)"""
    t = _default_transport()
    boot = getattr(t, "bootstrap_master", None)
    return boot() if boot is not None else t.ensure()


def _raw_mail_from(raw: bytes) -> str:
    """Envelope sender = the raw message's OWN From: header, frozen at
    compose time — never re-resolved from mutable config at fire time."""
    hdr = BytesHeaderParser().parsebytes(raw, headersonly=True)
    pairs = getaddresses([str(hdr.get("From", ""))])
    return pairs[0][1].strip() if pairs and pairs[0][1] else ""


def _raw_rcpt_to(raw: bytes) -> list[str]:
    """Envelope recipients = the raw message's To + Cc + Bcc headers."""
    hdr = BytesHeaderParser().parsebytes(raw, headersonly=True)
    fields = [str(v) for k in ("To", "Cc", "Bcc") for v in (hdr.get_all(k) or [])]
    return [a.strip() for _, a in getaddresses(fields) if a.strip()]


def _deliver(msg: EmailMessage) -> None:
    """Deliver a composed message via the default identity's transport.
    (Seam: default-identity send_email lands here.)"""
    _deliver_bytes(msg.as_bytes())


def _deliver_bytes(raw: bytes) -> None:
    """Deliver pre-serialised RFC-822 bytes via the default identity's
    transport. (Seam: the dispatcher's default-identity replays land here.)"""
    ident = identities.get(None)
    transports.get_transport(ident).deliver(
        raw,
        mail_from=_raw_mail_from(raw) or ident.from_addr,
        rcpt_to=_raw_rcpt_to(raw),
    )


_last_preflight_error: str | None = None


def preflight(ident: identities.Identity) -> tuple[bool, bool]:
    """Make `ident`'s transport ready to deliver → (ok, bootstrapped).

    The default identity runs the pre-0.7.0 flow through the seams above
    (socket check, then bootstrap); other identities use their transport's
    own ensure(). Failure reasons feed _transport_unavailable()."""
    global _last_preflight_error
    _last_preflight_error = None
    if _is_default(ident) and hasattr(
            transports.get_transport(ident), "socket_alive"):
        # The seam flow is gated on the transport actually being
        # SESSION-shaped, not on being the default: an smtp default used
        # to be pushed through check/boot/check here, dialling three
        # times and then reporting a DNS failure as "session dead —
        # establish it (2FA)" (first-user machine, 2026-08-01). The
        # failure reason belongs to the transport that failed, in its
        # own vocabulary.
        if _socket_alive():
            return True, False
        bootstrapped = _bootstrap_master()
        if _socket_alive():
            return True, bootstrapped
        boot = str(ident.params.get("bootstrap", "") or "")
        _last_preflight_error = (
            "session dead and bootstrap failed. Establish it (2FA) then "
            "retry" + (f" — e.g. run {boot}." if boot else ".")
        )
        return False, bootstrapped
    transport = transports.get_transport(ident)
    if transport.ensure():
        return True, False
    _last_preflight_error = transport.last_ensure_error
    return False, False


def _transport_unavailable(ident: identities.Identity) -> str:
    """The transport-unavailable line: names the lane and carries the
    ensure error (or a generic hint) so a failed send says what broke."""
    reason = _last_preflight_error or "transport not ready (see log)"
    _log.error("transport unavailable for [%s/%s]: %s",
               ident.name, ident.driver, reason)
    return f"[{ident.name}/{ident.driver}] transport unavailable: {reason}"


def deliver_for(
    ident: identities.Identity,
    raw: bytes,
    rcpt_to: list[str] | None = None,
) -> None:
    """Deliver raw RFC-822 bytes as `ident`. Default-identity traffic goes
    through the _deliver_bytes seam (which re-derives the envelope from the
    frozen headers); other identities go straight to their transport, with
    `rcpt_to` (or, if None, the raw To/Cc/Bcc headers) as the envelope."""
    if _is_default(ident):
        _deliver_bytes(raw)
        return
    envelope = (
        [b for b in (_bare(r) for r in rcpt_to) if b]
        if rcpt_to is not None
        else _raw_rcpt_to(raw)
    )
    transports.get_transport(ident).deliver(
        raw,
        mail_from=_raw_mail_from(raw) or ident.from_addr,
        rcpt_to=envelope,
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
    from_identity: str | None = None,
) -> SendResult:
    ident = identities.get(from_identity)
    to_l, cc_l, bcc_l = _recipient_lists(to, cc, bcc)
    if not to_l:
        raise SendError("`to` is required (no valid recipient address).",
                        code=codes.INVALID_INPUT)
    if not subject:
        raise SendError("`subject` is required.", code=codes.INVALID_INPUT)
    if not body.strip():
        raise SendError("`body` is empty.", code=codes.INVALID_INPUT)

    attach_loaded = _load_attachments(attachments)

    if ident.bcc_self:
        if _bare(ident.from_addr) not in {_bare(b) for b in bcc_l}:
            bcc_l.append(ident.from_addr)

    _enforce_allowlist(to_l + cc_l + bcc_l, ident)

    msg = compose(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc_l,
        in_reply_to=in_reply_to, references=references,
        quote_text=quote_text, quote_html=quote_html,
        attachments=attach_loaded, identity=ident,
    )

    ok, bootstrapped = preflight(ident)
    if not ok:
        raise SendError(_transport_unavailable(ident),
                        code=codes.TRANSPORT_UNAVAILABLE)

    if _is_default(ident):
        _deliver(msg)
    else:
        deliver_for(ident, msg.as_bytes(), rcpt_to=to_l + cc_l + bcc_l)
    return SendResult(
        ok=True,
        message_id=msg["Message-ID"],
        to=to_l, cc=cc_l, bcc=bcc_l,
        subject=subject,
        attachments=[fn for _, _, _, fn in attach_loaded],
        bootstrapped=bootstrapped,
    )


def create_draft(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    in_reply_to: str = "",
    from_identity: str | None = None,
) -> DraftResult:
    """File a draft in the identity's own Drafts folder — and stop.

    Intent handed back, never executed (docs/draft-design.md): our
    composer's bytes go to the DECLARED drafts lane or nowhere — no
    fallback, ever, because for a draft the location IS the artifact
    (a fallback that files elsewhere may publish the body to a third
    party — security-posture §2.12). No allowlist check (nothing
    transmits; the ledger still records recipients at the tool layer)
    and no Bcc-to-self (a human will edit and send this; Exchange
    populates Sent Items natively)."""
    ident = identities.get(from_identity)
    if ident.drafts != "graph":
        raise SendError(
            f"create_draft is not available for identity [{ident.name}]: "
            f'drafts require drafts = "graph" and a [{ident.name}.graph] '
            "block in ~/.email-mcp/identities.toml. Run `email-mcp setup` "
            "(one browser sign-in) to enable it, or compose the text and "
            "paste it yourself — see docs/reference.md, 'Drafts'.",
            code=codes.DRAFT_UNSUPPORTED,
        )
    to_l, cc_l, _ = _recipient_lists(to, cc, None)
    if not to_l:
        raise SendError("`to` is required (no valid recipient address).",
                        code=codes.INVALID_INPUT)
    if not subject:
        raise SendError("`subject` is required.", code=codes.INVALID_INPUT)
    if not body.strip():
        raise SendError("`body` is empty.", code=codes.INVALID_INPUT)

    msg = compose(to=to_l, subject=subject, body=body, cc=cc_l, bcc=[],
                  in_reply_to=in_reply_to, identity=ident)

    from . import graph

    draft_id = graph.create_mime_draft(ident, msg.as_bytes())
    receipt = graph.draft_receipt(ident, draft_id)
    if not (receipt["is_draft"]
            and receipt["internet_message_id"] == msg["Message-ID"]
            and receipt["in_drafts_folder"]):
        # Evidence, never assertion: a draft we cannot read back as OUR
        # message, in Drafts, unarmed, was not created — say exactly
        # which leg failed.
        raise graph.GraphError(
            f"[{ident.name}/graph] draft readback failed verification "
            f"(is_draft={receipt['is_draft']}, "
            f"message_id_match="
            f"{receipt['internet_message_id'] == msg['Message-ID']}, "
            f"in_drafts_folder={receipt['in_drafts_folder']}) — check "
            "the Drafts folder yourself before retrying."
        )
    return DraftResult(
        ok=True,
        draft_id=draft_id,
        message_id=msg["Message-ID"],
        to=to_l, cc=cc_l, subject=subject,
        account=_bare(ident.from_addr),
    )


def _parse_send_at(send_at: str) -> "datetime":
    """Parse the user's send_at. Naive timestamps mean LOCAL time (what a
    human asking for '9am' means); aware ones are respected. Returns UTC."""
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(send_at.replace("Z", "+00:00"))
    except ValueError as e:
        raise SendError(f"invalid send_at (want ISO-8601): {send_at!r}",
                        code=codes.INVALID_SEND_AT) from e
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → local wall-clock
    return dt.astimezone(timezone.utc)


def schedule_email(
    *,
    to: str | list[str],
    subject: str,
    body: str,
    send_at: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    attachments: str | list[str] | None = None,
    from_identity: str | None = None,
):
    """Compose NOW, deliver LATER: the message is validated, attachments
    embedded, Bcc-to-self added and a Message-ID minted immediately, then
    the finished RFC-822 is frozen into the spool for the launchd
    dispatcher to deliver at send_at. Editing/deleting a source file after
    scheduling does not change what goes out.

    The manifest records the identity name; the dispatcher resolves it to
    a transport at fire time, while the envelope sender stays the frozen
    From: header — config drift between scheduling and firing cannot
    change who the mail claims to be from.
    """
    from . import spool

    ident = identities.get(from_identity)
    when = _parse_send_at(send_at)
    now = spool.utcnow()
    if (now - when).total_seconds() > 120:
        raise SendError(
            f"send_at is in the past ({when.isoformat(timespec='seconds')}). "
            "Use send_email for immediate delivery.",
            code=codes.SEND_AT_IN_PAST,
        )

    to_l, cc_l, bcc_l = _recipient_lists(to, cc, bcc)
    if not to_l:
        raise SendError("`to` is required (no valid recipient address).",
                        code=codes.INVALID_INPUT)
    if not subject:
        raise SendError("`subject` is required.", code=codes.INVALID_INPUT)
    if not body.strip():
        raise SendError("`body` is empty.", code=codes.INVALID_INPUT)

    attach_loaded = _load_attachments(attachments)

    if ident.bcc_self:
        if _bare(ident.from_addr) not in {_bare(b) for b in bcc_l}:
            bcc_l.append(ident.from_addr)

    _enforce_allowlist(to_l + cc_l + bcc_l, ident)

    msg = compose(
        to=to_l, subject=subject, body=body, cc=cc_l, bcc=bcc_l,
        attachments=attach_loaded, identity=ident,
    )
    entry = spool.Entry(
        id=spool.new_id(now),
        send_at=spool.iso(when),
        created_at=spool.iso(now),
        to=to_l, cc=cc_l, bcc=bcc_l,
        subject=subject,
        attachments=[fn for _, _, _, fn in attach_loaded],
        message_id=msg["Message-ID"],
        identity=ident.name,
    )
    raw = msg.as_bytes()
    if ident.executor != "graph":
        spool.save(raw, entry)
        return entry

    from . import graph  # lazy: launchd-only setups never import it

    # Two-phase manifest write: the manifest lands FIRST (executor="graph",
    # no draft id), THEN Exchange gets the draft, THEN the manifest gains
    # the id. A crash in either window leaves a manifest whose frozen
    # Message-ID is the recovery key — the dispatcher's reconcile pass
    # searches Drafts by internetMessageId and adopts or flips (F1/F2).
    entry.executor = "graph"
    spool.save(raw, entry)
    try:
        entry.graph_draft_id = graph.create_deferred_draft(ident, raw, when)
    except graph.GraphError as e:
        # F5/F8: Graph refused (auth, throttle, 5xx…) — silent fallback to
        # the launchd executor. The frozen .eml is already in pending/, so
        # nothing is lost and nothing can double-send (create_deferred_draft
        # deletes its own draft on any post-create failure).
        entry.executor = "launchd"
        spool.update("pending", entry)
        _log.warning(
            "graph: schedule of %s via identity %r failed, falling back to "
            "launchd executor: %s", entry.id, ident.name, e)
    else:
        spool.update("pending", entry)
    return entry


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
    from_identity: str | None = None,
) -> SendResult:
    """Reply to message `id`, threading correctly (In-Reply-To / References /
    Re: subject). Defaults to replying to the original sender only; set
    reply_all=True to include the original To+Cc (minus the sending
    identity's own address).

    The original message is quoted below the reply (attribution line +
    `>`-prefixed plain text; `<blockquote type="cite">` in HTML), the way a
    normal mail client's Reply does. Pass include_history=False for a bare
    reply carrying only threading headers.
    """
    ident = identities.get(from_identity)
    original = source.get(id)  # Email dataclass
    headers = original.headers
    orig_msgid = headers.get("Message-ID") or headers.get("Message-Id") or ""
    orig_refs = headers.get("References", "")
    # Store-supplied value: sanitize, don't refuse (provenance rule) — a
    # hostile subject in the mailbox must not make the message unanswerable.
    orig_subject = _CTL_RE.sub(" ", original.ref.subject or headers.get("Subject", ""))
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"

    reply_to = headers.get("Reply-To") or original.ref.from_addr
    to_l = _split(reply_to)

    self_bare = _bare(ident.from_addr)
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
        attachments=attachments, from_identity=from_identity,
    )
