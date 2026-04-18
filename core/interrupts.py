"""Per-run interrupt helpers for cooperative cancellation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar


class OperationInterrupted(Exception):
    """Raised when the active run was interrupted by the user."""


_checker_var: ContextVar = ContextVar("interrupt_checker", default=None)
_request_var: ContextVar = ContextVar("interrupt_requester", default=None)


@contextmanager
def interrupt_context(checker=None, requester=None):
    """Install interrupt callbacks for the current execution context."""
    checker_token = _checker_var.set(checker)
    request_token = _request_var.set(requester)
    try:
        yield
    finally:
        _checker_var.reset(checker_token)
        _request_var.reset(request_token)


def interrupted() -> bool:
    """Return True when the current run has been asked to stop."""
    checker = _checker_var.get()
    if not checker:
        return False
    try:
        return bool(checker())
    except Exception:
        return False


def request_interrupt() -> bool:
    """Mark the current run as interrupted if a requester is installed."""
    requester = _request_var.get()
    if not requester:
        return False
    try:
        requester()
        return True
    except Exception:
        return False


def raise_if_interrupted(message: str = "Interrupted by /stop.") -> None:
    """Raise OperationInterrupted when the current run has been stopped."""
    if interrupted():
        raise OperationInterrupted(message)
