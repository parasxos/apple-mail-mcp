from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
import stat
import sqlite3
import time

import pytest

from email_mcp import envelope, health_history, state, store_health
from tests.conftest import _build_envelope_index
from tests.test_mcp_wire import _envelope, _server_env, _talk


@pytest.fixture(autouse=True)
def inactive(monkeypatch):
    monkeypatch.setattr(store_health, "_active", None)


def healthy():
    return {"ok": True, "reason": "readable", "detail": "readable"}


def denied():
    return {"ok": False, "reason": "permission_denied", "detail": "denied",
            "fix": store_health.fda_fix()}


def test_empty_index_is_healthy_without_counting_rows(monkeypatch, mail_fixture):
    index = mail_fixture / "MailData" / "Envelope Index"
    with sqlite3.connect(index) as conn:
        conn.execute("DELETE FROM messages")
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        assert kwargs["timeout"] == store_health.PROBE_TIMEOUT
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_health.sqlite3, "connect", traced)
    assert store_health.probe_store()["ok"] is True
    assert statements == ["SELECT 1 FROM messages LIMIT 1"]


@pytest.mark.parametrize("kind,reason", [
    ("missing", "store_missing"), ("index_missing", "index_missing"),
    ("corrupt", "invalid_database"), ("schema", "schema_unavailable"),
])
def test_probe_reasons_do_not_misdiagnose_fda(monkeypatch, tmp_path, kind, reason):
    base = tmp_path / "V10"
    index = base / "MailData" / "Envelope Index"
    if kind != "missing":
        index.parent.mkdir(parents=True)
    if kind == "corrupt":
        index.write_bytes(b"not an SQLite database")
    elif kind == "schema":
        sqlite3.connect(index).close()
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(base))
    result = store_health.probe_store()
    assert result["reason"] == reason
    assert "Full Disk Access" not in result["fix"]


def test_actual_permission_denial_and_recovery(monkeypatch, mail_fixture):
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    index = mail_fixture / "MailData" / "Envelope Index"
    index.chmod(0)
    try:
        health = store_health.StoreHealth(persist=False)
        result = health.refresh()
        assert result["reason"] == "permission_denied"
        assert result["last_denied_at"]
        assert "Grant status is unknown" in result["fix"]
        assert store_health.FDA_PANE in result["fix"]
    finally:
        index.chmod(0o600)
    recovered = health.refresh(force=True)
    assert recovered["status"] == "readable"
    assert recovered["last_readable_at"]
    assert recovered["last_denied_at"] == result["last_denied_at"]


def test_busy_database_is_bounded_and_not_permission_denied(monkeypatch, mail_fixture):
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    connection = sqlite3.connect(mail_fixture / "MailData" / "Envelope Index")
    try:
        connection.execute("BEGIN EXCLUSIVE")
        began = time.monotonic()
        result = store_health.probe_store()
        elapsed = time.monotonic() - began
    finally:
        connection.close()
    assert result["reason"] == "busy"
    assert elapsed < 1.0
    assert "Full Disk Access" not in result["fix"]


def test_fifo_index_does_not_block_startup_probe(monkeypatch, tmp_path):
    base = tmp_path / "V10"
    index = base / "MailData" / "Envelope Index"
    index.parent.mkdir(parents=True)
    os.mkfifo(index, 0o600)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(base))
    began = time.monotonic()
    result = store_health.probe_store()
    assert time.monotonic() - began < 1
    assert result["reason"] == "io_error"
    assert "regular file" in result["detail"]


