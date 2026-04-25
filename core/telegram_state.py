"""Global state and config accessors for the Telegram bridge."""

from __future__ import annotations

import json
import os

from core.paths import get_data_dir
from core.identity import chat_owner_id
from core.telegram_owner_config import load_owner_config, save_owner_config
from core.telegram_user_config import load_user_configs, save_user_configs
from core.chat_store import get_active_chat, load_chat, new_chat_id

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USERS_FILE = str(get_data_dir() / "telegram_users.json")

_raw_ids = os.getenv("TELEGRAM_ALLOWED_IDS", "").strip()
_env_id_list = [id.strip() for id in _raw_ids.split(",") if id.strip()]
_env_ids = set(_env_id_list)


def _load_users_file():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(str(uid) for uid in json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_users_file(ids):
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, USERS_FILE)


_file_ids = _load_users_file()
ALLOWED_IDS = _env_ids | _file_ids
OWNER_ID = _env_id_list[0] if _env_id_list else (sorted(_file_ids)[0] if _file_ids else None)
OWNER_CONFIG = load_owner_config()
USER_CONFIGS = load_user_configs()

# Per-user sessions: {chat_id: {"messages": [...], "chat_id": ..., "title": ..., ...}}
_sessions: dict = {}

# Per-user tool visibility toggle
_show_tools: dict = {}  # {chat_id: bool}

# Tracks which user is currently being served
_active_chat_id: dict = {"value": None}

# Global update offset tracker
_poll_offset: dict = {"value": None}

# Updates peeked mid-run by check_for_stop, drained by the main loop
_pending_updates: list = []

# Unauthorized users who attempted to message — for /adduser
_pending_users: dict = {}  # {chat_id: first_name}


def _save_allowed_ids():
    _save_users_file(ALLOWED_IDS)


def _get_session(chat_id):
    chat_id = str(chat_id)
    owner_id = chat_owner_id(chat_id)
    if chat_id not in _sessions:
        # Cross-surface resume: if this user has an active chat from web/another
        # surface, pick it up here so the conversation feels continuous.
        resumed = None
        active_id = get_active_chat(owner_id)
        if active_id:
            resumed = load_chat(active_id, owner_id=owner_id)
        if resumed:
            _sessions[chat_id] = {
                "chat_id": resumed["id"],
                "title": resumed["title"],
                "first_message_sent": True,
                "messages": resumed["messages"],
            }
        else:
            _sessions[chat_id] = {
                "chat_id": new_chat_id(),
                "title": "",
                "first_message_sent": False,
                "messages": None,
            }
    return _sessions[chat_id]


def _get_user_config(chat_id):
    chat_id = str(chat_id)
    config = USER_CONFIGS.get(chat_id)
    if not config:
        config = {"personality_prompt": "", "voice_replies": False, "voice_name": ""}
        USER_CONFIGS[chat_id] = config
    return config


def _save_user_configs():
    global USER_CONFIGS
    USER_CONFIGS = save_user_configs(USER_CONFIGS)


def _save_owner_config():
    global OWNER_CONFIG
    OWNER_CONFIG = save_owner_config(OWNER_CONFIG)


def _get_user_label(chat_id):
    from core.telegram_api import telegram_api
    chat_id = str(chat_id)
    try:
        result = telegram_api("getChat", {"chat_id": chat_id})
        chat = result.get("result", {})
        first = chat.get("first_name", "")
        last = chat.get("last_name", "")
        return f"{first} {last}".strip() or chat_id
    except Exception:
        return chat_id


def _get_pending_users():
    return [(uid, name) for uid, name in _pending_users.items()]
