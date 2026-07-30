"""v0.11 L6 red team — adversarial battery for the lifecycle surface.

This code deletes files and writes credentials-adjacent config on a real
machine, so the bar here is escape-proofing, not coverage: a purge that
leaves ``~/.email-mcp``, a setup that clobbers identities.toml without a
recoverable backup, a repair or uninstall that acts THROUGH a symlink, a
secret value that lands in any file — each is a catastrophe, and each
gets a test that fails against the un-fenced implementation.

Environment: every test runs in a fake ``$HOME`` with all EMAIL_MCP_*
wiped (then EMAIL_MCP_MAIL_DIR at the in-tmp mail fixture), the conftest
fts/audit guards shadowed by name, and a PATH-shimmed launchctl /
osascript / ssh / security / op set — the test_lifecycle_setup pattern.
Nothing in this module may ever reach the real launchd, Keychain,
1Password, network SMTP, or ``~/.email-mcp``.

Mutation-verified guards (each was temporarily broken, its test observed
to FAIL, and the guard restored — see the L6 hand-off):
  * purge ignores env-overridden dirs even at $HOME  (mutation: rmtree
    the resolved spool override inside run_uninstall's purge branch);
  * purge refuses a symlinked root (mutations: neuter the S_ISLNK
    branch; swap os.lstat for os.stat) and never follows a symlinked
    subdir (mutation: replace rmtree with a resolve-and-descend
    remover);
  * identities.toml is backed up before every clobber (mutation: skip
    the backup block in write_identities);
  * the identity-name fence blocks graph/<name>.token.json traversal
    (mutation: neuter the _NAME_RE check in _validate_tables).

The tests for holes FOUND this stage were additionally run against the
pre-fix modules at HEAD and observed to fail there: the symlinked-state-
dir chmod fence, the symlinked graph-dir token sweep, the launchctl-
absent degradations (setup / uninstall), the CR/LF/NUL header fence, and
the ``$HOME`` state-override refusals in both setup and doctor --fix.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from email_mcp import audit, cli, dispatcher, fts, lifecycle, repairs
from email_mcp.transports import SendError

FIXTURE_DIR = Path(__file__).parent / "fixtures"

SPOOL_STATES = ("pending", "sending", "sent", "failed", "cancelled")

KC_SENTINEL = "KC-hunter2-SENTINEL-9f31-never-on-disk"
OP_SENTINEL = "OP-hunter2-SENTINEL-7c22-never-on-disk"


# --------------------------------------------------------------------- #
# fixtures                                                               #
# --------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path, monkeypatch, mail_fixture):
    """Fake $HOME, EMAIL_MCP_* wiped, mail store at the in-tmp fixture."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in [v for v in os.environ if v.startswith("EMAIL_MCP_")]:
        monkeypatch.delenv(var)
    monkeypatch.setenv("EMAIL_MCP_MAIL_DIR", str(mail_fixture))
    yield h
    audit.set_process("server")  # lifecycle tags "cli"; restore


@pytest.fixture(autouse=True)
def fts_dir_guard(home):
    """Shadow conftest's guard (by name, on purpose): the fake HOME is
    the isolation; the env pin would point outside it."""
    return home / ".email-mcp" / "fts"


@pytest.fixture(autouse=True)
def audit_dir_guard(home):
    """Shadow conftest's guard (same reason): events must land through
    the real env-driven resolver, inside the fake HOME."""
    return home / ".email-mcp" / "audit"


