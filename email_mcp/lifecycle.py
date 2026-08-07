"""Lifecycle verbs as thin content over the primitives.

setup / update / uninstall / doctor --fix each compose the same four
moves: resolve state → build a plan (or the checks registry's repairs) →
render → confirm → execute → fold the exit code. ONE helper,
`confirm_and_run`, owns the typed confirmation, --yes, dry-run, and the
exit-code fold for every destructive verb — the preview it prints is
render() of the SAME list execute() consumes, so it cannot lie.

These functions print by design: they are CLI verbs (dispatched by the
`email-mcp` entry point), never the MCP wire.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import checks, config, plan, state

_UPDATE_CHECKS = frozenset({checks.META_VERSION, checks.PLIST_DRIFT})


def confirm_and_run(rows: list[plan.Row], *, verb: str, yes: bool = False,
                    dry_run: bool = False) -> int:
    """THE gate for every destructive verb. Renders the plan; dry-run stops
    there (render is pure, so a dry run provably touches nothing); a plan
    with no Action rows has nothing to confirm; otherwise the typed verb
    (or --yes) releases execute, and the exit code is a fold over the same
    results the report renders from. The preview prints only when someone
    will act on it — before a typed confirmation or as the dry run; under
    --yes the results ARE the report, and printing both stuttered every
    row twice (first-user transcript, 2026-08-04)."""
    if dry_run:
        print(plan.render(rows))
        return 0
    actions = [r for r in rows if isinstance(r, plan.Action)]
    if not actions:
        print(plan.render(rows))
        print("nothing to do.")
        return 0
    if not yes:
        print(plan.render(rows))
        answer = input(f"type '{verb}' to proceed ({len(actions)} "
                       f"action(s)): ")
        if answer.strip() != verb:
            print("aborted — nothing done.")
            return 1
    results = plan.execute(rows, verb=verb)
    print(plan.render_results(results))
    return 1 if any(r.failed for r in results) else 0


# ---------------------------------------------------------------------- #
# uninstall                                                               #
# ---------------------------------------------------------------------- #


def _listed(d: Path, consequence: str) -> tuple[list[str], plan.Row | None]:
    """Names in `d`, or the PrintOnly row saying the plan cannot see them.
    Path.glob swallows PermissionError, so a plan built over it silently
    under-describes; a plan that cannot enumerate must say so instead —
    one rule for every directory a plan builder walks."""
    try:
        return sorted(os.listdir(d)), None
    except FileNotFoundError:
        return [], None
    except OSError as e:
        return [], plan.PrintOnly(f"{d}: cannot list ({e}) — {consequence}")


def plan_uninstall(purge: bool = False) -> list[plan.Row]:
    """Agents out + graph token caches removed, state kept; --purge adds
    the state tree itself and the default logs. Env-overridden or refused
    paths become PrintOnly rows — named, never removed."""
    from . import dispatcher, fts

    rows: list[plan.Row] = []
    agents = Path.home() / "Library" / "LaunchAgents"
    for label in (dispatcher.LAUNCHD_LABEL, fts.LAUNCHD_LABEL,
                  *dispatcher.LEGACY_LABELS):
        plist = agents / f"{label}.plist"
        if os.path.lexists(plist):
            rows += [plan.BootoutAgent(label), plan.UnlinkFile(plist)]

    r = state.State.resolve()
    if isinstance(r, state.Refused):
        rows.append(plan.PrintOnly(f"state root refused: {r.reason} — "
                                   f"nothing under it was touched"))
    elif r.root != state.default_root():
        rows.append(plan.PrintOnly(
            f"state root is overridden to {r.root} ({state.ENV_VAR}) — "
            f"not touched; remove it yourself"))
    elif purge:
        rows.append(plan.RemoveTree.state_root())
    else:
        reader = r.reader()
        names, cannot = _listed(reader.graph, "token caches not removed")
        if cannot:
            rows.append(cannot)
        rows += [plan.UnlinkFile(reader.graph / n) for n in names
                 if n.endswith(".token.json")]
        rows.append(plan.Kept(r.root, "state kept — pass --purge to remove")
                    if os.path.isdir(r.root)
                    else plan.Kept(r.root, "already absent"))

    if purge:
        raw = os.environ.get("EMAIL_MCP_LOG_FILE", "").strip()
        if raw:
            rows.append(plan.PrintOnly(f"log file overridden "
                                       f"(EMAIL_MCP_LOG_FILE={raw}) — "
                                       f"not removed"))
        logs = Path.home() / "Library" / "Logs"
        names, cannot = _listed(logs, "log files not removed")
        if cannot:
            rows.append(cannot)
        rows += [plan.UnlinkFile(logs / n) for n in names
                 if n.startswith("email-mcp") and ".log" in n]
    return rows


def uninstall(*, purge: bool = False, yes: bool = False,
              dry_run: bool = False) -> int:
    return confirm_and_run(plan_uninstall(purge), verb="uninstall",
                           yes=yes, dry_run=dry_run)


# ---------------------------------------------------------------------- #
# doctor --fix and update — registry slices                               #
# ---------------------------------------------------------------------- #


def doctor_fix(*, yes: bool = False, dry_run: bool = False) -> int:
    """Map the registry's findings to repairs and hand them to the same
    executor uninstall uses — dry-run purity and failure honesty are
    inherited, not re-implemented."""
    r = state.State.resolve()
    if isinstance(r, state.Refused):
        print(f"cannot fix: {r.reason}")
        return 1
    rows = checks.plan_fix()
    if not rows:
        print("nothing to fix.")
        return 0
    return confirm_and_run(rows, verb="doctor_fix", yes=yes, dry_run=dry_run)


def update(*, yes: bool = False, dry_run: bool = False) -> int:
    """Bring an existing install forward: the meta state_version compare
    yields the pending migration actions; drifted launchd plists are
    re-rendered. Content IS the checks registry's migration slice."""
    r = state.State.resolve()
    if isinstance(r, state.Refused):
        print(f"cannot update: {r.reason}")
        return 1
    rows = checks.plan_fix(only=_UPDATE_CHECKS)
    if not rows:
        print("up to date — nothing to migrate.")
        return 0
    return confirm_and_run(rows, verb="update", yes=yes, dry_run=dry_run)


