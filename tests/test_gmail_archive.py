"""Fixture-only checks for bounded Gmail Inbox-label removal.

No test reads Mail, credentials, or a real IMAP server. The fake mailbox
shares state between connections so verification must observe a new session.
"""
from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from email_mcp import gmail_archive as archive
from email_mcp.identities import Identity


UID = "41001"
GMAIL_ID = "1812345678901234567"
MESSAGE_ID = "<archive-fixture@example.test>"
PRIVATE_BODY = b"Fixture body that must never enter an apply result."
RFC_MESSAGE = (
    b"From: sender@example.test\r\n"
    b"To: owner+archive-fixture@gmail.com\r\n"
    b"Subject: archive fixture\r\n"
    b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    + PRIVATE_BODY + b"\r\n"
)


def _write_emlx(path: Path, payload: bytes = RFC_MESSAGE) -> None:
    path.write_bytes(
        str(len(payload)).encode() + b"\n" + payload
        + b'<?xml version="1.0"?><plist version="1.0"><dict/></plist>'
    )


class FakeMailbox:
    def __init__(self):
        self.messages = {
            UID: archive.MessageMeta(
                uid=UID, gmail_id=GMAIL_ID,
                labels=("\\Inbox", "\\Important", "Personal"),
                flags=("\\Seen", "\\Flagged"), size=len(RFC_MESSAGE),
                message_id=MESSAGE_ID,
            )
        }
        self.uidvalidity = "14499"
        self.all_mail_folder = "[Gmail]/All Mail"
        self.sessions = []
        self.archive_calls = []
        self.archive_error = None
        self.after_archive = None
        self.factory_error_at = None

    def connect(self, identity, **kwargs):
        if self.factory_error_at == len(self.sessions):
            raise archive.ArchiveError(
                "fixture password=DO-NOT-LEAK " + GMAIL_ID + " " + MESSAGE_ID)
        session = FakeSession(self, identity)
        self.sessions.append(session)
        return session


class FakeSession:
    def __init__(self, mailbox, identity):
        self.mailbox = mailbox
        self.identity = identity
        self.uidvalidity = mailbox.uidvalidity
        self.all_mail_folder = mailbox.all_mail_folder
        self.closed = False
        self.fetch_calls = []
        self.inbox_calls = []

    def fetch(self, uids, headers=True):
        assert not self.closed
        self.fetch_calls.append((tuple(uids), headers))
        return {str(uid): self.mailbox.messages[str(uid)] for uid in uids
                if str(uid) in self.mailbox.messages}

    def inbox_gmids(self, gmids):
        assert not self.closed
        self.inbox_calls.append(tuple(gmids))
        return {message.gmail_id for message in self.mailbox.messages.values()
                if message.gmail_id in gmids and "\\Inbox" in message.labels}

    def archive(self, uids):
        assert not self.closed
        self.mailbox.archive_calls.append(tuple(uids))
        if self.mailbox.archive_error:
            raise self.mailbox.archive_error
        for uid in uids:
            message = self.mailbox.messages[str(uid)]
            self.mailbox.messages[str(uid)] = replace(
                message,
                labels=tuple(label for label in message.labels
                             if label != "\\Inbox"),
            )
        if self.mailbox.after_archive:
            self.mailbox.after_archive(self.mailbox)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    monkeypatch.delenv("EMAIL_MCP_READ_ONLY", raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))

    def fail(*args, **kwargs):
        pytest.fail("archive fixtures must not open a network connection")

    monkeypatch.setattr("socket.create_connection", fail)
    monkeypatch.setattr("imaplib.IMAP4_SSL", fail)


