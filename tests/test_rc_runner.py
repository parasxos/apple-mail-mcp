"""tools/rc_runner.py — the v1.0-rc runner core (W3 R1).

The scaffold's two hard promises are what these tests exist to hold down:

1. **Dry-run is the default and does nothing.** ``_spawn`` — the single
   process-spawning point in the runner — is fenced to raise for EVERY
   test in this module, so any test that would start launchctl, a wheel,
   Mail.app or an MCP client fails loudly instead of touching the
   machine. A dry run additionally writes no file anywhere and journals
   nothing.
2. **The Sentinel sees the real state or the run does not start.** A
   missing tree, an unreadable file, a planted byte, a chmod, a booted-out
   agent: each is proven here against a fake estate under tmp_path, with
   ``$HOME`` redirected so the real ``~/.email-mcp`` is unreachable by
   construction.

The runner is loaded by path (it is a tool, not a package module, and it
imports nothing from email_mcp on purpose — it drives an installed wheel).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "tools" / "rc_runner.py"
_spec = importlib.util.spec_from_file_location("rc_runner", _RUNNER_PATH)
runner = importlib.util.module_from_spec(_spec)
sys.modules["rc_runner"] = runner  # @dataclass resolves types via sys.modules
_spec.loader.exec_module(runner)


# --------------------------------------------------------------------- #
# fences + fixtures                                                      #
# --------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    """Nothing in this module may start a process. This is the fence that
    proves the scaffold cannot send mail, drive launchd or run a wheel
    from a test — the runner funnels every subprocess through _spawn."""
    def refuse(argv, **kw):
        raise AssertionError(f"the test suite spawned a process: {argv}")

    monkeypatch.setattr(runner, "_spawn", refuse)
    return refuse


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    """$HOME redirected: Path.home() inside the runner can never resolve
    to the operator's real account during a test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def estate(fake_home):
    """A stand-in ~/.email-mcp with one file of each policy class."""
    root = fake_home / ".email-mcp"
    (root / "audit").mkdir(parents=True)
    (root / "spool" / "pending").mkdir(parents=True)
    (root / "graph").mkdir()
    (root / "identities.toml").write_text('[work]\ndriver = "graph"\n')
    (root / "meta.json").write_text('{"state_version": 3}')
    (root / "audit" / "2026-07.jsonl").write_text('{"tool":"send"}\n')
    (root / "spool" / "pending" / "s1.json").write_text('{"id":"s1"}')
    (root / "graph" / "work.token.json").write_text('{"refresh":"AAA"}')
    for d in (root, root / "audit", root / "spool", root / "graph"):
        d.chmod(0o700)  # what config.py guarantees; the manifest records it
    return root


class FakeLaunchd:
    """An injectable stand-in for `launchctl print` — the Sentinel's
    launchd reader is a parameter precisely so tests never talk to the
    real per-user domain."""

    def __init__(self, **agents):
        self.agents = dict(agents)

    def __call__(self, label):
        text = self.agents.get(label)
        if text is None:
            return 113, f"Could not find service {label}"
        return 0, text

    def bootout(self, label):
        self.agents.pop(label, None)


def _agents():
    return FakeLaunchd(**{
        "com.email-mcp.dispatcher": "state = running\npath = /a/dispatcher.plist\n",
        "com.email-mcp.fts": "state = waiting\npath = /a/fts.plist\n",
    })


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def fake_plan(*ids):
    return tuple(runner.PhaseSpec(pid, f"fake {pid}", runner.SANDBOX,
                                  f"acceptance for {pid}")
                 for pid in ids)


def make_ctx(tmp_path, estate, *, dry_run=True, lane=runner.SANDBOX,
             answer=None, sink=None):
    sentinel = runner.Sentinel(estate, probe=_agents())
    report = runner.Report(None, live=False, sink=sink or io.StringIO())
    return runner.Context(
        lane=lane, dry_run=dry_run, repo_root=tmp_path,
        sandbox_home=tmp_path / "sandbox", real_home=estate.parent,
        state_dir=tmp_path / "rcstate", sentinel=sentinel, report=report,
        answer=answer)


# --------------------------------------------------------------------- #
# 1. dry-run is the default                                              #
# --------------------------------------------------------------------- #


def test_dry_run_is_the_default_and_execute_is_the_only_opt_in(tmp_path, estate):
    """A bare invocation must not execute a phase body's effects. The
    fake body asks for a process; the fence would raise if the runner
    had decided to run it."""
    ran = []

    def body(ctx):
        ran.append(ctx.dry_run)
        result = ctx.sh(["email-mcp", "--version"])
        ctx.require(result.dry, "a dry run must not really spawn")

    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"), "--report",
         str(tmp_path / "r.md")],
        plan=fake_plan("P01"), implementations={"P01": body},
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)

    assert code == runner.EXIT_OK
    assert ran == [True], "the phase body must see dry_run=True by default"
    assert "DRY RUN" in sink.getvalue()
    assert "would run `email-mcp --version`" in sink.getvalue()


def test_execute_and_dry_run_are_mutually_exclusive(tmp_path, estate, capsys):
    code = runner.main(["--execute", "--dry-run"], plan=fake_plan("P01"),
                       implementations={}, sink=io.StringIO())
    assert code == runner.EXIT_USAGE
    assert "mutually exclusive" in capsys.readouterr().err


def test_no_sentinel_is_refused_with_execute(tmp_path, estate, capsys):
    """The witness may be skipped while planning; a live run is exactly
    when it is load-bearing, so the combination is a usage error."""
    assert runner.main(["--no-sentinel"], plan=fake_plan("P01"),
                       implementations={}, sink=io.StringIO()) == runner.EXIT_OK

    code = runner.main(
        ["--no-sentinel", "--execute", "--state-dir", str(tmp_path / "s"),
         "--report", str(tmp_path / "r.md")],
        plan=fake_plan("P01"), implementations={}, sink=io.StringIO())
    assert code == runner.EXIT_USAGE
    assert "--no-sentinel is refused with --execute" in capsys.readouterr().err


def test_dry_run_touches_nothing_on_disk(tmp_path, estate):
    """No journal, no report file, no scratch — and the real repo's docs/
    gains no rc-report. The plan goes to stdout and stays there."""
    def body(ctx):
        ctx.sh(["pipx", "install", "dist/email_mcp.whl"])
        ctx.write(ctx.sandbox_home / "answers.json", "{}")

    before_tmp = tree(tmp_path)
    before_estate = tree(estate)
    repo_docs = _RUNNER_PATH.parent.parent / "docs"
    before_docs = tree(repo_docs) if repo_docs.exists() else set()

    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate")],
        plan=fake_plan("P01", "P02"),
        implementations={"P01": body, "P02": body},
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)

    assert code == runner.EXIT_OK
    assert tree(tmp_path) == before_tmp, "a dry run wrote to the filesystem"
    assert tree(estate) == before_estate, "a dry run touched the state root"
    if repo_docs.exists():
        assert tree(repo_docs) == before_docs
    assert "would write" in sink.getvalue()


# --------------------------------------------------------------------- #
# 2. the Sentinel                                                        #
# --------------------------------------------------------------------- #


def test_sentinel_detects_a_planted_change(estate):
    """Same-length content edit, a new file, a deletion and a chmod —
    all four must surface, and the same-length edit is why the manifest
    hashes content instead of trusting size."""
    watcher = runner.Sentinel(estate, probe=_agents())
    baseline = watcher.capture()

    (estate / "identities.toml").write_text('[work]\ndriver = "smtp!"\n')
    (estate / "intruder.txt").write_text("x")
    (estate / "meta.json").unlink()
    (estate / "graph").chmod(0o755)

    diff = watcher.verify(baseline)
    assert not diff.clean
    assert "identities.toml" in diff.changed
    assert "intruder.txt" in diff.added
    assert "meta.json" in diff.removed
    assert "graph" in diff.changed, "a mode change is a change"
    assert "MATERIAL DRIFT" in diff.render()


def test_sentinel_separates_expected_churn_from_credential_loss(estate):
    """A run legitimately appends to the ledger and moves spool files;
    it never makes a token cache disappear."""
    watcher = runner.Sentinel(estate, probe=_agents())
    baseline = watcher.capture()

    (estate / "audit" / "2026-07.jsonl").write_text('{"tool":"send"}\n{"x":1}\n')
    (estate / "spool" / "pending" / "s1.json").unlink()
    (estate / "graph" / "work.token.json").write_text('{"refresh":"BBB"}')

    diff = watcher.verify(baseline)
    assert diff.clean, diff.render()
    assert "audit/2026-07.jsonl" in diff.expected
    assert "spool/pending/s1.json" in diff.expected

    (estate / "graph" / "work.token.json").unlink()
    gone = watcher.verify(baseline)
    assert not gone.clean
    assert "graph/work.token.json" in gone.removed

    strict = watcher.verify(baseline, strict=True)
    assert "audit/2026-07.jsonl" in strict.changed, "strict allows no churn"


def test_sentinel_refuses_when_it_cannot_read_the_real_state(tmp_path, estate):
    """Absent tree and unreadable subtree both refuse — a Sentinel with a
    hole in it cannot say afterwards what changed."""
    missing = runner.Sentinel(tmp_path / "nope", probe=_agents())
    with pytest.raises(runner.SentinelError, match="does not exist"):
        missing.capture()

    if os.geteuid() == 0:
        pytest.skip("root reads through mode 000")
    watcher = runner.Sentinel(estate, probe=_agents())
    (estate / "audit").chmod(0o000)
    try:
        with pytest.raises(runner.SentinelError, match="cannot read"):
            watcher.capture()
    finally:
        (estate / "audit").chmod(0o700)

    (estate / "identities.toml").chmod(0o000)
    try:
        with pytest.raises(runner.SentinelError, match="cannot read"):
            watcher.capture()
    finally:
        (estate / "identities.toml").chmod(0o600)


def test_sentinel_refusal_blocks_the_run_before_any_phase(tmp_path, capsys):
    ran = []
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate")],
        plan=fake_plan("P01"),
        implementations={"P01": lambda ctx: ran.append(1)},
        sentinel=runner.Sentinel(tmp_path / "absent", probe=_agents()),
        sink=io.StringIO())
    assert code == runner.EXIT_SENTINEL_REFUSED
    assert ran == [], "no phase may run behind a refused Sentinel"
    assert "sentinel refused" in capsys.readouterr().err


def test_sentinel_detects_a_booted_out_prod_agent(estate):
    """The reason the Sentinel exists: a sandbox launchd action shares
    the per-user label space with the operator's real agents."""
    agents = _agents()
    watcher = runner.Sentinel(estate, probe=agents)
    baseline = watcher.capture()

    assert watcher.verify(baseline).clean

    agents.bootout("com.email-mcp.dispatcher")
    diff = watcher.verify(baseline)
    assert not diff.clean
    assert "com.email-mcp.dispatcher" in diff.agents_drifted

    agents.agents["com.email-mcp.dispatcher"] = (
        "state = running\npath = /elsewhere/dispatcher.plist\n")
    moved = watcher.verify(baseline)
    assert "com.email-mcp.dispatcher" in moved.agents_drifted, \
        "a re-pointed plist is drift even though the label is back"


def test_sentinel_ignores_volatile_launchctl_fields(estate):
    """An agent that merely ticked between the two reads is not drift."""
    agents = _agents()
    watcher = runner.Sentinel(estate, probe=agents)
    baseline = watcher.capture()
    agents.agents["com.email-mcp.fts"] = (
        "state = running\npath = /a/fts.plist\npid = 4242\nruns = 9\n")
    diff = watcher.verify(baseline)
    assert diff.clean, diff.render()


# --------------------------------------------------------------------- #
# 3. crash + resume                                                      #
# --------------------------------------------------------------------- #


class Kill(BaseException):
    """Stands in for `kill -9` — not an Exception, so the runner's phase
    error handling cannot catch it and tidy it away."""


def _live_args(tmp_path):
    return ["--execute", "--state-dir", str(tmp_path / "rcstate"),
            "--report", str(tmp_path / "rc-report.md")]


def test_a_crash_mid_phase_leaves_the_phase_journalled_as_running(tmp_path, estate):
    """The journal is flushed BEFORE a phase body runs, so a process kill
    mid-phase still says which phase was in flight."""
    def ok(ctx):
        ctx.note("fine")

    def crash(ctx):
        raise Kill("kill -9 during the FTS build")

    with pytest.raises(Kill):
        runner.main(_live_args(tmp_path), plan=fake_plan("P01", "P02", "P03"),
                    implementations={"P01": ok, "P02": ok, "P03": crash},
                    sentinel=runner.Sentinel(estate, probe=_agents()),
                    sink=io.StringIO())

    journals = list((tmp_path / "rcstate").glob("rc-*.json"))
    assert len(journals) == 1
    results = json.loads(journals[0].read_text())["results"]
    assert results["P01"]["status"] == runner.PASS
    assert results["P02"]["status"] == runner.PASS
    assert results["P03"]["status"] == runner.RUNNING
    assert stat.S_IMODE(journals[0].stat().st_mode) == 0o600


def test_resume_skips_settled_phases_and_reruns_the_crashed_one(tmp_path, estate):
    calls = []

    def make(pid, boom=False):
        def body(ctx):
            calls.append(pid)
            if boom:
                raise Kill("crash")
        return body

    plan = fake_plan("P01", "P02", "P03")
    sentinel = runner.Sentinel(estate, probe=_agents())
    with pytest.raises(Kill):
        runner.main(_live_args(tmp_path), plan=plan,
                    implementations={"P01": make("P01"), "P02": make("P02"),
                                     "P03": make("P03", boom=True)},
                    sentinel=sentinel, sink=io.StringIO())
    assert calls == ["P01", "P02", "P03"]

    calls.clear()
    code = runner.main(_live_args(tmp_path) + ["--resume"], plan=plan,
                       implementations={"P01": make("P01"), "P02": make("P02"),
                                        "P03": make("P03")},
                       sentinel=sentinel, sink=io.StringIO())
    assert calls == ["P03"], "settled phases must not run twice"
    assert code == runner.EXIT_OK

    journals = list((tmp_path / "rcstate").glob("rc-*.json"))
    assert len(journals) == 1, "a resumed pass continues the same journal"
    results = json.loads(journals[0].read_text())["results"]
    assert results["P03"]["status"] == runner.PASS


def test_resume_compares_against_the_original_baseline(tmp_path, estate):
    """Drift a crashed pass caused must not be laundered by re-capturing
    the estate at resume time."""
    def crash(ctx):
        (estate / "identities.toml").write_text("[tampered]\n")
        raise Kill("crash after damage")

    sentinel = runner.Sentinel(estate, probe=_agents())
    with pytest.raises(Kill):
        runner.main(_live_args(tmp_path), plan=fake_plan("P01"),
                    implementations={"P01": crash}, sentinel=sentinel,
                    sink=io.StringIO())

    sink = io.StringIO()
    code = runner.main(_live_args(tmp_path) + ["--resume"],
                       plan=fake_plan("P01"),
                       implementations={"P01": lambda ctx: None},
                       sentinel=sentinel, sink=sink)
    assert code == runner.EXIT_SENTINEL_DRIFT
    assert "identities.toml" in sink.getvalue()


