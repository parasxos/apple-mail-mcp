"""Doctor tests: report shape, permission-error mapping, verbatim identity
errors, soft hooks, and the transports check mirroring per-identity
healthchecks.

Both osascript probes and the transport factory are faked — nothing here
touches Mail.app, System Events, or a real SSH socket."""
from __future__ import annotations

import os
import subprocess

import pytest

from email_mcp import doctor, identities, server
from email_mcp.transports import SendError

CHECK_NAMES = {
    "mail_store", "automation", "accessibility", "identities",
    "transports", "dispatcher", "spool_plans", "fts", "graph",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, mail_fixture):
    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EMAIL_MCP_IDENTITIES",
                       str(tmp_path / "identities.toml"))
    # Post person-clean flip there is no default from_addr; the all-green
    # doctor scenario needs a configured sending identity.
    monkeypatch.setenv("EMAIL_MCP_FROM_ADDR", "you@example.org")


class FakeOsa:
    """Stands in for doctor._osascript; dispatches on the probed app."""

    def __init__(self):
        self.automation = subprocess.CompletedProcess([], 0, "Mail\n", "")
        self.accessibility = subprocess.CompletedProcess([], 0, "true\n", "")
        self.lines: list[str] = []

    def __call__(self, line: str, timeout: float = 15.0):
        self.lines.append(line)
        if "System Events" in line:
            return self.accessibility
        return self.automation


@pytest.fixture(autouse=True)
def fake_osa(monkeypatch):
    fake = FakeOsa()
    monkeypatch.setattr(doctor, "_osascript", fake)
    return fake


@pytest.fixture(autouse=True)
def fake_transports(monkeypatch):
    """All transports healthy unless a test swaps the factory."""

    class Healthy:
        def healthcheck(self):
            return {"ok": True}

    monkeypatch.setattr(doctor, "get_transport", lambda ident: Healthy())


# --------------------------------------------------------------------- #
# shape                                                                 #
# --------------------------------------------------------------------- #


def test_run_shape_and_all_green(tmp_path):
    report = doctor.run()
    assert report["ok"] is True
    assert report["read_only"] is False
    assert set(report["checks"]) == CHECK_NAMES
    for name, check in report["checks"].items():
        assert isinstance(check["ok"], bool), name
        assert isinstance(check["detail"], str) and check["detail"], name
    assert "4 messages" in report["checks"]["mail_store"]["detail"]
    assert report["checks"]["graph"]["ok"] is True
    # Absent index is a fresh install, not a fault — and doctor is a pure
    # reader: running it must not create ANY of the state tree.
    fts_check = report["checks"]["fts"]
    assert fts_check["ok"] is True
    assert "not built" in fts_check["detail"]
    assert "--build" in fts_check["fix"]
    assert not (tmp_path / "state").exists()


def test_read_only_flag_is_reflected(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_READ_ONLY", "1")
    assert doctor.run()["read_only"] is True


# --------------------------------------------------------------------- #
# permission mapping                                                    #
# --------------------------------------------------------------------- #


def test_automation_denied_maps_to_fix(fake_osa):
    fake_osa.automation = subprocess.CompletedProcess(
        [], 1, "",
        "execution error: Not authorized to send Apple events to Mail. (-1743)",
    )
    report = doctor.run()
    check = report["checks"]["automation"]
    assert check["ok"] is False
    assert check["error_code"] == -1743
    assert "Automation" in check["fix"]
    assert "Privacy & Security" in check["fix"]
    assert report["ok"] is False


def test_mail_app_missing_maps_to_1728(fake_osa):
    fake_osa.automation = subprocess.CompletedProcess(
        [], 1, "", "execution error: application isn't running. (-1728)")
    check = doctor.check_automation()
    assert check["ok"] is False
    assert check["error_code"] == -1728
    assert "not installed or not reachable" in check["detail"]


def test_accessibility_not_trusted_names_the_fallback(fake_osa):
    fake_osa.accessibility = subprocess.CompletedProcess([], 0, "false\n", "")
    check = doctor.check_accessibility()
    assert check["ok"] is False
    assert "mailbox_delete" in check["detail"]
    assert "Accessibility" in check["fix"]


def test_mail_store_unreadable_maps_to_fda_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(tmp_path / "no-such-V10"))
    check = doctor.check_mail_store()
    assert check["ok"] is False
    assert "Full Disk Access" in check["fix"]