# ---------------------------------------------------------------------- #
# setup — the smallest wizard: prompts + the registry run forward         #
# ---------------------------------------------------------------------- #


def _ask(prompt: str, default: str = "") -> str:
    tag = f" [{default}]" if default else ""
    return input(f"{prompt}{tag}: ").strip() or default


def _yn(prompt: str, default: bool = False) -> bool:
    raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw in {"y", "yes"}


def _identity_rows() -> list[plan.Row]:
    """Prompt the first identity into identities.toml — backup-first, and
    NEVER holding a secret value: the smtp app password stays in the
    Keychain, and the command that puts it there is printed for the user."""
    path = config.identities_file()
    if path.exists() and not _yn(f"{path} exists — reconfigure it "
                                 f"(the current file is kept as .bak)?"):
        return _upgrade_rows(path)
    print("\nSending identity (leave the address empty to skip — "
          "reading mail needs none):")
    from_addr = _ask("From: address")
    if not from_addr:
        return [plan.PrintOnly(f"no identity configured — sending stays "
                               f"off until {path} exists")]
    name = "main"
    lines = [f'default = "{name}"', "", f"[{name}]",
             f"from_addr = {json.dumps(from_addr)}"]
    from_name = _ask("display name")
    if from_name:
        lines.append(f"from_name = {json.dumps(from_name)}")
    driver = _ask("transport driver (smtp / ssh_sendmail / pipe)", "smtp")
    lines.append(f"driver = {json.dumps(driver)}")
    if driver == "smtp":
        lines.append(f"host = {json.dumps(_ask('SMTP host'))}")
        lines.append(f"port = {int(_ask('SMTP port', '587'))}")
        username = _ask("SMTP username", from_addr)
        lines.append(f"username = {json.dumps(username)}")
        item = _ask("Keychain item for the app password", "email-mcp-smtp")
        lines.append(f"keychain = {json.dumps(item)}")
        print("store the app password yourself — no file ever holds it:")
        print(f"  security add-generic-password -a {username} -s {item} -w")
        print("sending is unrestricted by default — declare an allowlist "
              "(or allow_all = false) in the file to restrict it")
    elif driver == "ssh_sendmail":
        lines.append(f"host = {json.dumps(_ask('SSH host'))}")
        lines.append(f"user = {json.dumps(_ask('SSH user'))}")
    elif driver == "pipe":
        lines.append(f"command = "
                     f"{json.dumps(_ask('delivery command (RFC822 on stdin)'))}")
    else:
        print(f"unknown driver {driver!r} — skipping identity setup.")
        return [plan.PrintOnly(f"no identity configured — unknown driver "
                               f"{driver!r}")]
    lines += _exchange_lines(name, from_addr)
    return [plan.WriteFile(path, "\n".join(lines) + "\n", mode=0o600,
                           backup=True)]