# --------------------------------------------------------------------- #
# 4. selection, manual protocol, fences                                  #
# --------------------------------------------------------------------- #


def test_phase_selector_expands_ranges_and_keeps_plan_order(tmp_path):
    assert runner.parse_phase_selector("P03") == ["P03"]
    assert runner.parse_phase_selector("p07,P02") == ["P02", "P07"]
    assert runner.parse_phase_selector("P05-P08") == ["P05", "P06", "P07", "P08"]
    with pytest.raises(ValueError, match="unknown phase"):
        runner.parse_phase_selector("P99")

    lane_filtered = runner.select(runner.PLAN, lane=runner.PROD, selector=None,
                                  state_dir=tmp_path)
    assert {s.lane for s in lane_filtered} == {runner.PROD, runner.BOTH}
    picked = runner.select(runner.PLAN, lane=runner.BOTH, selector="P16-P18",
                           state_dir=tmp_path)
    assert [s.id for s in picked] == ["P16", "P17", "P18"]


def test_a_once_phase_is_skipped_after_it_has_ever_passed(tmp_path):
    """P18 (the fresh-account walk) is a one-time proof, not a chore."""
    state_dir = tmp_path / "rcstate"
    state_dir.mkdir()
    assert "P18" in [s.id for s in runner.select(
        runner.PLAN, lane=runner.BOTH, selector="P18", state_dir=state_dir)]

    (state_dir / "rc-20260101-000000.json").write_text(json.dumps(
        {"run_id": "rc-20260101-000000",
         "results": {"P18": {"id": "P18", "status": runner.PASS}}}))
    assert runner.select(runner.PLAN, lane=runner.BOTH, selector="P18",
                         state_dir=state_dir) == []


def test_manual_phase_records_pending_without_an_operator(tmp_path, estate):
    """An unattended pass never invents a human verdict, and a pending
    manual step keeps the whole run INCOMPLETE."""
    spec = runner.PhaseSpec("P09", "lid-closed", runner.PROD, "delivers",
                            manual=True)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=None)
    result = runner.execute(spec, ctx, {})
    assert result.status == runner.PENDING
    verdict, code = runner.verdict_for([result], None, dry_run=False)
    assert code == runner.EXIT_INCOMPLETE
    assert "INCOMPLETE" in verdict


def test_manual_phase_takes_an_operator_verdict_with_evidence(tmp_path, estate):
    answers = iter(["what?", "pass: delivered 03:12, lid shut since 02:50"])
    ctx = make_ctx(tmp_path, estate, dry_run=False,
                   answer=lambda prompt: next(answers))
    spec = runner.PhaseSpec("P09", "lid-closed", runner.PROD, "delivers",
                            manual=True)
    result = runner.execute(spec, ctx, {})
    assert result.status == runner.PASS
    assert any("delivered 03:12" in d for d in result.detail)

    failing = make_ctx(tmp_path, estate, dry_run=False,
                       answer=lambda prompt: "fail: never arrived")
    assert runner.execute(spec, failing, {}).status == runner.FAIL


def test_context_refuses_to_write_inside_the_real_state_root(tmp_path, estate):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    with pytest.raises(runner.UnsafeAction, match="inside the real state root"):
        ctx.write(estate / "identities.toml", "[evil]\n")
    with pytest.raises(runner.UnsafeAction, match="outside the run's write roots"):
        ctx.write(estate.parent / "elsewhere.txt", "x")

    kept = ctx.write(ctx.sandbox_home / "answers.json", "{}")
    assert kept.read_text() == "{}"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600


def test_sandbox_lane_refuses_commands_and_env_naming_the_real_estate(
        tmp_path, estate):
    ctx = make_ctx(tmp_path, estate, dry_run=False, lane=runner.SANDBOX)
    with pytest.raises(runner.UnsafeAction, match="names the real state root"):
        ctx.sh(["rm", "-rf", str(estate / "spool")])
    with pytest.raises(runner.UnsafeAction, match="points into the real state"):
        ctx.env({"EMAIL_MCP_STATE_DIR": str(estate / "spool")})

    env = ctx.env()
    assert env["HOME"] == str(ctx.sandbox_home)
    assert not any(k.startswith("EMAIL_MCP_") for k in env), \
        "an inherited EMAIL_MCP_* must not leak into the sandbox"


def test_state_dir_inside_the_state_root_is_refused(tmp_path, estate, capsys):
    """The runner's own journals must never register as drift in its own
    manifest."""
    code = runner.main(["--state-dir", str(estate / "rc")],
                       plan=fake_plan("P01"), implementations={},
                       sentinel=runner.Sentinel(estate, probe=_agents()),
                       sink=io.StringIO())
    assert code == runner.EXIT_USAGE
    assert "must live outside" in capsys.readouterr().err


def test_a_strict_sentinel_phase_fails_on_churn_a_normal_phase_tolerates(
        tmp_path, estate):
    """P16's whole claim is that the real estate is byte-identical, so it
    is verified with zero expected churn allowed."""
    def append_to_ledger(ctx):
        (estate / "audit" / "2026-07.jsonl").write_text('{"a":1}\n{"b":2}\n')

    plan = (runner.PhaseSpec("P16", "uninstall + purge", runner.SANDBOX,
                             "estate byte-identical", sentinel_strict=True),)
    sink = io.StringIO()
    code = runner.main(_live_args(tmp_path), plan=plan,
                       implementations={"P16": append_to_ledger},
                       sentinel=runner.Sentinel(estate, probe=_agents()),
                       sink=sink)
    assert code == runner.EXIT_PHASE_FAILED
    assert "strict sentinel" in sink.getvalue()


def test_report_is_written_live_and_marks_manual_steps_unchecked(tmp_path, estate):
    plan = (runner.PhaseSpec("P01", "wheel install", runner.SANDBOX, "installs"),
            runner.PhaseSpec("P09", "lid-closed", runner.PROD, "delivers",
                             manual=True))
    report = tmp_path / "rc-report.md"
    runner.main(_live_args(tmp_path) + ["--keep-going"], plan=plan,
                implementations={"P01": lambda ctx: ctx.note("wheel 0.11.0")},
                sentinel=runner.Sentinel(estate, probe=_agents()),
                sink=io.StringIO())

    text = report.read_text()
    assert text.startswith("# v1.0-rc report")
    assert "P01 · wheel install · sandbox — PASS" in text
    assert "- [x] installs" in text
    assert "wheel 0.11.0" in text
    assert "[MANUAL] — **MANUAL — PENDING**" in text
    assert "- [ ] delivers" in text, "a pending manual step stays unchecked"

    runner.main(_live_args(tmp_path) + ["--keep-going"], plan=plan,
                implementations={"P01": lambda ctx: None},
                sentinel=runner.Sentinel(estate, probe=_agents()),
                sink=io.StringIO())
    assert report.read_text().count("# v1.0-rc report") == 1, \
        "a second pass appends to the day's report"


def test_soak_report_aggregates_every_journal(tmp_path):
    state_dir = tmp_path / "rcstate"
    state_dir.mkdir()
    for n, statuses in ((1, {"P01": runner.PASS, "P13": runner.FAIL}),
                        (2, {"P01": runner.PASS, "P13": runner.PASS})):
        (state_dir / f"rc-2026070{n}-120000.json").write_text(json.dumps({
            "run_id": f"rc-2026070{n}-120000", "started_at": f"2026-07-0{n}",
            "live": True,
            "results": {pid: {"id": pid, "status": s}
                        for pid, s in statuses.items()}}))
    (state_dir / "rc-broken.json").write_text("{not json")

    text = runner.soak_report(state_dir)
    assert "2 run(s)" in text, "the unreadable journal is skipped, not fatal"
    assert "| P01 | wheel install | 2 | 0 |" in text
    assert "| P13 | failure matrix FM1-FM10 | 1 | 1 |" in text


def test_the_shipped_plan_is_the_nineteen_phases_of_the_w3_document():
    assert [s.id for s in runner.PLAN] == [f"P{n:02d}" for n in range(1, 20)]
    assert [s.id for s in runner.PLAN if s.manual] == ["P09", "P14", "P18",
                                                       "P19"]
    assert [s.id for s in runner.PLAN if s.once] == ["P18"]
    assert [s.id for s in runner.PLAN if s.sentinel_strict] == ["P16"]
    assert all(s.lane in (runner.SANDBOX, runner.PROD, runner.BOTH)
               for s in runner.PLAN)
    assert all(s.acceptance for s in runner.PLAN)
    # R2 attached bodies stage by stage; S4 (failure matrix, v0.9
    # upgrade, uninstall+purge, the once-ever walk) closed the set —
    # every planned phase now has a body.
    assert set(runner.IMPLEMENTATIONS) == {s.id for s in runner.PLAN}


def test_the_runner_and_the_plan_document_agree():
    """docs/w3-rc-plan.md is the plan of record. The registry is a
    mirror of it, and a mirror that drifts is worse than no mirror —
    an operator reading the doc would be told the wrong lane."""
    doc = _RUNNER_PATH.parent.parent / "docs" / "w3-rc-plan.md"
    rows = re.findall(r"^\| \*\*(P\d\d)\*\* \| ([^|]+) \| ([^|]+) \| ([^|]+) \|",
                      doc.read_text(encoding="utf-8"), flags=re.M)
    assert len(rows) == 19, "the phase table must list every phase once"
    for (pid, title, lane, mode), spec in zip(rows, runner.PLAN):
        assert pid == spec.id
        assert title.strip() == spec.title, f"{pid} title drifted"
        assert lane.strip() == spec.lane, f"{pid} lane drifted"
        assert ("MANUAL" in mode) is spec.manual, f"{pid} mode drifted"
        assert ("once" in mode) is spec.once, f"{pid} once-ness drifted"
        assert ("strict" in mode) is spec.sentinel_strict, f"{pid} strictness"


def test_an_unbound_phase_blocks_a_live_run_but_not_a_plan(tmp_path, estate):
    sink = io.StringIO()
    code = runner.main(["--state-dir", str(tmp_path / "rcstate"),
                        "--phase", "P01,P02"],
                       sentinel=runner.Sentinel(estate, probe=_agents()),
                       sink=sink)
    assert code == runner.EXIT_OK
    assert "P02" in sink.getvalue(), "a dry run walks the whole selection"

    code = runner.main(_live_args(tmp_path) + ["--phase", "P01"],
                       sentinel=runner.Sentinel(estate, probe=_agents()),
                       sink=io.StringIO())
    assert code == runner.EXIT_PHASE_FAILED


# --------------------------------------------------------------------- #
# 5. the S1 bodies — P01..P05, the sandbox core                          #
# --------------------------------------------------------------------- #
#
# Bodies are tested through the runner's own seams and never by running
# real commands: the dry run proves each body's complete command plan
# through the intent notes, and the live paths run against a scripted
# _spawn whose canned outputs (and filesystem side effects) stand in for
# the wheel, the wizard, doctor, the index and the MCP wire.

_REPO = _RUNNER_PATH.parent.parent
_FIXTURE = _REPO / "tests" / "fixtures" / "setup_answers.json"


class _Proc:
    """What the scripted spawn hands back — the CompletedProcess surface
    the runner's Ran wrapper actually reads."""

    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class ScriptedSpawn:
    """A route table over argv, standing in for the one spawn point.

    Every call is recorded (argv, env, stdin) so a test can prove WHAT a
    body would have run and WHERE — lane separation is an env assertion,
    the fixture feed is a stdin assertion. An argv no route claims is a
    test bug, surfaced loudly."""

    def __init__(self, *routes):
        self.routes = routes
        self.calls = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=None,
                 stdin_text=None):
        argv = [str(a) for a in argv]
        call = {"argv": argv, "env": dict(env or {}), "stdin": stdin_text,
                "cwd": cwd}
        self.calls.append(call)
        for match, respond in self.routes:
            if match(argv):
                return respond(call)
        raise AssertionError(f"unscripted spawn: {argv}")


def run_phase(pid, ctx):
    return runner.execute(runner.PLAN_BY_ID[pid], ctx,
                          runner.IMPLEMENTATIONS)


def test_command_fence_stops_at_a_path_boundary(tmp_path, estate):
    """Found by the first bound body: ~/.email-mcp-rc — the runner's own
    journal dir and default sandbox home — shares the real root's
    spelling as a prefix, and the naive substring fence refused the
    sandbox's own venv path. The fence must hold the root and its
    children while letting the sibling through."""
    ctx = make_ctx(tmp_path, estate, dry_run=True, lane=runner.SANDBOX)
    with pytest.raises(runner.UnsafeAction):
        ctx.sh(["cat", str(estate)])
    with pytest.raises(runner.UnsafeAction):
        ctx.sh(["cat", f"{estate}/identities.toml"])
    sibling = f"{estate}-rc/sandbox-home/venv/bin/email-mcp"
    assert ctx.sh([sibling, "version"]).dry, \
        "a sibling spelled <root>-rc is not the estate"


def test_command_fence_refuses_dot_dot_reentry_into_the_estate(
        tmp_path, estate):
    """The boundary relaxation opened a hole the substring fence never
    had: <root>-rc/../<root> ends the raw prefix match on '-', yet the
    OS resolves it straight into the estate. The fence must judge the
    normalized spelling too — while the honest sibling keeps passing."""
    ctx = make_ctx(tmp_path, estate, dry_run=True, lane=runner.SANDBOX)
    reentry = f"{estate}-rc/../{estate.name}/graph/work.token.json"
    with pytest.raises(runner.UnsafeAction):
        ctx.sh(["cat", reentry])
    with pytest.raises(runner.UnsafeAction):
        ctx.sh(["cat", f"--file={reentry}"])


def test_s1_dry_run_plans_every_sandbox_core_command(tmp_path, estate):
    """A bare P01-P05 selection must print the complete command plan and
    spawn nothing (the autouse fence would raise): the venv, the wheel
    build+install, the version read, the scripted setup, doctor on each
    lane, the bounded index build, the search probe, the prod status
    read, and the wire client."""
    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"), "--phase", "P01-P05"],
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)
    text = sink.getvalue()

    assert code == runner.EXIT_OK
    for phase, title in (("P01", "wheel install"), ("P02", "scripted setup"),
                         ("P03", "doctor"), ("P04", "index"),
                         ("P05", "wire-level search/read")):
        assert f"{phase} · {title}" in text and "dry-run" in text
    for intent in ("-m venv", "wheel --no-deps",
                   "bin/email-mcp version", "bin/email-mcp setup",
                   "bin/email-mcp doctor", "bin/email-mcp --doctor",
                   "fts --build --limit 200", "fts --status --json",
                   "rc-p04-probe.py", "rc-p05-client.py"):
        assert intent in text, f"the dry run never planned: {intent}"
    assert "would write" in text  # the probe + wire-client scripts
    assert "(0 phase(s) with no body yet)" in text, \
        "every S1 phase must be bound"


