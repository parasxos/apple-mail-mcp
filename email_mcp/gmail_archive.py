"""Exact-message Gmail archiving for a reviewed local-copy recovery.

This internal adapter is deliberately not registered as an MCP tool. It uses
the configured IMAP identity, preserves the server message in All Mail, and
removes only Inbox membership. Plans contain private identifiers; public
results and audit events contain only hashes and counts. No message body is
downloaded. A complete local .emlx copy is required before planning/applying.
"""
from __future__ import annotations

import email
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from . import audit, config, imap


MAX_MESSAGES = 200
SEARCH_BATCH_SIZE = 100
VERSION = 1


class ArchiveError(Exception):
    """A safe, identifier-free failure requiring a new review or retry."""


def _protocol_failure(message: str, phase: str, *, status=None, error=None) -> ArchiveError:
    """Expose protocol phase/class, never the server's text or credentials."""
    if error is not None:
        name = type(error).__name__
        detail = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"
    else:
        detail = status if status in {"OK", "NO", "BAD", "BYE"} else "unexpected_status"
    return ArchiveError(f"{message} ({phase}: {detail})")


@dataclass(frozen=True)
class MessageMeta:
    uid: str
    gmail_id: str
    labels: tuple[str, ...]
    flags: tuple[str, ...]
    size: int
    message_id: str = ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity_hash(identity) -> str:
    username = str(identity.imap.get("username", "")).strip() or identity.from_addr
    return _sha((identity.name + "\0" + identity.from_addr.strip().lower()
                 + "\0" + str(identity.imap.get("host", "")).lower()
                 + "\0" + str(identity.imap.get("port", 993))
                 + "\0" + username.lower()).encode())


def _guard() -> None:
    if config.read_only():
        raise ArchiveError("Gmail archive planning/apply is disabled in read-only mode")


def _number(value) -> str:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ArchiveError("Invalid exact server identifier")
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", result):
        raise ArchiveError("Invalid exact server identifier")
    return result


def _mid(value: str) -> str:
    if not isinstance(value, str):
        raise ArchiveError("Missing Message-ID")
    result = value.strip().removeprefix("<").removesuffix(">")
    if not result or re.search(r"[\s<>]", result) or len(result) > 998:
        raise ArchiveError("Invalid Message-ID")
    return result


def _without_inbox(labels) -> tuple[str, ...]:
    return tuple(sorted(x for x in labels if x.lower() != "\\inbox"))


def _has_inbox(labels) -> bool:
    return any(x.lower() == "\\inbox" for x in labels)


def _local_copy(path: str, expected_mid: str) -> dict:
    """Validate a full local message, refusing externally stored MIME parts.

    An .emlx suffix/length alone does not prove attachment preservation.
    Apple's partial MIME markers therefore require separate recovery first.
    The digest covers RFC bytes, excluding the mutable trailing flags plist.
    """
    p = Path(path)
    if not p.is_absolute() or p.suffix != ".emlx" or p.name.endswith(".partial.emlx"):
        raise ArchiveError("A complete local .emlx copy is required")
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ArchiveError("Local preservation copy is not a regular file")
            prefix = stream.readline(32)
            if not re.fullmatch(rb"[ \t]*[1-9][0-9]*[ \t]*\r?\n", prefix):
                raise ArchiveError("Invalid local message length")
            count = int(prefix)
            if count > 100_000_000:
                raise ArchiveError("Local preservation copy exceeds validation limit")
            raw = stream.read(count)
    except (OSError, ValueError):
        raise ArchiveError("Local preservation copy is unavailable") from None
    if len(raw) != count:
        raise ArchiveError("Local preservation copy is truncated")
    if b"\n\n" not in raw.replace(b"\r\n", b"\n"):
        raise ArchiveError("Local preservation copy has no header/body separator")
    msg = email.message_from_bytes(raw)
    mids = msg.get_all("Message-ID", [])
    if len(mids) != 1 or _mid(mids[0]) != _mid(expected_mid):
        raise ArchiveError("Local preservation copy has a different Message-ID")
    for part in msg.walk():
        if any(part.get(k) is not None for k in (
            "X-Apple-Content-Length", "X-Apple-Content-Location",
            "X-Apple-Part-URL", "X-Apple-Partial-Message")):
            raise ArchiveError("Local copy references separately stored message content")
        if not part.is_multipart():
            part.get_payload(decode=True)  # also records invalid transfer encoding
        if part.defects:
            raise ArchiveError("Local preservation copy has malformed MIME content")
    return {"path": str(p), "rfc_sha256": _sha(raw), "rfc_bytes": count}