@pytest.fixture(autouse=True)
def tool_shims(home, tmp_path, monkeypatch):
    """PATH shims: stateful launchctl, green osascript, fast-failing
    ssh/security/op. Returns {shim, log, secret_log} so tests can drop
    launchctl (absent-binary cases) or swap in secret-printing
    security/op (the credential-sentinel case)."""
    shim = tmp_path / "shim"
    state = tmp_path / "launchd-state"
    shim.mkdir()
    state.mkdir()
    log = tmp_path / "launchctl.log"
    log.write_text("")
    secret_log = tmp_path / "secret-reads.log"
    secret_log.write_text("")
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
    (shim / "osascript").write_text("#!/bin/sh\necho true\nexit 0\n")
    (shim / "osascript").chmod(0o755)
    for tool in ("ssh", "security", "op"):
        (shim / tool).write_text("#!/bin/sh\nexit 1\n")
        (shim / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LAUNCHCTL_LOG", str(log))
    monkeypatch.setenv("LAUNCHCTL_STATE_DIR", str(state))
    monkeypatch.setenv("SECRET_LOG", str(secret_log))
    return {"shim": shim, "log": log, "secret_log": secret_log}


@pytest.fixture
def no_launchctl(tool_shims, monkeypatch):
    """A PATH on which `launchctl` does not resolve at all: subprocess
    lookups raise FileNotFoundError, exactly like a non-mac or stripped
    environment. The shims keep their absolute #!/bin/sh shebangs, so
    nothing else here needs PATH."""
    empty = tool_shims["shim"].parent / "no-tools"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    return empty


def _install_estate(home: Path) -> dict[str, Path]:
    """A lived-in installation inside the fake HOME."""
    root = home / ".email-mcp"
    for d in (root, root / "spool", root / "plans", root / "graph",
              root / "audit"):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)
    identities = root / "identities.toml"
    identities.write_text(textwrap.dedent("""\
        default = "work"

        [work]
        from_addr = "user@example.org"
        driver = "smtp"
        host = "smtp.example.org"
        keychain = "email-mcp-work"
    """))
    identities.chmod(0o600)
    (root / "meta.json").write_text(json.dumps({"state_version": 1}))
    (root / "meta.json").chmod(0o600)
    token = root / "graph" / "work.token.json"
    token.write_text("{}")
    token.chmod(0o600)
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL):
        (agents / f"{label}.plist").write_text("<plist/>")
    mail = home / "Library" / "Mail" / "V10" / "MailData"
    mail.mkdir(parents=True, exist_ok=True)
    (mail / "Envelope Index").write_text("mail-store-sentinel")
    (home / "Documents").mkdir()
    (home / "Documents" / "thesis.txt").write_text("irreplaceable")
    return {"root": root, "token": token, "agents": agents,
            "mail": home / "Library" / "Mail"}


def _write_answers(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(data))
    return path


def _scan_tree_for(home: Path, needle: bytes) -> list[Path]:
    return [p for p in home.rglob("*")
            if p.is_file() and not p.is_symlink()
            and needle in p.read_bytes()]


# --------------------------------------------------------------------- #
# (*1) purge with EMAIL_MCP_*=$HOME: the override survives, printed      #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("var", ["EMAIL_MCP_SPOOL_DIR",
                                 "EMAIL_MCP_PLANS_DIR",
                                 "EMAIL_MCP_AUDIT_DIR"])
def test_purge_env_override_at_home_never_removes_home(
    home, monkeypatch, capsys, var,
):
    """The headline attack: EMAIL_MCP_{SPOOL,PLANS,AUDIT}_DIR=$HOME. A
    purge that trusted the env-resolved paths would rmtree the home
    directory; the hardcoded-target fence must remove EXACTLY
    ~/.email-mcp, keep every sibling, and PRINT the override."""
    estate = _install_estate(home)
    monkeypatch.setenv(var, str(home))

    assert cli.main(["uninstall", "--purge", "--yes"]) == 0

    assert not estate["root"].exists()
    assert home.is_dir()
    assert (home / "Documents" / "thesis.txt").read_text() == "irreplaceable"
    assert (estate["mail"] / "V10" / "MailData"
            / "Envelope Index").read_text() == "mail-store-sentinel"
    out = capsys.readouterr().out
    assert var in out and "never removed" in out


@pytest.mark.parametrize("var", ["EMAIL_MCP_SPOOL_DIR",
                                 "EMAIL_MCP_PLANS_DIR",
                                 "EMAIL_MCP_GRAPH_DIR"])