# -- P01 ---------------------------------------------------------------- #


def _p01_spawn(ctx, *, wheel="email_mcp-0.11.0-py3-none-any.whl",
               reported="0.11.0\n"):
    dist = ctx.sandbox_home / "dist"

    def build(call):
        dist.mkdir(parents=True, exist_ok=True)
        (dist / wheel).write_bytes(b"")
        return _Proc(0)

    return ScriptedSpawn(
        (lambda a: a[1:3] == ["-m", "venv"], lambda c: _Proc(0)),
        (lambda a: "wheel" in a and "--no-deps" in a, build),
        (lambda a: a[0].endswith("pip") and a[1] == "install",
         lambda c: _Proc(0)),
        (lambda a: a[0].endswith("email-mcp") and a[1:] == ["version"],
         lambda c: _Proc(0, reported)),
    )


def test_p01_passes_when_the_wheel_answers_with_its_own_version(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p01_spawn(ctx))
    result = run_phase("P01", ctx)
    assert result.status == runner.PASS, result.detail
    assert any("email_mcp-0.11.0" in d for d in result.detail)


def test_p01_fails_when_the_reported_version_is_not_the_wheels(
        tmp_path, estate, monkeypatch):
    """The acceptance is the version ROUND TRIP: a binary that answers
    with any other version is the wrong bytes, however plausible."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p01_spawn(ctx, reported="9.9.9\n"))
    result = run_phase("P01", ctx)
    assert result.status == runner.FAIL
    assert any("'9.9.9'" in d and "'0.11.0'" in d for d in result.detail)


# -- P02 ---------------------------------------------------------------- #


def _p02_spawn(ctx, *, ident_mode=0o600, secret=False, fts_db=False,
               command=None):
    """Scripted `email-mcp setup`: the side effect builds the tree the
    wizard would leave, parameterised so each acceptance clause has a
    violation to catch."""
    def setup(call):
        root = ctx.sandbox_home / ".email-mcp"
        for leaf in ("", "spool", "spool/pending", "plans", "graph", "fts",
                     "audit"):
            d = root / leaf if leaf else root
            d.mkdir(parents=True, exist_ok=True)
            d.chmod(0o700)
        text = ('default = "main"\n\n[main]\n'
                'from_addr = "rc-sandbox@example.org"\n'
                'driver = "pipe"\ncommand = "/usr/sbin/sendmail -t -i"\n')
        if secret:
            text += 'password = "hunter2"\n'
        ident = root / "identities.toml"
        ident.write_text(text)
        ident.chmod(ident_mode)
        if fts_db:
            (root / "fts" / "fts.db").write_text("db")
        entry = command if command is not None else \
            str(ctx.sandbox_home / "venv" / "bin" / "python")
        return _Proc(0, (
            "MCP client config (claude mcp add-json):\n"
            "{\n"
            '  "mcpServers": {\n'
            '    "apple-mail": {\n'
            f'      "command": "{entry}",\n'
            '      "args": ["-m", "email_mcp.server"]\n'
            "    }\n"
            "  }\n"
            "}\n"
            "\nsmoke test (doctor):\n  ok  mail_store: 42 messages\n"))

    return ScriptedSpawn(
        (lambda a: a[0].endswith("email-mcp") and a[1:] == ["setup"], setup),
    )


def test_p02_feeds_the_fixture_answers_and_verifies_the_tree(
        tmp_path, estate, monkeypatch):
    """The body must feed EXACTLY the fixture's answers, in order, one
    per line — and then judge the tree the wizard left."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO  # the body reads the real fixture file
    spawn = _p02_spawn(ctx)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P02", ctx)
    assert result.status == runner.PASS, result.detail

    answers = json.loads(_FIXTURE.read_text())["answers"]
    fed = spawn.calls[0]["stdin"]
    assert fed == "".join(a["answer"] + "\n" for a in answers), \
        "the wizard was fed something other than the fixture"
    assert any("MCP entry" in d for d in result.detail)


@pytest.mark.parametrize("breakage, symptom", [
    ({"ident_mode": 0o644}, "identities.toml"),
    ({"secret": True}, "secret value"),
    ({"fts_db": True}, "FTS index"),
    ({"command": "python3"}, "not absolute"),
])
def test_p02_fails_on_each_violated_acceptance_clause(
        tmp_path, estate, monkeypatch, breakage, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    monkeypatch.setattr(runner, "_spawn", _p02_spawn(ctx, **breakage))
    result = run_phase("P02", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


def test_p02_fixture_matches_the_wizard_question_order(monkeypatch):
    """The rot-guard for tests/fixtures/setup_answers.json: feed its
    answers to the SHIPPED wizard in-process and require every prompt to
    arrive in the fixture's order — the two Exchange questions included
    (added 2026-08-02; the y/n pair exercises both without a browser).
    A wizard that asks anything else, or in another order, fails here
    before it can misfeed a live P02."""
    spec = json.loads(_FIXTURE.read_text())["answers"]
    feed = iter([a["answer"] for a in spec])
    seen = []

    def scripted(prompt=""):
        seen.append(prompt)
        return next(feed)

    monkeypatch.setattr("builtins.input", scripted)
    # Setup's smoke test must not probe the real Mail.app from the suite.
    monkeypatch.setattr("email_mcp.doctor.run", lambda: {
        "ok": True, "read_only": False, "checks": {},
        "audit": {"ok": True, "detail": "no events yet"}})
    from email_mcp import config, graph, lifecycle

    def refuse(_ident):
        raise AssertionError("the fixture must never reach a browser sign-in")

    monkeypatch.setattr(graph, "device_login", refuse)
    assert lifecycle.setup() == 0
    assert len(seen) == len(spec), \
        f"the wizard asked {len(seen)} question(s), the fixture answers {len(spec)}"
    for expected, prompt in zip(spec, seen):
        assert expected["prompt"] in prompt, (
            f"fixture prompt {expected['prompt']!r} does not match the "
            f"wizard's {prompt!r} — the fixture is stale")
    text = config.identities_file().read_text()
    assert 'driver = "pipe"' in text
    assert "drafts" not in text, "Exchange was declined — no graph lane"


# -- P03 ---------------------------------------------------------------- #


def test_p03_runs_doctor_once_per_lane_and_passes_when_green(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    spawn = ScriptedSpawn(
        (lambda a: a[-1] == "doctor",
         lambda c: _Proc(0, "ok  mail_store: 12 messages\n")),
        (lambda a: a[-1] == "--doctor",
         lambda c: _Proc(0, json.dumps({"ok": True, "checks": {},
                                        "audit": {"ok": True}}))),
    )
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P03", ctx)
    assert result.status == runner.PASS, result.detail
    homes = [c["env"].get("HOME") for c in spawn.calls
             if c["argv"][-1] == "doctor"]
    assert homes == [str(ctx.sandbox_home), os.environ["HOME"]], \
        "the same verb must run once per lane: sandbox HOME, then the real one"


def test_p03_prod_failures_must_name_a_concrete_fix(tmp_path, estate,
                                                    monkeypatch):
    """An unhealthy prod estate is acceptable exactly when every failure
    carries its remedy; a fix-less failure fails the phase."""
    def doctor(json_report):
        def spawn_for(ctx):
            return ScriptedSpawn(
                (lambda a: a[-1] == "doctor",
                 lambda c: _Proc(0, "ok\n")
                 if c["env"].get("HOME") == str(ctx.sandbox_home)
                 else _Proc(1, "FAIL mail_store: cannot read\n")),
                (lambda a: a[-1] == "--doctor",
                 lambda c: _Proc(1, json.dumps(json_report))),
            )
        return spawn_for

    fixed = {"ok": False, "audit": {"ok": True}, "checks": {"mail_store": {
        "ok": False, "detail": "cannot read the Envelope Index",
        "fix": "System Settings → Privacy & Security → Full Disk Access"}}}
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", doctor(fixed)(ctx))
    result = run_phase("P03", ctx)
    assert result.status == runner.PASS, result.detail
    assert any("Full Disk Access" in d for d in result.detail)

    fixless = {"ok": False, "audit": {"ok": True}, "checks": {"mail_store": {
        "ok": False, "detail": "cannot read the Envelope Index"}}}
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", doctor(fixless)(ctx))
    result = run_phase("P03", ctx)
    assert result.status == runner.FAIL
    assert any("name no fix" in d for d in result.detail)


# -- P04 ---------------------------------------------------------------- #


def _p04_status(**overrides):
    from datetime import datetime, timezone

    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = {"state": "ready", "built_at": fresh, "last_sync_at": fresh,
              "last_reconcile_at": None,
              "docs": {"indexed": 100, "partial": 1, "missing": 0,
                       "error": 0, "total": 101}}
    status.update(overrides)
    return status


def _p04_spawn(*, probe=None, status=None):
    probe = probe if probe is not None else \
        {"rowid": 7, "token": "hello", "hits": [7, 9]}
    return ScriptedSpawn(
        (lambda a: "--build" in a, lambda c: _Proc(0, '{"scanned": 200}')),
        (lambda a: a[-1].endswith("rc-p04-probe.py"),
         lambda c: _Proc(0, json.dumps(probe))),
        (lambda a: "--status" in a,
         lambda c: _Proc(0, json.dumps(status or _p04_status()))),
    )


def test_p04_builds_probes_the_sandbox_and_reads_the_prod_status(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    spawn = _p04_spawn()
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P04", ctx)
    assert result.status == runner.PASS, result.detail

    probe = ctx.sandbox_home / "rc-p04-probe.py"
    assert "rowids_matching" in probe.read_text(), \
        "the probe must query through the shipped read seam"
    build_home = next(c["env"]["HOME"] for c in spawn.calls
                      if "--build" in c["argv"])
    status_home = next(c["env"]["HOME"] for c in spawn.calls
                       if "--status" in c["argv"])
    assert build_home == str(ctx.sandbox_home), "the build stays sandboxed"
    assert status_home == os.environ["HOME"], \
        "the status read is the prod half"


@pytest.mark.parametrize("probe, status, symptom", [
    # the indexed doc does not come back out of a MATCH query
    ({"rowid": 7, "token": "hello", "hits": []}, None, "not searchable"),
    # holes in the prod index
    (None, _p04_status(docs={"indexed": 100, "partial": 0, "missing": 3,
                             "error": 0, "total": 103}), "not full"),
    # a prod index nobody has synced for days
    (None, _p04_status(built_at="2026-07-20T00:00:00+00:00",
                       last_sync_at="2026-07-20T00:00:00+00:00"), "stale"),
])
def test_p04_fails_on_unsearchable_hollow_or_stale_indexes(
        tmp_path, estate, monkeypatch, probe, status, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn",
                        _p04_spawn(probe=probe, status=status))
    result = run_phase("P04", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


# -- P05 ---------------------------------------------------------------- #


def _wire_report(*, results=({"id": "m1"},), read_env=None, garbage=None,
                 is_error=False, server_exit=0):
    """What the wire client prints after a session: the preserved frames
    plus the target it picked and the server's exit."""
    read_env = read_env if read_env is not None else {"ok": True, "id": "m1"}
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
            "serverInfo": {"name": "apple-mail"}, "capabilities": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "result": {
            "isError": False, "content": [{"type": "text", "text": json.dumps(
                {"ok": True, "fts": {"state": "ready"},
                 "results": list(results)})}]}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "result": {
            "isError": is_error, "content": [{"type": "text", "text":
                "boom" if is_error else json.dumps(read_env)}]}}),
    ]
    if garbage is not None:
        lines.insert(1, garbage)
    return json.dumps({"lines": lines, "target": "m1",
                       "server_exit": server_exit})


def _p05_spawn(report):
    # The route demands the server command as the client's final arg —
    # a client launched without it dies on sys.argv[1] before it can
    # report, so an argv regression must surface as an unscripted spawn.
    return ScriptedSpawn(
        (lambda a: a[-2].endswith("rc-p05-client.py")
         and a[-1].endswith("bin/email-mcp"),
         lambda c: _Proc(0, report)),
    )


def test_p05_wire_envelopes_come_back_through_a_real_client(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p05_spawn(_wire_report()))
    result = run_phase("P05", ctx)
    assert result.status == runner.PASS, result.detail

    client = (ctx.sandbox_home / "rc-p05-client.py").read_text()
    assert "initialize" in client and "tools/call" in client, \
        "the client the phase runs must speak the real protocol"
    assert any("clean JSON-RPC frame" in d for d in result.detail)


def test_p05_an_empty_store_still_answers_with_a_coded_envelope(
        tmp_path, estate, monkeypatch):
    """No results is not a failure of the wire claim — but the miss must
    come back coded, never as an exception."""
    report = _wire_report(results=(), read_env={
        "ok": False, "code": "invalid_input", "error": "unknown id"})
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p05_spawn(report))
    result = run_phase("P05", ctx)
    assert result.status == runner.PASS, result.detail


@pytest.mark.parametrize("report, symptom", [
    (_wire_report(garbage="Building index 42%..."), "non-JSON-RPC bytes"),
    (_wire_report(is_error=True), "exception reached the wire"),
    (_wire_report(server_exit=1), "server exited 1"),
])
def test_p05_fails_on_impure_stdout_or_a_wire_exception(
        tmp_path, estate, monkeypatch, report, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p05_spawn(report))
    result = run_phase("P05", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


# --------------------------------------------------------------------- #
# 6. the S2 bodies — P06/P07, P10-P12, the mutation story                #
# --------------------------------------------------------------------- #
#
# Same discipline as section 5: the dry run proves each body's complete
# command plan, and the live paths run against a scripted _spawn. The S2
# bodies speak MCP through one shared tool client, so most routes here
# read the calls file the body wrote and answer per tool — which also
# lets a test assert WHAT was asked (self-only recipients, TTL 0) and
# WHERE (the env's HOME is the lane).

_SB_ADDR = "rc-sandbox@example.org"  # the fixture's From: answer


def _session_route(respond):
    """Route the S2 tool client: read the calls file the body wrote and
    answer each call through respond(tool, arguments, call)."""
    def match(argv):
        return len(argv) >= 4 and argv[1].endswith("rc-s2-client.py")

    def reply(call):
        calls = json.loads(Path(call["argv"][-1]).read_text())
        out = [{"tool": c["tool"],
                "envelope": respond(c["tool"], c["arguments"], call),
                "rpc_error": None, "is_error": False}
               for c in calls]
        return _Proc(0, json.dumps({"calls": out, "server_exit": 0}))

    return (match, reply)


def _mints(ctx):
    path = ctx.sandbox_home / "rc-s2-mints.json"
    return json.loads(path.read_text()) if path.exists() else []


def _fast_polls(monkeypatch):
    """Timeout paths must not wait out live deadlines in a test."""
    monkeypatch.setattr(runner, "_P06_STORE_DEADLINE_S", 0.0)
    monkeypatch.setattr(runner, "_P07_DELIVER_DEADLINE_S", 0.0)


def test_s2_dry_run_plans_the_whole_mutation_story(tmp_path, estate):
    """A bare P06,P07,P10-P12 selection must print every planned effect
    and spawn nothing (the autouse fence would raise): the tool sessions
    with their call lists, the identity fence, the RC dispatcher plist,
    and the launchd bootstrap/bootout pair."""
    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"), "--phase",
         "P06,P07,P10-P12"],
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)
    text = sink.getvalue()

    assert code == runner.EXIT_OK
    for phase, title in (("P06", "send"),
                         ("P07", "schedule via launchd, then cancel"),
                         ("P10", "triage plans"), ("P11", "trash plan"),
                         ("P12", "audit inspection")):
        assert f"{phase} · {title}" in text and "dry-run" in text
    for intent in ("rc-s2-client.py",
                   "identities.toml", "sink.sh",
                   f"{runner._RC_DISPATCHER_LABEL}.plist",
                   "launchctl bootstrap", "launchctl bootout",
                   "p06-sandbox: tools/call send_email, send_email",
                   "p07-schedule: tools/call schedule_email, schedule_email",
                   "p07-cancel: tools/call cancel_scheduled",
                   "p10-plan: tools/call triage_plan",
                   "p11-plan: tools/call triage_plan_delete",
                   "p12-prod: tools/call audit"):
        assert intent in text, f"the dry run never planned: {intent}"
    assert "(0 phase(s) with no body yet)" in text, \
        "every S2 phase must be bound"


