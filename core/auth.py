"""Shared auth state — tracks who is currently talking and who the owner is.

Tools that should only run for the owner (e.g. email) call is_owner_active()
before doing anything. The telegram bridge sets the owner at startup and
updates the active user each turn.
"""

_state = {"active_user": None, "owner": None}


def set_active_user(chat_id):
    _state["active_user"] = str(chat_id) if chat_id is not None else None


def set_owner(chat_id):
    _state["owner"] = str(chat_id) if chat_id is not None else None


def get_active_user():
    return _state["active_user"]


def get_owner():
    return _state["owner"]


def is_owner_active():
    """True if the current active user is the owner. False otherwise."""
    owner = _state["owner"]
    active = _state["active_user"]
    return owner is not None and active is not None and owner == active