def test_setup_refuses_state_override_at_home(home, monkeypatch, capsys, var):
    """The write-side twin of the purge case. config.spool_dir() /
    plans_dir() / graph_dir() mkdir the target and ``chmod 0700`` its
    PARENT — so a state override at $HOME would chmod the directory that
    CONTAINS every user's home (on a real Mac: /Users) and scatter five
    spool subdirectories through $HOME. Setup must refuse before
    touching anything."""
    monkeypatch.setenv(var, str(home))
    parent_mode = home.parent.stat().st_mode & 0o777
    before = sorted(p.name for p in home.iterdir())

    rc = lifecycle.run_setup_cli(["--yes"])

    assert rc == 1  # blocked at state_dirs
    out = capsys.readouterr().out
    assert "BLOCKED" in out and var in out
    assert (home.parent.stat().st_mode & 0o777) == parent_mode
    assert sorted(p.name for p in home.iterdir()) == before
    for junk in SPOOL_STATES:
        assert not (home / junk).exists()


def test_doctor_fix_refuses_state_override_at_home(home, monkeypatch):
    """Same fence on the repair side: `doctor --fix` must not mkdir the
    spool states into $HOME nor chmod $HOME (or its parent) because an
    override points there."""
    monkeypatch.setenv("EMAIL_MCP_SPOOL_DIR", str(home))
    home.chmod(0o755)  # deliberately "wrong" for a state dir
    parent_mode = home.parent.stat().st_mode & 0o777

    result = repairs.run_fixes()

    assert (home.stat().st_mode & 0o777) == 0o755   # $HOME left alone
    assert (home.parent.stat().st_mode & 0o777) == parent_mode
    for junk in SPOOL_STATES:
        assert not (home / junk).exists()
    # The real state root is still healed — the fence is targeted, not a
    # blanket opt-out.
    assert (home / ".email-mcp").is_dir()
    assert not [f for f in result["failed"]]


# --------------------------------------------------------------------- #
# (*2) symlinked root refused; symlinked subdir never followed           #
# --------------------------------------------------------------------- #


def test_purge_refuses_symlinked_root(home, tmp_path, capsys):
    victim = tmp_path / "victim-root"
    victim.mkdir()
    (victim / "data.txt").write_text("precious")
    root = home / ".email-mcp"
    root.symlink_to(victim)

    # match on the fence's own phrasing, NOT "symlink": pytest's tmp dir
    # carries this test's name, so "symlink" appears in the PATH inside
    # every fence message and would match vacuously.
    with pytest.raises(lifecycle.PurgeRefused, match="refusing to follow"):
        lifecycle.purge_state()
    assert root.is_symlink()                       # even the link stays
    assert (victim / "data.txt").read_text() == "precious"

    assert cli.main(["uninstall", "--purge", "--yes"]) == 1
    assert (victim / "data.txt").read_text() == "precious"
    assert "purge refused" in capsys.readouterr().err


def test_purge_symlinked_subdir_unlinked_not_followed(home, tmp_path):
    """A symlink INSIDE the root (spool -> victim): purge must remove
    the link with the tree and leave the victim directory intact —
    rmtree may never descend through it."""
    victim = tmp_path / "victim-sub"
    victim.mkdir()
    (victim / "keep.txt").write_text("survives")
    root = home / ".email-mcp"
    root.mkdir()
    (root / "meta.json").write_text("{}")
    (root / "spool").symlink_to(victim)
    (root / "plans").mkdir()
    (root / "plans" / "p.json").write_text("{}")

    removed = lifecycle.purge_state()

    assert removed == root
    assert not root.exists()                       # tree + link gone
    assert victim.is_dir()
    assert (victim / "keep.txt").read_text() == "survives"


# --------------------------------------------------------------------- #
# (*3) identities.toml: backup before clobber, interleaved writers       #
# --------------------------------------------------------------------- #


