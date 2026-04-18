"""Track which interactive surface the current turn is serving."""

from __future__ import annotations


_state = {
    "surface": None,
    "user_id": None,
}


def set_interface(surface, user_id=None):
    _state["surface"] = str(surface) if surface else None
    _state["user_id"] = str(user_id) if user_id is not None else None


def get_interface():
    return _state["surface"]


def get_interface_user():
    return _state["user_id"]