# -- P06 ---------------------------------------------------------------- #


def _prod_identities(estate, addr="operator@example.com"):
    (estate / "identities.toml").write_text(
        f'default = "main"\n\n[main]\nfrom_addr = "{addr}"\n'
        'driver = "pipe"\n')
    return addr


def _p06_respond(ctx, *, fence_armed=True, store_hit=True):
    """Scripted lanes for P06: the sandbox self-send appends to the
    delivery store, the fence probe refuses (or leaks, to test the
    check), the prod half sends and is found via search + metadata."""
    store = ctx.sandbox_home / "rc-outbox" / "delivered.mbox"

    def respond(tool, args, call):
        if tool == "send_email" and args["to"] == _SB_ADDR:
            assert call["env"].get("HOME") == str(ctx.sandbox_home), \
                "the sandbox send must run under the sandbox HOME"
            store.parent.mkdir(parents=True, exist_ok=True)
            with store.open("a") as fh:
                fh.write("Message-ID: <rc-p06-sb@rc>\n\nbody\n")
            return {"ok": True, "message_id": "<rc-p06-sb@rc>",
                    "to": [args["to"]], "cc": [], "bcc": [],
                    "subject": args["subject"], "attachments": [],
                    "bootstrapped": False}
        if tool == "send_email" and "blocked" in args["to"]:
            if fence_armed:
                return {"ok": False, "code": "recipient_not_allowed",
                        "error": "refused: not on the allowlist"}
            return {"ok": True, "message_id": "<leak@rc>",
                    "to": [args["to"]], "cc": [], "bcc": [],
                    "subject": args["subject"], "attachments": [],
                    "bootstrapped": False}
        if tool == "send_email":
            assert call["env"].get("HOME") == os.environ["HOME"], \
                "the prod send must run under the real HOME"
            return {"ok": True, "message_id": "<rc-p06-prod@rc>",
                    "to": [args["to"]], "cc": [], "bcc": [],
                    "subject": args["subject"], "attachments": [],
                    "bootstrapped": False}
        if tool == "refresh_mail":
            return {"ok": True, "new_messages": 1}
        if tool == "search_emails":
            return {"ok": True, "fts": {"state": "ready"},
                    "results": [{"id": "42"}] if store_hit else []}
        if tool == "get_emails_batch":
            return {"ok": True, "view": "metadata", "errors": [],
                    "emails": [{"ref": {"id": "42"},
                                "headers": {"Message-Id":
                                            "<rc-p06-prod@rc>"}}]}
        raise AssertionError(f"unscripted tool: {tool}")

    return respond


def test_p06_sends_self_only_on_both_lanes_and_finds_the_message_id(
        tmp_path, estate, monkeypatch):
    addr = _prod_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = ScriptedSpawn(_session_route(_p06_respond(ctx)))
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P06", ctx)
    assert result.status == runner.PASS, result.detail

    # The fence is the product's own declared guard plus a file store —
    # written before anything could send.
    ident = (ctx.sandbox_home / ".email-mcp" / "identities.toml").read_text()
    assert f'allowlist = ["{_SB_ADDR}"]' in ident
    assert "sink.sh" in ident, "delivery must be re-pointed at the sink"
    sink = ctx.sandbox_home / "rc-outbox" / "sink.sh"
    assert stat.S_IMODE(sink.stat().st_mode) == 0o700
    assert str(ctx.sandbox_home / "rc-outbox" / "delivered.mbox") \
        in sink.read_text()

    # The prod recipient came from the real identities file: self-only.
    prod_calls = json.loads(
        (ctx.sandbox_home / "rc-p06-prod.calls.json").read_text())
    assert prod_calls[0]["arguments"]["to"] == addr

    mints = _mints(ctx)
    assert [(m["lane"], m["event"], m["outcome"]) for m in mints] == [
        ("sandbox", "send", "sent"), ("sandbox", "send", "failed"),
        ("prod", "send", "sent")]
    assert mints[0]["message_id"] == "<rc-p06-sb@rc>"
    assert mints[2]["message_id"] == "<rc-p06-prod@rc>"


def test_p06_fails_when_the_send_fence_is_not_armed(tmp_path, estate,
                                                    monkeypatch):
    """A foreign recipient that does NOT come back recipient_not_allowed
    means a sandbox send could reach a real mailbox — the one outcome
    this lane exists to make impossible."""
    _prod_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p06_respond(ctx, fence_armed=False))))
    result = run_phase("P06", ctx)
    assert result.status == runner.FAIL
    assert any("fence is not armed" in d for d in result.detail)


def test_p06_fails_when_the_prod_store_never_shows_the_message_id(
        tmp_path, estate, monkeypatch):
    _prod_identities(estate)
    _fast_polls(monkeypatch)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p06_respond(ctx, store_hit=False))))
    result = run_phase("P06", ctx)
    assert result.status == runner.FAIL
    assert any("not found in the store" in d for d in result.detail)


# -- P07 ---------------------------------------------------------------- #


def _p07_spawn(ctx, *, deliver=True):
    """Scripted schedule/cancel plus a launchd stand-in: bootstrap plays
    the agent's RunAtLoad pass (pending → sent + bytes into the store),
    cancel moves its entry to cancelled/ with both files."""
    spool = ctx.sandbox_home / ".email-mcp" / "spool"
    store = ctx.sandbox_home / "rc-outbox" / "delivered.mbox"

    def respond(tool, args, call):
        if tool == "schedule_email":
            which = "deliver" if "deliver" in args["subject"] else "cancel"
            sid, mid = f"sched-{which}", f"<rc-p07-{which}@rc>"
            pending = spool / "pending"
            pending.mkdir(parents=True, exist_ok=True)
            (pending / f"{sid}.json").write_text(json.dumps(
                {"id": sid, "status": "pending", "message_id": mid,
                 "subject": args["subject"], "send_at": args["send_at"]}))
            (pending / f"{sid}.eml").write_text(f"Message-ID: {mid}\n\nx\n")
            return {"ok": True, "id": sid, "send_at": args["send_at"],
                    "message_id": mid, "executor": "launchd",
                    "subject": args["subject"]}
        if tool == "cancel_scheduled":
            sid = args["id"]
            src, dst = spool / "pending", spool / "cancelled"
            dst.mkdir(parents=True, exist_ok=True)
            manifest = json.loads((src / f"{sid}.json").read_text())
            manifest["status"] = "cancelled"
            (dst / f"{sid}.json").write_text(json.dumps(manifest))
            (src / f"{sid}.json").unlink()
            (src / f"{sid}.eml").rename(dst / f"{sid}.eml")
            return {"ok": True, "id": sid, "status": "cancelled",
                    "subject": manifest["subject"],
                    "was_due": manifest["send_at"]}
        raise AssertionError(f"unscripted tool: {tool}")

    def tick(call):
        if deliver:
            src, dst = spool / "pending", spool / "sent"
            dst.mkdir(parents=True, exist_ok=True)
            sid = "sched-deliver"
            if (src / f"{sid}.json").exists():
                manifest = json.loads((src / f"{sid}.json").read_text())
                manifest["status"] = "sent"
                (dst / f"{sid}.json").write_text(json.dumps(manifest))
                (src / f"{sid}.json").unlink()
                (src / f"{sid}.eml").rename(dst / f"{sid}.eml")
                with store.open("a") as fh:
                    fh.write(f"Message-ID: {manifest['message_id']}\n\nx\n")
        return _Proc(0)

    return ScriptedSpawn(
        _session_route(respond),
        (lambda a: a[:2] == ["launchctl", "bootstrap"], tick),
        (lambda a: a[:2] == ["launchctl", "bootout"], lambda c: _Proc(0)),
    )


def test_p07_delivers_via_the_rc_agent_and_cancels_the_second_entry(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = _p07_spawn(ctx)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P07", ctx)
    assert result.status == runner.PASS, result.detail

    # The agent is RC-owned and sandbox-pinned: the shipped label would
    # displace the operator's dispatcher, and an unpinned HOME would aim
    # the agent at the real estate.
    plist = (ctx.sandbox_home / "Library" / "LaunchAgents"
             / f"{runner._RC_DISPATCHER_LABEL}.plist").read_text()
    assert runner._RC_DISPATCHER_LABEL in plist
    assert "com.email-mcp.dispatcher<" not in plist
    assert f"<key>HOME</key><string>{ctx.sandbox_home}</string>" in plist
    assert "email_mcp.dispatcher" in plist

    launchctl = [c["argv"] for c in spawn.calls
                 if c["argv"][0] == "launchctl"]
    assert [a[1] for a in launchctl] == ["bootout", "bootstrap", "bootout"], \
        "pre-clean, bootstrap, and the final bootout — in that order"
    assert all(runner._RC_DISPATCHER_LABEL in " ".join(a) or
               str(ctx.sandbox_home) in " ".join(a) for a in launchctl), \
        "launchd actions must never name a prod label"

    spool = ctx.sandbox_home / ".email-mcp" / "spool"
    assert not list((spool / "pending").glob("*.json")), "an entry is armed"
    assert (spool / "cancelled" / "sched-cancel.eml").exists()
    assert [(m["event"], m["op"]) for m in _mints(ctx)] == [
        ("schedule", "sched-deliver"), ("schedule", "sched-cancel"),
        ("deliver", "sched-deliver"), ("cancel", "sched-cancel")]


def test_p07_times_out_loudly_and_still_boots_its_agent_out(
        tmp_path, estate, monkeypatch):
    """A dispatcher that never ticks is a failed phase — and the finally
    must still unload the RC agent, or a dead sandbox keeps a live
    launchd label ticking it."""
    _fast_polls(monkeypatch)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = _p07_spawn(ctx, deliver=False)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P07", ctx)
    assert result.status == runner.FAIL
    assert any("did not deliver" in d for d in result.detail)
    assert spawn.calls[-1]["argv"][:2] == ["launchctl", "bootout"], \
        "the agent must be booted out even on failure"


# -- P10 / P11 ---------------------------------------------------------- #


_BOXES = [
    {"account": "ACC", "name": "INBOX", "path": "u1", "total": 9,
     "unread": 1, "local_count": 7},
    {"account": "ACC", "name": "Archive", "path": "u2", "total": 3,
     "unread": 0, "local_count": 2},
    {"account": "ACC", "name": "Trash", "path": "u3", "total": 5,
     "unread": 0, "local_count": 5},
]


def _plan_respond(ctx, *, plan_tool, planned_mailbox="INBOX",
                  apply_env=None, batch_moved=False, finish_summary=True):
    """One scripted triage store for P10 and P11: the plan freezes two
    INBOX messages, apply refuses plan_expired, the batch readback shows
    them unmoved (or moved, to test the check), and the ledger carries
    plan_create + plan_finish(expired) with the summary."""
    summary = "rc plan summary line"

    def respond(tool, args, call):
        if tool == "list_mailboxes":
            return {"ok": True, "mailboxes": _BOXES}
        if tool == plan_tool:
            assert call["env"].get("EMAIL_MCP_TRIAGE_TTL") == "0", \
                "the plan must freeze already-expired (TTL 0)"
            assert args["account"] == "ACC" and args["mailbox"] == "INBOX"
            actions = [{"action": "flag", "color": 1},
                       {"action": "move_to", "mailbox": "Archive"}] \
                if plan_tool == "triage_plan" \
                else [{"action": "delete"}]
            return {"ok": True, "plan_id": "tp-1", "count": 2,
                    "expires_at": "2026-08-02T00:00:00+00:00",
                    "summary": summary, "actions": actions,
                    "messages": [
                        {"id": "11", "subject": "a", "from_addr": "x",
                         "date": "d", "mailbox": planned_mailbox,
                         "unread": True},
                        {"id": "12", "subject": "b", "from_addr": "y",
                         "date": "d", "mailbox": planned_mailbox,
                         "unread": False}]}
        if tool == "triage_apply":
            return apply_env or {
                "ok": False, "code": "plan_expired",
                "error": "plan tp-1 expired", "operation_id": "tp-1"}
        if tool == "get_emails_batch":
            assert args["ids"] == ["11", "12"]
            return {"ok": True, "view": "minimal", "errors": [],
                    "emails": [
                        {"id": "11", "mailbox":
                         "Archive" if batch_moved else planned_mailbox,
                         "unread": True},
                        {"id": "12", "mailbox": planned_mailbox,
                         "unread": False}]}
        if tool == "audit":
            assert args["plan_id"] == "tp-1"
            return {"ok": True, "files_scanned": 1, "skipped_lines": 0,
                    "events": [
                        {"event": "plan_finish", "outcome": "expired",
                         "op": "tp-1",
                         "summary": summary if finish_summary else "other"},
                        {"event": "plan_create", "outcome": "created",
                         "op": "tp-1", "summary": summary}]}
        raise AssertionError(f"unscripted tool: {tool}")

    return respond


@pytest.mark.parametrize("pid, plan_tool", [
    ("P10", "triage_plan"), ("P11", "triage_plan_delete")])
def test_p10_p11_freeze_a_plan_and_prove_the_refusal_path(
        tmp_path, estate, monkeypatch, pid, plan_tool):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_plan_respond(ctx, plan_tool=plan_tool))))
    result = run_phase(pid, ctx)
    assert result.status == runner.PASS, result.detail
    assert [(m["event"], m["outcome"], m["op"]) for m in _mints(ctx)] == [
        ("plan_create", "created", "tp-1"),
        ("plan_finish", "expired", "tp-1")]
    assert any("summary" in d for d in result.detail)


