"""Persistent Telegram owner-only runtime config."""

import json

from core.paths import get_data_dir


CONFIG_PATH = get_data_dir() / "telegram_owner_config.json"

DEFAULT_CONFIG = {
    "primary_model": "",
    "fallback_model": "",
    "use_local_model": False,
    "system_prompt": "",
}


def load_owner_config():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}

    config = DEFAULT_CONFIG.copy()
    if isinstance(data, dict):
        config.update(
            {
                "primary_model": str(data.get("primary_model", "") or "").strip(),
                "fallback_model": str(data.get("fallback_model", "") or "").strip(),
                "use_local_model": bool(data.get("use_local_model", False)),
                "system_prompt": str(data.get("system_prompt", "") or "").strip(),
            }
        )
    return config


def save_owner_config(config):
    payload = DEFAULT_CONFIG.copy()
    payload.update(config)
    payload["primary_model"] = str(payload.get("primary_model", "") or "").strip()
    payload["fallback_model"] = str(payload.get("fallback_model", "") or "").strip()
    payload["system_prompt"] = str(payload.get("system_prompt", "") or "").strip()
    payload["use_local_model"] = bool(payload.get("use_local_model", False))

    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload
