"""Shared delivery helpers for Telegram and web UI image sending."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import requests

from core.interface_context import get_interface, get_interface_user
from core.paths import get_data_dir


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
WEB_MEDIA_DIR = get_data_dir() / "web_media"
SCREENSHOTS_DIR = get_data_dir() / "screenshots"


def resolve_image_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if not path.is_file():
        raise ValueError(f"not a file: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTS:
        raise ValueError(
            f"unsupported image format: {path.suffix}. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTS))}"
        )
    return path


def stage_image_for_web(path: Path) -> str:
    WEB_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix.lower()}"
    target = WEB_MEDIA_DIR / safe_name
    shutil.copy2(path, target)
    return f"/media/{target.name}"


def deliver_image_to_current_user(path: Path, caption: str = "") -> dict:
    interface = get_interface() or "telegram"
    if interface == "web":
        return {
            "sent": True,
            "interface": "web",
            "path": str(path),
            "url": stage_image_for_web(path),
            "caption": caption,
        }

    return _send_image_to_telegram(path, caption=caption)


def capture_screenshot_to_disk() -> Path:
    try:
        import pyautogui
    except ImportError as exc:
        raise RuntimeError("pyautogui not installed") from exc

    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = SCREENSHOTS_DIR / f"screenshot-{stamp}-{uuid.uuid4().hex[:6]}.png"
    image = pyautogui.screenshot()
    image.save(path)
    return path


def _send_image_to_telegram(path: Path, caption: str = "") -> dict:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set in .env"}

    chat_id = _telegram_chat_id()
    if not chat_id:
        return {"error": "No Telegram recipient available"}

    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(path, "rb") as fh:
            files = {"photo": (path.name, fh, "image/png")}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            response = requests.post(url, data=data, files=files, timeout=30)

        payload = response.json()
        if payload.get("ok"):
            return {
                "sent": True,
                "interface": "telegram",
                "path": str(path),
                "caption": caption,
                "chat_id": chat_id,
            }
        return {"error": payload.get("description", "Unknown Telegram error")}
    except Exception as exc:
        return {"error": str(exc)}


def _telegram_chat_id():
    interface = get_interface()
    user_id = get_interface_user()
    if interface == "telegram" and user_id:
        return user_id

    allowed = os.getenv("TELEGRAM_ALLOWED_IDS", "")
    if not allowed:
        return ""
    return allowed.split(",")[0].strip()

