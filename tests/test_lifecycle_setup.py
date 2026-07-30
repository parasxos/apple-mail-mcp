"""`email-mcp setup` (email_mcp.lifecycle, setup half) — v0.11 L3.

Every test runs inside a fake $HOME with every EMAIL_MCP_* override
wiped (then EMAIL_MCP_MAIL_DIR pointed at the in-tmp mail fixture), so
the DEFAULT ~/.email-mcp resolution is exercised end-to-end while the
real state tree stays untouchable — the test_repairs.py pattern. The
conftest fts/audit guards are shadowed by name for the same reason: these
tests need the real env-driven resolvers, inside the fake HOME. A
PATH-shimmed launchctl/osascript/ssh/security/op set fences every test
from the real launchd, Mail.app, SSH and secret stores.

The wizard's hard promises proven here: 0700 tree + 0600 files, the FTS
dir never created, identities backed up mode-preserved BEFORE clobber,
the ^[A-Za-z0-9_-]{1,64}$ name fence (graph/<name>.token.json traversal),
secret VALUES never written anywhere, the mcpServers JSON carrying an
absolute entry point, and exactly ONE `lifecycle` audit event with names
only — no addresses.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from email_mcp import audit, cli, dispatcher, doctor, lifecycle

FIXTURE = Path(__file__).parent / "fixtures" / "setup_answers.json"

SPOOL_STATES = ("pending", "sending", "sent", "failed", "cancelled")


@pytest.fixture
def home(tmp_path, monkeypatch, mail_fixture):
    """Fake $HOME, EMAIL_MCP_* wiped, mail store pointed at the in-tmp
    fixture so check_mail_store is honestly green."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    yield h
    audit.set_process("server")  # run_setup tags "cli"; restore for the suite


