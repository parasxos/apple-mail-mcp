"""MIME traversal shared by local reads and server body recovery."""
from __future__ import annotations

from email.message import Message


def walk_parts(message: Message, path: str = ""):
    """Yield (path, part, is_attachment), without entering attachments."""
    ctype = message.get_content_type()
    attached = (
        ctype == "message/rfc822"
        or message.get_content_disposition() == "attachment"
        or (message.get_filename() is not None
            and not message.is_multipart()
            and ctype not in ("text/plain", "text/html"))
    )
    if attached or not message.is_multipart():
        yield path, message, attached
    else:
        for index, part in enumerate(message.get_payload(), start=1):
            child = f"{path}.{index}" if path else str(index)
            yield from walk_parts(part, child)
