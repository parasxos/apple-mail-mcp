"""Mailbox-source adapter and lazy source registry."""
from __future__ import annotations

from threading import Lock
from typing import Callable

from .. import config
from ..domain.mail import EmailSource
from ..sources import get_source


class LazySourceProvider:
    def __init__(self, factory: Callable[[], EmailSource] | None = None):
        self._factory = factory or (lambda: get_source(config.source_name()))
        self._source: EmailSource | None = None
        self._lock = Lock()

    def get(self) -> EmailSource:
        if self._source is None:
            with self._lock:
                if self._source is None:
                    self._source = self._factory()
        return self._source


class FixedSourceProvider:
    """Explicit source injection for alternate front ends and tests."""

    def __init__(self, source: EmailSource):
        self._source = source

    def get(self) -> EmailSource:
        return self._source
