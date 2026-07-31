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
        ctx.env({"EMAIL_MCP_SPOOL_DIR": str(estate / "spool")})

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


def test_the_shipped_plan_is_the_eighteen_phases_of_the_w3_document():
    assert [s.id for s in runner.PLAN] == [f"P{n:02d}" for n in range(1, 19)]
    assert [s.id for s in runner.PLAN if s.manual] == ["P09", "P14", "P18"]
    assert [s.id for s in runner.PLAN if s.once] == ["P18"]
    assert [s.id for s in runner.PLAN if s.sentinel_strict] == ["P16"]
    assert all(s.lane in (runner.SANDBOX, runner.PROD, runner.BOTH)
               for s in runner.PLAN)
    assert all(s.acceptance for s in runner.PLAN)
    # R1 ships the core; the bodies are R2's to attach.
    assert runner.IMPLEMENTATIONS == {}


def test_the_runner_and_the_plan_document_agree():
    """docs/w3-rc-plan.md is the plan of record. The registry is a
    mirror of it, and a mirror that drifts is worse than no mirror —
    an operator reading the doc would be told the wrong lane."""
    doc = _RUNNER_PATH.parent.parent / "docs" / "w3-rc-plan.md"
    rows = re.findall(r"^\| \*\*(P\d\d)\*\* \| ([^|]+) \| ([^|]+) \| ([^|]+) \|",
                      doc.read_text(encoding="utf-8"), flags=re.M)
    assert len(rows) == 18, "the phase table must list every phase once"
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
