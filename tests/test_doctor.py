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

# Captured at import time, BEFORE conftest's no_host_launchd stub patches
# the module attribute — for the tests that exercise the real parser.
_REAL_AGENT_LAST_EXIT = doctor._agent_last_exit


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


def test_accessibility_denial_is_advisory_not_a_red_doctor(fake_osa):
    """The first-user finding (2026-08-04): a machine where everything
    the user touches works read '=> NOT ready' off the one permission
    that only backs mailbox_delete's UI fallback. A denial warns —
    advisory, fix visible — without flipping the report's ok."""
    fake_osa.accessibility = subprocess.CompletedProcess([], 0, "false\n", "")
    report = doctor.run()
    check = report["checks"]["accessibility"]
    assert check["ok"] is False
    assert check["advisory"] is True
    assert report["ok"] is True                      # not gated
    lines = doctor.render(report)
    (acc_line,) = [ln for ln in lines if "accessibility:" in ln]
    assert acc_line.startswith("warn")
    assert not any(ln.startswith("FAIL") for ln in lines)
    (fix_line,) = [ln for ln in lines if "Accessibility" in ln
                   and "fix:" in ln]
    assert fix_line                                   # remedy stays visible


def test_non_advisory_failure_still_reddens_through_the_advisory_gate(
    fake_osa,
):
    fake_osa.automation = subprocess.CompletedProcess(
        [], 1, "",
        "execution error: Not authorized to send Apple events to Mail. (-1743)",
    )
    fake_osa.accessibility = subprocess.CompletedProcess([], 0, "false\n", "")
    report = doctor.run()
    assert report["ok"] is False
    lines = doctor.render(report)
    assert any(ln.startswith("FAIL automation") for ln in lines)
    assert any(ln.startswith("warn accessibility") for ln in lines)


def test_mail_store_unreadable_maps_to_fda_fix(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(tmp_path / "no-such-V10"))
    check = doctor.check_mail_store()
    assert check["ok"] is False
    assert "Full Disk Access" in check["fix"]


def test_failing_agent_reddens_dispatcher_and_fts_checks(monkeypatch):
    """An agent whose last run exited nonzero is a real fault even while
    everything it manages still serves — the nightly fts sync failed
    every run (no FDA on its python) under a green doctor until RC P04
    caught it from the index side (live, 2026-08-03)."""
    monkeypatch.setattr(doctor, "_agent_last_exit", lambda label: 1)
    for check in (doctor.check_dispatcher(), doctor.check_fts()):
        assert check["ok"] is False
        assert "exited 1" in check["detail"]
        assert "Full Disk Access" in check["fix"]


def test_never_ran_agent_is_not_a_fault(monkeypatch):
    """Fresh bootstrap ('never exited') and absent launchctl (CI) both
    read as None — silence is not failure."""
    monkeypatch.setattr(doctor, "_agent_last_exit", lambda label: None)
    assert doctor.check_dispatcher()["ok"] is True


def test_running_agent_is_not_judged_by_the_previous_exit(monkeypatch):
    """The first-user finding (2026-08-05): setup's aftercare verified
    the freshly-granted sync by watching it RUN, then the smoke doctor
    seconds later read `last exit code = 1` — the pre-grant run's — and
    called the machine NOT ready. A mid-run agent's recorded exit code
    belongs to a previous run; only a finished run can be judged."""
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, "\tstate = running\n\tlast exit code = 1\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert _REAL_AGENT_LAST_EXIT("com.email-mcp.fts") is None

    def fake_run_done(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, "\tstate = not running\n\tlast exit code = 1\n", "")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run_done)
    assert _REAL_AGENT_LAST_EXIT("com.email-mcp.fts") == 1


def test_mail_store_tcc_denial_maps_to_fda_fix_not_crash(monkeypatch):
    """The TCC case: the Mail dir exists but macOS refuses the read, so
    config.mail_dir raises PermissionError, not FileNotFoundError.
    Uncaught it became "check crashed" with no structured fix — the RC's
    revoked-side P14 check refused exactly that (live, 2026-08-03)."""
    def _tcc_denied():
        raise PermissionError("cannot be read — grant Full Disk Access")

    monkeypatch.setattr(doctor.config, "mail_dir", _tcc_denied)
    check = doctor.check_mail_store()
    assert check["ok"] is False
    assert "crashed" not in check["detail"]
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


