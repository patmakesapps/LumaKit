import json
import os
import urllib.request


# Set by the bridge/CLI before each user message
_current_context = {
    "chat_id": None,
    "message_id": None,
}


def set_react_context(chat_id, message_id):
    """Called by the bridge to set which message to react to."""
    _current_context["chat_id"] = chat_id
    _current_context["message_id"] = message_id


# Emoji the model can pick from (Telegram requires these exact emoji strings)
ALLOWED_REACTIONS = {
    "thumbs_up": "\U0001f44d",
    "fire": "\U0001f525",
    "heart": "\u2764\ufe0f",
    "laugh": "\U0001f602",
    "mind_blown": "\U0001f92f",
    "eyes": "\U0001f440",
    "think": "\U0001f914",
    "clap": "\U0001f44f",
    "hundred": "\U0001f4af",
    "salute": "\U0001fae1",
}


def get_react_tool():
    return {
        "name": "react_to_message",
        "description": (
            "React to the user's last message with an emoji instead of "
            "sending a text reply. Use this when a reaction fits better "
            "than words — acknowledging, vibing with something cool, etc. "
            f"Available reactions: {', '.join(ALLOWED_REACTIONS.keys())}"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "reaction": {
                    "type": "string",
                    "description": f"One of: {', '.join(ALLOWED_REACTIONS.keys())}",
                },
            },
            "required": ["reaction"],
        },
        "execute": _react,
    }


def _react(inputs):
    reaction_key = inputs.get("reaction", "").lower().strip()
    emoji = ALLOWED_REACTIONS.get(reaction_key)
    if not emoji:
        return {"error": f"Unknown reaction '{reaction_key}'. Use one of: {', '.join(ALLOWED_REACTIONS.keys())}"}

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = _current_context["chat_id"]
    message_id = _current_context["message_id"]

    if not token or not chat_id or not message_id:
        # CLI mode or no context — just report what we'd do
        return {"reacted": True, "emoji": emoji, "mode": "cli"}

    url = f"https://api.telegram.org/bot{token}/setMessageReaction"
    payload = json.dumps({
        "chat_id": chat_id,
        "message_id": message_id,
        "reaction": [{"type": "emoji", "emoji": emoji}],
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("ok"):
            return {"reacted": True, "emoji": emoji}
        return {"error": data.get("description", "Telegram reaction failed")}
    except Exception as e:
        return {"error": str(e)}
