"""Crash-safety and integrity reporting for the scheduled-mail spool.

These tests exercise the storage boundary directly.  They never contact a
mail provider; delivery is replaced with an in-memory sink.
"""
from __future__ import annotations

import errno
import json
import os
import stat
from datetime import timedelta

import pytest

from email_mcp import (codes, config, dispatcher, doctor, sender, server,
                       spool, state)


@pytest.fixture(autouse=True)
def _send_env(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "self@example.org")
    monkeypatch.setenv("EMAIL_MCP_FROM_NAME", "Spool Reliability Test")
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES", "/nonexistent/identities.toml")


def _entry(id: str, *, subject: str = "before") -> spool.Entry:
    now = spool.utcnow()
    return spool.Entry(
        id=id,
        send_at=spool.iso(now + timedelta(hours=1)),
        created_at=spool.iso(now),
        to=["self@example.org"],
        cc=[],
        bcc=[],
        subject=subject,
        attachments=[],
        message_id=f"<{id}@example.org>",
    )


def _manifest(state: str, id: str):
    return config.spool_dir() / state / f"{id}.json"


def test_interrupted_update_preserves_the_last_valid_manifest(monkeypatch):
    entry = _entry("atomic-update")
    spool.save(b"Subject: before\r\n\r\nbody", entry)
    before = _manifest("pending", entry.id).read_bytes()

    def disk_full(fd: int, data: bytes) -> None:
        os.write(fd, data[:11])
        raise OSError(errno.ENOSPC, "simulated disk full")

    monkeypatch.setattr(spool, "_write_all", disk_full)
    entry.subject = "after"
    with pytest.raises(OSError, match="disk full"):
        spool.update("pending", entry)

    assert _manifest("pending", entry.id).read_bytes() == before
    assert spool.load("pending", entry.id).subject == "before"
    assert not list(_manifest("pending", entry.id).parent.glob("*.tmp-*"))