# The generic work-account endpoint and Microsoft's own public client:
# no tenant GUID to look up, no app to register — the two facts that let
# the wizard ask ONE plain question instead of quoting documentation.
_MS_TENANT = "organizations"
_MS_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


def _exchange_enable(name: str, from_addr: str,
                     graph_cfg: dict | None = None) -> bool:
    """Offer the Exchange extras — drafts and lid-closed scheduling — in
    the user's words, and do the sign-in HERE, while the human is
    present. A capability the user must read about in a doc is a
    capability they do not have (first-user finding, 2026-08-01)."""
    if not _yn(f"\nIs {from_addr} a Microsoft/Exchange mailbox "
               "(you read it in Outlook or OWA)?", default=False):
        return False
    print("Exchange extras: drafts filed in your real Drafts folder, and "
          "scheduled sends that fire even with the laptop closed.")
    if not _yn("Enable them now (one browser sign-in)?", default=True):
        return False
    from . import graph
    from .identities import Identity

    ident = Identity(
        name=name, from_addr=from_addr, drafts="graph",
        graph=graph_cfg or {"tenant": _MS_TENANT,
                            "client_id": _MS_CLIENT_ID})
    try:
        graph.device_login(ident)          # prints the code; binds to /me
    except Exception as e:                 # noqa: BLE001 — user-facing step
        # Not fatal: the identity is still written WITHOUT the lane, so
        # setup finishes and the retry command is the whole remedy.
        print(f"sign-in did not complete ({e})")
        print(f"  enable later with: email-mcp setup   (or: python -m "
              f"email_mcp.graph --login {name})")
        return False
    print("drafts + lid-closed scheduling enabled.")
    return True


def _exchange_lines(name: str, from_addr: str) -> list[str]:
    if not _exchange_enable(name, from_addr):
        return []
    return ['executor = "graph"', 'drafts = "graph"', "",
            f"[{name}.graph]",
            f"tenant = {json.dumps(_MS_TENANT)}",
            f"client_id = {json.dumps(_MS_CLIENT_ID)}"]


def _upgrade_rows(path) -> list[plan.Row]:
    """KEPT identities can still gain the Exchange extras: nothing is
    rewritten — the missing lines are inserted into the default
    identity's block (backup first). A capability gated behind
    "reconfigure everything" is a capability a returning user never
    enables — and reconfiguring would clobber a working transport."""
    from . import identities as ident_mod

    try:
        idents, default = ident_mod.load()
    except Exception as e:  # noqa: BLE001 — foreign/unreadable file: keep
        return [plan.Kept(path, f"existing identities kept ({e})")]
    ident = idents[default]
    if ident.drafts != "none" or not ident.from_addr:
        return [plan.Kept(path, "existing identities kept")]
    have_graph = all(str(ident.graph.get(k, "")).strip()
                     for k in ("tenant", "client_id"))
    cfg = ident.graph if have_graph else None
    if not _exchange_enable(default, ident.from_addr, graph_cfg=cfg):
        return [plan.Kept(path, "existing identities kept")]
    inner = ['drafts   = "graph"']
    if ident.executor != "graph":
        inner.append('executor = "graph"')
    text = path.read_text()
    out, inserted = [], False
    for line in text.splitlines():
        out.append(line)
        if not inserted and line.strip() == f"[{default}]":
            out += inner
            inserted = True
    if not inserted:  # header not found textually — never guess
        print(f"could not locate [{default}] in {path} — add "
              f'drafts = "graph" to it yourself')
        return [plan.Kept(path, "existing identities kept")]
    new = "\n".join(out) + "\n"
    if not have_graph:
        new += ("\n" + f"[{default}.graph]\n"
                f"tenant = {json.dumps(_MS_TENANT)}\n"
                f"client_id = {json.dumps(_MS_CLIENT_ID)}\n")
    return [plan.WriteFile(path, new, mode=0o600, backup=True)]


