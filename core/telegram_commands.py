"""Telegram slash-command handling and session/runtime management."""

from __future__ import annotations

from core.chat_store import list_chats, load_chat, make_title, new_chat_id, save_chat
from core.telegram_io import poll_for_reply, send_message
from core.telegram_state import (
    ALLOWED_IDS,
    OWNER_CONFIG,
    OWNER_ID,
    _get_pending_users,
    _get_user_config,
    _get_user_label,
    _save_allowed_ids,
    _save_owner_config,
    _save_user_configs,
    _sessions,
    _show_tools,
)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def swap_in(agent, session):
    """Load a user's message history into the agent."""
    if session["messages"] is None:
        session["messages"] = [agent.messages[0].copy()]
    agent.messages = session["messages"]


def resume_chat(chat_id_str, agent, session):
    """Load a saved conversation into the agent."""
    chat = load_chat(chat_id_str)
    if not chat:
        send_message(f"Chat '{chat_id_str}' not found.")
        return
    if session["first_message_sent"] and len(agent.messages) > 1:
        save_chat(session["chat_id"], session["title"], agent.messages)
    agent.messages = chat["messages"]
    session["messages"] = agent.messages
    session["chat_id"] = chat["id"]
    session["title"] = chat["title"]
    session["first_message_sent"] = True
    send_message(f"Resumed: {chat['title']} ({len(chat['messages'])} messages)")


# ---------------------------------------------------------------------------
# Runtime config
# ---------------------------------------------------------------------------

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


def apply_chat_runtime(agent, session, chat_id):
    """Switch agent runtime config for the active Telegram user."""
    user_cfg = _get_user_config(chat_id)
    personality_prompt = user_cfg.get("personality_prompt") or None
    if str(chat_id) == str(OWNER_ID):
        config = get_owner_effective_config(agent)
        agent.apply_runtime_overrides(
            messages=agent.messages,
            model=config["primary_model"],
            fallback_model=config["fallback_model"],
            extra_instructions=personality_prompt,
        )
    else:
        agent.apply_runtime_overrides(
            messages=agent.messages,
            model=agent.default_model,
            fallback_model=agent.default_fallback_model,
            extra_instructions=personality_prompt,
        )
    session["messages"] = agent.messages


# ---------------------------------------------------------------------------
# Owner model menu
# ---------------------------------------------------------------------------

def _send_owner_model_status(agent):
    cfg = get_owner_effective_config(agent)
    send_message(
        "Owner Telegram model config\n\n"
        f"Effective primary: {cfg['primary_model'] or 'not set'}\n"
        f"Effective fallback: {cfg['fallback_model'] or 'not set'}\n"
        f"Saved primary override: {OWNER_CONFIG.get('primary_model') or '(env default)'}\n"
        f"Saved fallback override: {OWNER_CONFIG.get('fallback_model') or '(env default)'}\n"
        f"Local mode: {'on' if cfg['use_local_model'] else 'off'}\n"
        f"Local model: {cfg['local_model'] or 'not set'}"
    )


