"""IMAP backfill lane — the mailbox's own copy of a message body, located
by RFC Message-ID, for accounts Graph cannot answer (Gmail and any other
imap:// mailbox with a declared [name.imap] identity table).

Mirrors graph.py's fetch contract exactly: a hit returns {"contentType",
"content"}; a CONFIRMED empty search returns None (this mailbox does not
hold the message); every failure raises ImapError — absence of evidence
must never read as evidence of absence (the backfill would stamp a
permanent imap_miss off a dropped connection).

An identity opts in with an [name.imap] table: `host` plus an `op`
(1Password secret reference) or `keychain` (macOS Keychain item) secret
source — the same app password Gmail SMTP submission uses. `username`
defaults to the identity's from_addr, `folder` overrides the search
folder for servers without a \\All special-use mailbox.

Connections are cached per identity for the life of the process (a
backfill pass asks thousands of times; TLS + LOGIN per message would be
the whole cost). Gmail is searched in the \\All folder — every message
lives there — via X-GM-RAW rfc822msgid, which is exact regardless of
UIDVALIDITY; other servers get a standard SEARCH HEADER Message-ID in
the declared folder.

Bodies are fetched with a bounded partial FETCH (text parts precede
attachments in real MIME, so the first megabyte nearly always holds the
whole body text); a message whose text parts escape the window is
re-fetched whole, up to a hard cap.
"""
from __future__ import annotations

import email as email_mod
import imaplib
import re
import threading

from .transports.smtp import _read_keychain, _read_op


class ImapError(Exception):
    """A lookup that produced no evidence: connection, auth, folder or
    protocol trouble. The backfill defers the doc, never stamps it."""


# First-window FETCH size, and the ceiling past which a full re-fetch is
# refused (a 200 MB message is not worth one index row).
_PEEK_BYTES = 1_048_576
_FULL_FETCH_MAX = 26_214_400

_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")

# imaplib's default per-line cap predates Gmail-sized FETCH literals.
imaplib._MAXLINE = max(imaplib._MAXLINE, 10_000_000)

_pool: dict[str, "_Session"] = {}
_pool_lock = threading.Lock()


def _name(ident) -> str:
    return str(getattr(ident, "name", ""))


