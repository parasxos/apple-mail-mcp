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
import sys
from pathlib import Path

from . import checks, config, plan, state

_UPDATE_CHECKS = frozenset({checks.META_VERSION, checks.PLIST_DRIFT})


def confirm_and_run(rows: list[plan.Row], *, verb: str, yes: bool = False,
                    dry_run: bool = False) -> int:
    """THE gate for every destructive verb. Renders the plan; dry-run stops
    there (render is pure, so a dry run provably touches nothing); a plan
    with no Action rows has nothing to confirm; otherwise the typed verb
    (or --yes) releases execute, and the exit code is a fold over the same
    results the report renders from."""
    print(plan.render(rows))
    if dry_run:
        return 0
    actions = [r for r in rows if isinstance(r, plan.Action)]
    if not actions:
        print("nothing to do.")
        return 0
    if not yes:
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
        return [plan.Kept(path, "existing identities kept")]
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
    return [plan.WriteFile(path, "\n".join(lines) + "\n", mode=0o600,
                           backup=True)]


def _agent_rows() -> list[plan.Row]:
    from . import dispatcher, fts

    rows: list[plan.Row] = []
    for label, plist, content, what, default in (
        (dispatcher.LAUNCHD_LABEL, dispatcher._plist_path(),
         dispatcher._plist_content,
         "scheduled-send dispatcher (every 60s)", True),
        (fts.LAUNCHD_LABEL, fts._plist_path(), fts._plist_content,
         "nightly FTS sync (03:30)", False),
    ):
        if _yn(f"install the {what} launchd agent?", default=default):
            rows += [plan.WriteFile(plist, content(), mode=0o644),
                     plan.BootstrapAgent(label, plist)]
    return rows


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
    print("  => ready" if report["ok"]
          else "  => NOT ready — fix the FAIL lines and re-run doctor")


def setup(*, yes: bool = False) -> int:
    """First run: adopt the state root, run the create-checks forward
    (meta stamp rides there), prompt the optional pieces (identity,
    launchd agents, FTS build), print the MCP client config, smoke-test.
    Non-interactive (--yes) takes no answers, so it skips every optional
    piece and just adopts + stamps + repairs."""
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
    if not yes and _yn("build the FTS body index now (first crawl can "
                       "take a while)?"):
        from . import fts

        try:
            print(f"fts build: {fts.FtsIndex().build()}")
        except Exception as e:
            # The build is OPTIONAL; its failure is a finding, not the end
            # of setup. Unguarded, it took the client config and the smoke
            # test down with it — the two things that would have named the
            # actual problem in plain words (first user, 2026-08-01: no
            # Full Disk Access → raw PermissionError traceback, half-
            # configured machine, zero diagnosis).
            print(f"fts build FAILED: {e}")
            print("  setup continues — build later with: "
                  "python -m email_mcp.fts --build")
    _print_client_config()
    _smoke()
    return code
