"""Shared runtime config for interactive bridges."""

from __future__ import annotations

import os

from core.app_runtime_config import get_app_runtime_config
from core.telegram_state import OWNER_CONFIG, OWNER_ID, _get_user_config


def get_owner_effective_config(agent):
    return get_effective_config_for_user(
        user_id=OWNER_ID,
        default_model=agent.default_model,
        default_fallback=agent.default_fallback_model,
        local_model=agent.local_model,
    )


def get_effective_config_for_user(
    user_id,
    default_model=None,
    default_fallback=None,
    local_model=None,
):
    """Return the effective model/runtime config for a user."""
    env_model = default_model if default_model is not None else os.getenv("OLLAMA_MODEL")
    default_fallback = (
        default_fallback
        if default_fallback is not None
        else os.getenv("OLLAMA_FALLBACK_MODEL")
    )
    local_model = local_model if local_model is not None else os.getenv("OLLAMA_LOCAL_MODEL", "")
    app_cfg = get_app_runtime_config()
    default_model = app_cfg.get("primary_model") or env_model
    default_fallback = app_cfg.get("fallback_model") or default_fallback

    if str(user_id) == str(OWNER_ID):
        primary = OWNER_CONFIG.get("primary_model") or default_model
        fallback = OWNER_CONFIG.get("fallback_model") or default_fallback
        use_local_model = bool(OWNER_CONFIG.get("use_local_model"))
        if use_local_model and local_model:
            primary = local_model
        return {
            "primary_model": primary,
            "fallback_model": fallback,
            "system_prompt": OWNER_CONFIG.get("system_prompt", ""),
            "use_local_model": use_local_model,
            "local_model": local_model or "",
        }

    return {
        "primary_model": default_model,
        "fallback_model": default_fallback,
        "system_prompt": "",
        "use_local_model": False,
        "local_model": local_model or "",
    }


def apply_user_runtime(agent, session, user_id, surface=None):
    """Apply the current effective runtime for a user onto the active session."""
    user_cfg = _get_user_config(user_id)
    personality_prompt = user_cfg.get("personality_prompt") or None
    context_instructions = _surface_instructions(surface)
    config = get_effective_config_for_user(
        user_id=user_id,
        default_model=agent.default_model,
        default_fallback=agent.default_fallback_model,
        local_model=agent.local_model,
    )

    agent.apply_runtime_overrides(
        messages=agent.messages,
        model=config["primary_model"],
        fallback_model=config["fallback_model"],
        extra_instructions=personality_prompt,
        context_instructions=context_instructions,
    )

    session["messages"] = agent.messages


def _surface_instructions(surface):
    if surface == "web":
        return (
            "The user is currently talking to you in the web UI. When they ask to see an "
            "image or screenshot in this conversation, prefer send_photo_user or "
            "screenshot_user so it appears inline in the web chat. Do not send it to "
            "Telegram unless they explicitly ask for Telegram."
        )

    if surface == "telegram":
        return (
            "The user is currently talking to you in Telegram. When they ask to see an "
            "image or screenshot in this conversation, prefer send_photo_user or "
            "screenshot_user so it is delivered back to this Telegram chat."
        )

    return ""
