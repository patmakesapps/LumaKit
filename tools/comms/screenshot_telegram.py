import os
import tempfile

import requests


def get_screenshot_telegram_tool():
    return {
        "name": "screenshot_telegram",
        "description": "Takes a screenshot of the current screen and sends it to the user via Telegram.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "caption": {
                    "type": "string",
                    "description": "Optional caption to include with the screenshot",
                },
            },
            "required": [],
        },
        "execute": _screenshot_telegram,
    }


def _screenshot_telegram(inputs):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = os.getenv("TELEGRAM_ALLOWED_IDS", "")
    chat_id = allowed.split(",")[0].strip() if allowed else ""

    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set in .env"}
    if not chat_id:
        return {"error": "TELEGRAM_ALLOWED_IDS not set in .env"}

    try:
        import pyautogui
    except ImportError:
        return {"error": "pyautogui not installed"}

    caption = inputs.get("caption", "")
    tmp_path = None

    try:
        img = pyautogui.screenshot()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        img.save(tmp_path)

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(tmp_path, "rb") as f:
            files = {"photo": ("screenshot.png", f, "image/png")}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            resp = requests.post(url, data=data, files=files, timeout=30)

        result = resp.json()
        if result.get("ok"):
            return {"sent": True, "caption": caption}
        return {"error": result.get("description", "Unknown Telegram error")}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