@pytest.mark.parametrize("breakage, symptom", [
    # apply "succeeded": a mutation happened against a read-only store
    ({"apply_env": {"ok": True, "status": "applied"}}, "refusal path"),
    # apply refused with the wrong code
    ({"apply_env": {"ok": False, "code": "plan_claimed", "error": "x",
                    "operation_id": "tp-1"}}, "refusal path"),
    # the store shows a planned message in the move target
    ({"batch_moved": True}, "moved mail"),
    # plan_finish lost the summary line that must outlive GC
    ({"finish_summary": False}, "summary"),
])
def test_p10_fails_when_apply_mutates_or_the_ledger_drops_the_summary(
        tmp_path, estate, monkeypatch, breakage, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(_session_route(
        _plan_respond(ctx, plan_tool="triage_plan", **breakage))))
    result = run_phase("P10", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


def test_p11_fails_when_the_delete_plan_selects_trash_residents(
        tmp_path, estate, monkeypatch):
    """The delete selection excludes Trash by design (a fumbled
    parameter must never reach it) — a Trash-resident planned message
    means that exclusion failed."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(_session_route(
        _plan_respond(ctx, plan_tool="triage_plan_delete",
                      planned_mailbox="Trash"))))
    result = run_phase("P11", ctx)
    assert result.status == runner.FAIL
    assert any("trash-resident" in d for d in result.detail), result.detail


# -- P12 ---------------------------------------------------------------- #


def _seed_mints(ctx, records):
    ctx.sandbox_home.mkdir(parents=True, exist_ok=True)
    (ctx.sandbox_home / "rc-s2-mints.json").write_text(json.dumps(records))


def _mint(lane, event, outcome, op=None, message_id=None,
          ts="2026-08-02T10:00:00Z"):
    return {"ts": ts, "lane": lane, "event": event, "outcome": outcome,
            "op": op, "message_id": message_id}


_S2_MINTS = [
    _mint("sandbox", "send", "sent", message_id="<sb@rc>"),
    _mint("sandbox", "send", "failed"),
    _mint("sandbox", "schedule", "scheduled", op="sched-1",
          message_id="<s1@rc>"),
    _mint("sandbox", "deliver", "sent", op="sched-1", message_id="<s1@rc>"),
    _mint("sandbox", "cancel", "cancelled", op="sched-2"),
    _mint("prod", "send", "sent", message_id="<pr@rc>"),
]


def _ledger_event(event, outcome, op="auto-op", message_id=None, **extra):
    e = {"v": 1, "ts": "2026-08-02T10:01:00Z", "op": op, "src": "server",
         "event": event, "outcome": outcome}
    if message_id:
        e["message_id"] = message_id
    e.update(extra)
    return e


def _sandbox_ledger():
    return [
        _ledger_event("send", "sent", message_id="<sb@rc>"),
        _ledger_event("send", "failed"),
        _ledger_event("schedule", "scheduled", op="sched-1",
                      message_id="<s1@rc>"),
        _ledger_event("deliver", "sent", op="sched-1",
                      message_id="<s1@rc>", src="dispatcher"),
        _ledger_event("cancel", "cancelled", op="sched-2"),
        # estate events are the lifecycle story, not a mail mutation —
        # P12 must not count them against the bijection
        _ledger_event("lifecycle", "setup"),
    ]


def _p12_respond(ctx, sandbox_events, prod_events, seen=None):
    def respond(tool, args, call):
        assert tool == "audit"
        if seen is not None:
            seen.append(args)
        events = sandbox_events \
            if call["env"].get("HOME") == str(ctx.sandbox_home) \
            else prod_events
        return {"ok": True, "events": events, "files_scanned": 1,
                "skipped_lines": 0}

    return respond


def test_p12_proves_one_event_per_mutation_threaded_by_operation_id(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    _seed_mints(ctx, _S2_MINTS)
    prod = [
        _ledger_event("send", "sent", message_id="<pr@rc>"),
        # the live estate keeps living during the pass — not our claim
        _ledger_event("send", "sent", message_id="<operator-noise@x>"),
    ]
    seen = []
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(_session_route(
        _p12_respond(ctx, _sandbox_ledger(), prod, seen))))
    result = run_phase("P12", ctx)
    assert result.status == runner.PASS, result.detail
    assert all(a == {"since": "2026-08-02T10:00:00+00:00", "limit": 500}
               for a in seen), "one audit call per lane, run-start bounded"
    assert len(seen) == 2
    assert any("1 unrelated" in d for d in result.detail)


@pytest.mark.parametrize("mutate, symptom", [
    # the deliver event never made the ledger
    (lambda sb, pr: sb.remove(sb[3]), "want exactly 1"),
    # a second event for one mutation (double emit)
    (lambda sb, pr: pr.append(_ledger_event("send", "sent",
                                            message_id="<pr@rc>")),
     "want exactly 1"),
    # an extra mail-mutation event nobody performed
    (lambda sb, pr: sb.append(_ledger_event("plan_create", "created",
                                            op="ghost")),
     "mutation event(s)"),
    # an event that lost its operation id cannot be threaded (the
    # failed send: its mint pins no op, so only this check can see it)
    (lambda sb, pr: sb[1].update(op=""), "no operation id"),
])
def test_p12_fails_on_missing_extra_or_unthreaded_events(
        tmp_path, estate, monkeypatch, mutate, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    _seed_mints(ctx, _S2_MINTS)
    sandbox = _sandbox_ledger()
    prod = [_ledger_event("send", "sent", message_id="<pr@rc>")]
    mutate(sandbox, prod)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(_session_route(
        _p12_respond(ctx, sandbox, prod))))
    result = run_phase("P12", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


def test_p12_refuses_to_pass_with_nothing_recorded(tmp_path, estate,
                                                   monkeypatch):
    """No expectations file means no earlier mutations — P12 must name
    the ordering mistake, never bless an unexamined ledger."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(_session_route(
        _p12_respond(ctx, [], []))))
    result = run_phase("P12", ctx)
    assert result.status == runner.FAIL
    assert any("no mutation expectations recorded" in d
               for d in result.detail)


# --------------------------------------------------------------------- #
# 7. the S3 bodies — P08/P09/P14/P17/P19, the prod lane                   #
# --------------------------------------------------------------------- #
#
# Same discipline again: the dry run proves each body's complete command
# plan, the live paths run against a scripted _spawn, and the estate the
# bodies read (the REAL identities file, the REAL LaunchAgents dir) is
# the tmp_path stand-in — $HOME is redirected for every test here.

def _graph_identities(estate, *, drafts=True, bare=True):
    """The prod identities file the S3 bodies read: a CERN identity on
    the graph executor (optionally with the drafts lane) plus a plain
    gmail identity with no lane — the refusal half's subject."""
    text = ('default = "cern"\n\n[cern]\n'
            'from_addr = "operator@cern.ch"\n'
            'driver = "ssh_sendmail"\nexecutor = "graph"\n')
    if drafts:
        text += 'drafts = "graph"\n'
    if bare:
        text += ('\n[gmail]\nfrom_addr = "operator@gmail.com"\n'
                 'driver = "smtp"\n')
    (estate / "identities.toml").write_text(text)


def test_s3_dry_run_plans_the_whole_prod_story(tmp_path, estate):
    """A bare P08,P09,P14,P17,P19 selection must print every planned
    effect and spawn nothing (the autouse fence would raise): the tool
    sessions, the two tenant probes, doctor on both sides of the revoke,
    and the launchd re-bootstrap — with every manual phase PENDING, not
    invented."""
    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"),
         "--phase", "P08,P09,P14,P17,P19"],
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)
    text = sink.getvalue()

    assert code == runner.EXIT_OK
    for phase, title in (("P08", "schedule via graph"),
                         ("P09", "lid-closed delivery"),
                         ("P14", "permission revoke / regrant"),
                         ("P17", "teardown"), ("P19", "drafts")):
        assert f"{phase} · {title}" in text
    for intent in ("p08-schedule: tools/call schedule_email",
                   "rc-p08-status.py",
                   "p08-cancel: tools/call cancel_scheduled",
                   "p09-schedule: tools/call schedule_email",
                   "bin/email-mcp doctor", "bin/email-mcp --doctor",
                   "launchctl bootout gui/", "launchctl bootstrap gui/",
                   "launchctl print gui/",
                   "p19-draft: tools/call create_draft, create_draft",
                   "rc-p19-readback.py"):
        assert intent in text, f"the dry run never planned: {intent}"
    assert text.count("MANUAL — PENDING") == 3, \
        "P09, P14 and P19 must stay PENDING on a dry run"
    assert "(0 phase(s) with no body yet)" in text, \
        "every S3 phase must be bound"


def test_a_bound_manual_body_stages_and_returns_the_gates_verdict(
        tmp_path, estate):
    """The S3 seam: a manual phase with a body runs the body, which ends
    at its own ctx.manual gate — so an unattended pass still ends
    PENDING, never an invented PASS from the body having merely run."""
    spec = runner.PhaseSpec("P09", "lid-closed", runner.PROD, "delivers",
                            manual=True)
    staged = []

    def body(ctx):
        staged.append(True)
        ctx.note("armed the send")
        return ctx.manual(spec)

    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=None)
    result = runner.execute(spec, ctx, {"P09": body})
    assert staged == [True], "the bound body must run"
    assert result.status == runner.PENDING

    ctx = make_ctx(tmp_path, estate, dry_run=False,
                   answer=lambda p: "pass: lid shut, delivered on time")
    result = runner.execute(spec, ctx, {"P09": body})
    assert result.status == runner.PASS
    assert any("lid shut" in d for d in result.detail)


# -- P08 ---------------------------------------------------------------- #


def _p08_respond(ctx, *, executor="graph", draft_id="AAMkAD-1"):
    def respond(tool, args, call):
        if tool == "schedule_email":
            assert call["env"].get("HOME") == os.environ["HOME"], \
                "the prod schedule must run under the real HOME"
            return {"ok": True, "id": "sched-p08", "send_at": args["send_at"],
                    "message_id": "<rc-p08@rc>", "executor": executor,
                    "graph_draft_id": draft_id if executor == "graph"
                    else None, "subject": args["subject"]}
        if tool == "cancel_scheduled":
            return {"ok": True, "id": args["id"], "status": "cancelled",
                    "subject": "rc-p08", "was_due": args.get("send_at", "t")}
        raise AssertionError(f"unscripted tool: {tool}")

    return respond


def _p08_probe_route(statuses):
    """The tenant-status probe answers a scripted sequence: what Exchange
    holds before the cancel, then after it."""
    feed = iter(statuses)
    return (lambda a: len(a) > 1 and a[1].endswith("rc-p08-status.py"),
            lambda c: _Proc(0, json.dumps({"status": next(feed)})))


def test_p08_arms_a_deferred_draft_and_cancels_it_cleanly(
        tmp_path, estate, monkeypatch):
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    spawn = ScriptedSpawn(_session_route(_p08_respond(ctx)),
                          _p08_probe_route(["held", "cancelled_externally"]))
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P08", ctx)
    assert result.status == runner.PASS, result.detail

    # Self-only by construction: the recipient and the identity both come
    # from the real identities file, never from the phase.
    sched = json.loads(
        (ctx.sandbox_home / "rc-p08-schedule.calls.json").read_text())
    assert sched[0]["arguments"]["to"] == "operator@cern.ch"
    assert sched[0]["arguments"]["from_identity"] == "cern"

    # The probe reads the TENANT through the shipped seam, twice: held
    # before the cancel, gone-unsent after it.
    probes = [c["argv"] for c in spawn.calls
              if len(c["argv"]) > 1 and c["argv"][1].endswith("rc-p08-status.py")]
    assert len(probes) == 2
    assert all(p[-3:] == ["cern", "AAMkAD-1", "<rc-p08@rc>"] for p in probes)
    assert "draft_status" in (ctx.sandbox_home / "rc-p08-status.py").read_text()
    assert [(m["lane"], m["event"], m["op"]) for m in _mints(ctx)] == [
        ("prod", "schedule", "sched-p08"), ("prod", "cancel", "sched-p08")]


def test_p08_fails_and_disarms_when_the_schedule_falls_back_to_launchd(
        tmp_path, estate, monkeypatch):
    """The silent graph→launchd fallback is a kept promise for a user and
    a failed phase for the RC — and the failed phase must not leave a
    real send pending on a one-hour fuse."""
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p08_respond(ctx, executor="launchd"))))
    result = run_phase("P08", ctx)
    assert result.status == runner.FAIL
    assert any("fell back" in d for d in result.detail), result.detail
    cancel = json.loads(
        (ctx.sandbox_home / "rc-p08-cancel.calls.json").read_text())
    assert cancel[0]["arguments"]["id"] == "sched-p08", \
        "the fallback entry must be disarmed before the phase fails"


@pytest.mark.parametrize("statuses, symptom", [
    # /send raced past the deferred property: the tenant is not holding
    (["sent", "sent"], "does not hold"),
    # the cancel envelope said cancelled but Exchange still holds it
    (["held", "held"], "not removed cleanly"),
])
def test_p08_fails_when_the_tenant_disagrees_with_the_envelopes(
        tmp_path, estate, monkeypatch, statuses, symptom):
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p08_respond(ctx)), _p08_probe_route(statuses)))
    result = run_phase("P08", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail
    # Judged AFTER the cancel: even the failing paths leave nothing armed.
    assert (ctx.sandbox_home / "rc-p08-cancel.calls.json").exists()


def test_p08_fails_when_the_estate_has_no_graph_identity(
        tmp_path, estate, monkeypatch):
    (estate / "identities.toml").write_text(
        'default = "main"\n\n[main]\nfrom_addr = "op@example.org"\n'
        'driver = "pipe"\n')
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    result = run_phase("P08", ctx)
    assert result.status == runner.FAIL
    assert any("Graph schedule lane needs one" in d for d in result.detail)


# -- P09 ---------------------------------------------------------------- #


def _p09_respond(ctx, *, executor="graph"):
    def respond(tool, args, call):
        assert tool == "schedule_email"
        return {"ok": True, "id": "sched-p09", "send_at": args["send_at"],
                "message_id": "<rc-p09@rc>", "executor": executor,
                "graph_draft_id": "AAMkAD-9", "subject": args["subject"]}

    return respond


def test_p09_unattended_arms_nothing_and_stays_pending(tmp_path, estate):
    """Scheduling a real deferred send with nobody at the lid would burn
    the evidence window on every unattended pass — the body must record
    PENDING without spawning anything (the autouse fence would raise)."""
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=None)
    result = run_phase("P09", ctx)
    assert result.status == runner.PENDING
    assert _mints(ctx) == [], "nothing may be armed without an operator"