def test_interleaved_identity_writes_lose_no_generation(home, monkeypatch):
    """Two concurrent setups (simulated at the write_identities level,
    with a frozen clock forcing the worst-case backup-name collision):
    whichever write lands last, EVERY generation of the file must remain
    recoverable — the original and the first writer's content both live
    on as backups, mode preserved."""
    from email_mcp import ids
    from datetime import datetime, timezone

    monkeypatch.setattr(
        ids, "utcnow",
        lambda: datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc))

    root = home / ".email-mcp"
    root.mkdir()
    path = root / "identities.toml"
    original = ('default = "old"\n\n[old]\nfrom_addr = "old@example.org"\n'
                'driver = "pipe"\ncommand = "/usr/bin/true"\n')
    path.write_text(original)
    path.chmod(0o640)  # distinctive: the backup must carry it

    # Both "wizards" load the SAME original state (the race), then write.
    tables_a, _ = lifecycle._load_existing_tables(path)
    tables_b, _ = lifecycle._load_existing_tables(path)
    tables_a["alpha"] = {"from_addr": "a@example.org", "driver": "pipe",
                         "command": "/usr/bin/true"}
    tables_b["beta"] = {"from_addr": "b@example.org", "driver": "pipe",
                        "command": "/usr/bin/true"}

    _, bak_a = lifecycle.write_identities(tables_a, "old", path)
    content_a = path.read_text()
    _, bak_b = lifecycle.write_identities(tables_b, "old", path)

    # Same-second stamps: the collision loop must have found a new name.
    assert bak_a is not None and bak_b is not None and bak_a != bak_b
    assert bak_a.read_text() == original           # generation 0
    assert (bak_a.stat().st_mode & 0o777) == 0o640  # mode preserved
    assert bak_b.read_text() == content_a          # generation 1 (alpha)
    assert "alpha" in bak_b.read_text()
    final = path.read_text()
    assert "beta" in final                         # generation 2 live
    assert (path.stat().st_mode & 0o777) == 0o600
    # No half-written tmp litter either.
    assert not list(root.glob("identities.toml.*.tmp"))


def test_setup_backs_up_before_clobber_no_bak_no_write(home, tmp_path):
    """The scripted-setup flavor of the same promise: an existing file
    is never replaced without a mode-preserved backup carrying its exact
    pre-clobber bytes."""
    root = home / ".email-mcp"
    root.mkdir()
    original = ('default = "old"\n\n[old]\nfrom_addr = "old@example.org"\n'
                'driver = "pipe"\ncommand = "/usr/bin/true"\n')
    path = root / "identities.toml"
    path.write_text(original)
    path.chmod(0o640)
    answers = _write_answers(tmp_path, {
        "identity_action": "add",
        "identities": [{"name": "extra", "from_addr": "x@example.org",
                        "driver": "pipe", "command": "/usr/bin/true"}],
    })

    assert lifecycle.run_setup_cli(["--answers", str(answers)]) == 0

    backups = list(root.glob("identities.toml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert (backups[0].stat().st_mode & 0o777) == 0o640
    assert "extra" in path.read_text() and "old" in path.read_text()


# --------------------------------------------------------------------- #
# (*4) identity-name fence: token-path traversal never reachable         #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [
    "../evil",             # classic traversal → graph/../evil.token.json
    "..%2Fevil",           # URL-encoded traversal lookalike
    "evil\nname",          # control character (header/log injection)
    "evil\x00name",        # NUL
    "..",                  # parent dir itself
    ".",                   # cwd
    "e" * 200,             # over the 64-char cap (valid TOML key!)
    "",                    # empty
])
def test_identity_name_fence_rejects_hostile_names(home, tmp_path, name):
    """Every hostile name is refused BEFORE any file is touched: no
    identities.toml, no backup, no token file, nothing named after the
    payload anywhere under $HOME."""
    answers = _write_answers(tmp_path, {
        "identity_action": "add",
        "identities": [{"name": name, "from_addr": "x@example.org",
                        "driver": "pipe", "command": "/usr/bin/true"}],
    })

    rc = lifecycle.run_setup_cli(["--answers", str(answers)])

    assert rc == 1  # blocked at the identity step
    root = home / ".email-mcp"
    assert not (root / "identities.toml").exists()
    assert not list(root.glob("identities.toml.bak-*"))
    assert not list(home.rglob("*.token.json"))
    assert not list(home.rglob("*evil*"))


# --------------------------------------------------------------------- #
# launchctl absent: degrade, never traceback                             #
# --------------------------------------------------------------------- #


