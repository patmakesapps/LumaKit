"""Persistent app-owned runtime config for web/UI-managed overrides.

This layer sits above `.env` defaults but below per-surface/per-user overrides
such as the Telegram owner's `/model` settings.
"""

from __future__ import annotations

import json

from core.paths import get_data_dir


CONFIG_PATH = get_data_dir() / "app_runtime_config.json"

DEFAULT_CONFIG = {
    "primary_model": "",
    "fallback_model": "",
    "require_tool_approvals": True,
}


def _coerce_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def load_app_runtime_config():
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
                "require_tool_approvals": _coerce_bool(
                    data.get("require_tool_approvals"),
                    True,
                ),
            }
        )
    return config


APP_RUNTIME_CONFIG = load_app_runtime_config()


def get_app_runtime_config():
    return APP_RUNTIME_CONFIG


def save_app_runtime_config(config):
    global APP_RUNTIME_CONFIG

    payload = DEFAULT_CONFIG.copy()
    payload.update(config)
    payload["primary_model"] = str(payload.get("primary_model", "") or "").strip()
    payload["fallback_model"] = str(payload.get("fallback_model", "") or "").strip()
    payload["require_tool_approvals"] = _coerce_bool(
        payload.get("require_tool_approvals"),
        True,
    )

    CONFIG_PATH.parent.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    APP_RUNTIME_CONFIG = payload
    return payload
