import json
import os
import urllib.parse
import urllib.request


def get_send_telegram_tool():
    return {
        "name": "send_telegram",
        "description": "Sends a message to the user via Telegram.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message text to send",
                },
            },
            "required": ["message"],
        },
        "execute": _send_telegram,
    }


def _send_telegram(inputs):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    allowed = os.getenv("TELEGRAM_ALLOWED_IDS", "")
    chat_id = allowed.split(",")[0].strip() if allowed else ""

    if not token:
        return {"error": "TELEGRAM_BOT_TOKEN not set in .env"}
    if not chat_id:
        return {"error": "TELEGRAM_ALLOWED_IDS not set in .env"}

    text = inputs["message"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("ok"):
            return {"sent": True, "message": text}
        return {"error": data.get("description", "Unknown Telegram error")}
    except Exception as e:
        return {"error": str(e)}