def _agent_rows() -> list[plan.Row]:
    """The two background helpers, offered in the user's words with the
    right answer as the default. A question phrased in launchd jargon
    with a safe-looking "N" is a question the first user answers wrong
    and never benefits from (2026-08-04). Already-installed agents are
    kept silently — setup re-renders drift via the checks registry, and
    removal belongs to `email-mcp uninstall`."""
    from . import dispatcher, fts

    rows: list[plan.Row] = []
    for label, plist, content, what, why in (
        (dispatcher.LAUNCHD_LABEL, dispatcher._plist_path(),
         dispatcher._plist_content, "background sender",
         "scheduled emails go out even after you close this terminal"),
        (fts.LAUNCHD_LABEL, fts._plist_path(), fts._plist_content,
         "nightly index refresh",
         "search inside message bodies stays current, hands-off"),
    ):
        if plist.exists():
            rows.append(plan.Kept(plist, f"{what} already installed"))
            continue
        if _yn(f"install the {what} ({why})?", default=True):
            rows += [plan.WriteFile(plist, content(), mode=0o644),
                     plan.BootstrapAgent(label, plist)]
    return rows


# ---------------------------------------------------------------------- #
# the nightly refresh needs Full Disk Access for ITS python — walk + verify
# ---------------------------------------------------------------------- #

# macOS offers NO API or permission pop-up for Full Disk Access (unlike
# Accessibility/Automation): TCC requires the human to flip the toggle.
# The closest legal thing to "just do it" is opening System Settings on
# the exact pane and revealing the exact binary to drag in — then
# verifying with a real run, because a probe spawned from this terminal
# inherits the terminal's TCC identity and proves nothing about launchd.
_FDA_PANE = ("x-apple.systempreferences:com.apple.preference.security"
             "?Privacy_AllFiles")

_VERIFY_POLL = 2.0     # seconds between launchctl looks
_VERIFY_GRACE = 8.0    # running this long ⇒ crawling, not crashing
_VERIFY_TIMEOUT = 45.0