def test_cold_ssh_socket_warns_without_reddening_the_doctor(monkeypatch):
    """The driver owns severity as it owns the remedy: a healthcheck
    marked advisory (cold socket, bootstrap configured — the next send
    re-establishes it headlessly) makes the transports check a warn, and
    the doctor stays ready. Second first-user round, 2026-08-05: her
    only FAILs were this and a mid-run agent — a fully working machine
    read NOT ready twice."""

    class ColdButSelfHealing:
        def healthcheck(self):
            return {"ok": False, "advisory": True,
                    "fix": "run ssh -fN lxplus to re-establish — the "
                           "next send bootstraps it headlessly."}

    monkeypatch.setattr(doctor, "get_transport",
                        lambda ident: ColdButSelfHealing())
    check = doctor.check_transports()
    assert check["ok"] is False
    assert check["advisory"] is True
    assert "bootstraps it headlessly" in check["fix"]
    report = doctor.run()
    assert report["ok"] is True
    lines = doctor.render(report)
    assert any(ln.startswith("warn transports") for ln in lines)


def test_hard_broken_lane_overrides_an_advisory_one(monkeypatch, tmp_path):
    (tmp_path / "identities.toml").write_text(
        'default = "a"\n'
        '[a]\nfrom_addr = "a@x.org"\ndriver = "pipe"\ncommand = "cat"\n'
        '[b]\nfrom_addr = "b@x.org"\ndriver = "pipe"\ncommand = "cat"\n'
    )

    def factory(ident):
        class T:
            def healthcheck(self):
                if ident.name == "a":
                    return {"ok": False, "advisory": True, "fix": "cold"}
                return {"ok": False, "fix": "dead credentials"}

        return T()

    monkeypatch.setattr(doctor, "get_transport", factory)
    check = doctor.check_transports()
    assert check["ok"] is False
    assert "advisory" not in check


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


def test_advisory_check_crash_stays_advisory():
    """Advisory is a property of the CHECK, not of one outcome: a crash
    in the accessibility probe (a PermissionError spawning osascript on
    an MDM-hardened Mac) warns exactly like its ordinary failures — it
    must never redden a machine where everything the user touches
    works."""
    def boom():
        raise PermissionError("osascript blocked by MDM policy")

    out = doctor._guarded("accessibility", boom)
    assert out["ok"] is False
    assert out.get("advisory") is True
    out = doctor._guarded("mail_store", boom)
    assert "advisory" not in out               # core checks still gate


def test_installed_but_unloaded_fts_agent_reddens(monkeypatch):
    """A plist on disk says what WOULD run; only launchd says whether
    anything will. Installed-but-unloaded ran NOTHING while every file
    probe read healthy — doctor now asks launchd."""
    from email_mcp import fts as fts_mod

    plist = fts_mod._plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>")
    monkeypatch.setattr(doctor, "_agent_loaded", lambda label: False)
    check = doctor.check_fts()
    assert check["ok"] is False
    assert "NOT loaded" in check["detail"]
    assert "doctor --fix" in check["fix"]


def test_unloaded_dispatcher_bites_once_something_is_pending(monkeypatch):
    """Same gating as not-installed: informational while the spool is
    empty, red the moment a scheduled send is waiting on it."""
    from email_mcp import dispatcher

    plist = dispatcher._plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist/>")
    monkeypatch.setattr(doctor, "_agent_loaded", lambda label: False)
    check = doctor.check_dispatcher()
    assert check["ok"] is True and check["loaded"] is False
    monkeypatch.setattr("email_mcp.spool.entries",
                        lambda s: ["e"] if s == "pending" else [])
    check = doctor.check_dispatcher()
    assert check["ok"] is False
    assert "doctor --fix" in check["fix"]


def test_backfill_trouble_is_an_advisory_warning(monkeypatch):
    """The backfill records identity trouble as state exactly so doctor
    can surface a lane that silently does nothing every night."""
    import email_mcp.fts as fts_mod

    st = {"state": "ready",
          "docs": {"indexed": 5, "partial": 0, "missing": 0, "error": 0,
                   "total": 5, "backfilled": 0},
          "last_rowid": 5,
          "last_backfill_error":
              "identity 'main': 3 error(s), last: HTTP 401"}
    monkeypatch.setattr(fts_mod, "status", lambda: st)
    check = doctor.check_fts()
    assert check["ok"] is False
    assert check["advisory"] is True
    assert "--backfill" in check["fix"]
    assert "HTTP 401" in check["detail"]


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


# --------------------------------------------------------------------- #
# a red check ALWAYS names a fix (RC P03, 2026-08-02)                    #
# --------------------------------------------------------------------- #


