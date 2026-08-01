"""The managed state tree (~/.email-mcp) as a capability, not a convention.

Concept primitive 1 of docs/v0.11-clean-concept.md — every path policy for
the state root lives HERE, once:

    State.resolve() -> Resolved | Refused(reason)   # pure and total
    Resolved.reader() -> StateReader                # path questions only
    Resolved.adopt()  -> StateWriter                # the ONE effectful door

``Refused(reason)`` is a value: doctor renders it, the server refuses on
it, the CLI prints it — always the same string. Call sites that must raise
get that same string as ``RefusedError`` (an OSError) via
``Refused.reader()`` / ``Refused.adopt()``, so ``State.resolve().adopt()``
is total-or-raise at every write seam.

Layout under the root: the ``.email-mcp-root`` marker (0600), then the
leaves ``spool/`` (+ its five state dirs), ``plans/``, ``graph/``,
``fts/``, ``audit/`` — all 0700: the tree holds message bodies, recipients
and token caches. Leaves are created on demand by StateWriter accessors
and independently: a broken audit leaf refuses audit writes without
touching the spool (contract §6 — an unwritable ledger never blocks mail).

The five v0.10 per-directory overrides (EMAIL_MCP_SPOOL_DIR /
PLANS_DIR / GRAPH_DIR / FTS_DIR / AUDIT_DIR) are retired: when set,
resolution refuses with a migration message — rejected, never silently
ignored.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "EMAIL_MCP_STATE_DIR"
MARKER = ".email-mcp-root"
_MARKER_BYTES = b"email-mcp state root\n"

RETIRED_VARS = (
    "EMAIL_MCP_SPOOL_DIR",
    "EMAIL_MCP_PLANS_DIR",
    "EMAIL_MCP_GRAPH_DIR",
    "EMAIL_MCP_FTS_DIR",
    "EMAIL_MCP_AUDIT_DIR",
)

SPOOL_STATES = ("pending", "sending", "sent", "failed", "cancelled")


class RefusedError(OSError):
    """A Refused(reason) carried as an exception — str(e) IS the reason."""


@dataclass(frozen=True)
class Refused:
    """Resolution or adoption declined; ``reason`` is the one policy string
    every surface shows. reader()/adopt() raise it, so exception-shaped
    call sites can chain ``State.resolve().adopt()`` without isinstance
    ceremony while value consumers keep the dataclass."""

    reason: str

    def reader(self) -> "StateReader":
        raise RefusedError(self.reason)

    def adopt(self) -> "StateWriter":
        raise RefusedError(self.reason)


@dataclass(frozen=True)
class StateReader:
    """Pure path questions about the managed tree. There is nothing
    effectful to call here — a read path holding a StateReader CANNOT
    create state, so 'reads never create' is a property of the type, not
    a create=False flag someone can forget."""

    root: Path

    @property
    def marker(self) -> Path:
        return self.root / MARKER

    @property
    def spool(self) -> Path:
        return self.root / "spool"

    def spool_state(self, name: str) -> Path:
        return self.root / "spool" / name

    @property
    def plans(self) -> Path:
        return self.root / "plans"

    @property
    def graph(self) -> Path:
        return self.root / "graph"

    @property
    def fts(self) -> Path:
        return self.root / "fts"

    @property
    def audit(self) -> Path:
        return self.root / "audit"


@dataclass(frozen=True)
class StateWriter:
    """Proof of adoption plus the leaf-creating accessors: asking a writer
    for a leaf returns a directory that exists — 0700, a real directory.
    A symlinked or squatted leaf raises RefusedError. Only adoption hands
    these out, so all filesystem effects for the tree live behind it."""

    root: Path

    @property
    def spool(self) -> Path:
        d = _ensure_dir(self.root / "spool")
        for name in SPOOL_STATES:
            _ensure_dir(d / name)
        return d

    @property
    def plans(self) -> Path:
        return _ensure_dir(self.root / "plans")

    @property
    def graph(self) -> Path:
        return _ensure_dir(self.root / "graph")

    @property
    def fts(self) -> Path:
        return _ensure_dir(self.root / "fts")

    @property
    def audit(self) -> Path:
        return _ensure_dir(self.root / "audit")


@dataclass(frozen=True)
class Resolved:
    """A root the policy accepts. reader() asks path questions; adopt() is
    the one door through which the tree comes to exist."""

    root: Path

    def reader(self) -> StateReader:
        return StateReader(self.root)

    def adopt(self) -> StateWriter:
        """Make the root real: mkdir 0700 with parents=False (a missing
        parent was already a resolve() refusal), chmod 0700 (mkdir modes
        are umask-clipped), then the 0600 marker — created O_EXCL and
        O_NOFOLLOW, never through a symlink. Idempotent and race-safe:
        losing a concurrent adoption is success. Raises RefusedError."""
        root = self.root
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
        except OSError as e:
            raise RefusedError(f"cannot create state root {root}: {e}") from None
        try:
            os.chmod(root, 0o700)
        except OSError as e:
            raise RefusedError(
                f"cannot secure state root {root} (chmod 0700): {e}"
            ) from None
        _ensure_marker(root / MARKER)
        return StateWriter(root)


class State:
    """Namespace for the single resolution entry point (concept §1)."""

    @staticmethod
    def resolve() -> Resolved | Refused:
        """Resolve EMAIL_MCP_STATE_DIR (default ~/.email-mcp), once.

        Pure and total: reads env + filesystem, changes nothing, never
        raises. The whole refusal policy lives in this body: retired
        per-directory vars; an unresolvable $HOME or override; a root
        that is $HOME or an ancestor of it (by inode identity, never
        path spelling); a non-directory or dangling symlink squatting on
        the root; a missing or unwritable parent; an unlistable existing
        root; an explicit override naming a non-empty directory without
        the marker. The default root is exempt from the content check
        only — adopting an unmarked ~/.email-mcp is the v0.10 upgrade
        path.
        """
        retired = [v for v in RETIRED_VARS if os.environ.get(v, "").strip()]
        if retired:
            return Refused(
                f"{', '.join(retired)} is retired — unset it and set "
                f"{ENV_VAR} to the parent state directory instead "
                f"(default ~/.email-mcp)."
            )

        try:
            home = Path.home()
        except RuntimeError as e:
            return Refused(f"cannot resolve the home directory: {e}")

        raw = os.environ.get(ENV_VAR, "").strip()
        explicit = bool(raw)
        if explicit:
            try:
                root = Path(raw).expanduser()
            except RuntimeError as e:
                return Refused(f"{ENV_VAR}={raw!r} cannot be expanded: {e}")
            if not root.is_absolute():
                try:
                    root = Path(os.getcwd()) / root
                except OSError:
                    return Refused(
                        f"{ENV_VAR}={raw!r} is relative and the working "
                        f"directory is gone — use an absolute path."
                    )
        else:
            root = home / ".email-mcp"

        try:
            lst = os.lstat(root)
        except (FileNotFoundError, NotADirectoryError):
            lst = None
        except OSError as e:  # ENAMETOOLONG, EACCES, ELOOP, ...
            return Refused(f"cannot stat {root}: {e}")

        if lst is not None and stat.S_ISLNK(lst.st_mode):
            try:
                st = os.stat(root)
            except OSError as e:
                return Refused(
                    f"{root} is a symlink that cannot be followed ({e}) — "
                    f"point {ENV_VAR} at a real directory."
                )
        else:
            st = lst

        if st is not None and not stat.S_ISDIR(st.st_mode):
            return Refused(
                f"{root} exists but is not a directory — move it aside or "
                f"point {ENV_VAR} elsewhere."
            )

        if st is not None:
            ident = (st.st_dev, st.st_ino)
            p = home
            while True:
                try:
                    hst = os.stat(p)
                except FileNotFoundError:
                    hst = None  # a missing ancestor cannot be the root
                except OSError as e:
                    return Refused(
                        f"cannot stat {p} while checking the root against "
                        f"the home directory: {e}"
                    )
                if hst is not None and (hst.st_dev, hst.st_ino) == ident:
                    return Refused(
                        f"{root} is your home directory or an ancestor of "
                        f"it — refusing to manage it; point {ENV_VAR} at a "
                        f"dedicated directory."
                    )
                if p == p.parent:
                    break
                p = p.parent

        if st is None:
            parent = root.parent
            try:
                pst = os.stat(parent)
            except OSError as e:
                return Refused(
                    f"{root} cannot be created: parent {parent} is not "
                    f"usable ({e})."
                )
            if not stat.S_ISDIR(pst.st_mode):
                return Refused(
                    f"{root} cannot be created: {parent} is not a directory."
                )
            if not os.access(parent, os.W_OK | os.X_OK):
                return Refused(
                    f"{root} cannot be created: {parent} is not writable."
                )
        else:
            try:
                names = os.listdir(root)  # listdir raises; glob would swallow
            except OSError as e:
                return Refused(f"{root} exists but cannot be listed: {e}")
            if explicit and names and MARKER not in names:
                return Refused(
                    f"{root} is not empty and has no {MARKER} marker — "
                    f"refusing to adopt a foreign directory; empty it or "
                    f"point {ENV_VAR} at a dedicated one."
                )

        return Resolved(root)


# ---------------------------------------------------------------------- #
# the effects (used only by adoption and the writer accessors)            #
# ---------------------------------------------------------------------- #


def _ensure_marker(marker: Path) -> None:
    try:
        lst = os.lstat(marker)
    except FileNotFoundError:
        lst = None
    except OSError as e:
        raise RefusedError(f"cannot stat {marker}: {e}") from None
    if lst is None:
        try:
            fd = os.open(
                marker,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            return _ensure_marker(marker)  # lost the adoption race — reverify
        except OSError as e:
            raise RefusedError(f"cannot write {marker}: {e}") from None
        try:
            os.write(fd, _MARKER_BYTES)
        finally:
            os.close(fd)
    elif stat.S_ISLNK(lst.st_mode):
        raise RefusedError(
            f"{marker} is a symlink — refusing to write the state marker "
            f"through a link."
        )
    elif not stat.S_ISREG(lst.st_mode):
        raise RefusedError(f"{marker} exists but is not a regular file.")
    try:
        os.chmod(marker, 0o600)  # O_CREAT modes are umask-clipped
    except OSError as e:
        raise RefusedError(f"cannot secure {marker} (chmod 0600): {e}") from None


def _ensure_dir(path: Path) -> Path:
    """Create-or-verify ONE managed directory: a real dir at 0700 (an owned
    chmod accident self-heals); symlinks and squatting files refuse."""
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            return _ensure_dir(path)  # lost a race — verify what won it
        except OSError as e:
            raise RefusedError(f"cannot create {path}: {e}") from None
        lst = None
    except OSError as e:
        raise RefusedError(f"cannot stat {path}: {e}") from None
    if lst is not None:
        if stat.S_ISLNK(lst.st_mode):
            raise RefusedError(
                f"{path} is a symlink — the managed state tree must be "
                f"real directories."
            )
        if not stat.S_ISDIR(lst.st_mode):
            raise RefusedError(
                f"{path} exists but is not a directory — move it aside."
            )
    try:
        os.chmod(path, 0o700)
    except OSError as e:
        raise RefusedError(f"cannot secure {path} (chmod 0700): {e}") from None
    return path