def test_setup_launchd_step_degrades_without_launchctl(
    home, no_launchctl, capsys,
):
    """launchctl missing from PATH: the launchd step reports a failure
    and the wizard would carry on — no exception escapes the step."""
    facts: dict = {}
    answers = lifecycle.Answers(install_dispatcher=True)

    result = lifecycle._step_launchd(answers, interactive=False,
                                     facts=facts)

    assert result.status == "failed"
    assert "install failed" in result.detail
    assert facts["agents"] == []
    assert "install failed" in capsys.readouterr().out


def test_uninstall_purge_completes_without_launchctl(
    home, no_launchctl, capsys,
):
    """launchctl missing must not abort uninstall --purge with a
    traceback: the agent step degrades (reported on stderr), tokens and
    the fenced purge still run, Mail survives."""
    estate = _install_estate(home)

    rc = cli.main(["uninstall", "--purge", "--yes"])

    assert rc == 0
    assert not estate["root"].exists()
    assert (estate["mail"] / "V10" / "MailData"
            / "Envelope Index").read_text() == "mail-store-sentinel"
    captured = capsys.readouterr()
    assert "removal failed" in captured.err
    assert "Traceback" not in captured.err + captured.out


def test_run_fixes_degrades_without_launchctl(home, no_launchctl):
    """A legacy plist present but no launchctl: the repair lands in
    failed[], run_fixes returns normally."""
    _install_estate(home)
    agents = home / "Library" / "LaunchAgents"
    (agents / "com.paris.email-mcp-dispatcher.plist").write_text("<plist/>")

    result = repairs.run_fixes()

    failed_ids = {f["repair"] for f in result["failed"]}
    assert "legacy_launchd" in failed_ids
    assert (agents / "com.paris.email-mcp-dispatcher.plist").exists()


# --------------------------------------------------------------------- #
# doctor --fix vs symlinks: flag, never chmod (or mkdir) through         #
# --------------------------------------------------------------------- #


def test_doctor_fix_never_acts_through_symlinked_state_dir(home, tmp_path):
    """spool -> victim dir (0755) and identities.toml -> victim file
    (0644): run_fixes must neither chmod the victims nor create the
    spool subtree inside the victim, and must FLAG both symlinks."""
    victim_dir = tmp_path / "victim-dir"
    victim_dir.mkdir()
    victim_dir.chmod(0o755)
    victim_file = tmp_path / "victim-file"
    victim_file.write_text("not yours")
    victim_file.chmod(0o644)

    root = home / ".email-mcp"
    for d in (root, root / "plans", root / "graph", root / "audit"):
        d.mkdir(parents=True)
        d.chmod(0o700)
    (root / "spool").symlink_to(victim_dir)
    (root / "identities.toml").symlink_to(victim_file)

    result = repairs.run_fixes()

    # Victims untouched: mode intact, no state subtree grown inside.
    assert (victim_dir.stat().st_mode & 0o777) == 0o755
    assert list(victim_dir.iterdir()) == []
    assert (victim_file.stat().st_mode & 0o777) == 0o644
    assert victim_file.read_text() == "not yours"
    # And flagged, not silently skipped:
    failures = {f["repair"]: f["error"] for f in result["failed"]}
    assert "state_dir_perms" in failures
    assert "symlink" in failures["state_dir_perms"]
    assert "state_file_perms" in failures
    assert "symlink" in failures["state_file_perms"]
    # Second pass: same refusals, still nothing touched (no oscillation).
    repairs.run_fixes()
    assert (victim_dir.stat().st_mode & 0o777) == 0o755
    assert (victim_file.stat().st_mode & 0o777) == 0o644


def test_run_fixes_with_unwritable_audit_dir_never_raises(
    home, tmp_path, monkeypatch,
):
    """EMAIL_MCP_AUDIT_DIR under a read-only parent: the mkdir repair
    fails, the failure is REPORTED, the emit is dropped silently, and
    run_fixes returns instead of raising."""
    _install_estate(home)
    ro = tmp_path / "ro"
    ro.mkdir()
    dead = ro / "audit"
    monkeypatch.setenv("EMAIL_MCP_AUDIT_DIR", str(dead))
    ro.chmod(0o500)
    try:
        result = repairs.run_fixes()
    finally:
        ro.chmod(0o700)

    failed_ids = {f["repair"] for f in result["failed"]}
    assert "state_dir_missing" in failed_ids
    assert not dead.exists()


