"""Shared runtime config for interactive bridges."""

from __future__ import annotations

from core.telegram_state import OWNER_CONFIG, OWNER_ID, _get_user_config


def get_owner_effective_config(agent):
    primary = OWNER_CONFIG.get("primary_model") or agent.default_model
    fallback = OWNER_CONFIG.get("fallback_model") or agent.default_fallback_model
    local_model = agent.local_model or ""
    use_local_model = bool(OWNER_CONFIG.get("use_local_model"))

    if use_local_model and local_model:
        primary = local_model

    return {
        "primary_model": primary,
        "fallback_model": fallback,
        "system_prompt": OWNER_CONFIG.get("system_prompt", ""),
        "use_local_model": use_local_model,
        "local_model": local_model,
    }


def apply_user_runtime(agent, session, user_id, surface=None):
    """Apply the current effective runtime for a user onto the active session."""
    user_cfg = _get_user_config(user_id)
    personality_prompt = user_cfg.get("personality_prompt") or None
    context_instructions = _surface_instructions(surface)

    if str(user_id) == str(OWNER_ID):
        config = get_owner_effective_config(agent)
        agent.apply_runtime_overrides(
            messages=agent.messages,
            model=config["primary_model"],
            fallback_model=config["fallback_model"],
            extra_instructions=personality_prompt,
            context_instructions=context_instructions,
        )
    else:
        agent.apply_runtime_overrides(
            messages=agent.messages,
            model=agent.default_model,
            fallback_model=agent.default_fallback_model,
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