def _tokens(raw: bytes) -> tuple[str, ...]:
    """Parse one IMAP parenthesized list, including quoted label names."""
    result: list[str] = []
    i = 0
    while i < len(raw):
        if raw[i:i + 1].isspace():
            i += 1
            continue
        if raw[i] == 34:
            i += 1
            token = bytearray()
            closed = False
            while i < len(raw):
                b = raw[i]
                i += 1
                if b == 34:
                    closed = True
                    break
                if b == 92:
                    if i >= len(raw):
                        raise ArchiveError("Malformed server metadata")
                    b = raw[i]
                    i += 1
                token.append(b)
            if not closed:
                raise ArchiveError("Malformed server metadata")
            result.append(bytes(token).decode("latin1"))
        else:
            end = i
            while end < len(raw) and not raw[end:end + 1].isspace():
                end += 1
            result.append(raw[i:end].decode("latin1"))
            i = end
    return tuple(sorted(result))


def _attribute_mask(raw: bytes) -> bytes:
    """Keep FETCH attributes at their offsets, hiding quoted and nested values."""
    masked = bytearray(b" " * len(raw))
    depth = 0
    quoted = escaped = False
    for i, b in enumerate(raw):
        if quoted:
            if escaped:
                escaped = False
            elif b == 92:
                escaped = True
            elif b == 34:
                quoted = False
            continue
        if b == 34:
            quoted = True
            continue
        if depth == 1:
            masked[i] = b
        if b == 40:
            depth += 1
        elif b == 41:
            depth -= 1
    return bytes(masked)


def _list_field(raw: bytes, name: bytes, attributes: bytes) -> tuple[str, ...]:
    match = re.search(rb"\b" + name + rb" \(", attributes)
    if not match:
        raise ArchiveError("Required server metadata is missing")
    start = match.end()
    quoted = escaped = False
    for i in range(start, len(raw)):
        b = raw[i]
        if escaped:
            escaped = False
        elif quoted and b == 92:
            escaped = True
        elif b == 34:
            quoted = not quoted
        elif not quoted and b == 41:
            return _tokens(raw[start:i])
    raise ArchiveError("Malformed server metadata")


def _parse_fetch(data, headers: bool) -> dict[str, MessageMeta]:
    result: dict[str, MessageMeta] = {}
    for item in data or []:
        if isinstance(item, tuple):
            prefix, literal = item
        elif isinstance(item, bytes):
            prefix, literal = item, b""
        else:
            continue
        attributes = _attribute_mask(prefix)
        uid = re.search(rb"\bUID (\d+)\b", attributes)
        if not uid:
            continue  # closing parenthesis after a header literal
        gid = re.search(rb"\bX-GM-MSGID (\d+)\b", attributes)
        size = re.search(rb"\bRFC822\.SIZE (\d+)\b", attributes)
        if not gid or not size:
            raise ArchiveError("Incomplete server metadata")
        message_id = ""
        if headers:
            parsed = email.message_from_bytes(literal)
            mids = parsed.get_all("Message-ID", [])
            if len(mids) != 1:
                raise ArchiveError("Server message does not have one Message-ID")
            message_id = _mid(mids[0])
        key = _number(uid.group(1).decode())
        if key in result:
            raise ArchiveError("Duplicate server metadata response")
        result[key] = MessageMeta(
            key, _number(gid.group(1).decode()),
            _list_field(prefix, b"X-GM-LABELS", attributes),
            _list_field(prefix, b"FLAGS", attributes), int(size.group(1)), message_id)
    return result