def test_cached_concurrent_probes_and_runtime_revocation():
    now = [0.0]
    calls = []
    outcome = [healthy()]

    def probe():
        calls.append(1)
        return outcome[0]

    monitor = store_health.StoreHealth(probe=probe, monotonic=lambda: now[0], persist=False)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: monitor.refresh(), range(48)))
    assert len(calls) == 1
    assert all(item["status"] == "readable" for item in results)
    outcome[0] = denied()
    assert monitor.refresh()["status"] == "readable"
    now[0] += store_health.CACHE_SECONDS + 1
    assert monitor.refresh()["reason"] == "permission_denied"
    assert len(calls) == 2
    generation = monitor.generation
    outcome[0] = healthy()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: monitor.refresh(force=True, after=generation), range(48)))
    assert len(calls) == 3
    assert all(item["status"] == "readable" for item in results)


def test_store_failure_reprobes_but_missing_attachment_does_not_poison_health(monkeypatch):
    outcome = [healthy()]
    monitor = store_health.StoreHealth(probe=lambda: outcome[0], persist=False)
    monitor.refresh()
    monkeypatch.setattr(store_health, "_active", monitor)

    @envelope.tool
    def tool_get_attachment():
        raise FileNotFoundError("attachment missing")

    result = tool_get_attachment()
    assert result["ok"] is False
    assert result["health"]["mail_store"]["status"] == "readable"
    assert "degraded" not in result
    outcome[0] = denied()
    result = tool_get_attachment()
    assert result["degraded"] == ["no-store-access"]
    outcome[0] = healthy()

    @envelope.tool
    def tool_list_recent():
        return {"messages": []}

    assert "degraded" not in tool_list_recent()


def test_broken_probe_never_blocks_independent_tool(monkeypatch):
    def broken():
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(store_health, "_active", store_health.StoreHealth(probe=broken, persist=False))

    @envelope.tool
    def tool_send_email():
        return {"sent": True}

    result = tool_send_email()
    assert result["ok"] is True
    assert result["sent"] is True
    assert result["health"]["mail_store"]["reason"] == "probe_failed"


@pytest.mark.parametrize("failure", ["decoration", "history"])
def test_optional_health_failure_preserves_successful_delivery_receipt(monkeypatch, failure):
    monitor = store_health.StoreHealth(probe=healthy)
    monkeypatch.setattr(store_health, "_active", monitor)

    def broken(*args, **kwargs):
        if failure == "decoration":
            args[0]["ok"] = False
            args[0].pop("message_id")
        raise RuntimeError("SECRET: health failed")

    if failure == "decoration":
        monkeypatch.setattr(monitor, "decorate", broken)
    else:
        monkeypatch.setattr(health_history, "observe", broken)

    @envelope.tool
    def tool_send_email():
        return {"message_id": "fixture-delivery-receipt"}

    result = tool_send_email()
    assert result["ok"] is True
    assert result["message_id"] == "fixture-delivery-receipt"
    assert result["health"]["mail_store"]["reason"] == "probe_failed"
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["host", "history"])
def test_startup_health_failure_does_not_abort_serving(monkeypatch, failure):
    def broken(*args):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(store_health, "probe_store", healthy)
    if failure == "host":
        monkeypatch.setattr(store_health, "host_candidate", broken)
    else:
        monkeypatch.setattr(health_history, "observe", broken)
    store_health.start()

    @envelope.tool
    def tool_list_scheduled():
        return {"pending": []}

    result = tool_list_scheduled()
    assert result["ok"] is True
    assert result["pending"] == []
    assert result["health"]["mail_store"]["reason"] == "probe_failed"
    assert "SECRET" not in json.dumps(result)


def test_host_candidate_is_explicitly_unverified(monkeypatch):
    monkeypatch.setenv("__CFBundleIdentifier", "example.host")
    host = store_health.host_candidate()
    assert host["candidate"] == "example.host"
    assert host["attribution"] == "unverified"
    assert "reveal_command" not in host


def test_history_never_adopts_a_virgin_root(monkeypatch, tmp_path):
    root = tmp_path / "virgin"
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(root))
    result = store_health.StoreHealth(probe=healthy).doctor()
    assert result["history_available"] is False
    assert not root.exists()