# --------------------------------------------------------------------- #
# uninstall: symlinked graph dir, idempotency, corrupt meta, bare serve  #
# --------------------------------------------------------------------- #


def test_uninstall_never_deletes_tokens_through_symlinked_graph(
    home, tmp_path, capsys,
):
    """~/.email-mcp/graph -> victim dir holding *.token.json: the token
    sweep must not reach through the link."""
    victim = tmp_path / "victim-tokens"
    victim.mkdir()
    (victim / "foreign.token.json").write_text("someone else's tokens")
    root = home / ".email-mcp"
    root.mkdir()
    (root / "graph").symlink_to(victim)

    assert cli.main(["uninstall", "--yes"]) == 0

    assert (victim / "foreign.token.json").read_text() \
        == "someone else's tokens"
    out = capsys.readouterr().out
    assert "symlink" in out and "never followed" in out
    # The side-effect-free plan says the same thing.
    plan = lifecycle.uninstall_plan(purge=False)
    assert not any("foreign.token.json" in line for line in plan["remove"])
    assert any("symlink" in line for line in plan["print_only"])


def test_uninstall_and_purge_idempotent_on_empty_home(home):
    """No plists, no tokens, no state tree: uninstall (and purge) are
    clean no-ops, twice."""
    assert cli.main(["uninstall", "--yes"]) == 0
    assert cli.main(["uninstall", "--yes"]) == 0
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert cli.main(["uninstall", "--purge", "--yes"]) == 0
    assert not (home / ".email-mcp" / "spool").exists()
    assert lifecycle.purge_state() is None


def test_corrupt_meta_json_never_tracebacks(home, capsys):
    """A truncated meta.json: version reports state_version 0, update
    adopts and rewrites it — clear output, no exception anywhere."""
    root = home / ".email-mcp"
    root.mkdir()
    (root / "meta.json").write_bytes(b'{"state_version": ')  # truncated

    assert cli.main(["version"]) == 0
    assert "state_version 0" in capsys.readouterr().out

    assert lifecycle.run_update_cli(["--yes"]) == 0
    meta = json.loads((root / "meta.json").read_text())
    assert meta["state_version"] == lifecycle.STATE_VERSION


