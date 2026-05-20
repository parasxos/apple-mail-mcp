"""Apple Mail source.

Reads the Envelope Index SQLite database in immutable mode and resolves
message bodies from `.emlx` files on demand. No writes. No network.

Designed to be safe against the Mail.app version drift documented in the
plan: every column we read is verified via PRAGMA at startup, and missing
columns degrade gracefully (None values) rather than raise.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import html
import html.parser
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from ..config import attach_dir, mail_dir, max_body_bytes
from .apple_mail_paths import (
    account_label,
    build_emlx_path,
    mailbox_name,
)
from .base import (
    AttachmentBlob,
    AttachmentRef,
    Email,
    EmailRef,
    EmailSource,
    Mailbox,
    SearchQuery,
)

# recipients.type values observed in Apple Mail Envelope Index.
# Apple stores type as a small int; 0/1/2 correspond to to/cc/bcc.
_RECIPIENT_TO = 0
_RECIPIENT_CC = 1
_RECIPIENT_BCC = 2


class _HTMLStripper(html.parser.HTMLParser):
    """Tiny HTML → text. No external deps."""

    def __init__(self) -> None:
        super().__init__()
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag in ("br", "p", "div", "li", "tr"):
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._buf.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._buf)).strip()


def _html_to_text(s: str) -> str:
    p = _HTMLStripper()
    try:
        p.feed(s)
    except Exception:
        # Bad HTML — fall back to a regex-stripped version.
        return html.unescape(re.sub(r"<[^>]+>", "", s))
    return p.text()


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the Envelope Index read-only + immutable.

    `immutable=1` lets us coexist with the running Mail.app process by
    bypassing WAL locking — we read a consistent snapshot ignoring any
    pending writes in the -shm/-wal sidecars.
    """
    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class AppleMailSource:
    """EmailSource implementation backed by ~/Library/Mail/V<N>/."""

    def __init__(self, mail_base: Path | None = None) -> None:
        self._mail_dir = mail_base or mail_dir()
        index = self._mail_dir / "MailData" / "Envelope Index"
        if not index.exists():
            raise FileNotFoundError(
                f"Envelope Index not found at {index}. "
                f"Either Mail.app isn't set up, or the process running this "
                f"server lacks Full Disk Access for ~/Library/Mail."
            )
        self._conn = _connect_readonly(index)
        self._columns = self._probe_columns()

    # ------------------------------------------------------------------ #
    # schema probing                                                     #
    # ------------------------------------------------------------------ #

    def _probe_columns(self) -> dict[str, set[str]]:
        """Return {table_name: {column_names}} for the tables we touch.

        Apple's schema drifts across macOS versions; we never SELECT * and we
        check before referencing a column.
        """
        wanted = (
            "messages",
            "subjects",
            "addresses",
            "mailboxes",
            "recipients",
            "attachments",
            "summaries",
            "conversations",
        )
        out: dict[str, set[str]] = {}
        cur = self._conn.cursor()
        for table in wanted:
            try:
                rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
            except sqlite3.DatabaseError:
                out[table] = set()
                continue
            out[table] = {r[1] for r in rows}
        return out

    def _have(self, table: str, column: str) -> bool:
        return column in self._columns.get(table, set())

    # ------------------------------------------------------------------ #
    # row → EmailRef                                                     #
    # ------------------------------------------------------------------ #

    def _row_to_ref(self, row: sqlite3.Row) -> EmailRef:
        # `row` must have: rowid, subject, sender_addr, sender_name,
        # date_unix, mailbox_url, read, has_attachment, conversation_id, snippet
        sender = row["sender_addr"] or ""
        sender_name = row["sender_name"] or ""
        if sender_name and sender:
            from_addr = f"{sender_name} <{sender}>"
        else:
            from_addr = sender or sender_name

        to_list, cc_list = self._recipients_for(row["rowid"])
        mbox_url = row["mailbox_url"] or ""
        date_unix = row["date_unix"] or 0
        ts = datetime.fromtimestamp(date_unix, tz=timezone.utc) if date_unix else datetime.fromtimestamp(0, tz=timezone.utc)

        return EmailRef(
            id=str(row["rowid"]),
            subject=row["subject"] or "",
            from_addr=from_addr,
            to=to_list,
            cc=cc_list,
            date=ts,
            mailbox=mailbox_name(mbox_url) if mbox_url else "",
            account=account_label(mbox_url) if mbox_url else "",
            snippet=row["snippet"] or "",
            unread=not bool(row["read"]),
            has_attachment=bool(row["has_attachment"]),
            thread_id=str(row["conversation_id"]) if row["conversation_id"] else "",
        )

    def _recipients_for(self, message_rowid: int) -> tuple[list[str], list[str]]:
        cur = self._conn.cursor()
        rows = cur.execute(
            """
            SELECT r.type AS rtype, a.address AS addr, a.comment AS name
              FROM recipients r
              JOIN addresses a ON a.ROWID = r.address
             WHERE r.message = ?
             ORDER BY r.position
            """,
            (message_rowid,),
        ).fetchall()
        to_list: list[str] = []
        cc_list: list[str] = []
        for r in rows:
            display = f"{r['name']} <{r['addr']}>" if r["name"] else (r["addr"] or "")
            if r["rtype"] == _RECIPIENT_CC:
                cc_list.append(display)
            elif r["rtype"] == _RECIPIENT_BCC:
                # We don't surface bcc separately — fold into cc for the ref.
                cc_list.append(display)
            else:  # _RECIPIENT_TO and anything else (defensive default)
                to_list.append(display)
        return to_list, cc_list

    # ------------------------------------------------------------------ #
    # base SQL builders                                                  #
    # ------------------------------------------------------------------ #

    def _base_select(self) -> str:
        # `summary` column (linked to summaries table) gives a free snippet.
        snippet_sql = (
            "(SELECT summary FROM summaries WHERE ROWID = m.summary)"
            if self._have("messages", "summary") and self._have("summaries", "summary")
            else "''"
        )
        date_col = "date_sent" if self._have("messages", "date_sent") else "date_received"
        conv_col = "conversation_id" if self._have("messages", "conversation_id") else "0"
        return f"""
            SELECT
                m.ROWID AS rowid,
                s.subject AS subject,
                a.address AS sender_addr,
                a.comment AS sender_name,
                m.{date_col} AS date_unix,
                mb.url AS mailbox_url,
                m.read AS read,
                EXISTS(SELECT 1 FROM attachments at WHERE at.message = m.ROWID) AS has_attachment,
                m.{conv_col} AS conversation_id,
                {snippet_sql} AS snippet
              FROM messages m
              JOIN subjects s ON s.ROWID = m.subject
              LEFT JOIN addresses a ON a.ROWID = m.sender
              JOIN mailboxes mb ON mb.ROWID = m.mailbox
        """

    # ------------------------------------------------------------------ #
    # EmailSource methods                                                #
    # ------------------------------------------------------------------ #

    def mailboxes(self) -> list[Mailbox]:
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT url, total_count, unread_count FROM mailboxes ORDER BY total_count DESC"
        ).fetchall()
        return [
            Mailbox(
                account=account_label(r["url"]) if r["url"] else "",
                name=mailbox_name(r["url"]) if r["url"] else "",
                path=r["url"] or "",
                total=int(r["total_count"] or 0),
                unread=int(r["unread_count"] or 0),
            )
            for r in rows
            if r["url"]
        ]

    def search(self, q: SearchQuery) -> list[EmailRef]:
        sql = self._base_select()
        where: list[str] = ["m.deleted = 0"] if self._have("messages", "deleted") else []
        params: list[object] = []

        if q.query:
            term = _like_escape(q.query)
            where.append(
                "(s.subject LIKE ? ESCAPE '\\' "
                "OR a.address LIKE ? ESCAPE '\\' "
                "OR a.comment LIKE ? ESCAPE '\\' "
                "OR EXISTS(SELECT 1 FROM summaries su WHERE su.ROWID = m.summary AND su.summary LIKE ? ESCAPE '\\'))"
            )
            params.extend([f"%{term}%"] * 4)

        if q.from_addr:
            where.append("(a.address LIKE ? ESCAPE '\\' OR a.comment LIKE ? ESCAPE '\\')")
            esc = _like_escape(q.from_addr)
            params.extend([f"%{esc}%", f"%{esc}%"])

        if q.to_addr:
            where.append(
                "EXISTS(SELECT 1 FROM recipients r2 "
                "JOIN addresses a2 ON a2.ROWID = r2.address "
                "WHERE r2.message = m.ROWID "
                "AND (a2.address LIKE ? ESCAPE '\\' OR a2.comment LIKE ? ESCAPE '\\'))"
            )
            esc = _like_escape(q.to_addr)
            params.extend([f"%{esc}%", f"%{esc}%"])

        if q.mailbox:
            where.append("mb.url LIKE ?")
            params.append(f"%/{urllib.parse.quote(q.mailbox)}%")

        if q.account:
            where.append("mb.url LIKE ?")
            params.append(f"%//{q.account}/%")

        date_col = "date_sent" if self._have("messages", "date_sent") else "date_received"
        if q.after:
            where.append(f"m.{date_col} >= ?")
            params.append(int(q.after.timestamp()))
        if q.before:
            where.append(f"m.{date_col} < ?")
            params.append(int(q.before.timestamp()))

        if q.has_attachment is True:
            where.append("EXISTS(SELECT 1 FROM attachments at2 WHERE at2.message = m.ROWID)")
        elif q.has_attachment is False:
            where.append("NOT EXISTS(SELECT 1 FROM attachments at2 WHERE at2.message = m.ROWID)")

        if q.unread_only:
            where.append("m.read = 0")

        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY m.{date_col} DESC LIMIT ? OFFSET ?"
        limit = max(1, min(int(q.limit), 200))
        offset = max(0, int(q.offset))
        params.extend([limit, offset])

        cur = self._conn.cursor()
        rows = cur.execute(sql, params).fetchall()
        return [self._row_to_ref(r) for r in rows]

    def recent(
        self,
        mailbox: str | None,
        account: str | None,
        limit: int,
    ) -> list[EmailRef]:
        return self.search(
            SearchQuery(mailbox=mailbox, account=account, limit=limit, offset=0)
        )

    def thread(self, thread_id: str) -> list[EmailRef]:
        try:
            tid = int(thread_id)
        except ValueError:
            return []
        if not self._have("messages", "conversation_id"):
            return []
        date_col = "date_sent" if self._have("messages", "date_sent") else "date_received"
        sql = self._base_select() + f" WHERE m.conversation_id = ? ORDER BY m.{date_col} ASC"
        cur = self._conn.cursor()
        return [self._row_to_ref(r) for r in cur.execute(sql, (tid,)).fetchall()]

    def get(self, id: str) -> Email:
        try:
            rowid = int(id)
        except ValueError as e:
            raise ValueError(f"invalid message id {id!r}") from e

        cur = self._conn.cursor()
        sql = self._base_select() + " WHERE m.ROWID = ?"
        row = cur.execute(sql, (rowid,)).fetchone()
        if row is None:
            raise LookupError(f"message {rowid} not found in Envelope Index")
        ref = self._row_to_ref(row)
        mailbox_url = row["mailbox_url"] or ""
        flagged = bool(
            cur.execute("SELECT flagged FROM messages WHERE ROWID = ?", (rowid,)).fetchone()[0]
        ) if self._have("messages", "flagged") else False

        # Body comes from .emlx
        try:
            emlx_path = build_emlx_path(self._mail_dir, mailbox_url, rowid)
        except FileNotFoundError:
            emlx_path = None

        body_text = ""
        body_html = ""
        headers: dict[str, str] = {}
        attachments: list[AttachmentRef] = []

        if emlx_path and emlx_path.exists():
            parsed = _parse_emlx(emlx_path, max_body_bytes())
            headers = parsed["headers"]
            body_text = parsed["body_text"]
            body_html = parsed["body_html"]
            attachments = parsed["attachments"]
        else:
            # IMAP-only mailbox without a local copy. Surface what we can.
            headers = {
                "Subject": ref.subject,
                "From": ref.from_addr,
                "Date": ref.date.isoformat(),
            }
            body_text = ref.snippet

        flags = {"read": not ref.unread, "flagged": flagged}
        return Email(
            ref=ref,
            headers=headers,
            body_text=body_text,
            body_html=body_html,
            attachments=attachments,
            flags=flags,
        )

    def attachment(self, id: str, attachment_id: str) -> AttachmentBlob:
        try:
            rowid = int(id)
        except ValueError as e:
            raise ValueError(f"invalid message id {id!r}") from e

        cur = self._conn.cursor()
        row = cur.execute(
            "SELECT mb.url FROM messages m JOIN mailboxes mb ON mb.ROWID = m.mailbox WHERE m.ROWID = ?",
            (rowid,),
        ).fetchone()
        if row is None:
            raise LookupError(f"message {rowid} not found")
        emlx_path = build_emlx_path(self._mail_dir, row["url"] or "", rowid)
        if not emlx_path.exists():
            raise FileNotFoundError(f"no local .emlx for message {rowid}")

        parsed_parts = _parse_emlx_parts(emlx_path)
        if attachment_id not in parsed_parts:
            raise LookupError(
                f"attachment {attachment_id!r} not found in message {rowid}"
            )
        part = parsed_parts[attachment_id]

        # Persist bytes to a stable tmp path keyed by content hash + name.
        digest = hashlib.sha1()
        digest.update(part["bytes"])
        digest.update(part["name"].encode("utf-8"))
        base = attach_dir() / f"{rowid}-{digest.hexdigest()[:12]}"
        base.mkdir(parents=True, exist_ok=True)
        out = base / part["name"]
        out.write_bytes(part["bytes"])
        return AttachmentBlob(
            name=part["name"],
            mime=part["mime"],
            size=len(part["bytes"]),
            path=str(out),
        )


