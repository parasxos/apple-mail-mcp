"""Outgoing mail: compose RFC-822 messages and deliver them.

Why not Mail.app? Its AppleScript compose path wraps the body in a collapsed
`<blockquote class="Apple-Mail-URLShareWrapperClass">` that renders as an
*empty* message in Outlook/Exchange. Verified across six compose variants —
it is the OS, not our scripting. So we bypass Mail.app for sending and build
RFC-822 ourselves.

Delivery is routed by identity (see `email_mcp.identities`): the resolved
From: address names a `MailTransport` driver — `ssh_sendmail` (the original
bastion path), `smtp`, or `pipe` (see `email_mcp.transports`). With no
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

from email.message import EmailMessage
from email.parser import BytesHeaderParser
from email.utils import getaddresses

from . import codes, identities, transports
from .addressing import (
    CONTROL_RE as _CTL_RE,
    bare_address as _bare,
    enforce_allowlist as _enforce_allowlist,
    recipient_lists as _recipient_lists,
    reject_header_injection as _reject_header_injection,
    split_addresses as _split,
    validate_bare_addresses as _validate_bare_addresses,
)
from .attachments import (
    attachment_paths as _attachment_paths,
    load_attachments as _load_attachments,
)
from .domain.models import DraftResult, SendResult
from .log import get_logger
from .mime import (
    PreparedTransmission as _PreparedTransmission,
    attribution as _attribution,
    compose,
    html_body as _html_body,
    html_paragraphs as _html_paras,
    prepare_transmission as _prepare_transmission,
    quote_html as _quote_html,
    quote_plain as _quote_plain,
    reencode_text_base64 as _reencode_text_base64,
    require_message_fields as _require_message_fields,
    strip_tags as _strip_tags,
)
from .transports import SendError  # re-export: same class everywhere

_log = get_logger()


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
    prepared = _prepare_transmission(
        ident, to=to, subject=subject, body=body, cc=cc, bcc=bcc,
        in_reply_to=in_reply_to, references=references,
        quote_text=quote_text, quote_html=quote_html,
        attachments=attachments,
    )

    ok, bootstrapped = preflight(ident)
    if not ok:
        raise SendError(_transport_unavailable(ident),
                        code=codes.TRANSPORT_UNAVAILABLE)

    if _is_default(ident):
        _deliver(prepared.message)
    else:
        deliver_for(
            ident,
            prepared.message.as_bytes(),
            rcpt_to=prepared.to + prepared.cc + prepared.bcc,
        )
    return SendResult(
        ok=True,
        message_id=prepared.message["Message-ID"],
        to=prepared.to, cc=prepared.cc, bcc=prepared.bcc,
        subject=subject,
        attachments=prepared.attachment_names,
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
    _require_message_fields(to_l, subject, body)

    msg = compose(to=to_l, subject=subject, body=body, cc=cc_l, bcc=[],
                  in_reply_to=in_reply_to, identity=ident)
    _reencode_text_base64(msg)

    from . import graph

    draft_id = graph.create_mime_draft(ident, msg.as_bytes())
    receipt = graph.draft_receipt(ident, draft_id)
    if not (receipt["is_draft"]
            and receipt["internet_message_id"] == msg["Message-ID"]
            and receipt["in_drafts_folder"]):
        # Evidence, never assertion: a draft we cannot read back as OUR
        # message, in Drafts, unarmed, was not created — say exactly
        # which leg failed, and name the server-side artifact that DOES
        # exist (this is the one failure with an orphan to reconcile;
        # the id also rides into the failed audit event via the error).
        raise graph.GraphError(
            f"[{ident.name}/graph] draft readback failed verification "
            f"(is_draft={receipt['is_draft']}, "
            f"message_id_match="
            f"{receipt['internet_message_id'] == msg['Message-ID']}, "
            f"in_drafts_folder={receipt['in_drafts_folder']}) — a draft "
            f"WAS created (id {draft_id}); check your Drafts before "
            "retrying."
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

    prepared = _prepare_transmission(
        ident, to=to, subject=subject, body=body, cc=cc, bcc=bcc,
        attachments=attachments,
    )
    entry = spool.Entry(
        id=spool.new_id(now),
        send_at=spool.iso(when),
        created_at=spool.iso(now),
        to=prepared.to, cc=prepared.cc, bcc=prepared.bcc,
        subject=subject,
        attachments=prepared.attachment_names,
        message_id=prepared.message["Message-ID"],
        identity=ident.name,
    )
    if ident.executor == "graph":
        # Exchange imports this MIME to arm the deferred draft, and its
        # importer garbles QP (see _reencode_text_base64). Re-encode
        # BEFORE freezing: the launchd fallback must deliver the same
        # bytes Exchange was shown.
        _reencode_text_base64(prepared.message)
    raw = prepared.message.as_bytes()
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