class GmailSession:
    """Existing configured IMAP credentials; no new authentication path."""

    def __init__(self, identity):
        self._session = None
        try:
            connection_identity = replace(identity, imap={**identity.imap, "folder": "INBOX"})
            self._session = imap._Session(connection_identity)
            if not self._session.gmail:
                raise ArchiveError("Configured server lacks Gmail IMAP support")
            self.all_mail_folder = self._all_mail_folder()
            self.uidvalidity = self._select(self.all_mail_folder)
        except ArchiveError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise _protocol_failure("Configured Gmail IMAP connection failed", "connect", error=exc) from None

    def _all_mail_folder(self) -> str:
        try:
            status, listing = self._session.conn.list()
            if status != "OK":
                raise _protocol_failure("Gmail All Mail discovery failed", "LIST", status=status)
            folders = set()
            for raw in listing or []:
                if not isinstance(raw, bytes):
                    continue
                fields = re.fullmatch(
                    rb'\(([^)]*)\)\s+(?:NIL|"(?:[^"\\]|\\.)*")\s+'
                    rb'("(?:[^"\\]|\\.)*"|[^\s]+)', raw)
                if fields and rb"\all" in fields[1].lower().split():
                    folder = fields[2]
                    if folder.startswith(b'"'):
                        folder = re.sub(rb"\\(.)", rb"\1", folder[1:-1])
                    folders.add(folder.decode("ascii"))
            if len(folders) != 1 or "INBOX" in {folder.upper() for folder in folders}:
                raise ArchiveError("One Gmail All Mail folder must be discovered by its special-use attribute")
            return folders.pop()
        except ArchiveError:
            raise
        except Exception as exc:
            raise _protocol_failure("Gmail All Mail discovery failed", "LIST", error=exc) from None

    def _select(self, folder: str, *, readonly: bool = True) -> str:
        phase = "EXAMINE" if readonly else "SELECT"
        try:
            status, _ = self._session.conn.select(imap._quote(folder), readonly=readonly)
            if status != "OK":
                raise _protocol_failure("Gmail mailbox selection failed", phase, status=status)
            phase = "UIDVALIDITY"
            _, values = self._session.conn.response("UIDVALIDITY")
            if not values or len(values) != 1:
                raise ArchiveError("Server did not provide UIDVALIDITY")
            value = _number(values[0].decode("ascii"))
            self._session.selected = folder
            return value
        except ArchiveError:
            raise
        except Exception as exc:
            raise _protocol_failure("Gmail mailbox selection failed", phase, error=exc) from None

    def fetch(self, uids, *, headers: bool = True) -> dict[str, MessageMeta]:
        ids = [_number(x) for x in uids]
        if not ids or len(ids) > MAX_MESSAGES or len(set(ids)) != len(ids):
            raise ArchiveError("Invalid metadata batch")
        parts = "(UID X-GM-MSGID X-GM-LABELS FLAGS RFC822.SIZE"
        parts += " BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])" if headers else ")"
        try:
            status, data = self._session.conn.uid("FETCH", ",".join(ids), parts)
            if status != "OK":
                raise ArchiveError("Gmail metadata fetch failed")
            found = _parse_fetch(data, headers)
            if not set(found).issubset(ids):
                raise ArchiveError("Unexpected server metadata response")
            return found
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Gmail metadata fetch failed") from None

    def inbox_gmids(self, gmids) -> set[str]:
        ids = [_number(x) for x in gmids]
        if not ids or len(ids) > MAX_MESSAGES or len(set(ids)) != len(ids):
            raise ArchiveError("Invalid Inbox verification batch")
        found: set[str] = set()
        self._select("INBOX")
        try:
            for pos in range(0, len(ids), SEARCH_BATCH_SIZE):
                chunk = ids[pos:pos + SEARCH_BATCH_SIZE]
                query = " ".join(["OR"] * (len(chunk) - 1)
                                 + ["X-GM-MSGID " + x for x in chunk])
                status, data = self._session.conn.uid("SEARCH", query)
                if status != "OK" or len(data) != 1 or not isinstance(data[0], bytes):
                    raise ArchiveError("Gmail Inbox verification failed")
                uids = data[0].decode("ascii").split()
                if uids:
                    metadata = self.fetch(uids, headers=False)
                    if len(metadata) != len(uids):
                        raise ArchiveError("Gmail Inbox metadata is incomplete")
                    matched = {m.gmail_id for m in metadata.values()}
                    if not matched.issubset(chunk):
                        raise ArchiveError("Gmail Inbox search returned an unexpected message")
                    found.update(matched)
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Gmail Inbox verification failed") from None
        finally:
            validity = self._select(self.all_mail_folder)
            if validity != self.uidvalidity:
                raise ArchiveError("All Mail UIDVALIDITY changed")
        return found

    def archive(self, uids) -> None:
        _guard()
        ids = [_number(x) for x in uids]
        if not ids or len(ids) > MAX_MESSAGES or len(set(ids)) != len(ids):
            raise ArchiveError("Invalid archive batch")
        if self._select(self.all_mail_folder, readonly=False) != self.uidvalidity:
            raise ArchiveError("All Mail UIDVALIDITY changed")
        try:
            status, _ = self._session.conn.uid(
                "STORE", ",".join(ids), "-X-GM-LABELS", r"(\Inbox)")
            if status != "OK":
                raise ArchiveError("Gmail archive was not acknowledged")
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Gmail archive result is uncertain") from None

    def close(self) -> None:
        if self._session is not None:
            connection = self._session.conn
            self._session = None
            try:
                connection.logout()
            except Exception:
                pass
            finally:
                try:
                    connection.shutdown()
                except Exception:
                    pass