def test_interrupted_initial_publish_leaves_no_visible_record(monkeypatch):
    entry = _entry("atomic-create")
    real_write = spool._write_all
    calls = 0

    def fail_manifest(fd: int, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            os.write(fd, data[:9])
            raise OSError(errno.ENOSPC, "simulated manifest disk full")
        real_write(fd, data)

    monkeypatch.setattr(spool, "_write_all", fail_manifest)
    with pytest.raises(OSError, match="manifest disk full"):
        spool.save(b"Subject: create\r\n\r\nbody", entry)

    pending = config.spool_dir() / "pending"
    assert not (pending / f"{entry.id}.json").exists()
    assert not (pending / f"{entry.id}.eml").exists()
    assert not list(pending.glob("*.tmp-*"))


def test_atomic_update_syncs_file_and_parent_directory(monkeypatch):
    entry = _entry("durable-update")
    spool.save(b"Subject: before\r\n\r\nbody", entry)
    synced: list[str] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        synced.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(spool.os, "fsync", spy)
    entry.subject = "after"
    spool.update("pending", entry)

    assert "file" in synced
    assert "directory" in synced
    assert spool.load("pending", entry.id).subject == "after"


def test_scan_counts_corrupt_manifest_instead_of_hiding_it():
    entry = _entry("corrupt-visible")
    spool.save(b"Subject: visible\r\n\r\nbody", entry)
    _manifest("pending", entry.id).write_bytes(b'{"id": "corrupt-visible"')

    scan = spool.scan("pending")

    assert scan.manifest_files == 1
    assert scan.readable_manifests == 0
    assert scan.entries == []
    assert {issue.code for issue in scan.issues} >= {"manifest_invalid"}


def test_scan_detects_broken_pairs_and_interrupted_temp_files():
    pending = state.State.resolve().adopt().spool / "pending"
    (pending / "manifest-only.json").write_text("{}")
    (pending / "message-only.eml").write_bytes(b"Subject: orphan\r\n\r\nbody")
    (pending / ".record.json.tmp-123-deadbeef").write_bytes(b"partial")

    scan = spool.scan("pending")
    codes_seen = {issue.code for issue in scan.issues}

    assert "manifest_invalid" in codes_seen
    assert "message_missing" in codes_seen
    assert "manifest_missing" in codes_seen
    assert "temporary_file" in codes_seen


def test_scan_and_reader_refuse_a_symlinked_frozen_message(tmp_path):
    entry = _entry("linked-message")
    spool.save(b"Subject: original\r\n\r\nbody", entry)
    eml = config.spool_dir() / "pending" / f"{entry.id}.eml"
    decoy = tmp_path / "decoy.eml"
    decoy.write_bytes(b"Subject: decoy\r\n\r\nwrong")
    eml.unlink()
    eml.symlink_to(decoy)

    scan = spool.scan("pending")

    assert any(i.code == "message_invalid" and i.id == entry.id
               for i in scan.issues)
    with pytest.raises(OSError):
        spool.read_eml("pending", entry.id)


def test_doctor_reports_raw_and_readable_counts_for_corrupt_manifest():
    good = _entry("doctor-good")
    spool.save(b"Subject: good\r\n\r\nbody", good)
    bad = _entry("doctor-bad")
    spool.save(b"Subject: bad\r\n\r\nbody", bad)
    _manifest("pending", bad.id).write_bytes(b'{"id":')

    out = doctor.check_spool_plans()

    assert out["ok"] is False
    assert out["counts"]["pending"] == 2
    assert out["readable_counts"]["pending"] == 1
    assert out["integrity"]["ok"] is False
    assert any(i["id"] == bad.id for i in out["integrity"]["issues"])
    assert "scheduled records need attention" in out["fix"]


def test_list_scheduled_returns_healthy_siblings_but_fails_visibly():
    good = _entry("list-good")
    spool.save(b"Subject: good\r\n\r\nbody", good)
    bad = _entry("list-bad")
    spool.save(b"Subject: bad\r\n\r\nbody", bad)
    _manifest("pending", bad.id).write_bytes(b'{"id":')

    out = server.tool_list_scheduled(state="pending")

    assert out["ok"] is False
    assert out["code"] == codes.SPOOL_INTEGRITY
    assert [e["id"] for e in out["pending"]] == [good.id]
    assert out["integrity"]["counts"] == {"pending": 2}
    assert out["integrity"]["readable_counts"] == {"pending": 1}
    assert any(i["id"] == bad.id for i in out["integrity"]["issues"])


def test_dispatcher_delivers_healthy_sibling_and_reports_corruption(monkeypatch):
    sent: list[bytes] = []
    monkeypatch.setattr(sender, "_socket_alive", lambda: True)
    monkeypatch.setattr(sender, "_deliver_bytes", lambda raw: sent.append(raw))
    monkeypatch.setattr(dispatcher, "_notify", lambda *a, **k: None)

    due = _entry("dispatch-good")
    due.send_at = spool.iso(spool.utcnow() - timedelta(minutes=1))
    spool.save(b"Subject: good\r\n\r\nbody", due)
    bad = _entry("dispatch-bad")
    spool.save(b"Subject: bad\r\n\r\nbody", bad)
    _manifest("pending", bad.id).write_bytes(b'{"id":')

    out = dispatcher.run_once()

    assert out["results"][due.id] == "sent"
    assert len(sent) == 1
    assert spool.load("sent", due.id) is not None
    assert _manifest("pending", bad.id).read_bytes() == b'{"id":'
    assert out["integrity"]["ok"] is False


def test_dispatcher_status_is_nonzero_when_counts_are_incomplete(capsys):
    bad = _entry("status-bad")
    spool.save(b"Subject: bad\r\n\r\nbody", bad)
    _manifest("pending", bad.id).write_bytes(b'{"id":')

    status = dispatcher.status()
    assert status["ok"] is False
    assert status["counts"]["pending"] == 1
    assert status["integrity"]["readable_counts"]["pending"] == 0

    assert dispatcher.main(["--status"]) == 1
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["ok"] is False
