"""Low-level I/O primitives for the Telegram bridge.

Covers: sending messages/audio, polling for replies, stop-interrupt,
confirmation prompts, and TTS dispatch.
"""

from __future__ import annotations

import re
import socket
import urllib.error

from core.interrupts import request_interrupt
from core.telegram_api import (
    edit_message_text as telegram_edit_message_text,
    send_audio,
    send_message as telegram_send_message,
    telegram_api,
)
from core.telegram_state import (
    _active_chat_id,
    _get_user_config,
    _pending_updates,
    _poll_offset,
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _strip_emojis(text: str) -> str:
    return re.sub(
        r'[\U00010000-\U0010ffff\U00002600-\U000027BF\U0001F300-\U0001FAFF]',
        '',
        text,
        flags=re.UNICODE,
    ).strip()


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

def send_message(text, chat_id=None):
    chat_id = chat_id or _active_chat_id["value"]
    return telegram_send_message(text, chat_id)


def edit_message_text(text, chat_id, message_id):
    return telegram_edit_message_text(text, chat_id, message_id)


def send_tts_reply(text, chat_id, speech_client):
    if not speech_client.can_speak:
        return False
    try:
        voice_name = _get_user_config(chat_id).get("voice_name") or None
        clean_text = _strip_emojis(text)
        if not clean_text:
            return False
        audio_bytes, content_type, extension = speech_client.synthesize(clean_text, voice=voice_name)
        send_audio(
            audio_bytes,
            chat_id=chat_id,
            filename=f"lumi-reply.{extension}",
            title="Lumi reply",
            content_type=content_type,
        )
        return True
    except Exception as e:
        print(f"[tts error] {e}")
        return False


# ---------------------------------------------------------------------------
# Inbound / polling
# ---------------------------------------------------------------------------

def poll_for_reply(chat_id=None):
    """Block until the specified user sends a reply. Returns (text, new_offset)."""
    chat_id = str(chat_id or _active_chat_id["value"])
    while True:
        try:
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != chat_id:
                    continue
                text = msg.get("text", "").strip()
                if text:
                    return text, _poll_offset["value"]
        except (socket.timeout, urllib.error.URLError):
            continue


def check_for_stop():
    """Non-blocking peek for a /stop from the active user.

    Any non-/stop updates are buffered in _pending_updates so the main
    poll loop can process them normally. Returns True if /stop was found.
    """
    chat_id = _active_chat_id["value"]
    if not chat_id:
        return False

    params = {"timeout": 0}
    if _poll_offset["value"] is not None:
        params["offset"] = _poll_offset["value"]

    try:
        updates = telegram_api("getUpdates", params).get("result", [])
    except Exception:
        return False

    if not updates:
        return False

    found_stop = False
    for update in updates:
        _poll_offset["value"] = update["update_id"] + 1
        msg = update.get("message", {})
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        if text.lower() == "/stop" and msg_chat_id == str(chat_id):
            found_stop = True
            try:
                send_message("Stopping...", chat_id=msg_chat_id)
            except Exception:
                pass
        else:
            _pending_updates.append(update)
    return found_stop


def telegram_confirm(prompt):
    """Replacement for cli.confirm() — polls Telegram directly for y/n."""
    send_message(f"⚠️ {prompt}\nReply y or n")
    chat_id = str(_active_chat_id["value"])

    while True:
        try:
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != chat_id:
                    continue
                text = msg.get("text", "").strip().lower()
                if text == "/stop":
                    request_interrupt()
                    send_message("Stopping...", chat_id=chat_id)
                    return False
                if text in ("y", "yes", "n", "no"):
                    return text in ("y", "yes")
        except (socket.timeout, urllib.error.URLError):
            continue