def test_history_retains_observed_transitions_across_restart(monkeypatch, tmp_path):
    root = state.State.resolve().adopt().root
    first = store_health.StoreHealth(probe=healthy)
    readable = first.refresh()
    second = store_health.StoreHealth(probe=denied)
    report = second.doctor()
    assert report["history_available"] is True
    assert report["last_readable_at"] == readable["checked_at"]
    assert [item["status"] for item in report["transitions"]] == ["readable", "unavailable"]
    for name in ("health.json", "health.lock"):
        assert (root / name).stat().st_mode & 0o777 == 0o600
    assert "messages" not in (root / "health.json").read_text()


def test_cli_doctor_reads_cross_host_history_without_writes(monkeypatch):
    root = state.State.resolve().adopt().root
    monkeypatch.setenv("__CFBundleIdentifier", "example.previous-host")
    store_health.StoreHealth(probe=healthy).refresh()
    before = (root / "health.json").read_bytes()
    metadata = (root / "health.json").stat()
    monkeypatch.setenv("__CFBundleIdentifier", "example.current-host")
    report = store_health.StoreHealth(probe=denied, persist=False).doctor()
    assert report["history_available"] is True
    assert report["last_readable_at"] is None
    assert report["recent_hosts"][0]["host"]["candidate"] == "example.previous-host"
    assert report["transitions"][-1]["host"]["candidate"] == "example.current-host"
    assert (root / "health.json").read_bytes() == before
    assert (root / "health.json").stat().st_mtime_ns == metadata.st_mtime_ns


def test_history_supports_deliberate_state_root_relocation(monkeypatch, tmp_path):
    root = state.State.resolve().adopt().root
    alias = tmp_path / "state-link"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(alias))
    assert store_health.StoreHealth(probe=healthy).refresh()["history_available"] is True
    assert (root / "health.json").is_file()


def test_history_is_bounded_and_keeps_latest_observed_times():
    root = state.State.resolve().adopt().root
    for host in range(health_history.MAX_HOSTS + 2):
        for change in range(health_history.MAX_TRANSITIONS + 2):
            observation = {"status": "readable" if change % 2 else "unavailable",
                           "reason": "readable" if change % 2 else "permission_denied",
                           "checked_at": f"2026-09-28T12:{host:02}:{change:02}+00:00",
                           "host": {"candidate": str(host), "attribution": "unverified"}}
            assert health_history.observe(str(host), observation)[1] is True
    payload = json.loads((root / "health.json").read_bytes())
    assert len(payload["hosts"]) == health_history.MAX_HOSTS
    assert all(len(items) == health_history.MAX_TRANSITIONS
               for items in payload["hosts"].values())
    last = payload["hosts"][str(host)][-1]
    observation["checked_at"] = "2026-09-28T15:00:00+00:00"
    items, available, _ = health_history.observe(str(host), observation)
    assert available
    assert items[-1]["checked_at"] == observation["checked_at"]
    assert items[-1]["first_observed_at"] == last["first_observed_at"]


@pytest.mark.parametrize("bad", ["symlink", "corrupt", "public", "fifo"])
def test_invalid_history_cannot_block_tools_or_touch_external_files(tmp_path, bad):
    root = state.State.resolve().adopt().root
    path = root / "health.json"
    external = tmp_path / "external"
    external.write_text("untouched")
    if bad == "symlink":
        path.symlink_to(external)
    elif bad == "corrupt":
        path.write_text("broken")
        path.chmod(0o600)
    elif bad == "public":
        path.write_text("{}")
        path.chmod(0o644)
    else:
        os.mkfifo(path, 0o600)
    result = store_health.StoreHealth(probe=healthy).refresh()
    assert result["status"] == "readable"
    assert result["history_available"] is False
    assert external.read_text() == "untouched"