@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name, on purpose): the env pin would
    point the FTS dir OUTSIDE the fake HOME and defeat the
    setup-never-creates-it assertion. The fake HOME is the isolation."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason): the lifecycle event must
    land through the real env-driven resolver, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def tool_shims(home, tmp_path, monkeypatch):
    """PATH shims: stateful launchctl (test_repairs pattern), osascript
    answering green for both permission probes, ssh/security/op failing
    fast so doctor's transport healthchecks never leave the sandbox."""
    shim = tmp_path / "shim"
    state = tmp_path / "launchd-state"
    shim.mkdir()
    state.mkdir()
    log = tmp_path / "launchctl.log"
    log.write_text("")
    script = shim / "launchctl"
    script.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "$@" >> "$LAUNCHCTL_LOG"
        d="$LAUNCHCTL_STATE_DIR"
        case "$1" in
          bootstrap) b=$(basename "$3" .plist); : > "$d/$b"; exit 0 ;;
          bootout)
            if [ -n "$3" ]; then b=$(basename "$3" .plist)
            else b=$(basename "$2"); fi
            rm -f "$d/$b"; exit 0 ;;
          print) b=$(basename "$2"); [ -e "$d/$b" ] && exit 0 || exit 1 ;;
        esac
        exit 0
    """))
    script.chmod(0o755)
    # `get name` (automation) checks the exit code; `UI elements enabled`
    # (accessibility) additionally wants "true" on stdout.
    (shim / "osascript").write_text("#!/bin/sh\necho true\nexit 0\n")
    (shim / "osascript").chmod(0o755)
    for tool in ("ssh", "security", "op"):
        (shim / tool).write_text("#!/bin/sh\nexit 1\n")
        (shim / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("LAUNCHCTL_STATE_DIR", str(state))
    return log


def _lifecycle_events() -> list[dict]:
    return audit.query(event="lifecycle")["events"]


def _scan_tree_for(home: Path, needle: bytes) -> list[Path]:
    return [p for p in home.rglob("*")
            if p.is_file() and needle in p.read_bytes()]


def _write_answers(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(data))
    return path


# --------------------------------------------------------------------- #
# the scripted full run                                                  #
# --------------------------------------------------------------------- #


def test_scripted_setup_completes_all_eight_steps(home, capsys):
    """--answers FILE through the real CLI wiring: all eight steps run,
    exit 0, meta.json records state_version 1."""
    assert cli.main(["setup", "--answers", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    for i, step in enumerate(
            ("preflight", "permissions", "state_dirs", "identity",
             "launchd", "fts", "client_config", "smoke"), 1):
        assert f"[{i}/8] {step}" in out
    assert "setup complete" in out
    meta = json.loads((home / ".email-mcp" / "meta.json").read_text())
    assert meta["state_version"] == lifecycle.STATE_VERSION == 1
    assert meta["created_at"] and meta["updated_at"]
    assert "EMAIL_MCP_MAIL_DIR" in meta["env_overrides"]


def test_state_tree_0700_meta_0600_and_fts_never_created(home):
    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    root = home / ".email-mcp"
    for d in (root, root / "spool",
              *(root / "spool" / s for s in SPOOL_STATES),
              root / "plans", root / "graph", root / "audit"):
        assert d.is_dir(), d
        assert (d.stat().st_mode & 0o777) == 0o700, d
    meta = root / "meta.json"
    assert (meta.stat().st_mode & 0o777) == 0o600
    assert not list(root.glob("meta.json.*"))  # tmp renamed away
    # The FTS index is derived state: setup with fts_choice=defer must
    # not create even its directory — --build is the only builder.
    assert not (root / "fts").exists()


# --------------------------------------------------------------------- #
# identity: 0600, no secrets, backup-first, name fence                   #
# --------------------------------------------------------------------- #


def test_identities_written_0600_with_keychain_reference_only(home, capsys):
    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    path = home / ".email-mcp" / "identities.toml"
    assert (path.stat().st_mode & 0o777) == 0o600
    import tomllib
    data = tomllib.loads(path.read_text())
    assert data["default"] == "work"
    assert set(k for k, v in data.items() if isinstance(v, dict)) == {
        "work", "gmail"}
    assert data["gmail"]["keychain"] == "email-mcp-gmail"
    assert "password" not in json.dumps(data).lower()
    # The wizard prints the exact command the USER runs to store the app
    # password — it never reads or stores the value itself.
    out = capsys.readouterr().out
    assert ("security add-generic-password -s email-mcp-gmail "
            "-a paris.gmail@example.org -w") in out


def test_existing_identities_backed_up_before_clobber(home):
    root = home / ".email-mcp"
    root.mkdir()
    original = textwrap.dedent("""\
        default = "old"

        [old]
        from_addr = "old@example.org"
        driver = "ssh_sendmail"
        host = "h.example.org"
        user = "u"
        socket = "/tmp/sock"
    """)
    path = root / "identities.toml"
    path.write_text(original)
    path.chmod(0o640)  # distinctive mode: the backup must preserve it

    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0

    backups = list(root.glob("identities.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original  # pre-clobber bytes, intact
    assert (backups[0].stat().st_mode & 0o777) == 0o640  # mode preserved
    assert (path.stat().st_mode & 0o777) == 0o600
    import tomllib
    data = tomllib.loads(path.read_text())
    assert set(k for k, v in data.items() if isinstance(v, dict)) == {
        "old", "work", "gmail"}  # merged, nothing lost
    assert data["default"] == "work"


def test_identity_name_traversal_rejected_nothing_touched(home, tmp_path):
    """`../evil` would become graph/../evil.token.json — the name regex
    is a path fence. Validation runs BEFORE the backup, so a refused
    write leaves the existing file byte-identical and .bak-less."""
    root = home / ".email-mcp"
    root.mkdir()
    existing = 'default = "old"\n\n[old]\nfrom_addr = "old@example.org"\ndriver = "pipe"\ncommand = "/usr/bin/true"\n'
    (root / "identities.toml").write_text(existing)
    answers = _write_answers(tmp_path, {
        "identity_action": "add",
        "identities": [{
            "name": "../evil", "from_addr": "x@example.org",
            "driver": "pipe", "command": "/usr/bin/true",
        }],
    })
    rc = lifecycle.run_setup_cli(["--answers", str(answers)])
    assert rc == 1  # blocked at the identity step
    assert (root / "identities.toml").read_text() == existing
    assert not list(root.glob("identities.toml.bak-*"))
    assert not list(home.rglob("*evil*"))
    events = _lifecycle_events()
    assert len(events) == 1  # state was touched, so the run is on record
    assert events[0]["detail"]["blocked"] == "identity"


def test_secret_value_key_refused_and_never_on_disk(home, tmp_path):
    sentinel = "hunter2-SENTINEL-VALUE"
    answers = _write_answers(tmp_path, {
        "identity_action": "add",
        "identities": [{
            "name": "leaky", "from_addr": "x@example.org",
            "driver": "smtp", "host": "smtp.example.org",
            "password": sentinel,
        }],
    })
    rc = lifecycle.run_setup_cli(["--answers", str(answers)])
    assert rc == 1
    assert not (home / ".email-mcp" / "identities.toml").exists()
    # The value must be unreachable in EVERY file setup wrote (meta,
    # ledger, anything) — not merely absent from identities.toml.
    assert _scan_tree_for(home, sentinel.encode()) == []


def test_read_only_skips_identity_and_dispatcher_agent(home, capsys):
    rc = lifecycle.run_setup_cli(
        ["--answers", str(FIXTURE), "--read-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "read-only mode" in out
    assert not (home / ".email-mcp" / "identities.toml").exists()
    # install_dispatcher=true in the answers, but read-only never sends:
    agents = home / "Library" / "LaunchAgents"
    assert not (agents / f"{dispatcher.LAUNCHD_LABEL}.plist").exists()
    events = _lifecycle_events()
    assert events[0]["detail"]["identities"] == []
    assert events[0]["detail"]["agents"] == []


# --------------------------------------------------------------------- #
# launchd + client config + audit event                                  #
# --------------------------------------------------------------------- #


def test_launchd_agents_installed_only_when_answered_yes(home, tool_shims):
    from email_mcp import fts

    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    agents = home / "Library" / "LaunchAgents"
    plist = agents / f"{dispatcher.LAUNCHD_LABEL}.plist"
    assert plist.exists()  # install_dispatcher: true
    assert plist.read_text() == dispatcher._plist_content()
    assert not (agents / f"{fts.LAUNCHD_LABEL}.plist").exists()  # false
    calls = [c for c in tool_shims.read_text().splitlines() if c]
    assert any(c.startswith("bootstrap") and dispatcher.LAUNCHD_LABEL in c
               for c in calls)
    assert not any(fts.LAUNCHD_LABEL in c and c.startswith("bootstrap")
                   for c in calls)


def test_client_config_prints_absolute_entry_point_json(home, capsys):
    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    decoder = json.JSONDecoder()
    blocks, i = [], 0
    while True:
        j = out.find('{\n  "mcpServers"', i)
        if j == -1:
            break
        obj, end = decoder.raw_decode(out[j:])
        blocks.append(obj)
        i = j + end
    assert len(blocks) == 2  # the default block + the read-only variant
    for block in blocks:
        spec = block["mcpServers"]["apple-mail"]
        assert os.path.isabs(spec["command"])
    assert "env" not in blocks[0]["mcpServers"]["apple-mail"]
    assert blocks[1]["mcpServers"]["apple-mail"]["env"] == {
        "EMAIL_MCP_READ_ONLY": "1"}
    # In a venv with the console script installed (this repo's .venv),
    # the printed command must be THAT script, not a python -m fallback.
    import sys
    script = Path(sys.executable).parent / "email-mcp"
    if script.is_file():
        assert blocks[0]["mcpServers"]["apple-mail"]["command"] == str(script)


def test_lifecycle_event_emitted_once_with_names_only(home):
    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    events = _lifecycle_events()
    assert len(events) == 1  # ONE event per setup run
    event = events[0]
    assert event["outcome"] == "setup"
    assert event["src"] == "cli"
    assert event["detail"] == {
        "mode": "setup",
        "identities": ["gmail", "work"],
        "agents": [dispatcher.LAUNCHD_LABEL],
        "fts_choice": "defer",
    }
    # Identity NAMES only — never addresses, never secrets.
    assert "example.org" not in json.dumps(event)


def test_existing_meta_flips_into_review_mode(home, capsys):
    root = home / ".email-mcp"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps({"state_version": 1}))
    assert lifecycle.run_setup_cli(["--answers", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "existing installation detected" in out
    assert "mode review" in out
    assert _lifecycle_events()[0]["detail"]["mode"] == "review"
    meta = json.loads((root / "meta.json").read_text())
    assert meta["state_version"] == 1
    assert meta["updated_at"]


# --------------------------------------------------------------------- #
# permissions: non-interactive block, interactive retry/skip loop        #
# --------------------------------------------------------------------- #


def test_non_interactive_permission_red_blocks_with_exit_1(
    home, monkeypatch, capsys,
):
    """No mail store, no skip_permissions answer: a red check must block
    a scripted run (nonzero exit) BEFORE any state is created, with the
    FDA restart honesty printed."""
    monkeypatch.delenv("EMAIL_MCP_MAIL_DIR")
    rc = lifecycle.run_setup_cli(["--yes"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "FAIL mail_store" in out
    assert "restart this terminal" in out  # FDA binds at process start
    assert "skip_permissions" in out  # the scripted way past it is named
    assert not (home / ".email-mcp").exists()  # blocked = nothing created
    assert "setup blocked at permissions" in out


def test_interactive_permission_loop_retry_then_skip(
    home, monkeypatch, capsys,
):
    """Red check interactively: the check's own fix text is printed,
    Enter retries (the probe runs again), `s` skips and the run goes on."""
    calls = {"n": 0}

    def red_check() -> dict:
        calls["n"] += 1
        return {"ok": False, "detail": "no Envelope Index",
                "fix": "grant Full Disk Access in System Settings"}

    monkeypatch.setattr(doctor, "check_mail_store", red_check)
    prompts = iter(["", "s"])  # Enter → retry, then s → skip
    monkeypatch.setattr(lifecycle, "_input", lambda _: next(prompts))

    result = lifecycle._step_permissions(lifecycle.Answers(),
                                         interactive=True)
    assert calls["n"] == 2  # Enter really re-ran the probe
    assert result.status == "ok"
    assert "mail_store" in result.detail
    out = capsys.readouterr().out
    assert "grant Full Disk Access in System Settings" in out  # its own fix
    assert "restart this terminal" in out


def test_no_terminal_without_answers_exits_2(home, capsys):
    """Interactive setup needs a terminal: under a captured/closed stdin
    (as here) the CLI refuses with a usage error, printing nothing to
    stdout. The refusal keeps the frozen stub contract that
    test_cli.py::test_lifecycle_stubs_exit_2_with_pointer still pins for
    `setup`: exit 2, silent stdout, "setup" + "L3" on stderr."""
    assert lifecycle.run_setup_cli([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--yes" in captured.err and "--answers" in captured.err
    assert "setup" in captured.err and "L3" in captured.err
    # And through the real dispatch, as MCP clients would hit it:
    assert cli.main(["setup"]) == 2
