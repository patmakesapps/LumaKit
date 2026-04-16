"""Persistent per-user Telegram personality settings."""

import json

from core.paths import get_data_dir


CONFIG_PATH = get_data_dir() / "telegram_user_config.json"


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
            "personality_prompt": str(config.get("personality_prompt", "") or "").strip(),
            "voice_replies": bool(config.get("voice_replies", False)),
            "voice_name": str(config.get("voice_name", "") or "").strip(),
        }
    return result


def save_user_configs(configs):
    payload = {}
    for chat_id, config in configs.items():
        payload[str(chat_id)] = {
            "personality_prompt": str(config.get("personality_prompt", "") or "").strip(),
            "voice_replies": bool(config.get("voice_replies", False)),
            "voice_name": str(config.get("voice_name", "") or "").strip(),
        }

    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