class _Session:
    """One authenticated, folder-selected connection per identity."""

    def __init__(self, ident) -> None:
        cfg = dict(getattr(ident, "imap", {}) or {})
        host = str(cfg.get("host", "")).strip()
        if not host:
            raise ImapError(
                f"[{_name(ident)}/imap] identity has no [*.imap] table — "
                "not this lane's to answer."
            )
        self.prefix = f"[{_name(ident)}/imap]"
        username = str(cfg.get("username", "")).strip() \
            or str(getattr(ident, "from_addr", "")).strip()
        try:
            if str(cfg.get("op", "")).strip():
                secret = _read_op(str(cfg["op"]).strip())
            else:
                secret = _read_keychain(
                    str(cfg.get("keychain", "")).strip(), username)
        except Exception as e:
            raise ImapError(f"{self.prefix} secret unavailable: {e}") from e
        try:
            self.conn = imaplib.IMAP4_SSL(
                host, int(cfg.get("port", 993)), timeout=60)
            self.conn.login(username, secret)
        except Exception as e:
            raise ImapError(f"{self.prefix} connect/login to {host} "
                            f"failed: {e}") from e
        # Post-auth CAPABILITY: Gmail advertises X-GM-EXT-1 only after
        # login, and imaplib caches the pre-auth greeting's set.
        try:
            _, caps = self.conn.capability()
            self.gmail = b"X-GM-EXT-1" in (caps[0] or b"").upper()
        except Exception:  # noqa: BLE001 — capability probe is best-effort
            self.gmail = "X-GM-EXT-1" in self.conn.capabilities
        # Search scope: the \All folder first (Gmail: everything that
        # is not Spam or Trash), then \Trash and \Junk — a triage rule
        # that bins a message removes it from All Mail, and a binned
        # partial is still a recoverable body, not a miss.
        folders = [str(cfg.get("folder", "")).strip()] \
            if str(cfg.get("folder", "")).strip() else self._search_folders()
        self.folders = folders
        self.selected: str | None = None
        self._select(folders[0])

    def _select(self, folder: str) -> None:
        if folder == self.selected:
            return
        status, _ = self.conn.select(_quote(folder), readonly=True)
        if status != "OK":
            raise ImapError(f"{self.prefix} cannot SELECT {folder!r}")
        self.selected = folder

    def _search_folders(self) -> list[str]:
        """Special-use folders worth asking, in order: \\All (Gmail:
        "[Gmail]/All Mail" in the account's locale — every message that
        is not Spam or Trash), then \\Trash and \\Junk. Servers without
        special-use fall back to INBOX; declare `folder` in the
        [*.imap] table when that is wrong."""
        try:
            status, listing = self.conn.list()
        except Exception as e:
            raise ImapError(f"{self.prefix} LIST failed: {e}") from e
        found: dict[bytes, str] = {}
        if status == "OK":
            for raw in listing or []:
                if not isinstance(raw, bytes):
                    continue
                for attr in (rb"\All", rb"\Trash", rb"\Junk"):
                    if attr in raw and attr not in found:
                        m = re.search(rb'"([^"]+)"\s*$|\s(\S+)$', raw)
                        if m:
                            name = (m.group(1) or m.group(2)).decode(
                                "utf-8", "replace")
                            # a no-space name arrives with the wire's own
                            # quotes still on — _select re-quotes
                            if name.startswith('"') and name.endswith('"'):
                                name = name[1:-1]
                            found[attr] = name
        ordered = [found[a] for a in (rb"\All", rb"\Trash", rb"\Junk")
                   if a in found]
        return ordered or ["INBOX"]

    def search_uid(self, message_id: str) -> str | None:
        """First UID holding this Message-ID, searched across the scope
        folders (the hit's folder stays SELECTED for the fetch); None on
        a CONFIRMED empty search in every folder. Raises ImapError when
        any folder did not answer OK — a half-asked scope is no
        evidence of absence."""
        mid = re.sub(r"[\s\"\\]", "", message_id)
        if not mid:
            return None
        if self.gmail:
            args = ("X-GM-RAW", _quote(f"rfc822msgid:{mid}"))
        else:
            args = ("HEADER", "Message-ID", _quote(mid))
        for folder in self.folders:
            self._select(folder)
            try:
                status, data = self.conn.uid("SEARCH", *args)
            except Exception as e:
                raise ImapError(f"{self.prefix} SEARCH in {folder!r} "
                                f"failed: {e}") from e
            if status != "OK":
                raise ImapError(f"{self.prefix} SEARCH in {folder!r} "
                                f"answered {status}: {data!r}")
            uids = (data[0] or b"").split()
            if uids:
                return uids[0].decode("ascii")
        return None

    def fetch_raw(self, uid: str) -> bytes:
        """The message's MIME, whole when it fits the first window,
        re-fetched whole when its text may lie beyond it."""
        raw, size = self._fetch(uid, f"(RFC822.SIZE BODY.PEEK[]<0.{_PEEK_BYTES}>)")
        if size <= _PEEK_BYTES or size > _FULL_FETCH_MAX:
            return raw
        if _extract_body(raw)["content"]:
            return raw
        whole, _ = self._fetch(uid, "(RFC822.SIZE BODY.PEEK[])")
        return whole

    def _fetch(self, uid: str, parts: str) -> tuple[bytes, int]:
        try:
            status, data = self.conn.uid("FETCH", uid, parts)
        except Exception as e:
            raise ImapError(f"{self.prefix} FETCH {uid} failed: {e}") from e
        if status != "OK":
            raise ImapError(f"{self.prefix} FETCH {uid} answered {status}")
        raw = b""
        size = 0
        for item in data or []:
            if isinstance(item, tuple) and len(item) >= 2:
                m = _SIZE_RE.search(item[0] if isinstance(item[0], bytes)
                                    else b"")
                if m:
                    size = int(m.group(1))
                raw = item[1] or b""
        if not raw:
            raise ImapError(f"{self.prefix} FETCH {uid} returned no data")
        return raw, size or len(raw)


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _extract_body(raw: bytes) -> dict:
    """MIME → the graph.py body shape. text/plain parts win; an
    HTML-only message returns contentType "html" so the index's ONE
    stripper (fts._remote_text) handles it, exactly like a Graph hit.
    Attachment parts (Content-Disposition: attachment) never count."""
    msg = email_mod.message_from_bytes(raw)
    plains: list[str] = []
    htmls: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        if str(part.get("Content-Disposition", "")
               ).lower().startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # a truncated window can break one part's b64
            payload = None
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "replace")
        except LookupError:
            text = payload.decode("utf-8", "replace")
        if part.get_content_subtype() == "plain":
            plains.append(text)
        elif part.get_content_subtype() == "html":
            htmls.append(text)
    if plains:
        return {"contentType": "text", "content": "\n\n".join(plains)}
    if htmls:
        return {"contentType": "html", "content": "\n\n".join(htmls)}
    return {"contentType": "text", "content": ""}


def _session(ident) -> "_Session":
    key = _name(ident)
    with _pool_lock:
        sess = _pool.get(key)
    if sess is not None:
        return sess
    sess = _Session(ident)
    with _pool_lock:
        _pool[key] = sess
    return sess


def _drop_session(ident) -> None:
    with _pool_lock:
        sess = _pool.pop(_name(ident), None)
    if sess is not None:
        try:
            sess.conn.logout()
        except Exception:  # noqa: BLE001 — already broken is fine
            pass


def fetch_body_by_message_id(ident, message_id: str) -> dict | None:
    """The mailbox's own copy of a message body, located by RFC
    Message-ID. Returns {"contentType", "content"} on a hit, None on a
    CONFIRMED empty search; lookup failures raise ImapError. A protocol
    error drops the pooled connection so the next ask reconnects —
    one dead TLS session must not retire the identity for the pass."""
    sess = _session(ident)
    try:
        uid = sess.search_uid(message_id)
        if uid is None:
            return None
        return _extract_body(sess.fetch_raw(uid))
    except ImapError:
        _drop_session(ident)
        raise
    except Exception as e:  # imaplib abort mid-stream, socket EOF, …
        _drop_session(ident)
        raise ImapError(f"{sess.prefix} lookup failed: {e}") from e
