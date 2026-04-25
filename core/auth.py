"""Shared auth state.

`owner` is process-wide (set once at startup). `active_user` is per-turn —
stored in a ContextVar so concurrent turns from different surfaces stay
isolated.
"""

from __future__ import annotations

from contextvars import ContextVar

from core.identity import OWNER_USER_ID


_active_user: ContextVar[str | None] = ContextVar("lumakit_active_user", default=None)
_owner = {"value": None}


def set_active_user(chat_id):
    _active_user.set(str(chat_id) if chat_id is not None else None)


def set_owner(chat_id):
    _owner["value"] = str(chat_id) if chat_id is not None else None


def get_active_user():
    return _active_user.get()


def get_owner():
    return _owner["value"]


def is_owner_active():
    """True if the current active user is the owner. False otherwise."""
    owner = _owner["value"]
    active = _active_user.get()
    if active is None:
        return False
    if active == OWNER_USER_ID:
        return True
    return owner is not None and owner == active