def test_p09_attended_arms_the_send_and_the_prompt_carries_the_steps(
        tmp_path, estate, monkeypatch):
    _graph_identities(estate)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "pass: delivered 03:12:00Z, lid down 03:02"

    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=answer)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p09_respond(ctx))))
    result = run_phase("P09", ctx)
    assert result.status == runner.PASS, result.detail
    assert any("delivered 03:12:00Z" in d for d in result.detail)

    sched = json.loads(
        (ctx.sandbox_home / "rc-p09-schedule.calls.json").read_text())
    assert sched[0]["arguments"]["to"] == "operator@cern.ch", "self-only"
    # The exact steps ride the manual prompt: the entry id, the lid, the
    # send time the human must compare the Date header against.
    assert len(prompts) == 1
    assert "sched-p09" in prompts[0]
    assert "CLOSE THE LID" in prompts[0]
    assert "calibration 2026-07-29" in prompts[0]
    assert [(m["lane"], m["event"], m["op"]) for m in _mints(ctx)] == [
        ("prod", "schedule", "sched-p09")]


def test_p09_fails_before_the_human_when_the_entry_falls_back(
        tmp_path, estate, monkeypatch):
    """A launchd entry cannot deliver with the lid closed — asking the
    human to close it anyway would waste the window on a doomed run."""
    _graph_identities(estate)
    prompts = []
    ctx = make_ctx(tmp_path, estate, dry_run=False,
                   answer=lambda p: prompts.append(p) or "pass")
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p09_respond(ctx, executor="launchd"))))
    result = run_phase("P09", ctx)
    assert result.status == runner.FAIL
    assert any("cannot deliver" in d for d in result.detail), result.detail
    assert prompts == [], "the lid gate must not open for a doomed entry"


# -- P14 ---------------------------------------------------------------- #


def _p14_spawn(*, baseline_rc=0, revoked_rc=1,
               revoked_out="FAIL mail_store: cannot read\n",
               revoked_report=None, regrant_rc=0):
    """Scripted doctor for the revoke round trip, answered in call order:
    baseline (green), revoked (red + the JSON behind the fix check),
    regranted (green again)."""
    plain = iter([
        _Proc(baseline_rc, "ok  mail_store: 42 messages\n"
              if baseline_rc == 0 else "FAIL mail_store: dark\n"),
        _Proc(revoked_rc, revoked_out),
        _Proc(regrant_rc, "ok  mail_store: 42 messages\n"
              if regrant_rc == 0 else "FAIL mail_store: still dark\n"),
    ])
    report = revoked_report if revoked_report is not None else {
        "ok": False, "audit": {"ok": True},
        "checks": {"mail_store": {
            "ok": False, "detail": "cannot read the Envelope Index",
            "fix": "System Settings → Privacy & Security → Full Disk "
                   "Access, then restart it."}}}
    return ScriptedSpawn(
        (lambda a: a[-1] == "doctor", lambda c: next(plain)),
        (lambda a: a[-1] == "--doctor",
         lambda c: _Proc(1, json.dumps(report))),
    )


def test_p14_walks_the_revoke_regrant_round_trip(tmp_path, estate,
                                                 monkeypatch):
    answers = iter(["pass: toggled off", "pass: toggled back on"])
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=answer)
    spawn = _p14_spawn()
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P14", ctx)
    assert result.status == runner.PASS, result.detail

    # Two gates, each with the pane spelled out; doctor ran three times
    # (baseline, revoked, regranted) plus the JSON read.
    assert len(prompts) == 2
    assert all("Full Disk Access" in p for p in prompts)
    doctors = [c["argv"][-1] for c in spawn.calls]
    assert doctors == ["doctor", "doctor", "--doctor", "doctor"]
    assert any("Full Disk Access" in d for d in result.detail)


def test_p14_requires_a_green_baseline_before_blaming_the_revoke(
        tmp_path, estate, monkeypatch):
    """A red estate before the revoke would let P14 blame Full Disk
    Access for someone else's failure."""
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=lambda p: "pass")
    monkeypatch.setattr(runner, "_spawn", _p14_spawn(baseline_rc=1))
    result = run_phase("P14", ctx)
    assert result.status == runner.FAIL
    assert any("before the revoke" in d for d in result.detail)


@pytest.mark.parametrize("breakage, symptom", [
    # doctor stayed green: the human toggled the wrong binary's grant
    ({"revoked_rc": 0, "revoked_out": "ok\n"}, "did not bite"),
    # a traceback is a crash, not a report (the acceptance's own words)
    ({"revoked_out": "Traceback (most recent call last):\n  boom\n"},
     "crashed instead of reporting"),
    # the failure reports but names no remedy
    ({"revoked_report": {"ok": False, "audit": {"ok": True},
                         "checks": {"mail_store": {
                             "ok": False, "detail": "cannot read"}}}},
     "name no fix"),
    # the regrant did not restore the estate
    ({"regrant_rc": 1}, "not green after the regrant"),
])
def test_p14_fails_on_each_broken_side_of_the_round_trip(
        tmp_path, estate, monkeypatch, breakage, symptom):
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=lambda p: "pass")
    monkeypatch.setattr(runner, "_spawn", _p14_spawn(**breakage))
    result = run_phase("P14", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


def test_p14_stops_at_the_gate_when_the_operator_cannot_revoke(
        tmp_path, estate, monkeypatch):
    """A refused first gate must end the phase with the operator's word —
    doctor's revoked side never runs against a still-granted estate."""
    ctx = make_ctx(tmp_path, estate, dry_run=False,
                   answer=lambda p: "fail: Settings pane is locked")
    spawn = _p14_spawn()
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P14", ctx)
    assert result.status == runner.FAIL
    assert any("Settings pane is locked" in d for d in result.detail)
    assert [c["argv"][-1] for c in spawn.calls] == ["doctor"], \
        "only the baseline may run before the revoke gate"


# -- P17 ---------------------------------------------------------------- #


def _prod_plists(fake_home, *labels):
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in labels:
        (agents / f"{label}.plist").write_text(f"<plist>{label}</plist>")
    return agents


def _p17_spawn(*, runs="1", exit_code="0"):
    return ScriptedSpawn(
        (lambda a: a[:2] == ["launchctl", "bootout"], lambda c: _Proc(0)),
        (lambda a: a[:2] == ["launchctl", "bootstrap"], lambda c: _Proc(0)),
        (lambda a: a[:2] == ["launchctl", "print"],
         lambda c: _Proc(0, "state = waiting\npath = /a/d.plist\n"
                            f"\truns = {runs}\n"
                            f"\tlast exit code = {exit_code}\n")),
    )


def test_p17_rebootstraps_the_existing_plists_and_sees_the_tick(
        tmp_path, estate, fake_home, monkeypatch):
    agents = _prod_plists(fake_home, "com.email-mcp.dispatcher",
                          "com.email-mcp.fts")
    monkeypatch.setattr(runner, "_P17_TICK_DEADLINE_S", 0.0)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    spawn = _p17_spawn()
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P17", ctx)
    assert result.status == runner.PASS, result.detail

    launchctl = [c["argv"] for c in spawn.calls]
    assert [a[1] for a in launchctl] == ["bootout", "bootstrap",
                                         "bootout", "bootstrap", "print"], \
        "bootout then bootstrap per agent, then the tick watch"
    # The EXISTING plists, not a re-render: bootstrap names the files on
    # disk, and the legacy v0.9 label is never touched.
    boots = [a[-1] for a in launchctl if a[1] == "bootstrap"]
    assert boots == [str(agents / "com.email-mcp.dispatcher.plist"),
                     str(agents / "com.email-mcp.fts.plist")]
    assert not any("com.paris" in " ".join(a) for a in launchctl)
    assert any("ticked" in d for d in result.detail)


def test_p17_fails_when_the_dispatcher_never_completes_a_run(
        tmp_path, estate, fake_home, monkeypatch):
    _prod_plists(fake_home, "com.email-mcp.dispatcher", "com.email-mcp.fts")
    monkeypatch.setattr(runner, "_P17_TICK_DEADLINE_S", 0.0)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p17_spawn(runs="0"))
    result = run_phase("P17", ctx)
    assert result.status == runner.FAIL
    assert any("no completed run within" in d for d in result.detail)