def test_unhealthy_transport_check_carries_the_drivers_own_fix(monkeypatch):
    """The transports check used to go red with no `fix` at all — the one
    thing every other doctor check avoids. The remedy comes from the
    driver that knows its lane, aggregated per identity."""
    from email_mcp import doctor as doc
    from email_mcp.identities import Identity

    ident = Identity(name="cern", from_addr="p@cern.ch",
                     driver="ssh_sendmail",
                     params={"host": "lxplus.cern.ch", "user": "pm",
                             "socket": "/tmp/sock-x"})
    monkeypatch.setattr(doc.identities, "load",
                        lambda: ({"cern": ident}, "cern"))
    # This file's autouse fixture stubs every transport healthy; here we
    # want the REAL driver's own report — that is the thing under test.
    from email_mcp.transports import get_transport as real_get_transport
    monkeypatch.setattr(doc, "get_transport", real_get_transport)
    monkeypatch.setattr(
        "email_mcp.transports.ssh_sendmail.SshSendmailTransport.socket_alive",
        lambda self: False)
    out = doc.check_transports()
    assert out["ok"] is False
    assert out["fix"], "a red transports check must name a fix"
    assert "cern" in out["fix"]
    assert "ControlMaster" in out["fix"]  # the ssh driver's own remedy


def test_healthy_transport_check_carries_no_fix(monkeypatch):
    from email_mcp import doctor as doc
    from email_mcp.identities import Identity

    ident = Identity(name="local", from_addr="p@x.org", driver="pipe",
                     params={"command": "/bin/cat"})
    monkeypatch.setattr(doc.identities, "load",
                        lambda: ({"local": ident}, "local"))
    from email_mcp.transports import get_transport as real_get_transport
    monkeypatch.setattr(doc, "get_transport", real_get_transport)
    out = doc.check_transports()
    assert out["ok"] is True and "fix" not in out


def test_spool_plans_check_survives_mode_000_and_names_chmod(monkeypatch,
                                                             tmp_path):
    """RC P13 FM8 (2026-08-02): a mode-000 spool made the counts scan
    raise OUT of the whole check — doctor died of the exact fault it
    exists to diagnose, and the chmod remedy went unread. The check must
    report the mode problem, name `chmod 700`, and degrade the counts."""
    import os

    from email_mcp import doctor, state

    for k in list(os.environ):
        if k.startswith("EMAIL_MCP_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMAIL_MCP_STATE_DIR", str(tmp_path / "state"))
    w = state.State.resolve().adopt()
    spool_dir, plans_dir = w.spool, w.plans
    os.chmod(spool_dir, 0o000)
    os.chmod(plans_dir, 0o000)
    try:
        out = doctor.check_spool_plans()
    finally:
        os.chmod(spool_dir, 0o700)
        os.chmod(plans_dir, 0o700)
    # The load-bearing claims, true on every runtime: the check RETURNS
    # (it used to raise OSError out of the counts scan), goes red, and
    # names the chmod remedy. The counts line differs by Python version:
    # 3.11's pathlib glob RAISES on an unreadable dir (the measured
    # crash → the degradation note fires); 3.14's glob swallows it and
    # serves zero counts. Either way the report names the mode fault —
    # the reader is never left with a green lie.
    assert out["ok"] is False
    assert f"chmod 700 {spool_dir}" in out["fix"]
    assert f"chmod 700 {plans_dir}" in out["fix"]
    assert ("unreadable" in out["detail"]          # 3.11: glob raised
            or "mode 0" in out["detail"])          # 3.14: named anyway


def test_fts_body_gap_warns_with_the_mail_side_lever(monkeypatch):
    """Phase 3(a) of the body-gap fix (2026-08-06): partial + missing is
    mail whose body Mail.app never downloaded — search silently cannot
    see it. Big gaps warn (advisory — nothing is broken) and the remedy
    names the Mail-side lever, not a rebuild that cannot help."""
    monkeypatch.setattr("email_mcp.fts.status", lambda: {
        "state": "ready",
        "docs": {"indexed": 500, "partial": 400, "missing": 200,
                 "error": 0, "total": 1100, "backfilled": 25}})
    check = doctor.check_fts()
    assert check["ok"] is False
    assert check["advisory"] is True
    assert "600 of 1100 bodies" in check["detail"]
    assert "25 backfilled" in check["detail"]
    assert "download all messages" in check["fix"]
    assert "graph identity backfill themselves nightly" in check["fix"]
    report = doctor.run()
    assert report["ok"] is True                      # warn, not a red


def test_fts_small_or_young_body_gaps_stay_quiet(monkeypatch):
    # Young index: huge ratio but too few docs to mean anything.
    monkeypatch.setattr("email_mcp.fts.status", lambda: {
        "state": "ready",
        "docs": {"indexed": 10, "partial": 400, "missing": 0,
                 "error": 0, "total": 410, "backfilled": 0}})
    assert doctor.check_fts()["ok"] is True
    # Mature index, small gap: normal traffic, not a coverage ceiling.
    monkeypatch.setattr("email_mcp.fts.status", lambda: {
        "state": "ready",
        "docs": {"indexed": 9500, "partial": 300, "missing": 200,
                 "error": 0, "total": 10000, "backfilled": 0}})
    check = doctor.check_fts()
    assert check["ok"] is True
    assert "advisory" not in check
