"""Which work goes first: scans and Paperless uploads before stacks.

A worker marks the file it processes; the OCR job budget and the Mistral
request throttle read the mark. Thread pools don't carry it over by
themselves, so their tasks are wrapped with `keep`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, TypeVar

T = TypeVar("T")

_PRIORITY: ContextVar[bool] = ContextVar("priority", default=False)


def current() -> bool:
    return _PRIORITY.get()


@contextmanager
def marked(priority: bool):
    token = _PRIORITY.set(priority)
    try:
        yield
    finally:
        _PRIORITY.reset(token)


def keep(fn: Callable[..., T]) -> Callable[..., T]:
    """`fn` for a thread pool, running with the caller's priority."""
    priority = current()

    def run(*args, **kwargs) -> T:
        with marked(priority):
            return fn(*args, **kwargs)

    return run