def _handle_owner_model_menu(agent, session, chat_id):
    while True:
        _send_owner_model_status(agent)
        send_message(
            "\nChoose an option:\n"
            "1. Set primary model\n"
            "2. Set fallback model\n"
            "3. Toggle local model mode\n"
            "4. Reset primary override\n"
            "5. Reset fallback override\n"
            "6. Reset all model overrides\n"
            "7. Cancel"
        )

        reply, _ = poll_for_reply(chat_id)
        choice = reply.strip().lower()

        if choice in {"7", "cancel", "c", "done"}:
            send_message("Cancelled.")
            return True

        if choice == "1":
            send_message("Send the new primary model name, or reply 'cancel'.")
            model_reply, _ = poll_for_reply(chat_id)
            model_name = model_reply.strip()
            if model_name.lower() in {"cancel", "c"}:
                send_message("Cancelled.")
                return True
            OWNER_CONFIG["primary_model"] = model_name
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message(f"Owner primary model set to: {model_name}")
            return True

        if choice == "2":
            send_message("Send the new fallback model name, or reply 'cancel'.")
            model_reply, _ = poll_for_reply(chat_id)
            model_name = model_reply.strip()
            if model_name.lower() in {"cancel", "c"}:
                send_message("Cancelled.")
                return True
            OWNER_CONFIG["fallback_model"] = model_name
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message(f"Owner fallback model set to: {model_name}")
            return True

        if choice == "3":
            if not agent.local_model:
                send_message("OLLAMA_LOCAL_MODEL is not set in .env, so local mode can't be enabled.")
                return True
            OWNER_CONFIG["use_local_model"] = not bool(OWNER_CONFIG.get("use_local_model"))
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            state = "on" if OWNER_CONFIG["use_local_model"] else "off"
            send_message(
                f"Local model mode: {state}."
                + (f" Effective primary is now {agent.model}." if state == "on" else "")
            )
            return True

        if choice == "4":
            OWNER_CONFIG["primary_model"] = ""
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message("Primary override reset.")
            return True

        if choice == "5":
            OWNER_CONFIG["fallback_model"] = ""
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message("Fallback override reset.")
            return True

        if choice == "6":
            OWNER_CONFIG.update({"primary_model": "", "fallback_model": "", "use_local_model": False})
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message("All model overrides reset.")
            return True

        send_message("Invalid choice. Reply with 1-7.")


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------