def _open_in_macos(target: str, reveal: bool = False) -> None:
    """Best-effort `open` — a failed open never derails setup."""
    try:
        subprocess.run(["open", "-R", target] if reveal
                       else ["open", target],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001 — cosmetic helper
        pass


def _service_dump(tgt: str) -> str | None:
    """One launchctl print, or None when there is nothing to judge
    (launchctl unavailable, agent not loaded, not a service dump)."""
    try:
        r = plan._launchctl("print", tgt)
    except Exception:  # noqa: BLE001 — non-macOS CI
        return None
    if r.returncode != 0 or "state = " not in (r.stdout or ""):
        return None
    return r.stdout


def _dump_int(dump: str, pattern: str) -> int | None:
    m = re.search(pattern, dump)
    return int(m.group(1)) if m else None


def _verify_fts_agent() -> bool | None:
    """Ground truth on the nightly refresh: kick it once and judge ONLY
    fresh evidence. Freshness is either watching the run happen or
    launchd's `runs` counter advancing past its pre-kick value — a
    `last exit code` read without one of those belongs to a PREVIOUS
    run (a loaded machine's slow spawn once got last night's exit 0
    presented as today's verdict). True = the kicked run exited 0 or is
    visibly crawling (an FDA denial dies in under a second, so
    surviving the grace period proves disk access). False = the kicked
    run died. None = cannot judge here — never guessed."""
    from . import fts

    tgt = f"gui/{os.getuid()}/{fts.LAUNCHD_LABEL}"
    before = _service_dump(tgt)
    if before is None:
        return None
    runs0 = _dump_int(before, r"\bruns = (\d+)")
    try:
        if plan._launchctl("kickstart", "-k", tgt).returncode != 0:
            return None
    except Exception:  # noqa: BLE001 — non-macOS CI
        return None
    ran = 0.0
    deadline = time.monotonic() + _VERIFY_TIMEOUT
    while time.monotonic() < deadline:
        dump = _service_dump(tgt)
        if dump is None:
            return None
        if "state = running" in dump:
            ran += _VERIFY_POLL
            if ran >= _VERIFY_GRACE:
                return True
        else:
            runs = _dump_int(dump, r"\bruns = (\d+)")
            code = _dump_int(dump, r"last exit code = (-?\d+)")
            fresh = ran > 0 or (runs is not None and runs0 is not None
                                and runs > runs0)
            if fresh and code is not None:
                return code == 0
        time.sleep(_VERIFY_POLL)
    return None


# The dead run's own log is the diagnosis — never an assumption. The
# markers mirror what the failing lanes actually print: TCC denials
# raise PermissionError / "Operation not permitted"; a damaged index
# raises out of sqlite3.
_FDA_MARKERS = ("PermissionError", "Operation not permitted")
_INDEX_MARKERS = ("sqlite3.", "database disk image is malformed",
                  "file is not a database")


def _log_tail(log: Path, size: int = 4096) -> str:
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _diagnose_fts_failure(log: Path) -> str:
    """'fda' | 'index' | 'unknown', judged by the LAST marker in the
    log's tail — the log accumulates runs, and an already-granted FDA
    denial from last week must not outvote today's sqlite error."""
    tail = _log_tail(log)

    def last(markers: tuple[str, ...]) -> int:
        return max((tail.rfind(m) for m in markers), default=-1)

    fda, index = last(_FDA_MARKERS), last(_INDEX_MARKERS)
    if fda < 0 and index < 0:
        return "unknown"
    return "fda" if fda >= index else "index"


def _fts_agent_aftercare() -> None:
    """Setup must never end with a nightly refresh that silently dies
    every night (first user: agent installed 2026-08-01, PermissionError
    at every 03:30 since). Verify with a real run; on failure the dead
    run's own log names the cause: a TCC denial gets the hands-on Full
    Disk Access walk and a re-verify; a damaged index gets the rebuild
    remedy; anything else gets its log — the FDA walk is never
    prescribed for a failure that is not an FDA failure (it used to
    loop a damaged-index machine through System Settings until the only
    green exit was uninstalling a perfectly healthy agent)."""
    from . import fts

    if not fts._plist_path().exists():
        return
    attempt = 0
    while True:
        print("\nchecking the nightly index refresh actually runs "
              "(takes a few seconds)...")
        verdict = _verify_fts_agent()
        if verdict is None:
            print("could not verify from here — `email-mcp doctor` will "
                  "flag it if it misbehaves.")
            return
        if verdict:
            print("nightly refresh verified — it runs with disk access.")
            return
        log = fts._log_path()
        cause = _diagnose_fts_failure(log)
        if cause == "index":
            print("the refresh died on a damaged body-search index — not "
                  "a permissions problem. Rebuild it with:")
            print("  python -m email_mcp.fts --rebuild")
            print("the nightly agent stays installed and will succeed "
                  "once the index is rebuilt.")
            return
        if cause == "unknown":
            print("the refresh died; its log has the details:")
            print(f"  {log}")
            print("the agent stays installed — `email-mcp doctor` keeps "
                  "flagging it until the cause is fixed.")
            return
        attempt += 1
        py = Path(sys.executable).resolve()
        print("the refresh died on its first run — its log shows macOS "
              "blocking background programs from reading Mail until you "
              "grant Full Disk Access to this exact program:")
        print(f"  {py}")
        print("there is no pop-up for this one; it is a one-time toggle:")
        print("  1) a Finder window opens with the program selected")
        print("  2) System Settings opens on Privacy & Security -> Full "
              "Disk Access")
        print("  3) drag the program from Finder into that list and "
              "switch it ON")
        if attempt > 1:
            print(f"  still failing — the log may say more: {log}")
        _open_in_macos(str(py), reveal=True)
        _open_in_macos(_FDA_PANE)
        ans = input("press Enter here once granted, and I re-check "
                    "(or type 'skip' to remove the nightly refresh — "
                    "re-add it any time with `email-mcp setup`): ")
        if ans.strip().lower() in {"s", "skip"}:
            print(fts.uninstall_launchd())
            return


def _print_client_config() -> None:
    cfg = {"mcpServers": {"apple-mail": {
        "command": sys.executable, "args": ["-m", "email_mcp.server"]}}}
    print("\nMCP client config (claude mcp add-json / "
          "claude_desktop_config.json):")
    print(json.dumps(cfg, indent=2))


def _smoke() -> None:
    from . import doctor

    print("\nsmoke test (doctor):")
    report = doctor.run()
    for line in doctor.render(report, indent="  "):
        print(line)
    if not report["ok"]:
        print("  => NOT ready — fix the FAIL lines and re-run doctor")
        return
    warns = sum(1 for c in {**report["checks"],
                            "audit": report["audit"]}.values()
                if not c["ok"] and c.get("advisory"))
    print(f"  => ready ({warns} warning(s) — none block use)"
          if warns else "  => ready")


def setup(*, yes: bool = False) -> int:
    """First run: adopt the state root, run the create-checks forward
    (meta stamp rides there), prompt the optional pieces (identity,
    launchd agents, FTS build) in plain words with the right answer as
    the default, verify the nightly refresh actually runs (guiding the
    Full Disk Access grant when it does not), print the MCP client
    config, smoke-test. Non-interactive (--yes) takes no answers, so it
    skips every optional piece and just adopts + stamps + repairs."""
    r = state.State.resolve()
    if isinstance(r, state.Refused):
        print(f"cannot set up: {r.reason}")
        return 1
    # Adoption is a BUILD effect here, deliberately — unlike --fix/update,
    # where it rides the plan. Setup is the create verb: the registry
    # reads an absent root as healthy (doctor stays quiet on machines
    # that never installed), so its findings — the meta stamp above all —
    # exist only against the adopted tree. There is no dry-run or typed
    # confirmation on this path for the ordering to betray.
    writer = r.adopt()  # the one effectful door: root + marker exist now
    # Materialize every managed leaf (0700, accessor-verified):
    _ = (writer.spool, writer.plans, writer.graph, writer.fts, writer.audit)
    rows = checks.plan_fix()  # the create-checks run forward
    if not yes:
        rows += _identity_rows()
        rows += _agent_rows()
    code = confirm_and_run(rows, verb="setup", yes=True)
    if not yes:
        from . import fts

        index_state = fts.status().get("state")
        if index_state == "ready":
            print("body-search index already built — kept.")
        elif index_state == "error":
            print("body-search index is damaged — rebuild with: "
                  "python -m email_mcp.fts --rebuild")
        elif _yn("build the body-search index now (lets Claude search "
                 "inside message bodies; the first crawl can take a "
                 "while and resumes if interrupted)?", default=True):
            try:
                print(f"fts build: {fts.FtsIndex().build()}")
            except Exception as e:
                # The build is OPTIONAL; its failure is a finding, not the
                # end of setup. Unguarded, it took the client config and
                # the smoke test down with it — the two things that would
                # have named the actual problem in plain words (first
                # user, 2026-08-01: no Full Disk Access → raw
                # PermissionError traceback, half-configured machine,
                # zero diagnosis).
                print(f"fts build FAILED: {e}")
                print("  setup continues — build later with: "
                      "python -m email_mcp.fts --build")
        _fts_agent_aftercare()
    _print_client_config()
    _smoke()
    return code