def test_bare_invocation_still_serves_after_lifecycle_edits(home, tmp_path):
    """Paris's registration guard, re-proven at the end of the L-chain:
    `email-mcp` with no args and an empty stdin serves and exits clean,
    with a byte-silent stdout (stdout is the MCP wire)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("EMAIL_MCP_")}
    env["HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-m", "email_mcp.cli"],
        input=b"", capture_output=True, timeout=120, env=env,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0
    assert proc.stdout == b""


# --------------------------------------------------------------------- #
# wizard input fences: control characters in addresses                   #
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("field,value", [
    ("from_addr", "x@example.org\nBcc: attacker@example.net"),
    ("from_addr", "x@example.org\r\nX-Inject: 1"),
    ("from_name", "Nice Name\nBcc: attacker@example.net"),
    ("from_addr", "x@example.org\x00"),
])
def test_wizard_rejects_control_chars_in_header_values(
    home, tmp_path, field, value,
):
    """CR/LF/NUL in from_addr/from_name would arm a header injection the
    compose fence later refuses — the wizard must reject it at write
    time, leaving nothing on disk."""
    block = {"name": "inj", "from_addr": "x@example.org",
             "driver": "pipe", "command": "/usr/bin/true"}
    block[field] = value
    answers = _write_answers(tmp_path, {
        "identity_action": "add", "identities": [block],
    })

    rc = lifecycle.run_setup_cli(["--answers", str(answers)])

    assert rc == 1
    assert not (home / ".email-mcp" / "identities.toml").exists()
    assert _scan_tree_for(home, b"attacker@example.net") == []


# --------------------------------------------------------------------- #
# F8 — the credential sentinel: secret VALUES never persist              #
# --------------------------------------------------------------------- #


class _RecordCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):  # noqa: A003 — logging API name
        self.records.append(record)


@pytest.fixture
def email_mcp_log_records():
    """email_mcp's logger has propagate=False (stdout is the wire), so
    caplog never sees it — attach a collector directly."""
    handler = _RecordCollector()
    logging.getLogger("email_mcp").addHandler(handler)
    yield handler.records
    logging.getLogger("email_mcp").removeHandler(handler)


def test_f8_credential_sentinel_never_reaches_disk_ledger_or_logs(
    home, tmp_path, tool_shims, email_mcp_log_records, capsys,
):
    """Carried audit finding F8, closed: shim `security` and `op` to
    hand out sentinel secret VALUES, drive failing send paths through a
    Keychain-backed and an op://-backed smtp identity plus a full setup
    run (doctor transport healthchecks + failing self-send), then prove
    the values are unreachable in every file under $HOME, in the audit
    ledger, in every email_mcp log record, and on stdout — while the
    references themselves ARE on disk (the non-vacuity guard, mirroring
    the F5 body-sentinel test)."""
    from email_mcp import sender

    # Secret stores that actually answer (and log that they were asked).
    shim = tool_shims["shim"]
    (shim / "security").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "security $@" >> "$SECRET_LOG"
        printf '%s\\n' '{KC_SENTINEL}'
        exit 0
    """))
    (shim / "op").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "op $@" >> "$SECRET_LOG"
        printf '%s\\n' '{OP_SENTINEL}'
        exit 0
    """))

    # Two smtp identities whose delivery can only fail (nothing listens
    # on 127.0.0.1:9), with allow_all so the failure happens PAST the
    # secret read.
    root = home / ".email-mcp"
    root.mkdir()
    (root / "identities.toml").write_text(textwrap.dedent("""\
        default = "work"

        [work]
        from_addr = "user@example.org"
        driver = "smtp"
        host = "127.0.0.1"
        port = 9
        keychain = "email-mcp-work"
        allow_all = true

        [onep]
        from_addr = "onep@example.org"
        driver = "smtp"
        host = "127.0.0.1"
        port = 9
        op = "op://Work/mail/password"
        allow_all = true
    """))
    (root / "identities.toml").chmod(0o600)

    # Failing send through BOTH identities: the transport reads the
    # secret, then the connection is refused.
    with pytest.raises(SendError):
        sender.send_email(to="user@example.org", subject="f8-kc",
                          body="b", from_identity="work")
    with pytest.raises(SendError):
        sender.send_email(to="onep@example.org", subject="f8-op",
                          body="b", from_identity="onep")

    # Failing SETUP path on top: keep the identities, run the smoke
    # step's doctor (transport healthchecks read both secrets again)
    # and a failing self-send.
    answers = _write_answers(tmp_path, {
        "identity_action": "keep", "self_send": True,
        "fts_choice": "defer",
    })
    assert lifecycle.run_setup_cli(["--answers", str(answers)]) == 0

    # Non-vacuity: the sentinels really entered this process...
    secret_calls = tool_shims["secret_log"].read_text()
    assert "security find-generic-password" in secret_calls
    assert "op read" in secret_calls
    # ...the references (not values) really are in written config...
    ident_text = (root / "identities.toml").read_text()
    assert "email-mcp-work" in ident_text
    assert "op://Work/mail/password" in ident_text
    # ...and the run left a ledger.
    assert audit.query(event="lifecycle")["events"]

    # The sentinels themselves: nowhere. Not in any file under $HOME
    # (identities, meta, ledger months, spool, logs), not in a log
    # record, not on stdout.
    for sentinel in (KC_SENTINEL, OP_SENTINEL):
        assert _scan_tree_for(home, sentinel.encode()) == []
    log_text = "\n".join(r.getMessage() for r in email_mcp_log_records)
    assert KC_SENTINEL not in log_text and OP_SENTINEL not in log_text
    out = capsys.readouterr().out
    assert KC_SENTINEL not in out and OP_SENTINEL not in out
