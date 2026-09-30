"""Standards-correct MIME composition and reply-history rendering."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.headerregistry import HeaderRegistry, UnstructuredHeader
from email.policy import default as _default_policy
from email.utils import formataddr, make_msgid

from . import codes, identities
from .addressing import (
    bare_address,
    enforce_allowlist,
    recipient_lists,
    reject_header_injection,
)
from .attachments import load_attachments
from .transports import SendError


@dataclass
class PreparedTransmission:
    message: EmailMessage
    to: list[str]
    cc: list[str]
    bcc: list[str]
    attachment_names: list[str]


def html_paragraphs(text: str) -> str:
    return "".join(
        "<p>" + html.escape(paragraph).replace("\n", "<br>") + "</p>"
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    )


def html_body(text: str, quote_html: str = "") -> str:
    return f"<html><body>{html_paragraphs(text)}{quote_html}</body></html>"


_HTML_INNER_RE = re.compile(r"(?is)^.*?<body[^>]*>(.*)</body>.*$")
_TAG_BLOCK_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


def attribution(ref) -> str:
    stamp = ref.date.astimezone().strftime("%a, %d %b %Y at %H:%M")
    return f"On {stamp}, {ref.from_addr} wrote:"


def strip_tags(html_document: str) -> str:
    text = _TAG_BLOCK_RE.sub("", html_document)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def quote_plain(
    original_text: str,
    original_html: str,
    attribution_line: str,
) -> str:
    source = original_text.strip() or strip_tags(original_html)
    quoted = "\n".join("> " + line for line in source.rstrip().splitlines())
    return f"{attribution_line}\n{quoted}" if quoted else attribution_line


def quote_html(
    original_html: str,
    original_text: str,
    attribution_line: str,
) -> str:
    if original_html.strip():
        match = _HTML_INNER_RE.match(original_html)
        inner = match.group(1) if match else original_html
        inner = _TAG_BLOCK_RE.sub("", inner)
    else:
        inner = html_paragraphs(original_text)
    return (
        f"<div>{html.escape(attribution_line)}</div>"
        '<blockquote type="cite" style="margin:0 0 0 0.8ex;'
        f'border-left:2px solid #cccccc;padding-left:1ex">{inner}</blockquote>'
    )


# RFC 5322 2.1.1: the hard limit for a line, excluding CRLF.
_HARD_LINE_LIMIT = 998


def _msg_ids(value: str) -> list[str] | None:
    """The msg-ids of a References / In-Reply-To value, in order, or None
    when the value does not read as a list of msg-ids.

    Uses the stdlib's own msg-id grammar (private module, isolated here,
    present since 3.8 and unchanged through 3.13): it walks comments,
    including nested ones and quoted pairs, quoted-string local parts and
    domain literals, so `(note <fake@x>)` yields nothing and `<a@[b>c]>`
    stays whole. Must run on the raw value, before the header parser
    decodes encoded words: a decoded comment can contain what looks like
    syntax. Each id is rebuilt from its lexical tokens minus CFWS, never
    from `.value`, which would strip the quotes of `<"a b"@x>`.
    Obsolete forms are kept (they are still ids); an invalid id (missing
    `>` or id-right) or text that is not a msg-id at all returns None and
    the caller falls back to the stdlib folder.
    """
    try:
        from email import errors
        from email._header_value_parser import CFWS_LEADER, get_cfws, get_msg_id
    except ImportError:  # pragma: no cover - stdlib private surface moved
        return None
    ids: list[str] = []
    rest = value
    while rest:
        if rest[0] in CFWS_LEADER:
            _, rest = get_cfws(rest)
            continue
        try:
            token, rest = get_msg_id(rest)
        except errors.HeaderParseError:
            return None
        if any(isinstance(d, errors.InvalidHeaderDefect) for d in token.all_defects):
            return None
        ids.append("".join(str(t) for t in token if t.token_type != "cfws"))
    return ids or None


class _MessageIDListHeader(UnstructuredHeader):
    """In-Reply-To / References: a whitespace separated list of msg-ids.

    Python's registry treats these two as unstructured text, so a msg-id
    longer than the line limit (every Outlook Message-ID is) gets RFC 2047
    encoded on output, `=?utf-8?q?=3CZRAP...?=`, and the reply falls out of
    the thread in any client that matches References literally. Fold at the
    whitespace between ids, never encode, keep every id intact.

    Only the msg-ids are serialized. RFC 5322 allows comments between them
    (`<id> (=?utf-8?q?Jos=C3=A9?=)`); the parser decodes those to Unicode,
    which a non-encoding folder could not emit, and they carry nothing a
    threading client reads, so they are dropped. A value that does not
    parse as a msg-id list is left to the stdlib folder rather than
    emitting a line this class cannot vouch for.
    """

    @classmethod
    def parse(cls, value, kwds):
        # Before super().parse decodes encoded words (see _msg_ids).
        kwds["msg_ids"] = _msg_ids(value)
        super().parse(value, kwds)

    def init(self, *args, msg_ids=None, **kw):
        self._msg_ids = msg_ids
        super().init(*args, **kw)

    def fold(self, *, policy):
        ids = self._msg_ids
        if not ids or any(not token.isascii() for token in ids):
            return super().fold(policy=policy)
        soft = policy.max_line_length or _HARD_LINE_LIMIT
        name_line = f"{self.name}:"
        lines = []
        current = name_line
        for token in ids:
            candidate = f"{current} {token}"
            if len(candidate) <= soft or (
                current == name_line and len(candidate) <= _HARD_LINE_LIMIT
            ):
                current = candidate
                continue
            # Fold before the id. The first id normally stays on the name
            # line even past the soft limit (a reparse keeps the value
            # cleaner); only when it would break the hard limit does it move
            # whole to a continuation line. Never split, never encoded.
            lines.append(current)
            current = f" {token}"
        lines.append(current)
        return policy.linesep.join(lines) + policy.linesep


_HEADERS = HeaderRegistry()
_HEADERS.map_to_type("in-reply-to", _MessageIDListHeader)
_HEADERS.map_to_type("references", _MessageIDListHeader)
# One policy for every composed message: the default folding rules for
# everything, msg-id aware folding for the two threading headers.
COMPOSE_POLICY = _default_policy.clone(header_factory=_HEADERS)


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
    """Build the multipart message shared by every delivery lane."""
    selected = identity if identity is not None else identities.get(None)
    from_addr = selected.from_addr
    reject_header_injection({
        "subject": subject,
        "in_reply_to": in_reply_to,
        "references": references,
        "to": to,
        "cc": cc or [],
        "bcc": bcc or [],
        "from_addr": from_addr,
        "from_name": selected.from_name,
    })
    message = EmailMessage(policy=COMPOSE_POLICY)
    try:
        message["From"] = formataddr((selected.from_name, from_addr))
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        if bcc:
            message["Bcc"] = ", ".join(bcc)
        message["Subject"] = subject
        domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "localhost"
        message["Message-ID"] = make_msgid(domain=domain)
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = (references + " " + in_reply_to).strip()
    except ValueError as error:
        raise SendError(
            f"invalid header content: {error}", code=codes.INVALID_HEADER,
        ) from error
    message.set_content(
        f"{body}\n\n{quote_text}\n" if quote_text else body
    )
    message.add_alternative(html_body(body, quote_html), subtype="html")
    for data, maintype, subtype, filename in attachments or []:
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename,
        )
    return message


def require_message_fields(to: list[str], subject: str, body: str) -> None:
    if not to:
        raise SendError(
            "`to` is required (no valid recipient address).",
            code=codes.INVALID_INPUT,
        )
    if not subject:
        raise SendError("`subject` is required.", code=codes.INVALID_INPUT)
    if not body.strip():
        raise SendError("`body` is empty.", code=codes.INVALID_INPUT)


def prepare_transmission(
    identity: identities.Identity,
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
) -> PreparedTransmission:
    to_list, cc_list, bcc_list = recipient_lists(to, cc, bcc)
    require_message_fields(to_list, subject, body)
    loaded = load_attachments(attachments)

    if identity.bcc_self and bare_address(identity.from_addr) not in {
            bare_address(address) for address in bcc_list}:
        bcc_list.append(identity.from_addr)

    enforce_allowlist(to_list + cc_list + bcc_list, identity)
    message = compose(
        to=to_list, subject=subject, body=body,
        cc=cc_list, bcc=bcc_list,
        in_reply_to=in_reply_to, references=references,
        quote_text=quote_text, quote_html=quote_html,
        attachments=loaded, identity=identity,
    )
    return PreparedTransmission(
        message=message,
        to=to_list,
        cc=cc_list,
        bcc=bcc_list,
        attachment_names=[filename for _, _, _, filename in loaded],
    )


def reencode_text_base64(message: EmailMessage) -> None:
    """Protect message bodies from Exchange's quoted-printable importer bug."""
    for subtype in ("plain", "html"):
        part = message.get_body((subtype,))
        if part is not None:
            part.set_content(
                part.get_content(),
                subtype=subtype,
                charset="utf-8",
                cte="base64",
            )
