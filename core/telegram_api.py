"""Telegram Bot API helpers shared by the bridge and comms tools."""

from __future__ import annotations

import json
import os
from io import BytesIO
from typing import Any

import requests


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_TIMEOUT = 30


def telegram_api(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    response = requests.post(url, json=params or {}, timeout=API_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload


def send_message(text: str, chat_id: str | int):
    """Send a text message, splitting it into Telegram-sized chunks."""
    while text:
        chunk, text = text[:4096], text[4096:]
        telegram_api("sendMessage", {"chat_id": chat_id, "text": chunk})


def send_chat_action(chat_id: str | int, action: str):
    telegram_api("sendChatAction", {"chat_id": chat_id, "action": action})


def download_telegram_file(file_id: str) -> tuple[bytes | None, str | None]:
    """Download any Telegram file by file_id. Returns (bytes, file_path)."""
    try:
        file_info = telegram_api("getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            return None, None

        if not TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        response = requests.get(url, timeout=API_TIMEOUT)
        response.raise_for_status()
        return response.content, file_path
    except Exception:
        return None, None


def download_telegram_photo(file_id: str) -> bytes | None:
    payload, _ = download_telegram_file(file_id)
    return payload


def send_audio(
    audio_bytes: bytes,
    chat_id: str | int,
    filename: str = "lumi-reply.mp3",
    title: str | None = None,
    caption: str | None = None,
    content_type: str = "audio/mpeg",
):
    """Send as a voice message (not a music track) so Telegram doesn't auto-play
    previous messages in sequence after this one finishes."""
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    url = f"https://api.telegram.org/bot{TOKEN}/sendVoice"
    files = {
        "voice": (
            filename,
            BytesIO(audio_bytes),
            content_type,
        )
    }
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    response = requests.post(url, data=data, files=files, timeout=API_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", False):
        raise RuntimeError(payload.get("description", "Telegram API error"))
    return payload