def test_p17_fails_without_a_dispatcher_plist_but_tolerates_no_fts(
        tmp_path, estate, fake_home, monkeypatch):
    """The dispatcher is the phase's whole claim; the FTS agent is a
    setup option an estate may legitimately lack."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    result = run_phase("P17", ctx)
    assert result.status == runner.FAIL
    assert any("no prod dispatcher to re-bootstrap" in d
               for d in result.detail)

    _prod_plists(fake_home, "com.email-mcp.dispatcher")
    monkeypatch.setattr(runner, "_P17_TICK_DEADLINE_S", 0.0)
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn", _p17_spawn())
    result = run_phase("P17", ctx)
    assert result.status == runner.PASS, result.detail
    assert any("com.email-mcp.fts: not installed" in d
               for d in result.detail)


# -- P19 ---------------------------------------------------------------- #


_P19_READBACK = {"is_draft": True, "internet_message_id": "<rc-p19@rc>",
                 "in_drafts_folder": True,
                 "text_parts": [["text/html", "base64"],
                                ["text/plain", "base64"]]}


def _p19_respond(ctx, *, refusal_code="draft_unsupported",
                 refusal_ok=False):
    def respond(tool, args, call):
        assert tool == "create_draft"
        if args["from_identity"] == "cern":
            return {"ok": True, "draft_id": "AAMkAD-9",
                    "message_id": "<rc-p19@rc>", "to": [args["to"]],
                    "cc": [], "subject": args["subject"],
                    "folder": "drafts", "account": "operator@cern.ch"}
        if refusal_ok:
            return {"ok": True, "draft_id": "leak",
                    "message_id": "<leak@rc>", "to": [args["to"]],
                    "cc": [], "subject": args["subject"],
                    "folder": "drafts", "account": "x"}
        return {"ok": False, "code": refusal_code,
                "error": "create_draft is not available for identity "
                         "[gmail]: drafts require drafts = \"graph\" ... "
                         "~/.email-mcp/identities.toml."}

    return respond


def _p19_probe_route(readback):
    return (lambda a: len(a) > 1 and a[1].endswith("rc-p19-readback.py"),
            lambda c: _Proc(0, json.dumps(readback)))


def test_p19_files_a_verified_base64_draft_and_hands_it_to_the_human(
        tmp_path, estate, monkeypatch):
    _graph_identities(estate)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "pass: OWA round-trip clean, sent copy intact"

    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=answer)
    spawn = ScriptedSpawn(_session_route(_p19_respond(ctx)),
                          _p19_probe_route(_P19_READBACK))
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P19", ctx)
    assert result.status == runner.PASS, result.detail
    assert any("OWA round-trip clean" in d for d in result.detail)

    # Both halves went through one session: the filing under the drafts
    # identity, the refusal probe under the lane-less one.
    calls = json.loads(
        (ctx.sandbox_home / "rc-p19-draft.calls.json").read_text())
    assert [c["arguments"]["from_identity"] for c in calls] == ["cern",
                                                                "gmail"]
    assert all(c["arguments"]["to"] == "operator@cern.ch" for c in calls)
    # The independent readback ran through the shipped seams.
    probes = [c["argv"] for c in spawn.calls
              if len(c["argv"]) > 1
              and c["argv"][1].endswith("rc-p19-readback.py")]
    assert len(probes) == 1 and probes[0][-2:] == ["cern", "AAMkAD-9"]
    probe_text = (ctx.sandbox_home / "rc-p19-readback.py").read_text()
    assert "draft_receipt" in probe_text and "$value" in probe_text
    # The human half's prompt names the client editor, not this tool.
    assert len(prompts) == 1 and "OWA" in prompts[0]
    assert "rc-p19 draft" in prompts[0]


def test_p19_unattended_still_proves_the_auto_half_but_stays_pending(
        tmp_path, estate, monkeypatch):
    """A draft never transmits, so the unattended pass may earn the
    automated evidence — but the rendering verdict is the human's, and
    without one the phase is PENDING, keeping the run INCOMPLETE."""
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=None)
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p19_respond(ctx)), _p19_probe_route(_P19_READBACK)))
    result = run_phase("P19", ctx)
    assert result.status == runner.PENDING
    assert (ctx.sandbox_home / "rc-p19-draft.calls.json").exists()
    assert any("base64 text parts" in d for d in result.detail)


@pytest.mark.parametrize("readback, symptom", [
    # a leg down: the draft is not in the Drafts folder
    (dict(_P19_READBACK, in_drafts_folder=False), "three-legged readback"),
    # a leg down: the stored message is not ours
    (dict(_P19_READBACK, internet_message_id="<other@x>"),
     "three-legged readback"),
    # stored QP: exactly what OWA's editor mangles (round 1, 2026-08-02)
    (dict(_P19_READBACK, text_parts=[["text/html", "base64"],
                                     ["text/plain", "quoted-printable"]]),
     "not base64"),
])
def test_p19_fails_when_the_tenant_readback_breaks_a_leg(
        tmp_path, estate, monkeypatch, readback, symptom):
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=lambda p: "pass")
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p19_respond(ctx)), _p19_probe_route(readback)))
    result = run_phase("P19", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


@pytest.mark.parametrize("breakage, symptom", [
    # the lane-less identity filed a draft: the no-fallback rule broke
    ({"refusal_ok": True}, "draft_unsupported"),
    # refused, but with the wrong code
    ({"refusal_code": "invalid_input"}, "draft_unsupported"),
])
def test_p19_fails_when_the_lane_less_identity_does_not_refuse(
        tmp_path, estate, monkeypatch, breakage, symptom):
    _graph_identities(estate)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=lambda p: "pass")
    monkeypatch.setattr(runner, "_spawn", ScriptedSpawn(
        _session_route(_p19_respond(ctx, **breakage)),
        _p19_probe_route(_P19_READBACK)))
    result = run_phase("P19", ctx)
    assert result.status == runner.FAIL
    assert any(symptom in d for d in result.detail), result.detail


def test_p19_names_the_missing_identity_shape(tmp_path, estate):
    """The refusal half needs a lane-less identity on the REAL estate;
    an estate without one is a finding with a name, not a silent pass."""
    _graph_identities(estate, bare=False)
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=lambda p: "pass")
    result = run_phase("P19", ctx)
    assert result.status == runner.FAIL
    assert any("refusal half needs one" in d for d in result.detail)


# --------------------------------------------------------------------- #
# 8. the S4 bodies — P13, P15, P16, P18: failure matrix + closure        #
# --------------------------------------------------------------------- #
#
# Same discipline once more, with one addition: P13's ten injections
# only mean something against a COHERENT estate, so its scripted spawn
# is a small world — the fake dispatcher obeys whatever sink the body
# armed, the ledger accrues events exactly where the real one would
# (and stays silent exactly where a kill would silence it), and doctor
# renders from the same mutable state. The negative tests then flip one
# world rule at a time: a reconciler that backfills FM3's lost event, a
# recovery that delivers twice, a fuse that misses the build.


def test_s4_dry_run_plans_the_failure_and_lifecycle_story(tmp_path, estate):
    """A bare P13,P15,P16,P18 selection must print every planned effect
    and spawn nothing (the autouse fence would raise): the kill fuses,
    the FM tool sessions, the chmod + doctor --fix pair, the v0.9
    worktree walk, both uninstall invocations — with P18 PENDING, never
    invented."""
    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"),
         "--phase", "P13,P15,P16,P18"],
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)
    text = sink.getvalue()

    assert code == runner.EXIT_OK
    for phase, title in (("P13", "failure matrix FM1-FM10"),
                         ("P15", "upgrade from v0.9"),
                         ("P16", "uninstall + purge"),
                         ("P18", "fresh macOS user account walk")):
        assert f"{phase} · {title}" in text
    for intent in ("kill -9", "fts --status --json",
                   "fts --rebuild --limit 200",
                   "p13-fm4-good: tools/call schedule_email",
                   "p13-fm4-wire: tools/call cancel_scheduled, "
                   "list_scheduled",
                   "p13-fm7-apply: tools/call triage_apply, triage_apply, "
                   "audit",
                   "chmod 000", "doctor --fix",
                   "p13-fm9: tools/call create_draft",
                   "p13-fm10: tools/call search_emails, triage_plan",
                   "p13-fm3: tools/call schedule_email",
                   f"launchctl print gui/{os.getuid()}/"
                   f"{runner._LEGACY_LABEL}",
                   "worktree add --detach", runner._V09_REF,
                   "rc-p15-v09-schedule.py", "email-mcp update",
                   "-m email_mcp.dispatcher",
                   "uninstall --yes", "uninstall --purge --yes",
                   "rc-sandbox.token.json"):
        assert intent in text, f"the dry run never planned: {intent}"
    assert "MANUAL — PENDING" in text, "P18 stays a human's verdict"
    assert "(0 phase(s) with no body yet)" in text, \
        "every planned phase must be bound after S4"


# -- P13 ---------------------------------------------------------------- #


class _P13World:
    """One mutable sandbox estate behind every P13 route.

    The fake dispatcher reads the sink the body armed and behaves the
    way the real one would under it: a kill-before-cat sink strands the
    claim with no bytes and no event; a cat-then-kill sink delivers the
    bytes, strands the claim, and emits NOTHING — the silence FM3's
    whole demonstration rests on. Doctor and audit render from the same
    files, so the body's cross-surface requirements are tested against
    one estate rather than per-call canned answers."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.root = ctx.sandbox_home / ".email-mcp"
        self.spool = self.root / "spool"
        self.store = ctx.sandbox_home / "rc-outbox" / "delivered.mbox"
        self.month = self.root / "audit" / "2026-08.jsonl"
        self.fts_built_at = "2026-08-02T00:00:00+00:00"  # P04 built it
        self.applies = 0
        # the mutants the negative tests flip
        self.reconcile_fm3 = False
        self.double_deliver = False
        self.fuse_misses = False
        for leaf in ("spool/pending", "spool/sending", "spool/sent",
                     "plans", "graph", "fts", "audit"):
            (self.root / leaf).mkdir(parents=True, exist_ok=True)
        (self.root / "fts" / "fts.db").write_text("rc-fake-sqlite")
        self.month.write_text("")
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text("")

    # -- shared state ------------------------------------------------- #

    def _fts_state(self):
        db = self.root / "fts" / "fts.db"
        if not db.exists():
            return "absent"
        return "ready" if db.read_text().startswith("rc-fake-sqlite") \
            else "error"

    def emit(self, event, outcome, op, message_id=None):
        line = {"v": 1, "ts": "2026-08-02T12:00:00+00:00", "op": op,
                "event": event, "outcome": outcome}
        if message_id:
            line["message_id"] = message_id
        with self.month.open("a") as fh:
            fh.write(json.dumps(line) + "\n")

    def _append_store(self, mid):
        with self.store.open("a") as fh:
            fh.write(f"Message-ID: {mid}\n\nx\n")

    # -- routes ------------------------------------------------------- #

    def kill_build(self, call):
        if self.fuse_misses:
            return _Proc(0, "rc=0\n")
        (self.root / "fts" / "fts.db").write_text("rc-fake-sqlite")
        self.fts_built_at = None
        return _Proc(0, "rc=137\n")

    def fts_status(self, call):
        return _Proc(0, json.dumps({
            "state": self._fts_state(), "built_at": self.fts_built_at,
            "docs": {"indexed": 200, "partial": 0, "missing": 0,
                     "error": 0, "total": 200}}))

    def fts_rebuild(self, call):
        (self.root / "fts" / "fts.db").write_text("rc-fake-sqlite")
        self.fts_built_at = "2026-08-02T12:34:56+00:00"
        return _Proc(0, "{}")

    def dispatch(self, call):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        sink = (self.ctx.sandbox_home / "rc-outbox"
                / "sink.sh").read_text()
        kills, accepts = "kill -9" in sink, "/bin/cat" in sink
        for mf in sorted((self.spool / "sending").glob("*.json")):
            entry = json.loads(mf.read_text())
            nxt = entry.get("next_attempt_at")
            if not nxt or datetime.fromisoformat(nxt) \
                    > now - timedelta(minutes=10):
                continue
            entry["next_attempt_at"] = None
            (self.spool / "pending" / mf.name).write_text(
                json.dumps(entry))
            mf.unlink()
            eml = mf.with_suffix(".eml")
            if eml.exists():
                eml.rename(self.spool / "pending" / eml.name)
            self.emit("recover", "requeued", entry["id"])
        for mf in sorted((self.spool / "pending").glob("*.json")):
            try:
                entry = json.loads(mf.read_text())
            except ValueError:
                continue  # the corrupt manifest, skipped like entries()
            if datetime.fromisoformat(entry["send_at"]) > now:
                continue
            mf.rename(self.spool / "sending" / mf.name)
            eml = self.spool / "pending" / f"{entry['id']}.eml"
            if eml.exists():
                eml.rename(self.spool / "sending" / eml.name)
            if accepts:
                self._append_store(entry["message_id"])
            if kills:
                if accepts and self.reconcile_fm3:
                    # the D11 mutant: a "helpful" reconciler backfills
                    # the deliver event the kill lost
                    self.emit("deliver", "sent", entry["id"],
                              entry["message_id"])
                return _Proc(-9, "", "Killed: 9")
            if self.double_deliver:
                self._append_store(entry["message_id"])
            for name in (f"{entry['id']}.json", f"{entry['id']}.eml"):
                src = self.spool / "sending" / name
                if src.exists():
                    src.rename(self.spool / "sent" / name)
            entry["status"] = "sent"
            (self.spool / "sent" / f"{entry['id']}.json").write_text(
                json.dumps(entry))
            self.emit("deliver", "sent", entry["id"],
                      entry["message_id"])
        return _Proc(0, "{}")

    def doctor(self, call):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        stranded = []
        try:
            claims = sorted((self.spool / "sending").glob("*.json"))
        except OSError:
            claims = []
        for mf in claims:
            try:
                entry = json.loads(mf.read_text())
            except (ValueError, OSError):
                continue
            nxt = entry.get("next_attempt_at")
            if nxt and datetime.fromisoformat(nxt) \
                    <= now - timedelta(minutes=10):
                stranded.append(entry["id"])
        modes_ok = all(
            stat.S_IMODE(os.lstat(d).st_mode) == 0o700
            for d in (self.spool, self.root / "plans"))
        sp = {"ok": modes_ok and not stranded,
              "stranded_sending": stranded}
        fixes = []
        if not modes_ok:
            fixes.append(f"chmod 700 {self.spool}; "
                         f"chmod 700 {self.root / 'plans'}")
        if stranded:
            fixes.append("python -m email_mcp.dispatcher   # one pass "
                         "recovers stranded claims")
        if fixes:
            sp["fix"] = "; ".join(fixes)
        state = self._fts_state()
        fts_check = {"ok": state != "error",
                     "status": {"state": state,
                                "built_at": self.fts_built_at}}
        if state == "error":
            fts_check["fix"] = "python -m email_mcp.fts --rebuild"
        return _Proc(0, json.dumps({
            "ok": True, "audit": {"ok": True},
            "checks": {"spool_plans": sp, "fts": fts_check}}))

    def doctor_fix(self, call):
        assert call["stdin"] == "doctor_fix\n", \
            "doctor --fix must be fed its typed confirmation"
        for d in (self.spool, self.root / "plans"):
            os.chmod(d, 0o700)
        return _Proc(0, "repaired\n")

    def chmod(self, call):
        for p in call["argv"][2:]:
            os.chmod(p, int(call["argv"][1], 8))
        return _Proc(0)

    def audit_env(self, args):
        events, skipped = [], 0
        for line in self.month.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            wanted = args.get("operation_id") or args.get("plan_id")
            if wanted and event.get("op") != wanted:
                continue
            events.append(event)
        return {"ok": True, "events": list(reversed(events)),
                "files_scanned": 1, "skipped_lines": skipped}

    def tool(self, tool, args, call):
        mail = call["env"].get("EMAIL_MCP_MAIL_DIR", "")
        if mail.endswith("rc-p13-fm10-no-store"):
            return {"ok": False, "code": "mail_unavailable",
                    "error": "cannot read the store",
                    "fix": "run doctor"}
        if tool == "schedule_email":
            key = args["subject"].split()[1]      # fm4-good / fm2 / fm3
            sid, mid = f"sched-{key}", f"<rc-p13-{key}@rc>"
            (self.spool / "pending" / f"{sid}.json").write_text(
                json.dumps({"id": sid, "status": "pending",
                            "send_at": args["send_at"],
                            "message_id": mid,
                            "subject": args["subject"],
                            "next_attempt_at": None}))
            (self.spool / "pending" / f"{sid}.eml").write_text("x")
            self.emit("schedule", "scheduled", sid, mid)
            return {"ok": True, "id": sid, "message_id": mid,
                    "executor": "launchd", "send_at": args["send_at"],
                    "subject": args["subject"]}
        if tool == "cancel_scheduled":
            path = self.spool / "pending" / f"{args['id']}.json"
            try:
                json.loads(path.read_text())
            except ValueError:
                return {"ok": False, "code": "invalid_input",
                        "error": "manifest unreadable",
                        "fix": "run doctor"}
            raise AssertionError("P13 only cancels the corrupt entry")
        if tool == "list_scheduled":
            pend = []
            for mf in sorted((self.spool / "pending").glob("*.json")):
                try:
                    pend.append(json.loads(mf.read_text())["id"])
                except ValueError:
                    continue
            return {"ok": True, "dispatcher_installed": False,
                    "pending": pend}
        if tool == "search_emails":
            return {"ok": True, "results": [],
                    "fts": {"state": self._fts_state()}}
        if tool == "list_mailboxes":
            return {"ok": True, "mailboxes": _BOXES}
        if tool == "triage_plan":
            assert call["env"].get("EMAIL_MCP_TRIAGE_TTL") == "0", \
                "FM7's plan must freeze already-expired"
            (self.root / "plans" / "tp-13.json").write_text("{}")
            self.emit("plan_create", "created", "tp-13")
            return {"ok": True, "plan_id": "tp-13", "count": 2,
                    "summary": "rc p13 plan",
                    "actions": args["actions"],
                    "messages": [{"id": "11", "mailbox": "INBOX"}]}
        if tool == "triage_apply":
            self.applies += 1
            if self.applies == 1:
                self.emit("plan_finish", "expired", "tp-13")
            return {"ok": False, "code": "plan_expired",
                    "error": "plan tp-13 expired",
                    "operation_id": "tp-13"}
        if tool == "create_draft":
            return {"ok": False, "code": "transport_unavailable",
                    "error": "[rcgraph/graph] no token cache at "
                             f"{self.root / 'graph'}/rcgraph.token.json "
                             "— run: python -m email_mcp.graph --login "
                             "rcgraph"}
        if tool == "audit":
            return self.audit_env(args)
        raise AssertionError(f"unscripted tool: {tool}")


def _p13_spawn(world):
    return ScriptedSpawn(
        (lambda a: a[0] == "/bin/sh" and "kill -9" in a[2],
         world.kill_build),
        (lambda a: "fts" in a and "--status" in a, world.fts_status),
        (lambda a: "fts" in a and "--rebuild" in a, world.fts_rebuild),
        (lambda a: a[-1].endswith("rc-p13-fm1-probe.py"),
         lambda c: _Proc(0, json.dumps({"rowid": 7, "token": "hello",
                                        "hits": [7]}))),
        _session_route(world.tool),
        (lambda a: a[1:] == ["-m", "email_mcp.dispatcher"],
         world.dispatch),
        (lambda a: a[-1] == "--doctor", world.doctor),
        (lambda a: a[-2:] == ["doctor", "--fix"], world.doctor_fix),
        (lambda a: a[0] == "chmod", world.chmod),
    )