def test_locked_history_is_nonblocking():
    root = state.State.resolve().adopt().root
    fd = os.open(root / "health.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        began = time.monotonic()
        result = store_health.StoreHealth(probe=healthy).refresh()
        assert time.monotonic() - began < 1
        assert result["history_available"] is False
    finally:
        os.close(fd)


@pytest.mark.parametrize("target", ["marker", "lock", "root"])
def test_unsafe_history_boundary_is_refused(tmp_path, target):
    root = state.State.resolve().adopt().root
    outside = tmp_path / "outside"
    outside.write_text("outside")
    if target == "root":
        root.chmod(0o755)
    else:
        path = root / (state.MARKER if target == "marker" else "health.lock")
        path.unlink(missing_ok=True)
        path.symlink_to(outside)
    result = store_health.StoreHealth(probe=healthy).refresh()
    assert result["history_available"] is False
    assert result["status"] == "readable"
    assert outside.read_text() == "outside"


@pytest.mark.parametrize("timestamp", ["yesterday", "2026-09-28T13:00:00", "2026-19-55T25:00:00Z"])
def test_invalid_persisted_timestamps_are_not_reported(timestamp):
    root = state.State.resolve().adopt().root
    monitor = store_health.StoreHealth(probe=healthy)
    monitor.refresh()
    path = root / "health.json"
    payload = json.loads(path.read_bytes())
    payload["hosts"][monitor._key][0]["checked_at"] = timestamp
    path.write_text(json.dumps(payload))
    report = store_health.StoreHealth(probe=denied, persist=False).doctor()
    assert report["history_available"] is False
    assert report["last_readable_at"] is None


def test_directory_sync_failure_does_not_claim_durable_history(monkeypatch):
    state.State.resolve().adopt()
    fsync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        fsync(fd)

    monkeypatch.setattr(health_history.os, "fsync", fail_directory)
    result = store_health.StoreHealth(probe=healthy).refresh()
    assert result["status"] == "readable"
    assert result["history_available"] is False


@pytest.mark.parametrize("read_only", [False, True])
def test_first_stdio_response_reports_store_health_and_recovers(tmp_path, read_only):
    mail = tmp_path / "V10"
    env = _server_env(tmp_path, mail, EMAIL_MCP_READ_ONLY=str(int(read_only)))

    async def body(session, init):
        listed = await session.list_tools()
        first = _envelope(await session.call_tool("list_scheduled", {"limit": 1}))
        missing = _envelope(await session.call_tool("list_recent", {"limit": 1}))
        _build_envelope_index(mail / "MailData" / "Envelope Index")
        recovered = _envelope(await session.call_tool("list_recent", {"limit": 1}))
        return len(listed.tools), first, missing, recovered

    count, first, missing, recovered = _talk(env, body)
    assert count == (11 if read_only else 21)
    assert first["ok"] is True
    assert first["degraded"] == ["no-store-access"]
    assert first["health"]["mail_store"]["reason"] == "store_missing"
    assert first["health"]["mail_store"]["host"]["candidate"]
    assert missing["ok"] is False
    assert "degraded" in missing
    assert recovered["ok"] is True
    assert "degraded" not in recovered


def test_stdio_denied_at_boot_recovers_without_sticky_flag(tmp_path, mail_fixture):
    index = mail_fixture / "MailData" / "Envelope Index"
    index.chmod(0)
    env = _server_env(tmp_path, mail_fixture)

    async def body(session, init):
        first = _envelope(await session.call_tool("list_scheduled", {"limit": 1}))
        index.chmod(0o600)
        recovered = _envelope(await session.call_tool("list_recent", {"limit": 1}))
        return first, recovered

    try:
        first, recovered = _talk(env, body)
    finally:
        index.chmod(0o600)
    assert first["ok"] is True
    assert first["health"]["mail_store"]["reason"] == "permission_denied"
    assert first["degraded"]
    assert recovered["ok"] is True
    assert recovered["health"]["mail_store"]["status"] == "readable"
    assert "degraded" not in recovered
