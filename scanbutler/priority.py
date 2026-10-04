"""Which work goes first: scans and Paperless uploads before stacks.

A worker marks the file it processes; the OCR job budget and the Mistral
request throttle read the mark. Thread pools don't carry it over by
themselves, so their tasks are wrapped with `keep`, which carries the whole
context: the mark, and the file name the log lines are tagged with.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
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
    """`fn` for a thread pool, running in the caller's context (priority, log source)."""
    context = copy_context()

    def run(*args, **kwargs) -> T:
        # One copy per call: a context can't be entered by two threads at once.
        return context.copy().run(fn, *args, **kwargs)

    return run
