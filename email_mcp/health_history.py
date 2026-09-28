"""Bounded observations in an existing, private managed state root."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from datetime import datetime

from . import state

MAX_BYTES = 65536
MAX_HOSTS = 16
MAX_TRANSITIONS = 12


def _private(fd: int, *, directory: bool = False) -> None:
    info = os.fstat(fd)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError("health history requires private, owned files")


def _read(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=directory)
    try:
        _private(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("health history is too large")
        return raw
    finally:
        os.close(fd)


def _history(directory: int) -> dict:
    payload = json.loads(_read(directory, "health.json"))
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int
            or payload["version"] != 1
            or not isinstance(payload.get("hosts"), dict)):
        raise ValueError("invalid health history")
    hosts = payload["hosts"]
    if len(hosts) > MAX_HOSTS or any(not isinstance(items, list)
           or len(items) > MAX_TRANSITIONS
           or any(not isinstance(item, dict)
                  or item.get("status") not in {"readable", "unavailable"}
                  or not all(isinstance(item.get(name), str) for name in (
                      "reason", "checked_at", "first_observed_at"))
                  or not isinstance(item.get("host"), dict)
                  or not all(isinstance(value, str) and len(value) <= 1024
                             for value in item["host"].values())
                  for item in items)
           for items in hosts.values()):
        raise ValueError("invalid health observations")
    for items in hosts.values():
        for item in items:
            for name in ("checked_at", "first_observed_at"):
                stamp = datetime.fromisoformat(item[name])
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError("health timestamps require a timezone")
    return hosts


def _recent(hosts: dict) -> list[dict]:
    return [items[-1] for items in list(hosts.values())[-MAX_HOSTS:] if items]


def read(key: str) -> tuple[list[dict], bool, list[dict]]:
    try:
        root = state.State.resolve().reader().root
        with state.open_directory(root, root) as directory:
            _private(directory, directory=True)
            if _read(directory, state.MARKER) != state._MARKER_BYTES:
                return [], False, []
            hosts = _history(directory)
            return hosts.get(key, []), True, _recent(hosts)
    except (OSError, ValueError, TypeError, RecursionError):
        return [], False, []


def observe(key: str, observation: dict) -> tuple[list[dict], bool, list[dict]]:
    """Append a transition without adopting a root or following child links."""
    try:
        root = state.State.resolve().reader().root
        with state.open_directory(root, root) as directory:
            _private(directory, directory=True)
            if _read(directory, state.MARKER) != state._MARKER_BYTES:
                return [], False, []
            lock = os.open("health.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                           | os.O_NONBLOCK, 0o600, dir_fd=directory)
            try:
                _private(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    hosts = _history(directory)
                except FileNotFoundError:
                    hosts = {}
                previous = hosts.get(key, [])
                transition = {name: observation[name] for name in (
                    "status", "reason", "checked_at", "host")}
                transition["first_observed_at"] = transition["checked_at"]
                if not previous or any(previous[-1].get(name) != transition[name]
                                       for name in ("status", "reason")):
                    previous = [*previous, transition][-MAX_TRANSITIONS:]
                else:
                    transition["first_observed_at"] = previous[-1]["first_observed_at"]
                    previous = [*previous[:-1], transition]
                hosts.pop(key, None)
                hosts[key] = previous
                hosts = dict(list(hosts.items())[-MAX_HOSTS:])
                payload = {"version": 1, "hosts": hosts}
                raw = json.dumps(payload, ensure_ascii=False).encode()
                if len(raw) > MAX_BYTES:
                    return [], False, []
                temporary = f".health-{uuid.uuid4().hex}.tmp"
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, "health.json", src_dir_fd=directory,
                               dst_dir_fd=directory)
                    os.fsync(directory)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=directory)
                    except FileNotFoundError:
                        pass
                return previous, True, _recent(hosts)
            finally:
                os.close(lock)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return [], False, []
