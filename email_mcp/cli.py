"""The email-mcp entry point: dispatch and nothing else (concept §5).

Bare invocation serves — the entry-point flip must be invisible to every
MCP client already registered with ``command: email-mcp`` — and
leading-dash argv forwards verbatim to the legacy email_mcp.server flags
(--selftest, --doctor, --send-test, ...). Everything else is a verb
composing the primitives: State.resolve() once at the top; a Refused
reason prints to stderr (stdout may be the MCP wire) and exits 2 —
except ``doctor``, whose job is to report it.
"""
from __future__ import annotations

import argparse
import importlib
import sys

from . import __version__, state

_PASSTHROUGH = ("audit", "fts", "graph", "dispatcher")
_VERBS = ("serve", "setup", "doctor", "update", "uninstall", "version",
          *_PASSTHROUGH)

_USAGE = """\
usage: email-mcp [verb] [options]

  (bare) / serve                 serve MCP over stdio
  setup                          first-run wizard: adopt state, identity,
                                 launchd agents, client config, smoke test
  doctor [--fix] [--dry-run]     report problems; --fix repairs them
  update                         migrate an existing install forward
  uninstall [--purge] [--yes]    agents out, token caches removed;
                                 --purge also removes state + logs
  version                        print the version
  audit|fts|graph|dispatcher ... module CLIs (arguments forwarded)

Leading-dash arguments forward to the legacy server flags
(`email-mcp --help` lists them).
"""


def _doctor(rest: list[str]) -> int:
    """Report first-class: a refused root is a rendered finding, never a
    gate — doctor must work precisely when everything else refuses."""
    parser = argparse.ArgumentParser(prog="email-mcp doctor")
    parser.add_argument("--fix", action="store_true",
                        help="repair the findings")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --fix: preview the repairs, touch nothing")
    args = parser.parse_args(rest)
    if args.dry_run and not args.fix:
        parser.error("--dry-run previews --fix; add --fix")
    from . import checks, doctor, lifecycle

    if args.fix:
        return lifecycle.doctor_fix(dry_run=args.dry_run)
    hits = checks.findings()
    for f in hits:
        print(f"FAIL {f.check}: {f.detail}")
    if any(checks.repairable(f) for f in hits):
        print("run `email-mcp doctor --fix` to repair.")
    report = doctor.run()
    for line in doctor.render(report):
        print(line)
    return 0 if report["ok"] and not hits else 1


def _uninstall(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="email-mcp uninstall")
    parser.add_argument("--purge", action="store_true",
                        help="also remove the state tree and the logs")
    parser.add_argument("--yes", action="store_true",
                        help="skip the typed confirmation")
    args = parser.parse_args(rest)
    from . import lifecycle

    return lifecycle.uninstall(purge=args.purge, yes=args.yes)


def _no_flags(verb: str, rest: list[str]) -> None:
    argparse.ArgumentParser(prog=f"email-mcp {verb}").parse_args(rest)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if argv and argv[0].startswith("-"):
        from . import server

        return server.main(argv)  # legacy flags, verbatim
    verb, rest = (argv[0], argv[1:]) if argv else ("serve", [])
    if verb == "doctor":
        return _doctor(rest)
    if verb not in _VERBS:
        sys.stderr.write(_USAGE)
        return 2

    resolved = state.State.resolve()  # once, at the top of every verb
    if isinstance(resolved, state.Refused):
        print(resolved.reason, file=sys.stderr)
        return 2

    if verb == "serve":
        from . import server

        return server.main(rest)
    if verb == "version":
        _no_flags(verb, rest)
        print(__version__)
        return 0
    if verb == "setup":
        _no_flags(verb, rest)
        from . import lifecycle

        return lifecycle.setup()
    if verb == "update":
        _no_flags(verb, rest)
        from . import lifecycle

        return lifecycle.update()
    if verb == "uninstall":
        return _uninstall(rest)
    return importlib.import_module(f".{verb}", __package__).main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