@pytest.fixture(autouse=True)
def audit_events(monkeypatch):
    events = []

    def emit(event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(archive.audit, "emit", emit)
    return events


@pytest.fixture
def identity():
    return Identity(
        name="archive-fixture", from_addr="owner+archive-fixture@gmail.com",
        imap={"host": "imap.gmail.com", "username": "owner+archive-fixture@gmail.com",
              "keychain": "fixture-secret-reference"},
    )


@pytest.fixture
def candidate(tmp_path):
    local = tmp_path / "keeper.emlx"
    _write_emlx(local)
    return {"all_mail_uid": UID, "rfc_message_id": MESSAGE_ID,
            "local_copy_path": str(local), "apple_global_message_id": 90123,
            "keeper_rowid": 80456}


@pytest.fixture
def mailbox():
    return FakeMailbox()


def _plan(identity, candidate, mailbox):
    session = mailbox.connect(identity)
    try:
        return archive.plan_archive(identity, [candidate], session=session)
    finally:
        session.close()


def _saved(tmp_path, identity, candidate, mailbox):
    plan = _plan(identity, candidate, mailbox)
    path = tmp_path / "archive-plan.json"
    digest = archive.save_plan(plan, path)
    return path, digest


def test_saved_plan_has_exact_digest_and_private_permissions(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert PRIVATE_BODY not in path.read_bytes()
    assert b"fixture-secret-reference" not in path.read_bytes()
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("read_only", ["1", "true", "True", "yes"])
def test_read_only_mode_refuses_plan_before_session_work(
    identity, candidate, mailbox, monkeypatch, read_only,
):
    monkeypatch.setenv("EMAIL_MCP_READ_ONLY", read_only)
    session = mailbox.connect(identity)
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate], session=session)
    assert session.fetch_calls == []
    assert session.inbox_calls == []
    assert mailbox.archive_calls == []


def test_read_only_mode_refuses_apply_before_opening_session(
    tmp_path, identity, candidate, mailbox, monkeypatch,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    monkeypatch.setenv("EMAIL_MCP_READ_ONLY", "1")
    before = len(mailbox.sessions)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert len(mailbox.sessions) == before
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("count", [0, 201])
def test_candidate_count_rejected_before_fetch(identity, candidate, mailbox, count):
    session = mailbox.connect(identity)
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate] * count, session=session)
    assert session.fetch_calls == []
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("uid", ["0", "-1", "1:9", "1,2", "*", "1 UID STORE 2", ""])
def test_invalid_uid_never_reaches_imap(identity, candidate, mailbox, uid):
    session = mailbox.connect(identity)
    candidate["all_mail_uid"] = uid
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate], session=session)
    assert session.fetch_calls == []
    assert mailbox.archive_calls == []


def test_duplicate_uid_rejected(identity, candidate, mailbox):
    session = mailbox.connect(identity)
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate, dict(candidate)], session=session)
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("server_change", ["missing_uid", "wrong_uid", "wrong_mid"])
def test_plan_requires_exact_server_uid_and_message_id(
    identity, candidate, mailbox, server_change,
):
    if server_change == "missing_uid":
        mailbox.messages.clear()
    elif server_change == "wrong_uid":
        mailbox.messages[UID] = replace(mailbox.messages[UID], uid="41002")
    else:
        mailbox.messages[UID] = replace(
            mailbox.messages[UID], message_id="<someone-else@example.test>")
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("local_change", ["missing", "wrong_mid", "truncated", "partial"])
def test_plan_requires_complete_matching_local_copy(
    identity, candidate, mailbox, local_change,
):
    local = Path(candidate["local_copy_path"])
    if local_change == "missing":
        local.unlink()
    elif local_change == "wrong_mid":
        _write_emlx(local, RFC_MESSAGE.replace(
            MESSAGE_ID.encode(), b"<someone-else@example.test>"))
    elif local_change == "truncated":
        local.write_bytes(str(len(RFC_MESSAGE) + 1000).encode() + b"\n" + RFC_MESSAGE)
    else:
        partial = local.with_name("keeper.partial.emlx")
        local.rename(partial)
        candidate["local_copy_path"] = str(partial)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("change", ["deleted", "changed_payload", "changed_mid"])