def plan_archive(identity, candidates: list[dict], *, session=None) -> dict:
    """Freeze exact UIDs and preserved local copies; no server writes."""
    _guard()
    if not candidates or len(candidates) > MAX_MESSAGES:
        raise ArchiveError("A plan must contain between 1 and 200 messages")
    uids = [_number(c["all_mail_uid"]) for c in candidates]
    if len(set(uids)) != len(uids):
        raise ArchiveError("Plan contains duplicate exact server identifiers")
    proofs = [_local_copy(c["local_copy_path"], c["rfc_message_id"])
              for c in candidates]
    owned = session is None
    active = session or GmailSession(identity)
    try:
        found = active.fetch(uids)
        if set(found) != set(uids):
            raise ArchiveError("An exact source UID is missing; rebuild the plan")
        gmids = [found[uid].gmail_id for uid in uids]
        if len(set(gmids)) != len(gmids):
            raise ArchiveError("Plan contains duplicate Gmail messages")
        inbox = active.inbox_gmids(gmids)
        items = []
        for candidate, uid, proof in zip(candidates, uids, proofs):
            meta = found[uid]
            if meta.uid != uid or _mid(meta.message_id) != _mid(candidate["rfc_message_id"]):
                raise ArchiveError("Exact source UID has a different Message-ID")
            if "\\Deleted" in meta.flags:
                raise ArchiveError("A source message is already marked deleted")
            if _has_inbox(meta.labels) != (meta.gmail_id in inbox):
                raise ArchiveError("Gmail Inbox membership is inconsistent")
            item = {"all_mail_uid": uid, "gmail_id": meta.gmail_id,
                    "message_id": _mid(meta.message_id), "labels": list(meta.labels),
                    "flags": list(meta.flags), "size": meta.size,
                    "local_copy": proof, "in_inbox": meta.gmail_id in inbox}
            for key in ("apple_global_message_id", "keeper_rowid"):
                if key in candidate:
                    item[key] = int(candidate[key])
            items.append(item)
        return {"version": VERSION, "operation": "remove_gmail_inbox_label",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "identity_sha256": _identity_hash(identity),
                "uidvalidity": str(active.uidvalidity),
                "all_mail_folder": active.all_mail_folder, "items": items}
    finally:
        if owned:
            active.close()


