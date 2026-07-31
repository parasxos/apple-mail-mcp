"""Command-line front door: ``email-mcp [subcommand]`` dispatch — v0.11 L2.

The console script points here (``email_mcp.cli:main``) since v0.11.
Three dispatch rules, applied in order and frozen by the lifecycle
design:

1. **Bare invocation serves.** ``email-mcp`` with no arguments starts
   the MCP stdio server exactly like v0.10's ``server:main`` entry point
   did — existing registrations (``~/.claude.json`` pointing at
   ``.venv/bin/email-mcp``) survive the upgrade untouched. This path
   writes NOTHING to stdout before ``mcp.run()``: stdout is the MCP wire
   (contract §7).
2. **A leading dash forwards to the server CLI.** ``email-mcp --doctor``
   / ``--selftest`` / ``--send-test`` / ``--transport-check`` (the whole
   legacy flag surface of ``server.main()``) behave exactly as before;
   the full argv is forwarded verbatim.
3. **Otherwise the first token is a subcommand** from ``COMMANDS``;
   anything else is an argparse error (exit 2) listing the valid names.

``audit``/``fts``/``graph`` delegate to their module ``main(argv)`` with
the remaining argv verbatim; ``dispatcher`` and ``serve`` go through a
``sys.argv`` shim because their targets parse ``sys.argv`` themselves.
``setup``/``update``/``uninstall`` delegate to the lifecycle module
(L3/L4). Printing here is by design
— this module is a CLI entry point, not library code; contract §7 lists
the print-by-design entry points and the serve path stays silent.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable


# --------------------------------------------------------------------- #
# serve + legacy forwarding (sys.argv shims)                             #
# --------------------------------------------------------------------- #


def _shim_argv(rest: list[str], fn: Callable[[], int]) -> int:
    """Call ``fn()`` (which parses sys.argv itself) as if the process had
    been invoked with exactly ``rest`` after the program name."""
    old = sys.argv
    sys.argv = [old[0] if old else "email-mcp", *rest]
    try:
        return fn()
    finally:
        sys.argv = old


def _run_server(rest: list[str]) -> int:
    """Start ``server.main()`` — the MCP stdio server when ``rest`` is
    empty, the legacy flag surface otherwise. Never prints: on the serve
    path stdout belongs to the wire from here on."""
    from . import server  # late: keep bare startup lean

    return _shim_argv(rest, server.main)


def cmd_serve(rest: list[str]) -> int:
    argparse.ArgumentParser(
        prog="email-mcp serve",
        description="Run the MCP stdio server (same as bare `email-mcp`).",
    ).parse_args(rest)
    return _run_server([])


# --------------------------------------------------------------------- #
# doctor                                                                 #
# --------------------------------------------------------------------- #


def _render_doctor(report: dict) -> None:
    """Human-readable doctor report: one line per check, summary last."""
    for name, check in report.get("checks", {}).items():
        ok = bool(check.get("ok"))
        line = f"{'ok  ' if ok else 'FAIL'} {name}"
        if not ok:
            why = check.get("error") or check.get("detail")
            if why:
                line += f" — {why}"
        print(line)
    verdict = "green" if report.get("ok") else "RED"
    suffix = " (read-only)" if report.get("read_only") else ""
    print(f"doctor: {verdict}{suffix}")


def _render_fixes(before: dict, fixes: dict, after: dict) -> None:
    """Human-readable --fix account: per-repair finding -> action, framed
    by the doctor verdict before and after the pass."""
    print(f"doctor before: {'green' if before.get('ok') else 'RED'}")
    for a in fixes["applied"]:
        print(f"fixed  {a['repair']}: {a['finding']} -> {a['action']}")
    for f in fixes["failed"]:
        print(f"FAILED {f['repair']}: {f.get('finding')} -> {f['error']}")
    for s in fixes["skipped"]:
        if s.get("reason") != "healthy":
            print(f"skip   {s['repair']}: {s['reason']}")
    would = [s for s in fixes["skipped"] if s.get("reason") != "healthy"]
    if not (fixes["applied"] or fixes["failed"] or would):
        print("nothing to fix")
    _render_doctor(after)


def cmd_doctor(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="email-mcp doctor",
        description="Run every diagnostic check; exit 0 only when all ok.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="run the whitelisted safe repairs first, then re-run the "
             "doctor (exit reflects the re-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="with --fix: report what WOULD be repaired and touch nothing",
    )
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args(rest)
    if args.dry_run and not args.fix:
        parser.error("--dry-run only makes sense with --fix")

    from . import server  # late: the doctor pulls the whole tool layer

    if not args.fix:
        report = server.tool_doctor()
        if args.json:
            json.dump(report, sys.stdout, indent=2, default=str)
            sys.stdout.write("\n")
        else:
            _render_doctor(report)
        return 0 if report["ok"] else 1

    from . import repairs

    before = server.tool_doctor()
    fixes = repairs.run_fixes(dry_run=args.dry_run)
    # A dry run touches nothing, so there is nothing to re-check: report the
    # same state twice rather than implying a second, changed reading.
    after = before if args.dry_run else server.tool_doctor()
    if args.json:
        json.dump({"before": before, "fixes": fixes, "after": after},
                  sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        _render_fixes(before, fixes, after)
    return 0 if after["ok"] else 1


# --------------------------------------------------------------------- #
# version                                                                #
# --------------------------------------------------------------------- #


def _package_version() -> str:
    """The version of the code actually executing.

    Read straight off the module rather than via importlib.metadata: the
    latter resolves against the *interpreter*, so a stale editable install
    reports its own number while newer source runs — and `lifecycle` stamps
    this value into ~/.email-mcp/meta.json, making the wrong one durable and
    invisible. The module literal travels with the source, so bare checkout,
    `pip install -e .` and `pipx install` all agree (pyproject derives the
    distribution metadata from this same literal — see [tool.setuptools.dynamic]).
    """
    from . import __version__

    return __version__


def _state_version() -> int:
    """The on-disk state schema version: ``~/.email-mcp/meta.json``
    (written by `email-mcp setup`, L3) — 0 when absent or unreadable."""
    meta = Path.home() / ".email-mcp" / "meta.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    value = data.get("state_version", 0) if isinstance(data, dict) else 0
    return value if isinstance(value, int) else 0


def cmd_version(rest: list[str]) -> int:
    argparse.ArgumentParser(
        prog="email-mcp version",
        description="Print the package version and the state version.",
    ).parse_args(rest)
    print(f"email-mcp {_package_version()}")
    print(f"state_version {_state_version()}")
    return 0


# --------------------------------------------------------------------- #
# delegates + lifecycle commands                                         #
# --------------------------------------------------------------------- #


def cmd_audit(rest: list[str]) -> int:
    from . import audit

    return audit.main(rest)


def cmd_fts(rest: list[str]) -> int:
    from . import fts

    return fts.main(rest)


def cmd_graph(rest: list[str]) -> int:
    from . import graph

    return graph.main(rest)


def cmd_dispatcher(rest: list[str]) -> int:
    from . import dispatcher  # its main() parses sys.argv itself

    return _shim_argv(rest, dispatcher.main)


def cmd_setup(rest: list[str]) -> int:
    from . import lifecycle

    return lifecycle.run_setup_cli(rest)


def cmd_update(rest: list[str]) -> int:
    from . import lifecycle

    return lifecycle.run_update_cli(rest)


def cmd_uninstall(rest: list[str]) -> int:
    from . import lifecycle

    return lifecycle.run_uninstall_cli(rest)


# --------------------------------------------------------------------- #
# dispatch                                                               #
# --------------------------------------------------------------------- #

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "serve": cmd_serve,
    "setup": cmd_setup,
    "doctor": cmd_doctor,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
    "audit": cmd_audit,
    "fts": cmd_fts,
    "graph": cmd_graph,
    "dispatcher": cmd_dispatcher,
    "version": cmd_version,
}


def _unknown_command(cmd: str) -> int:
    """argparse-standard error (usage + invalid choice listing COMMANDS,
    exit 2) for a first token that is neither a flag nor a subcommand."""
    parser = argparse.ArgumentParser(
        prog="email-mcp",
        description="Local MCP server for Apple Mail — bare invocation "
                    "serves; see subcommands below.",
    )
    parser.add_argument("command", choices=tuple(COMMANDS))
    parser.parse_args([cmd])  # always errors: SystemExit(2)
    return 2  # unreachable — parse_args above cannot succeed


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        # A retired per-directory variable is refused HERE, before any
        # command runs, so the operator learns on the first invocation
        # rather than from a read command that quietly reports nothing.
        # `doctor` is exempt: its whole job is to explain what is wrong,
        # and it reports the same message in its state_root check.
        from . import config
        retired = config.retired_state_var_error()
        if retired is not None and (not argv or argv[0] != "doctor"):
            print(f"email-mcp: {retired}", file=sys.stderr)
            print("email-mcp: run `email-mcp doctor` for the full report.",
                  file=sys.stderr)
            return 2
        if not argv:
            return _run_server([])
        if argv[0].startswith("-"):
            return _run_server(argv)
        handler = COMMANDS.get(argv[0])
        if handler is None:
            return _unknown_command(argv[0])
        return handler(argv[1:])
    except SystemExit as e:  # argparse exits; keep main() -> int total
        code = e.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
