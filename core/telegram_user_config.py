"""Persistent per-user Telegram personality settings."""

import json
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent.parent / ".lumakit" / "telegram_user_config.json"


def load_user_configs():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    if not isinstance(data, dict):
        return {}

    result = {}
    for chat_id, config in data.items():
        if not isinstance(config, dict):
            continue
        result[str(chat_id)] = {
            "personality_prompt": str(config.get("personality_prompt", "") or "").strip()
        }
    return result


def save_user_configs(configs):
    payload = {}
    for chat_id, config in configs.items():
        payload[str(chat_id)] = {
            "personality_prompt": str(config.get("personality_prompt", "") or "").strip()
        }

    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