def test_p13_all_ten_injections_pass_and_leave_the_fm3_exhibit(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    world = _P13World(ctx)
    monkeypatch.setattr(runner, "_spawn", _p13_spawn(world))
    result = run_phase("P13", ctx)
    assert result.status == runner.PASS, result.detail

    # every FM left its own evidence line
    for fm in (f"FM{n}" for n in range(1, 11)):
        assert any(d.startswith(f"{fm}:") for d in result.detail), \
            f"{fm} left no evidence"

    # the FM3 exhibit: delivered bytes, unsettled claim, silent ledger
    spool = ctx.sandbox_home / ".email-mcp" / "spool"
    assert (spool / "sending" / "sched-fm3.json").exists()
    assert not (spool / "sent" / "sched-fm3.json").exists()
    store = (ctx.sandbox_home / "rc-outbox" / "delivered.mbox").read_text()
    assert store.count("<rc-p13-fm3@rc>") == 1, "the delivery was real"
    assert not any('"deliver"' in ln and "sched-fm3" in ln
                   for ln in world.month.read_text().splitlines()), \
        "no deliver event may exist for the FM3 entry"

    # FM2 delivered exactly once; FM4's corrupt sibling parked untouched
    assert store.count("<rc-p13-fm2@rc>") == 1
    assert (spool / "pending" / "sched-fm4-bad.json").read_text() \
        == '{"id": "torn mid-write'

    # the ledger-worthy mutations were recorded for a later P12 rerun
    assert [(m["event"], m["op"]) for m in _mints(ctx)] == [
        ("schedule", "sched-fm4-good"), ("schedule", "sched-fm4-bad"),
        ("deliver", "sched-fm4-good"),
        ("schedule", "sched-fm2"), ("recover", "sched-fm2"),
        ("deliver", "sched-fm2"),
        ("plan_create", "tp-13"), ("plan_finish", "tp-13"),
        ("schedule", "sched-fm3"),
    ]


def test_p13_fails_when_the_fm3_window_is_reconciled_away(
        tmp_path, estate, monkeypatch):
    """The D11 test: a reconciler that backfills the deliver event the
    kill lost would make the ledger claim exactly-once — FM3 must fail
    rather than bless it."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    world = _P13World(ctx)
    world.reconcile_fm3 = True
    monkeypatch.setattr(runner, "_spawn", _p13_spawn(world))
    result = run_phase("P13", ctx)
    assert result.status == runner.FAIL
    assert any("reconciled away" in d for d in result.detail), \
        result.detail


def test_p13_fails_on_a_double_send_after_recovery(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    world = _P13World(ctx)
    world.double_deliver = True
    monkeypatch.setattr(runner, "_spawn", _p13_spawn(world))
    result = run_phase("P13", ctx)
    assert result.status == runner.FAIL
    assert any("double send" in d for d in result.detail), result.detail


def test_p13_fails_when_the_fuse_misses_the_build(
        tmp_path, estate, monkeypatch):
    """A build that finished before the kill demonstrates nothing — FM1
    must refuse to count it."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    world = _P13World(ctx)
    world.fuse_misses = True
    monkeypatch.setattr(runner, "_spawn", _p13_spawn(world))
    result = run_phase("P13", ctx)
    assert result.status == runner.FAIL
    assert any("did not interrupt" in d for d in result.detail), \
        result.detail


# -- P15 ---------------------------------------------------------------- #


def _p15_spawn(ctx, *, version="0.9.0", update_rc=0, legacy_loaded=False):
    """Scripted v0.9 upgrade: the pre-gate reads the legacy label in
    the real domain (loaded only when a test says so), the generator
    writes the old-shape spool entry under the v0.9 home (asserting it
    ran with the worktree on PYTHONPATH), update stamps meta, doctor
    --fix retires the legacy plist and tightens v0.9's 0755 spool
    subdirs, the dispatcher delivers the frozen entry."""
    v09home = ctx.sandbox_home / "rc-v09-home"
    v09root = v09home / ".email-mcp"
    worktree = ctx.sandbox_home / "rc-v09-src"
    store = v09home / "rc-outbox" / "delivered.mbox"

    def generate(call):
        assert call["env"]["HOME"] == str(v09home), \
            "the v0.9 generator must run against the v0.9 home"
        assert call["env"]["PYTHONPATH"] == str(worktree), \
            "the generator must import the worktree's bytes"
        pending = v09root / "spool" / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        os.chmod(pending, 0o755)          # v0.9's umask-mode subdirs
        (pending / "v09-1.json").write_text(json.dumps(
            {"id": "v09-1", "status": "pending",
             "send_at": call["argv"][-1], "message_id": "<rc-p15@rc>",
             "subject": call["argv"][-2]}))
        (pending / "v09-1.eml").write_text("Message-ID: <rc-p15@rc>\n\nx")
        return _Proc(0, json.dumps({"version": version, "id": "v09-1",
                                    "message_id": "<rc-p15@rc>"}))

    def update(call):
        assert call["stdin"] == "update\n", \
            "update must be fed its typed confirmation"
        if update_rc:
            return _Proc(update_rc, "", "migration exploded")
        v09root.mkdir(parents=True, exist_ok=True)
        (v09root / "meta.json").write_text('{"state_version": 1}')
        return _Proc(0, "migrated\n")

    def fix(call):
        assert call["stdin"] == "doctor_fix\n"
        (v09home / "Library" / "LaunchAgents"
         / "com.paris.email-mcp-dispatcher.plist").unlink(missing_ok=True)
        os.chmod(v09root / "spool" / "pending", 0o700)
        return _Proc(0, "fixed\n")

    def dispatch(call):
        assert call["env"]["HOME"] == str(v09home)
        assert call["env"].get("PYTHONPATH") != str(worktree), \
            "the CURRENT wheel must deliver, not the v0.9 bytes"
        pending = v09root / "spool" / "pending"
        sent = v09root / "spool" / "sent"
        sent.mkdir(parents=True, exist_ok=True)
        entry = json.loads((pending / "v09-1.json").read_text())
        entry["status"] = "sent"
        (sent / "v09-1.json").write_text(json.dumps(entry))
        (pending / "v09-1.json").unlink()
        store.parent.mkdir(parents=True, exist_ok=True)
        with store.open("a") as fh:
            fh.write("Message-ID: <rc-p15@rc>\n\nx\n")
        return _Proc(0, "{}")

    return ScriptedSpawn(
        (lambda a: a[:2] == ["launchctl", "print"],
         lambda c: _Proc(0, "state = running\n") if legacy_loaded
         else _Proc(113, "", "Could not find service")),
        (lambda a: a[:3] == ["git", "cat-file", "-e"],
         lambda c: _Proc(0)),
        (lambda a: a[:3] == ["git", "worktree", "remove"],
         lambda c: _Proc(0)),
        (lambda a: a[:3] == ["git", "worktree", "prune"],
         lambda c: _Proc(0)),
        (lambda a: a[:3] == ["git", "worktree", "add"],
         lambda c: _Proc(0)),
        (lambda a: len(a) > 1 and a[1].endswith("rc-p15-v09-schedule.py"),
         generate),
        (lambda a: a[-1] == "update", update),
        (lambda a: a[-2:] == ["doctor", "--fix"], fix),
        (lambda a: a[1:] == ["-m", "email_mcp.dispatcher"], dispatch),
    )


def test_p15_lets_the_v09_bytes_write_and_the_wheel_migrate(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = _p15_spawn(ctx)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P15", ctx)
    assert result.status == runner.PASS, result.detail

    # the worktree pinned the exact v0.9 ref, and no launchd was touched
    add = next(c["argv"] for c in spawn.calls
               if c["argv"][:3] == ["git", "worktree", "add"])
    assert add[-1] == runner._V09_REF and "--detach" in add
    launchctl = [c["argv"] for c in spawn.calls
                 if c["argv"][0] == "launchctl"]
    assert launchctl == [["launchctl", "print",
                          f"gui/{os.getuid()}/{runner._LEGACY_LABEL}"]], \
        "P15 may only READ the shared label space — the pre-gate — " \
        "never load or unload it"

    # the v0.9 config the body wrote: pipe transport, self-only
    v09root = ctx.sandbox_home / "rc-v09-home" / ".email-mcp"
    ident = (v09root / "identities.toml").read_text()
    assert 'driver = "pipe"' in ident and _SB_ADDR in ident
    # the legacy plist is gone and the frozen entry delivered
    assert not (ctx.sandbox_home / "rc-v09-home" / "Library"
                / "LaunchAgents"
                / "com.paris.email-mcp-dispatcher.plist").exists()
    assert (v09root / "spool" / "sent" / "v09-1.json").exists()
    assert any("__version__ 0.9.0" in d for d in result.detail)


def test_p15_fails_when_the_current_bytes_answer(
        tmp_path, estate, monkeypatch):
    """The 'not a fixture' rule has teeth: a generator that ran the
    installed package instead of the worktree fakes the whole upgrade."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    monkeypatch.setattr(runner, "_spawn",
                        _p15_spawn(ctx, version="0.11.0"))
    result = run_phase("P15", ctx)
    assert result.status == runner.FAIL
    assert any("old code did not write the state" in d
               for d in result.detail), result.detail


def test_p15_unregisters_the_worktree_even_when_the_upgrade_fails(
        tmp_path, estate, monkeypatch):
    """A stale worktree registration would refuse the next pass's add —
    the finally must remove it on the failure path too."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = _p15_spawn(ctx, update_rc=1)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P15", ctx)
    assert result.status == runner.FAIL
    assert any("`email-mcp update` exited 1" in d for d in result.detail)
    git_removes = [i for i, c in enumerate(spawn.calls)
                   if c["argv"][:3] == ["git", "worktree", "remove"]]
    update_at = next(i for i, c in enumerate(spawn.calls)
                     if c["argv"][-1] == "update")
    assert git_removes and git_removes[-1] > update_at, \
        "the worktree must be removed after the failing step"


def test_p15_refuses_while_the_legacy_label_is_loaded(
        tmp_path, estate, monkeypatch):
    """doctor --fix's legacy_launchd repair boots the legacy label out
    of the REAL per-user domain — launchd ignores the fake HOME — so a
    loaded label is an operator's live pre-v0.8 agent. The gate must
    stop the phase before any other effect (P16's shape: refuse before
    the verb, don't witness the damage after), which makes the passing
    run's residual bootout provably a no-op."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    ctx.repo_root = _REPO
    spawn = _p15_spawn(ctx, legacy_loaded=True)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P15", ctx)
    assert result.status == runner.FAIL
    assert any("loaded in the real per-user launchd domain" in d
               for d in result.detail), result.detail
    assert [c["argv"][:2] for c in spawn.calls] == [["launchctl",
                                                     "print"]], \
        "the refusal must precede every other effect"


# -- P16 ---------------------------------------------------------------- #


def _p16_spawn(ctx, *, really_purge=True):
    root = ctx.sandbox_home / ".email-mcp"
    logs = ctx.sandbox_home / "Library" / "Logs"

    def sweep(call):
        (root / "graph" / "rc-sandbox.token.json").unlink(missing_ok=True)
        return _Proc(0, "state kept — pass --purge to remove\n")

    def purge(call):
        if really_purge:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
            if logs.is_dir():
                for p in logs.glob("email-mcp*.log*"):
                    p.unlink()
        return _Proc(0, "removed\n")

    return ScriptedSpawn(
        (lambda a: a[-2:] == ["uninstall", "--yes"], sweep),
        (lambda a: a[-3:] == ["uninstall", "--purge", "--yes"], purge),
    )


def test_p16_sweeps_the_cache_keeps_the_tree_then_purges_it(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    spawn = _p16_spawn(ctx)
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P16", ctx)
    assert result.status == runner.PASS, result.detail

    assert not (ctx.sandbox_home / ".email-mcp").exists()
    # both invocations ran under the sandbox HOME, in order
    homes = [c["env"].get("HOME") for c in spawn.calls]
    assert homes == [str(ctx.sandbox_home)] * 2
    assert spawn.calls[0]["argv"][-2:] == ["uninstall", "--yes"]
    assert spawn.calls[1]["argv"][-3:] == ["uninstall", "--purge",
                                           "--yes"]
    assert any("strict sentinel" in d for d in result.detail), \
        "the byte-identical claim belongs to the runner's strict verify"


def test_p16_fails_when_the_purge_leaves_the_tree(
        tmp_path, estate, monkeypatch):
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    monkeypatch.setattr(runner, "_spawn",
                        _p16_spawn(ctx, really_purge=False))
    result = run_phase("P16", ctx)
    assert result.status == runner.FAIL
    assert any("survived the purge" in d for d in result.detail), \
        result.detail


def test_p16_refuses_to_run_uninstall_over_a_product_plist(
        tmp_path, estate, monkeypatch):
    """uninstall boots out every product plist it finds, and those
    labels live in the SHARED per-user domain — a product plist in the
    sandbox would aim the bootout at the operator's real agents, so the
    body must stop before the verb ever runs."""
    ctx = make_ctx(tmp_path, estate, dry_run=False)
    agents = ctx.sandbox_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.email-mcp.dispatcher.plist").write_text("<plist/>")
    spawn = ScriptedSpawn()
    monkeypatch.setattr(runner, "_spawn", spawn)
    result = run_phase("P16", ctx)
    assert result.status == runner.FAIL
    assert any("displace the operator" in d for d in result.detail), \
        result.detail
    assert spawn.calls == [], \
        "no uninstall may run over the shared label space"


# -- P18 ---------------------------------------------------------------- #


def test_p18_unattended_stays_pending(tmp_path, estate):
    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=None)
    result = run_phase("P18", ctx)
    assert result.status == runner.PENDING


def test_p18_prompt_carries_the_walk_protocol_and_takes_the_verdict(
        tmp_path, estate):
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return "pass: 11 min, only the FDA pane needed explaining"

    ctx = make_ctx(tmp_path, estate, dry_run=False, answer=answer)
    result = run_phase("P18", ctx)
    assert result.status == runner.PASS
    assert any("11 min" in d for d in result.detail)
    assert len(prompts) == 1
    for token in ("STOPWATCH", "pipx", "15 minutes", "archaeology",
                  "ONCE ever", "search_emails"):
        assert token in prompts[0], f"the protocol lost: {token}"


# --------------------------------------------------------------------- #
# 9. S5 — the programme end to end                                       #
# --------------------------------------------------------------------- #
#
# S5 binds no body: it proves the assembled programme. The full dry run
# must reach all nineteen phases and leave the machine untouched, and
# the Sentinel — the thing every pass trusts first, pointed at the REAL
# ~/.email-mcp — must witness the estate without writing a byte to it.


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Every byte, mode and mtime under root, taken WITHOUT the Sentinel
    — an instrument may not certify its own read-only claim."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = (
            stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns,
            p.read_bytes() if p.is_file() else b"")
    return out


def test_sentinel_witnesses_the_estate_without_modifying_it(estate):
    """Read-only by construction: capture + verify (normal and strict)
    change nothing under the witnessed root. The safety of aiming the
    Sentinel at the operator's real state tree rests on exactly this,
    so it is asserted against an independent fingerprint rather than
    trusted from the code's shape."""
    watcher = runner.Sentinel(estate, probe=_agents())
    before = _fingerprint(estate)
    baseline = watcher.capture()
    assert watcher.verify(baseline).clean
    assert watcher.verify(baseline, strict=True).clean
    assert watcher.capture().files == baseline.files, \
        "two reads of an untouched estate must manifest identically"
    assert _fingerprint(estate) == before, \
        "the witness wrote to the estate it only reads"


def test_the_full_dry_run_reaches_every_phase_and_writes_nothing(
        tmp_path, estate):
    """The S5 gate: the shipped registry, all nineteen phases, one bare
    invocation. Every phase must be reached (a FAIL stops the walk, so
    nineteen sections IS the no-failure claim), none unbound, exit 0 —
    and the machine untouched, --report included: the path takes effect
    only under --execute."""
    before = _fingerprint(estate)
    report = tmp_path / "scratch" / "rc-dry.md"
    sink = io.StringIO()
    code = runner.main(
        ["--state-dir", str(tmp_path / "rcstate"),
         "--report", str(report)],
        sentinel=runner.Sentinel(estate, probe=_agents()), sink=sink)
    out = sink.getvalue()

    assert code == runner.EXIT_OK
    for spec in runner.PLAN:
        assert f"### {spec.id} · {spec.title}" in out, \
            f"{spec.id} was never reached"
    assert "15 dry · 4 manual-pending" in out
    assert "(0 phase(s) with no body yet)" in out
    assert not report.exists(), "--report leaked into a dry run"
    assert _fingerprint(estate) == before, \
        "the dry run modified the estate the Sentinel witnesses"