# --------------------------------------------------------------------- #
# identities + transports                                               #
# --------------------------------------------------------------------- #


def test_malformed_identities_toml_is_verbatim_red(tmp_path):
    path = tmp_path / "identities.toml"
    path.write_text('default = "a"\n[a\nfrom_addr = broken\n')
    with pytest.raises(identities.IdentityError) as ei:
        identities.load()
    report = doctor.run()
    check = report["checks"]["identities"]
    assert check["ok"] is False
    assert check["detail"] == str(ei.value)  # verbatim, names file + cause
    # Downstream checks degrade without crashing the report.
    assert report["checks"]["transports"]["ok"] is False
    assert "identities unreadable" in report["checks"]["transports"]["detail"]
    assert report["checks"]["graph"]["ok"] is True


def test_transports_mirrors_mocked_healthchecks(monkeypatch, tmp_path):
    (tmp_path / "identities.toml").write_text(
        'default = "alpha"\n'
        '[alpha]\n'
        'from_addr = "alpha@example.com"\n'
        'driver = "pipe"\n'
        'command = "/usr/sbin/sendmail -t"\n'
        '[beta]\n'
        'from_addr = "beta@example.com"\n'
        'driver = "pipe"\n'
        'command = "/usr/sbin/sendmail -t"\n'
    )

    def factory(ident):
        class T:
            def healthcheck(self):
                if ident.name == "alpha":
                    return {"ok": True, "driver": "pipe"}
                raise SendError(f"[{ident.name}/pipe] dead lane")

        return T()

    monkeypatch.setattr(doctor, "get_transport", factory)
    check = doctor.check_transports()
    assert check["ok"] is False
    assert check["default"] == "alpha"
    assert "1/2" in check["detail"]
    assert check["identities"]["alpha"]["ok"] is True
    assert check["identities"]["alpha"]["from_addr"] == "alpha@example.com"
    assert check["identities"]["beta"]["ok"] is False
    assert "dead lane" in check["identities"]["beta"]["error"]
    assert check["identities"]["beta"]["from_addr"] == "beta@example.com"


def test_transport_check_cli_is_alias_for_transports_section(monkeypatch, capsys):
    """--transport-check prints exactly the doctor's transports check."""
    rc = server._transport_check()
    out = __import__("json").loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True
    assert "identities" in out and "default" in out


# --------------------------------------------------------------------- #
# soft hooks                                                            #
# --------------------------------------------------------------------- #


def test_fts_soft_hook_never_reddens(monkeypatch):
    import email_mcp.fts as fts

    def boom():
        raise RuntimeError("index exploded")

    monkeypatch.setattr(fts, "status", boom)
    check = doctor.check_fts()
    assert check["ok"] is True
    assert "unavailable" in check["detail"]
    assert "exploded" in check["detail"]


def test_fts_disabled_reports_the_switch(monkeypatch):
    monkeypatch.setenv("EMAIL_MCP_FTS_ENABLED", "0")
    check = doctor.check_fts()
    assert check["ok"] is True
    assert "disabled" in check["detail"]


def test_crashed_check_becomes_red_entry_not_exception(monkeypatch):
    # run() iterates _CHECKS, which binds the functions directly — patch it.
    def boom():
        raise RuntimeError("boom")

    checks = tuple(
        (n, boom) if n == "mail_store" else (n, f) for n, f in doctor._CHECKS
    )
    monkeypatch.setattr(doctor, "_CHECKS", checks)
    report = doctor.run()
    assert report["ok"] is False
    assert "crashed" in report["checks"]["mail_store"]["detail"]


def test_check_audit_flags_file_where_dir_belongs(monkeypatch, tmp_path):
    """Red-team S3 finding (left for ownership reasons): a regular FILE at
    the audit path must read as a fault, not a fresh install."""
    bogus = tmp_path / "audit"
    bogus.write_text("not a directory")
    from email_mcp import config, doctor
    monkeypatch.setattr(config, "audit_dir", lambda: bogus)
    res = doctor.check_audit()
    assert res["ok"] is False
    assert "not a directory" in res["detail"]
    assert "mv " in res["fix"]
