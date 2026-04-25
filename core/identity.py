"""Shared user identity helpers.

The product has one canonical owner identity for saved owner data, regardless
of whether the owner is using web, CLI, or Telegram. Telegram chat ids remain
delivery addresses and permission subjects.
"""

from __future__ import annotations

OWNER_USER_ID = "owner"
CLI_USER_ID = OWNER_USER_ID
WEB_USER_ID = OWNER_USER_ID


def telegram_owner_id() -> str | None:
    try:
        from core.telegram_state import OWNER_ID
    except Exception:
        return None
    return str(OWNER_ID) if OWNER_ID else None


def is_telegram_owner(user_id) -> bool:
    owner = telegram_owner_id()
    return bool(owner and user_id is not None and str(user_id) == owner)


def chat_owner_id(user_id=None, *, owner_surface: bool = False) -> str:
    """Return the owner id used for saved chats.

    Web and CLI pass owner_surface=True. Telegram owner chat ids also map to
    OWNER_USER_ID. Non-owner Telegram users keep their own isolated chat scope.
    """
    if owner_surface or user_id is None or is_telegram_owner(user_id):
        return OWNER_USER_ID
    return str(user_id)
