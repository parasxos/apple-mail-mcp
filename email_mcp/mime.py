"""Standards-correct MIME composition and reply-history rendering."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
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
    message = EmailMessage()
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
    """Protect text parts from Exchange's quoted-printable importer bug."""
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            part.set_content(
                part.get_content(),
                subtype=part.get_content_subtype(),
                charset="utf-8",
                cte="base64",
            )
