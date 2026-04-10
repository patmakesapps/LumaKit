"""Send an image file from disk to the user via Telegram.

Unlike screenshot_telegram (which captures the desktop), this forwards an
existing image file — e.g. the screenshot produced by browser_automation.
"""

import os
from pathlib import Path

import requests


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def get_send_photo_telegram_tool():
    return {
        "name": "send_photo_telegram",
        "description": (
            "Sends an existing image file from disk to the user via Telegram. "
            "Use this to forward screenshots from browser_automation (path in its "
            "screenshot_path result field) or any other image file. "
            "Do NOT use this to capture the desktop — use screenshot_telegram for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the image file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption to include with the photo.",
                },
            },
            "required": ["path"],
        },
        "execute": _send_photo_telegram,
    }


def _send_photo_telegram(inputs):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = os.getenv("TELEGRAM_ALLOWED_IDS", "")
    chat_id = allowed.split(",")[0].strip() if allowed else ""

    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set in .env"}
    if not chat_id:
        return {"error": "TELEGRAM_ALLOWED_IDS not set in .env"}

    raw_path = inputs.get("path", "")
    if not raw_path:
        return {"error": "path is required"}

    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        return {"error": f"file not found: {path}"}
    if not path.is_file():
        return {"error": f"not a file: {path}"}
    if path.suffix.lower() not in SUPPORTED_EXTS:
        return {
            "error": f"unsupported image format: {path.suffix}. "
                     f"Supported: {', '.join(sorted(SUPPORTED_EXTS))}"
        }

    caption = inputs.get("caption", "")

    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(path, "rb") as f:
            files = {"photo": (path.name, f, "image/png")}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            resp = requests.post(url, data=data, files=files, timeout=30)

        result = resp.json()
        if result.get("ok"):
            return {"sent": True, "path": str(path), "caption": caption}
        return {"error": result.get("description", "Unknown Telegram error")}
    except Exception as e:
        return {"error": str(e)}