def save_plan(plan: dict, path: Path) -> str:
    """Private, exclusive plan creation; return the digest to review/apply."""
    _guard()
    payload = (json.dumps(plan, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return _sha(payload)


def _load_plan(path: Path, expected_sha256: str) -> dict:
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                raise ArchiveError("Plan must be a private regular file owned by the current user")
            payload = stream.read(2_000_001)
        if len(payload) > 2_000_000 or _sha(payload) != expected_sha256:
            raise ArchiveError("Plan does not match the reviewed digest")
        plan = json.loads(payload)
    except ArchiveError:
        raise
    except Exception:
        raise ArchiveError("Reviewed plan cannot be read") from None
    if (plan.get("version") != VERSION
            or plan.get("operation") != "remove_gmail_inbox_label"
            or not 1 <= len(plan.get("items", [])) <= MAX_MESSAGES):
        raise ArchiveError("Invalid reviewed plan")
    try:
        created = datetime.fromisoformat(plan["created_at"])
        if created.tzinfo is None:
            raise ValueError
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age < 0 or age > config.triage_ttl_seconds():
            raise ArchiveError("Reviewed plan is expired or has a future timestamp")
    except (TypeError, ValueError, KeyError):
        raise ArchiveError("Invalid reviewed plan timestamp") from None
    return plan


def apply_archive(identity, path: Path, expected_sha256: str, *,
                  session_factory=GmailSession) -> dict:
    """Apply only the reviewed IDs; independently verify each result.

    A failed/ambiguous STORE is still verified through a fresh connection.
    Retrying this same plan is safe: messages already outside Inbox receive
    no additional mutation. Other labels/flags may not drift from the plan.
    """
    _guard()
    plan = _load_plan(path, expected_sha256)
    if plan["identity_sha256"] != _identity_hash(identity):
        raise ArchiveError("Reviewed plan belongs to another identity")
    items = plan["items"]
    uids = [_number(x["all_mail_uid"]) for x in items]
    gmids = [_number(x["gmail_id"]) for x in items]
    if len(set(uids)) != len(uids) or len(set(gmids)) != len(gmids):
        raise ArchiveError("Reviewed plan repeats a server message")
    for item in items:
        if _local_copy(item["local_copy"]["path"], item["message_id"]) != item["local_copy"]:
            raise ArchiveError("Preserved local copy changed since review")
    active = session_factory(identity)
    pending = []
    acknowledged = False
    try:
        if (str(active.uidvalidity) != plan["uidvalidity"]
                or active.all_mail_folder != plan["all_mail_folder"]):
            raise ArchiveError("All Mail identity changed; rebuild the plan")
        found = active.fetch(uids)
        inbox = active.inbox_gmids(gmids)
        for item in items:
            meta = found.get(item["all_mail_uid"])
            if (meta is None or meta.uid != item["all_mail_uid"]
                    or meta.gmail_id != item["gmail_id"]
                    or _mid(meta.message_id) != item["message_id"]
                    or meta.size != item["size"]):
                raise ArchiveError("Exact source identity changed since review")
            if (tuple(sorted(meta.flags)) != tuple(sorted(item["flags"]))
                    or _without_inbox(meta.labels) != _without_inbox(item["labels"])):
                raise ArchiveError("Source flags or other labels changed since review")
            if _has_inbox(meta.labels) != (meta.gmail_id in inbox):
                raise ArchiveError("Gmail Inbox membership is inconsistent")
            if meta.gmail_id in inbox:
                pending.append(meta.uid)
        if pending:
            _guard()
            audit.emit("gmail_archive_attempt", outcome="pending", tool="gmail_archive",
                       identity=identity.name, plan_id=expected_sha256,
                       detail={"count": len(pending)})
            try:
                active.archive(pending)
                acknowledged = True
            except ArchiveError:
                pass  # connection failure does not prove a mutation failed
        else:
            acknowledged = True
    finally:
        active.close()
    results = []
    fresh = None
    try:
        fresh = session_factory(identity)
        if (str(fresh.uidvalidity) != plan["uidvalidity"]
                or fresh.all_mail_folder != plan["all_mail_folder"]):
            raise ArchiveError("All Mail identity changed during verification")
        found = fresh.fetch(uids)
        inbox = fresh.inbox_gmids(gmids)
        for item in items:
            meta = found.get(item["all_mail_uid"])
            valid = (meta is not None and meta.uid == item["all_mail_uid"]
                     and meta.gmail_id == item["gmail_id"]
                     and _mid(meta.message_id) == item["message_id"]
                     and meta.size == item["size"]
                     and tuple(sorted(meta.flags)) == tuple(sorted(item["flags"]))
                     and _without_inbox(meta.labels) == _without_inbox(item["labels"])
                     and not _has_inbox(meta.labels) and meta.gmail_id not in inbox)
            results.append({"gmail_id_sha256": _sha(item["gmail_id"].encode()),
                            "status": "verified" if valid else "not_verified"})
    except ArchiveError:
        results = [{"gmail_id_sha256": _sha(x["gmail_id"].encode()),
                    "status": "unknown"} for x in items]
    finally:
        if fresh is not None:
            fresh.close()
    verified = sum(x["status"] == "verified" for x in results)
    result = {"plan_sha256": expected_sha256, "attempted": len(pending),
              "store_acknowledged": acknowledged, "verified": verified,
              "total": len(items), "complete": verified == len(items), "results": results}
    audit.emit("gmail_archive_result", outcome="success" if result["complete"] else "partial",
               tool="gmail_archive", identity=identity.name, plan_id=expected_sha256,
               detail={"attempted": len(pending), "verified": verified, "total": len(items)})
    return result