# ---------------------------------------------------------------------- #
# helpers                                                                #
# ---------------------------------------------------------------------- #


def _like_escape(s: str) -> str:
    """Escape `%`, `_`, and `\\` for SQL LIKE patterns using `ESCAPE '\\'`."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_emlx_bytes(path: Path) -> bytes:
    """Strip Mail.app's byte-count prefix + plist suffix, return RFC 822 bytes."""
    data = path.read_bytes()
    nl = data.find(b"\n")
    if nl < 0:
        # Not an .emlx (no length prefix) — treat whole file as message.
        return data
    try:
        count = int(data[:nl].strip())
    except ValueError:
        return data
    start = nl + 1
    end = start + count
    if end > len(data):
        end = len(data)
    return data[start:end]


def _decode_part_text(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


def _parse_emlx(path: Path, max_body: int) -> dict:
    raw = _read_emlx_bytes(path)
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    headers = {k: str(v) for k, v in msg.items()}

    body_text_parts: list[str] = []
    body_html_parts: list[str] = []
    attachments: list[AttachmentRef] = []

    counter = [0]  # for attachments with no Content-ID

    def walk(part: email.message.Message, path_id: str) -> None:
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get("Content-Disposition") or "").lower()
        is_attachment = "attachment" in disp or (
            part.get_filename() is not None and ctype not in ("text/plain", "text/html")
        )

        if part.is_multipart():
            for i, sub in enumerate(part.iter_parts(), start=1):
                walk(sub, f"{path_id}.{i}" if path_id else str(i))
            return

        if is_attachment:
            counter[0] += 1
            name = part.get_filename() or f"attachment-{counter[0]}"
            payload = part.get_payload(decode=True) or b""
            attachments.append(
                AttachmentRef(
                    name=name,
                    mime=ctype or None,
                    size=len(payload),
                    attachment_id=path_id or str(counter[0]),
                )
            )
            return

        if ctype == "text/plain":
            body_text_parts.append(_decode_part_text(part))
        elif ctype == "text/html":
            body_html_parts.append(_decode_part_text(part))

    walk(msg, "")

    body_text = "\n\n".join(s for s in body_text_parts if s)
    body_html = "\n\n".join(s for s in body_html_parts if s)
    if not body_text and body_html:
        body_text = _html_to_text(body_html)

    if max_body and len(body_text) > max_body:
        body_text = body_text[:max_body] + "\n[…body truncated…]"
    if max_body and len(body_html) > max_body:
        body_html = body_html[:max_body] + "\n<!-- body truncated -->"

    return {
        "headers": headers,
        "body_text": body_text,
        "body_html": body_html,
        "attachments": attachments,
    }


def _parse_emlx_parts(path: Path) -> dict[str, dict]:
    """Return {path_id: {bytes, name, mime}} for every attachment-like part."""
    raw = _read_emlx_bytes(path)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    out: dict[str, dict] = {}
    counter = [0]

    def walk(part: email.message.Message, path_id: str) -> None:
        if part.is_multipart():
            for i, sub in enumerate(part.iter_parts(), start=1):
                walk(sub, f"{path_id}.{i}" if path_id else str(i))
            return
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get("Content-Disposition") or "").lower()
        is_attachment = "attachment" in disp or (
            part.get_filename() is not None and ctype not in ("text/plain", "text/html")
        )
        if not is_attachment:
            return
        counter[0] += 1
        payload = part.get_payload(decode=True) or b""
        out[path_id or str(counter[0])] = {
            "bytes": payload,
            "name": part.get_filename() or f"attachment-{counter[0]}",
            "mime": ctype or "application/octet-stream",
        }

    walk(msg, "")
    return out
