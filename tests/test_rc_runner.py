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


def test_the_shipped_plan_is_the_eighteen_phases_of_the_w3_document():
    assert [s.id for s in runner.PLAN] == [f"P{n:02d}" for n in range(1, 19)]
    assert [s.id for s in runner.PLAN if s.manual] == ["P09", "P14", "P18"]
    assert [s.id for s in runner.PLAN if s.once] == ["P18"]
    assert [s.id for s in runner.PLAN if s.sentinel_strict] == ["P16"]
    assert all(s.lane in (runner.SANDBOX, runner.PROD, runner.BOTH)
               for s in runner.PLAN)
    assert all(s.acceptance for s in runner.PLAN)
    # R2 attaches bodies stage by stage; S1 bound the sandbox core.
    assert set(runner.IMPLEMENTATIONS) == {"P01", "P02", "P03", "P04", "P05"}


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
