"""Per-turn interface context.

Uses contextvars so concurrent turns from different surfaces (web, Telegram,
CLI, future connectors) don't race on shared module state.
"""

from __future__ import annotations

from contextvars import ContextVar


_surface: ContextVar[str | None] = ContextVar("lumakit_surface", default=None)
_user_id: ContextVar[str | None] = ContextVar("lumakit_surface_user", default=None)


def set_interface(surface, user_id=None):
    _surface.set(str(surface) if surface else None)
    _user_id.set(str(user_id) if user_id is not None else None)


def get_interface():
    return _surface.get()


def get_interface_user():
    return _user_id.get()