def test_apply_rechecks_local_copy_before_mutation(
    tmp_path, identity, candidate, mailbox, change,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    local = Path(candidate["local_copy_path"])
    if change == "deleted":
        local.unlink()
    elif change == "changed_payload":
        _write_emlx(local, RFC_MESSAGE.replace(PRIVATE_BODY, b"Altered fixture body."))
    else:
        _write_emlx(local, RFC_MESSAGE.replace(
            MESSAGE_ID.encode(), b"<altered@example.test>"))
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


def test_wrong_plan_hash_rejected_before_opening_session(
    tmp_path, identity, candidate, mailbox,
):
    path, _ = _saved(tmp_path, identity, candidate, mailbox)
    before = len(mailbox.sessions)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, "0" * 64, session_factory=mailbox.connect)
    assert len(mailbox.sessions) == before
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("field,value", [
    ("name", "different-identity"),
    ("from_addr", "someone-else@gmail.com"),
    ("username", "someone-else@gmail.com"),
    ("host", "attacker.example.test"),
    ("port", 1993),
])
def test_wrong_identity_rejected_before_opening_session(
    tmp_path, identity, candidate, mailbox, field, value,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    if field in {"name", "from_addr"}:
        changed = replace(identity, **{field: value})
    else:
        changed = replace(identity, imap={**identity.imap, field: value})
    before = len(mailbox.sessions)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(changed, path, digest, session_factory=mailbox.connect)
    assert len(mailbox.sessions) == before
    assert mailbox.archive_calls == []


def test_stale_uidvalidity_refuses_all_mutation(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    mailbox.uidvalidity = "14500"
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []
    assert mailbox.sessions[-1].closed


@pytest.mark.parametrize("field,value", [
    ("gmail_id", "1812345678901234568"),
    ("message_id", "<changed@example.test>"),
    ("flags", ("\\Seen",)),
    ("labels", ("\\Inbox", "Personal", "New label")),
])
def test_changed_server_metadata_refuses_all_mutation(
    tmp_path, identity, candidate, mailbox, field, value,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    mailbox.messages[UID] = replace(mailbox.messages[UID], **{field: value})
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


def test_apply_does_not_mutate_when_planned_uid_is_missing(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    mailbox.messages.clear()
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


def test_apply_removes_only_inbox_and_verifies_with_a_new_connection(
    tmp_path, identity, candidate, mailbox, audit_events,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    original = mailbox.messages[UID]
    local = Path(candidate["local_copy_path"])
    local_bytes = local.read_bytes()
    result = archive.apply_archive(
        identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is True
    assert result["verified"] == result["total"] == result["attempted"] == 1
    assert result["store_acknowledged"] is True
    assert result["results"][0]["status"] == "verified"
    assert mailbox.archive_calls == [(UID,)]
    assert mailbox.messages[UID] == replace(
        original, labels=("\\Important", "Personal"))
    assert local.read_bytes() == local_bytes
    assert len(mailbox.sessions) == 3  # plan, preflight/write, fresh verification
    assert all(session.closed for session in mailbox.sessions)
    assert mailbox.sessions[1].fetch_calls == [((UID,), True)]
    assert mailbox.sessions[2].fetch_calls == [((UID,), True)]
    assert mailbox.sessions[2].inbox_calls == [(GMAIL_ID,)]
    public = json.dumps({"result": result, "events": audit_events})
    for private in (GMAIL_ID, MESSAGE_ID, str(local),
                    PRIVATE_BODY.decode(), "fixture-secret-reference"):
        assert private not in public
    assert set(result) == {"plan_sha256", "attempted", "store_acknowledged",
                           "verified", "total", "complete", "results"}
    assert set(result["results"][0]) == {"gmail_id_sha256", "status"}


def test_same_plan_retry_is_idempotent(tmp_path, identity, candidate, mailbox):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    first = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    second = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert first["complete"] is second["complete"] is True
    assert second["attempted"] == 0
    assert mailbox.archive_calls == [(UID,)]
    assert len(mailbox.sessions) == 5


def test_already_archived_message_requires_no_mutation(
    tmp_path, identity, candidate, mailbox,
):
    mailbox.messages[UID] = replace(
        mailbox.messages[UID], labels=("\\Important", "Personal"))
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is True
    assert result["attempted"] == 0
    assert mailbox.archive_calls == []


def test_store_failure_is_checked_and_reported_without_claiming_success(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    mailbox.archive_error = archive.ArchiveError("fixture password=DO-NOT-LEAK")
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is False
    assert result["verified"] == 0
    assert result["store_acknowledged"] is False
    assert result["results"][0]["status"] == "not_verified"
    assert len(mailbox.sessions) == 3
    assert "DO-NOT-LEAK" not in json.dumps(result)


def test_lost_store_acknowledgement_can_still_verify_success(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)

    def lose_ack(box):
        raise archive.ArchiveError("fixture disconnected after label removal")

    mailbox.after_archive = lose_ack
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is True
    assert result["verified"] == 1
    assert result["store_acknowledged"] is False


def test_fresh_verification_failure_is_unknown_and_sanitized(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    mailbox.factory_error_at = 2
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is False
    assert result["verified"] == 0
    assert result["store_acknowledged"] is True
    assert result["results"][0]["status"] == "unknown"
    for value in ("DO-NOT-LEAK", GMAIL_ID, MESSAGE_ID):
        assert value not in json.dumps(result)


@pytest.mark.parametrize("change", ["flags", "other_labels", "missing", "uidvalidity"])
def test_postwrite_drift_never_reports_verified(
    tmp_path, identity, candidate, mailbox, change,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)

    def drift(box):
        if change == "missing":
            box.messages.clear()
        elif change == "uidvalidity":
            box.uidvalidity = "99999"
        elif change == "flags":
            box.messages[UID] = replace(box.messages[UID], flags=())
        else:
            box.messages[UID] = replace(box.messages[UID], labels=("Personal",))

    mailbox.after_archive = drift
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is False
    assert result["verified"] == 0
    assert result["results"][0]["status"] == (
        "unknown" if change == "uidvalidity" else "not_verified")


def _second_candidate(tmp_path, candidate, mailbox):
    second = dict(candidate, all_mail_uid="41002", rfc_message_id="<second@example.test>")
    local = tmp_path / "second.emlx"
    payload = RFC_MESSAGE.replace(MESSAGE_ID.encode(), second["rfc_message_id"].encode())
    _write_emlx(local, payload)
    second["local_copy_path"] = str(local)
    mailbox.messages["41002"] = replace(
        mailbox.messages[UID], uid="41002", gmail_id="1812345678901234568",
        message_id=second["rfc_message_id"], size=len(payload))
    return second


def test_partial_batch_reports_each_message_independently(
    tmp_path, identity, candidate, mailbox,
):
    second = _second_candidate(tmp_path, candidate, mailbox)
    session = mailbox.connect(identity)
    plan = archive.plan_archive(identity, [candidate, second], session=session)
    session.close()
    path = tmp_path / "two-message-plan.json"
    digest = archive.save_plan(plan, path)

    def partial(box):
        box.messages["41002"] = replace(
            box.messages["41002"], labels=("\\Inbox", "\\Important", "Personal"))
        raise archive.ArchiveError("fixture only first message changed before disconnect")

    mailbox.after_archive = partial
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is False
    assert result["verified"] == 1
    assert result["attempted"] == result["total"] == 2
    assert result["store_acknowledged"] is False
    assert [row["status"] for row in result["results"]] == ["verified", "not_verified"]


def test_second_message_drift_prevents_mutation_of_entire_batch(
    tmp_path, identity, candidate, mailbox,
):
    second = _second_candidate(tmp_path, candidate, mailbox)
    session = mailbox.connect(identity)
    plan = archive.plan_archive(identity, [candidate, second], session=session)
    session.close()
    path = tmp_path / "two-message-plan.json"
    digest = archive.save_plan(plan, path)
    mailbox.messages["41002"] = replace(mailbox.messages["41002"], flags=())
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


def test_duplicate_gmail_id_refused_even_for_distinct_uids(
    tmp_path, identity, candidate, mailbox,
):
    second = _second_candidate(tmp_path, candidate, mailbox)
    mailbox.messages["41002"] = replace(mailbox.messages["41002"], gmail_id=GMAIL_ID)
    session = mailbox.connect(identity)
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate, second], session=session)
    assert mailbox.archive_calls == []


def test_duplicate_local_message_id_headers_are_refused(identity, candidate, mailbox):
    raw = RFC_MESSAGE.replace(b"Content-Type:", b"Message-ID: " + MESSAGE_ID.encode()
                              + b"\r\nContent-Type:", 1)
    _write_emlx(Path(candidate["local_copy_path"]), raw)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)


@pytest.mark.parametrize("header", [
    "X-Apple-Content-Length", "X-Apple-Content-Location",
    "X-Apple-Part-URL", "X-Apple-Partial-Message",
])
def test_external_content_markers_refuse_local_preservation(
    identity, candidate, mailbox, header,
):
    raw = RFC_MESSAGE.replace(b"Content-Type:", header.encode()
                              + b": externally-stored\r\nContent-Type:", 1)
    _write_emlx(Path(candidate["local_copy_path"]), raw)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)


def test_truncated_multipart_refuses_local_preservation(identity, candidate, mailbox):
    raw = (b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n"
           b'Content-Type: multipart/mixed; boundary="fixture-boundary"\r\n\r\n'
           b"--fixture-boundary\r\nContent-Type: text/plain\r\n\r\npartial body")
    _write_emlx(Path(candidate["local_copy_path"]), raw)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)


def test_local_symlink_refused(tmp_path, identity, candidate, mailbox):
    symlink = tmp_path / "linked.emlx"
    symlink.symlink_to(candidate["local_copy_path"])
    candidate["local_copy_path"] = str(symlink)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)


def test_plan_file_cannot_be_replaced_or_read_through_a_symlink(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        archive.save_plan({"arbitrary": "replacement"}, path)
    assert path.read_bytes() == original
    symlink = tmp_path / "linked-plan.json"
    symlink.symlink_to(path)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, symlink, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


def test_world_readable_plan_refused(tmp_path, identity, candidate, mailbox):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    path.chmod(0o644)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


class WireFixture:
    def __init__(self, response=None):
        self.calls = []
        self.validity = b"14499"
        self.response_data = response

    def select(self, folder, readonly=True):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def response(self, kind):
        assert kind == "UIDVALIDITY"
        return kind, [self.validity]

    def uid(self, *args):
        self.calls.append(args)
        return "OK", self.response_data or []


def _wire_session(response=None):
    wire = WireFixture(response)
    session = object.__new__(archive.GmailSession)
    session._session = SimpleNamespace(conn=wire, selected="[Gmail]/All Mail")
    session.uidvalidity = "14499"
    session.all_mail_folder = "[Gmail]/All Mail"
    return session, wire


def test_protocol_mutation_is_only_exact_uid_inbox_label_removal():
    session, wire = _wire_session()
    session.archive([UID])
    assert wire.calls == [
        ("select", '"[Gmail]/All Mail"', False),
        ("STORE", UID, "-X-GM-LABELS", r"(\Inbox)"),
    ]


def test_protocol_reselect_checks_uidvalidity_before_store():
    session, wire = _wire_session()
    wire.validity = b"14500"
    with pytest.raises(archive.ArchiveError):
        session.archive([UID])
    assert all(call[0] != "STORE" for call in wire.calls)


def _fetch_response(headers=None):
    prefix = (b"1 (UID " + UID.encode() + b" X-GM-MSGID " + GMAIL_ID.encode()
              + b' X-GM-LABELS (\\Inbox "Label (with) spaces" \\Important)'
              + b" FLAGS (\\Seen \\Flagged) RFC822.SIZE "
              + str(len(RFC_MESSAGE)).encode()
              + b" BODY[HEADER.FIELDS (MESSAGE-ID)] {49}")
    return [(prefix, headers or b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n\r\n"), b")"]


def test_protocol_fetch_is_header_peek_and_preserves_label_tokens():
    session, wire = _wire_session(_fetch_response())
    found = session.fetch([UID])
    assert wire.calls == [("FETCH", UID,
        "(UID X-GM-MSGID X-GM-LABELS FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")]
    assert found[UID].uid == UID
    assert found[UID].gmail_id == GMAIL_ID
    assert set(found[UID].labels) == {"\\Inbox", "\\Important", "Label (with) spaces"}
    assert set(found[UID].flags) == {"\\Seen", "\\Flagged"}
    assert found[UID].message_id == MESSAGE_ID.strip("<>")


def _shadowed_fetch_response(flags=b"\\Seen", size=len(RFC_MESSAGE)):
    prefix = (b'1 (X-GM-LABELS (\\Inbox "UID 9" "X-GM-MSGID 10" '
              b'"FLAGS (fake)" "RFC822.SIZE 1" UID 8) UID ' + UID.encode()
              + b" X-GM-MSGID " + GMAIL_ID.encode() + b" FLAGS (" + flags
              + b") RFC822.SIZE " + str(size).encode()
              + b" BODY[HEADER.FIELDS (MESSAGE-ID)] {49}")
    return [(prefix, b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n\r\n"), b")"]


@pytest.mark.parametrize("headers", [False, True])
def test_fetch_attributes_ignore_quoted_and_nested_label_contents(headers):
    response = _shadowed_fetch_response()
    if not headers:
        response = [response[0][0].split(b" BODY[", 1)[0] + b")"]
    session, _ = _wire_session(response)
    meta = session.fetch([UID], headers=headers)[UID]
    assert meta.gmail_id == GMAIL_ID
    assert meta.flags == ("\\Seen",)
    assert meta.size == len(RFC_MESSAGE)
    assert "FLAGS (fake)" in meta.labels
    assert "RFC822.SIZE 1" in meta.labels


@pytest.mark.parametrize("change", ["flags", "size"])
def test_label_contents_cannot_hide_server_drift_before_archive(
    tmp_path, identity, candidate, mailbox, change,
):
    mailbox.messages[UID] = archive._parse_fetch(_shadowed_fetch_response(), True)[UID]
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    changed = (_shadowed_fetch_response(flags=b"\\Flagged") if change == "flags"
               else _shadowed_fetch_response(size=len(RFC_MESSAGE) + 100))
    mailbox.messages[UID] = archive._parse_fetch(changed, True)[UID]
    with pytest.raises(archive.ArchiveError, match="changed since review"):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("change", ["flags", "size"])
def test_label_contents_cannot_hide_server_drift_during_archive(
    tmp_path, identity, candidate, mailbox, change,
):
    mailbox.messages[UID] = archive._parse_fetch(_shadowed_fetch_response(), True)[UID]
    path, digest = _saved(tmp_path, identity, candidate, mailbox)

    def change_metadata(box):
        changed = (_shadowed_fetch_response(flags=b"\\Flagged") if change == "flags"
                   else _shadowed_fetch_response(size=len(RFC_MESSAGE) + 100))
        parsed = archive._parse_fetch(changed, True)[UID]
        box.messages[UID] = replace(parsed, labels=box.messages[UID].labels)

    mailbox.after_archive = change_metadata
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["verified"] == 0 and result["complete"] is False


def test_server_duplicate_message_id_headers_are_refused():
    headers = (b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n") * 2 + b"\r\n"
    session, _ = _wire_session(_fetch_response(headers))
    with pytest.raises(archive.ArchiveError):
        session.fetch([UID])


@pytest.mark.parametrize("timestamp", ["expired", "future", "naive", "invalid", "missing"])
def test_invalid_plan_timestamp_refused_before_any_session(
    tmp_path, identity, candidate, mailbox, monkeypatch, timestamp,
):
    monkeypatch.setenv("EMAIL_MCP_TRIAGE_TTL", "600")
    plan = _plan(identity, candidate, mailbox)
    if timestamp == "expired":
        plan["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    elif timestamp == "future":
        plan["created_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    elif timestamp == "naive":
        plan["created_at"] = datetime.now().isoformat()
    elif timestamp == "invalid":
        plan["created_at"] = "not-a-timestamp"
    else:
        del plan["created_at"]
    path = tmp_path / "invalid-time-plan.json"
    digest = archive.save_plan(plan, path)
    before = len(mailbox.sessions)
    with pytest.raises(archive.ArchiveError):
        archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert len(mailbox.sessions) == before
    assert mailbox.archive_calls == []


@pytest.mark.parametrize("payload", [
    b"Message-ID: " + MESSAGE_ID.encode() + b"\r\nSubject: headers only",
    b"Message-ID: " + MESSAGE_ID.encode()
    + b"\r\nContent-Transfer-Encoding: base64\r\n\r\n!!!bad%%%base64",
])
def test_invalid_local_mime_is_refused(identity, candidate, mailbox, payload):
    _write_emlx(Path(candidate["local_copy_path"]), payload)
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)


def test_full_multipart_attachment_remains_byte_identical(
    tmp_path, identity, candidate, mailbox,
):
    payload = (
        b"Message-ID: " + MESSAGE_ID.encode() + b"\r\n"
        b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain\r\n\r\nFixture body\r\n"
        b"--b\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="fixture.bin"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nAQIDBA==\r\n--b--\r\n"
    )
    local = Path(candidate["local_copy_path"])
    _write_emlx(local, payload)
    original = local.read_bytes()
    mailbox.messages[UID] = replace(mailbox.messages[UID], size=len(payload))
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is True
    assert local.read_bytes() == original
    assert b"AQIDBA==" not in path.read_bytes()
    assert "AQIDBA==" not in json.dumps(result)


def test_local_flags_plist_changes_do_not_invalidate_rfc_preservation(
    tmp_path, identity, candidate, mailbox,
):
    path, digest = _saved(tmp_path, identity, candidate, mailbox)
    local = Path(candidate["local_copy_path"])
    local.write_bytes(local.read_bytes().replace(
        b"<dict/>", b"<dict><key>flags</key><integer>17</integer></dict>"))
    result = archive.apply_archive(identity, path, digest, session_factory=mailbox.connect)
    assert result["complete"] is True


def test_inbox_membership_inconsistency_is_refused(identity, candidate, mailbox):
    session = mailbox.connect(identity)
    session.inbox_gmids = lambda gmids: set()
    with pytest.raises(archive.ArchiveError):
        archive.plan_archive(identity, [candidate], session=session)
    assert mailbox.archive_calls == []


def test_already_deleted_message_is_refused(identity, candidate, mailbox):
    mailbox.messages[UID] = replace(mailbox.messages[UID], flags=("\\Deleted",))
    with pytest.raises(archive.ArchiveError):
        _plan(identity, candidate, mailbox)
    assert mailbox.archive_calls == []


def test_selection_error_reports_phase_and_class_without_server_text():
    session, wire = _wire_session()
    def fail(*args, **kwargs):
        raise ConnectionResetError('private-token-and-message-id')
    wire.select = fail
    with pytest.raises(archive.ArchiveError) as exc:
        session._select('[Gmail]/All Mail')
    assert str(exc.value) == 'Gmail mailbox selection failed (EXAMINE: ConnectionResetError)'
    assert 'private-token' not in str(exc.value)


def test_selection_rejection_reports_only_status():
    session, wire = _wire_session()
    wire.select = lambda *args, **kwargs: ('NO', [b'private-server-diagnostic'])
    with pytest.raises(archive.ArchiveError) as exc:
        session._select('[Gmail]/All Mail', readonly=False)
    assert str(exc.value) == 'Gmail mailbox selection failed (SELECT: NO)'
    assert 'private-server' not in str(exc.value)


def test_socket_shutdown_runs_after_failed_logout_once():
    session, wire = _wire_session()
    def logout():
        wire.calls.append(('logout',))
        raise TimeoutError('private-detail')
    wire.logout = logout
    wire.shutdown = lambda: wire.calls.append(('shutdown',))
    session.close()
    session.close()
    assert wire.calls == [('logout',), ('shutdown',)]


def test_inbox_verification_search_batches_remain_bounded_and_exact():
    session, wire = _wire_session()
    requested = [str(1000000000000000000 + index) for index in range(200)]
    def uid(*args):
        wire.calls.append(args)
        assert args[0] == 'SEARCH'
        return 'OK', [b'']
    wire.uid = uid
    assert session.inbox_gmids(requested) == set()
    queries = [call[1] for call in wire.calls if call[0] == 'SEARCH']
    assert len(queries) == 2
    import re
    assert [item for query in queries for item in re.findall(r'X-GM-MSGID (\d+)', query)] == requested
    assert all(query.count('X-GM-MSGID ') == 100 for query in queries)


@pytest.mark.parametrize('override', [None, '[Gmail]/Trash', 'Custom Folder', 'missing-folder'])
def test_archive_discovers_special_use_all_independently_of_configured_folder(identity, monkeypatch, override):
    wire = WireFixture()
    wire.list = lambda: ('OK', [
        rb'(\HasNoChildren) "/" "label \\All"',
        rb'(\HasNoChildren \Trash) "/" "[Gmail]/Trash"',
        rb'(\HasNoChildren \All) "/" "[Google Mail]/Alle Nachrichten"',
    ])
    wire.logout = lambda: None
    wire.shutdown = lambda: None
    imap_options = dict(identity.imap)
    if override:
        imap_options['folder'] = override
    configured = replace(identity, imap=imap_options)

    def connect(selected_identity):
        assert selected_identity.imap['folder'] == 'INBOX'
        assert selected_identity.name == configured.name
        assert selected_identity.imap['keychain'] == configured.imap['keychain']
        return SimpleNamespace(gmail=True, folders=['INBOX'], conn=wire, selected='INBOX')

    monkeypatch.setattr(archive.imap, '_Session', connect)
    session = archive.GmailSession(configured)
    assert session.all_mail_folder == '[Google Mail]/Alle Nachrichten'
    assert wire.calls == [('select', '"[Google Mail]/Alle Nachrichten"', True)]
    assert configured.imap.get('folder') == override
    session.close()


@pytest.mark.parametrize('listing', [
    [rb'(\HasNoChildren) "/" "[Gmail]/All Mail"'],
    [rb'(\HasNoChildren) "/" "label \\All"'],
    [rb'(\All) "/" "First"', rb'(\All) "/" "Second"'],
    [rb'(\All) "/" "INBOX"'],
])
def test_missing_or_ambiguous_all_mail_discovery_fails_closed(identity, monkeypatch, listing):
    wire = WireFixture()
    wire.list = lambda: ('OK', listing)
    wire.logout = lambda: wire.calls.append(('logout',))
    wire.shutdown = lambda: wire.calls.append(('shutdown',))
    monkeypatch.setattr(archive.imap, '_Session', lambda selected_identity:
                        SimpleNamespace(gmail=True, folders=['Custom'], conn=wire, selected='Custom'))
    with pytest.raises(archive.ArchiveError, match='special-use'):
        archive.GmailSession(identity)
    assert wire.calls == [('logout',), ('shutdown',)]


@pytest.mark.parametrize('kind', ['local', 'plan'])
def test_fifo_inputs_are_refused_without_blocking(tmp_path, kind):
    import os
    import subprocess
    import sys

    path = tmp_path / ('keeper.emlx' if kind == 'local' else 'plan.json')
    os.mkfifo(path, 0o600)
    code = '''
import sys
from pathlib import Path
from email_mcp import gmail_archive as archive
try:
    if sys.argv[1] == 'local':
        archive._local_copy(sys.argv[2], '<fixture@example.test>')
    else:
        archive._load_plan(Path(sys.argv[2]), '0' * 64)
except archive.ArchiveError:
    print('refused')
'''
    proc = subprocess.run([sys.executable, '-c', code, kind, str(path)],
                          capture_output=True, text=True, timeout=2, check=True)
    assert proc.stdout.strip() == 'refused'