def handle_telegram_command(text, agent, session, chat_id, speech_client):
    """Handle /commands sent via Telegram. Returns True if handled."""
    raw = text.strip()
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/stop":
        send_message("Nothing to stop — I wasn't working on anything.")
        return True

    if cmd == "/tools":
        current = _show_tools.get(str(chat_id), False)
        _show_tools[str(chat_id)] = not current
        send_message(f"Tool visibility: {'on' if not current else 'off'}")
        return True

    if cmd == "/help":
        lines = [
            "Commands:\n",
            "/chats - list & resume saved conversations",
            "/new - start a fresh conversation",
            "/stop - interrupt Lumi mid-task",
            "/tools - toggle tool call visibility",
            "/status - show model, storage, index info",
            "/help - this message",
            "/voice - toggle replies and switch Edge voices",
            "\nYou can also send a photo directly — Lumi will analyze it if the model supports vision.",
        ]
        if str(chat_id) == str(OWNER_ID):
            lines.append("/adduser - authorize a new user")
            lines.append("/model - choose the owner's Telegram model settings")
            lines.append("/users - list authorized users")
        lines.append("/personality - view or change your Telegram personality override")
        send_message("\n".join(lines))
        return True

    if cmd == "/chats":
        chats = list_chats(limit=20)
        if not chats:
            send_message("No saved conversations.")
            return True
        lines = ["Saved conversations:\n"]
        for i, chat in enumerate(chats, 1):
            lines.append(f"{i}. {chat['title']}")
        lines.append("\nReply with a number to resume, or 'cancel'.")
        send_message("\n".join(lines))

        reply, _ = poll_for_reply(chat_id)
        if reply.lower() in ("cancel", "c", "n", "no", "nevermind"):
            send_message("Cancelled.")
            return True
        try:
            pick = int(reply) - 1
            if 0 <= pick < len(chats):
                resume_chat(chats[pick]["id"], agent, session)
                apply_chat_runtime(agent, session, chat_id)
            else:
                send_message("Invalid number.")
        except ValueError:
            resume_chat(reply.strip(), agent, session)
            apply_chat_runtime(agent, session, chat_id)
        return True

    if cmd == "/new":
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)
        session["chat_id"] = new_chat_id()
        session["title"] = ""
        session["first_message_sent"] = False
        system_msg = agent.messages[0] if agent.messages else None
        agent.messages = [system_msg] if system_msg else []
        session["messages"] = agent.messages
        send_message("New conversation started.")
        return True

    if cmd == "/status":
        health = agent.storage.check_health()
        sym_count = len(agent.code_index.table.all_symbols())
        msg_count = len(agent.messages)
        model = agent.model or "not set"
        fallback = agent.fallback_model or "not set"
        chat_count = len(list_chats(limit=100))
        user_count = len(ALLOWED_IDS)
        owner_suffix = ""
        if str(chat_id) == str(OWNER_ID):
            owner_cfg = get_owner_effective_config(agent)
            owner_suffix = (
                f"\nLocal mode: {'on' if owner_cfg['use_local_model'] else 'off'}"
                f"\nLocal model: {owner_cfg['local_model'] or 'not set'}"
            )
        user_cfg = _get_user_config(chat_id)
        send_message(
            f"Status\n\n"
            f"Model: {model}\n"
            f"Fallback: {fallback}\n"
            f"Messages: {msg_count} in current conversation\n"
            f"Saved chats: {chat_count}\n"
            f"Index: {sym_count} symbols\n"
            f"Storage: {health['total_display']} / {health['budget_display']} "
            f"({health['usage_percent']:.0f}%)\n"
            f"Users: {user_count} authorized\n"
            f"Personality override: {'set' if user_cfg.get('personality_prompt') else 'not set'}\n"
            f"Voice replies: {'on' if user_cfg.get('voice_replies') else 'off'}\n"
            f"Voice name: {user_cfg.get('voice_name') or speech_client.config.default_voice}\n"
            f"Speech input: {'ready' if speech_client.can_transcribe else 'not ready'}\n"
            f"Speech output: {'ready' if speech_client.can_speak else 'not ready'}"
            f"{owner_suffix}"
        )
        return True

    if cmd in {"/adduser", "/users", "/model"} and str(chat_id) != str(OWNER_ID):
        send_message("This command is owner-only.")
        return True

    if cmd == "/model" and str(chat_id) == str(OWNER_ID):
        if not args:
            return _handle_owner_model_menu(agent, session, chat_id)

        subparts = args.split(maxsplit=1) if args else []
        action = subparts[0].lower() if subparts else ""
        value = subparts[1].strip() if len(subparts) > 1 else ""

        if action in {"primary", "fallback"}:
            if not value:
                send_message(f"Usage: /model {action} <model>")
                return True
            OWNER_CONFIG[f"{action}_model"] = value
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message(f"Owner {action} model set to: {value}")
            return True

        if action == "local":
            mode = value.lower()
            if mode not in {"on", "off"}:
                send_message("Usage: /model local on|off")
                return True
            if mode == "on" and not agent.local_model:
                send_message("OLLAMA_LOCAL_MODEL is not set in .env, so local mode can't be enabled.")
                return True
            OWNER_CONFIG["use_local_model"] = mode == "on"
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message(
                f"Local model mode: {mode}."
                + (f" Effective primary is now {agent.model}." if mode == "on" else "")
            )
            return True

        if action == "reset":
            target = value.lower()
            if target == "primary":
                OWNER_CONFIG["primary_model"] = ""
            elif target == "fallback":
                OWNER_CONFIG["fallback_model"] = ""
            elif target == "all":
                OWNER_CONFIG.update({"primary_model": "", "fallback_model": "", "use_local_model": False})
            else:
                send_message("Usage: /model reset primary|fallback|all")
                return True
            _save_owner_config()
            apply_chat_runtime(agent, session, chat_id)
            send_message(f"Owner model config reset: {target}")
            return True

        send_message("Unknown /model command. Send /model to open the menu.")
        return True

    if cmd in {"/personality", "/prompt"}:
        user_cfg = _get_user_config(chat_id)
        if not args:
            current = user_cfg.get("personality_prompt", "")
            if current:
                send_message(
                    "Your Telegram personality override\n\n"
                    f"{current}\n\n"
                    "Usage:\n"
                    "/personality set <text>\n"
                    "/personality reset"
                )
            else:
                send_message(
                    "No Telegram personality override is set for you.\n\n"
                    "Usage:\n"
                    "/personality set <text>\n"
                    "/personality reset"
                )
            return True

        prompt_parts = args.split(maxsplit=1)
        prompt_action = prompt_parts[0].lower()
        prompt_value = prompt_parts[1].strip() if len(prompt_parts) > 1 else ""

        if prompt_action == "set":
            if not prompt_value:
                send_message("Usage: /personality set <text>")
                return True
            user_cfg["personality_prompt"] = prompt_value
            _save_user_configs()
            apply_chat_runtime(agent, session, chat_id)
            send_message("Your Telegram personality override was updated.")
            return True

        if prompt_action == "reset":
            user_cfg["personality_prompt"] = ""
            _save_user_configs()
            apply_chat_runtime(agent, session, chat_id)
            send_message("Your Telegram personality override was cleared.")
            return True

        send_message(
            "Unknown personality command. Use /personality, /personality set <text>, or /personality reset."
        )
        return True

    if cmd == "/voice":
        user_cfg = _get_user_config(chat_id)
        action = args.strip().lower()
        voice_options = speech_client.get_voice_options()

        if not action or action == "status":
            send_message(
                "Voice replies\n\n"
                f"Current setting: {'on' if user_cfg.get('voice_replies') else 'off'}\n"
                f"Current voice: {user_cfg.get('voice_name') or speech_client.config.default_voice}\n\n"
                "Usage:\n"
                "/voice on\n"
                "/voice off\n"
                "/voice list\n"
                "/voice set ava\n"
                "/voice status"
            )
            return True

        if action in {"on", "off"}:
            user_cfg["voice_replies"] = action == "on"
            _save_user_configs()
            send_message(f"Voice replies: {action}")
            return True

        if action == "list":
            lines = ["Available voices:\n"]
            for key, value in voice_options.items():
                lines.append(f"- {key}: {value}")
            lines.append("\nUse /voice set <name> or /voice set <full voice id>.")
            send_message("\n".join(lines))
            return True

        if action.startswith("set "):
            target = args.strip()[4:].strip()
            if not target:
                send_message("Usage: /voice set ava")
                return True
            try:
                resolved = speech_client.resolve_voice(target)
            except ValueError as e:
                send_message(str(e))
                return True
            user_cfg["voice_name"] = resolved
            _save_user_configs()
            send_message(f"Voice set to: {resolved}")
            return True

        send_message("Unknown /voice command. Use /voice on, /voice off, /voice list, /voice set <name>, or /voice status.")
        return True

    if cmd == "/adduser" and str(chat_id) == str(OWNER_ID):
        pending = _get_pending_users()
        if not pending:
            send_message(
                "No new users have messaged the bot yet. "
                "Have them send a message first, then try /adduser again."
            )
            return True
        lines = ["These users messaged the bot:\n"]
        for i, (uid, name) in enumerate(pending, 1):
            lines.append(f"{i}. {name} (id: {uid})")
        lines.append("\nReply with a number to authorize, or 'cancel'.")
        send_message("\n".join(lines))

        reply, _ = poll_for_reply(chat_id)
        if reply.lower() in ("cancel", "c", "n", "no"):
            send_message("Cancelled.")
            return True
        try:
            pick = int(reply) - 1
            if 0 <= pick < len(pending):
                new_id, new_name = pending[pick]
                ALLOWED_IDS.add(str(new_id))
                _save_allowed_ids()
                send_message(f"Authorized {new_name}!")
                send_message(
                    "Welcome! You can now chat with Lumi. Send /help for commands.",
                    chat_id=new_id,
                )
            else:
                send_message("Invalid number.")
        except ValueError:
            send_message("Invalid input.")
        return True

    if cmd == "/users" and str(chat_id) == str(OWNER_ID):
        lines = ["Authorized users:\n"]
        for uid in ALLOWED_IDS:
            name = _get_user_label(uid)
            tag = " (owner)" if uid == str(OWNER_ID) else ""
            lines.append(f"- {name}{tag} (id: {uid})")
        send_message("\n".join(lines))
        return True

    if cmd == "/start":
        return True

    return False
