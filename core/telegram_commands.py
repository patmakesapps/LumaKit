"""Telegram slash-command handling and session/runtime management."""

from __future__ import annotations

from pathlib import Path

from core.chat_store import (
    get_chat_lumabot_profile,
    list_chats,
    list_known_workspaces,
    load_chat,
    make_title,
    new_chat_id,
    save_chat,
    set_active_chat,
    set_chat_lumabot_profile,
)
from core.app_runtime_config import (
    get_app_runtime_config,
    save_app_runtime_config,
    set_tools_enabled,
    tools_enabled,
)
from core.identity import chat_owner_id
from core.runtime_config import apply_user_runtime, get_owner_effective_config
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
from tools.lumabot.remote import (
    REMOTE_HELP,
    execute_remote_action,
    execute_remote_command,
)


def lumabot_remote_keyboard():
    """Inline buttons with structured callback payloads, not language."""
    return {
        "inline_keyboard": [
            [{"text": "▲ Forward", "callback_data": "lbot:drive:forward"}],
            [
                {"text": "↶ Left", "callback_data": "lbot:turn:left"},
                {"text": "STOP", "callback_data": "lbot:stop"},
                {"text": "Right ↷", "callback_data": "lbot:turn:right"},
            ],
            [{"text": "▼ Reverse", "callback_data": "lbot:drive:backward"}],
            [
                {"text": "Turn 180°", "callback_data": "lbot:turn_around"},
                {"text": "Park", "callback_data": "lbot:park"},
                {"text": "Status", "callback_data": "lbot:status"},
            ],
        ]
    }


def handle_lumabot_callback(data: str, session: dict) -> dict:
    """Execute one structured Telegram button callback without an LLM."""
    parts = str(data or "").split(":")
    if not parts or parts[0] != "lbot":
        return {"ok": False, "text": "Unknown LumaBot control."}
    profile = get_chat_lumabot_profile(session.get("chat_id"))
    action = parts[1] if len(parts) > 1 else ""
    if profile != "remote" and action not in {"stop", "park"}:
        return {"ok": False, "text": "LumaBot Remote mode is off."}
    try:
        if action == "drive" and len(parts) == 3:
            return execute_remote_action("drive", direction=parts[2], continuous=True)
        if action == "turn" and len(parts) == 3:
            return execute_remote_action("turn", direction=parts[2])
        if action in {"turn_around", "stop", "park", "status"} and len(parts) == 2:
            return execute_remote_action(action)
    except ValueError as error:
        return {"ok": False, "text": str(error)}
    return {"ok": False, "text": "Unknown LumaBot control."}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def swap_in(agent, session):
    """Load a user's message history into the agent."""
    if session["messages"] is None:
        session["messages"] = [agent.messages[0].copy()]
    agent.messages = session["messages"]


def resume_chat(chat_id_str, agent, session, telegram_chat_id=None):
    """Load a saved conversation into the agent."""
    owner_id = chat_owner_id(telegram_chat_id) if telegram_chat_id else None
    chat = load_chat(chat_id_str, owner_id=owner_id)
    if not chat:
        send_message(f"Chat '{chat_id_str}' not found.")
        return
    if session["first_message_sent"] and len(agent.messages) > 1:
        save_chat(session["chat_id"], session["title"], agent.messages, owner_id=owner_id)
    agent.messages = chat["messages"]
    session["messages"] = agent.messages
    session["chat_id"] = chat["id"]
    session["title"] = chat["title"]
    session["first_message_sent"] = True
    if telegram_chat_id:
        set_active_chat(owner_id, chat["id"])
    send_message(f"Resumed: {chat['title']} ({len(chat['messages'])} messages)")


# Workspace the process started in — what "reset" returns to.
_DEFAULT_WORKSPACE = Path.cwd().resolve(strict=False)


def _apply_owner_workspace(agent):
    """Point the agent at the owner's saved working directory (if any)."""
    saved = str(OWNER_CONFIG.get("workspace_path") or "").strip()
    if not saved:
        agent.set_workspace_root(_DEFAULT_WORKSPACE)
        return
    path = Path(saved).expanduser()
    if path.is_dir():
        agent.set_workspace_root(path)
    else:
        agent.set_workspace_root(_DEFAULT_WORKSPACE)


def _resolve_workspace_input(agent, raw):
    path = Path(str(raw).strip().strip('"')).expanduser()
    if not path.is_absolute():
        path = agent.workspace_root / path
    return path.resolve(strict=False)


def _known_workspaces():
    """Candidate workspaces for the picker: default, Telegram recents, web chat workspaces."""
    candidates = [str(_DEFAULT_WORKSPACE)]
    candidates += OWNER_CONFIG.get("recent_workspaces") or []
    candidates += list_known_workspaces(limit=10)
    options, seen = [], set()
    for raw in candidates:
        path = Path(str(raw)).expanduser().resolve(strict=False)
        key = str(path).lower()
        if key in seen or not path.is_dir():
            continue
        seen.add(key)
        options.append(path)
    return options[:8]


def _set_owner_workspace(agent, path):
    OWNER_CONFIG["workspace_path"] = str(path)
    recents = [str(path)] + [
        p for p in (OWNER_CONFIG.get("recent_workspaces") or []) if str(p).lower() != str(path).lower()
    ]
    OWNER_CONFIG["recent_workspaces"] = recents[:10]
    _save_owner_config()
    agent.set_workspace_root(path)
    send_message(f"Working directory set to: {path}")


def _reset_owner_workspace(agent):
    OWNER_CONFIG["workspace_path"] = ""
    _save_owner_config()
    agent.set_workspace_root(_DEFAULT_WORKSPACE)
    send_message(f"Working directory reset to default: {_DEFAULT_WORKSPACE}")


def apply_chat_runtime(agent, session, chat_id):
    """Switch agent runtime config for the active Telegram user."""
    _apply_owner_workspace(agent)
    apply_user_runtime(agent, session, chat_id, surface="telegram")


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
        current = _show_tools.get(str(chat_id), True)
        _show_tools[str(chat_id)] = not current
        send_message(f"Tool visibility: {'on' if not current else 'off'}")
        return True

    if cmd == "/tooluse":
        if str(chat_id) != str(OWNER_ID):
            send_message("This command is owner-only.")
            return True
        current = tools_enabled()
        value = args.strip().lower()
        if not value:
            send_message(
                f"Tool use is currently {'ON' if current else 'OFF'}.\n\n"
                "ON: Lumi can read files, run commands, and use every tool.\n"
                "OFF: plain chat — no tool definitions are sent to the model at "
                "all. This is what makes a model without tool support usable "
                "(check yours with `ollama show <model>`).\n\n"
                "Usage: /tooluse on|off"
            )
            return True
        if value not in {"on", "off"}:
            send_message("Usage: /tooluse on|off")
            return True
        set_tools_enabled(value == "on")
        # The system prompt differs with tools off — refresh it now instead of
        # telling the model about tools it no longer has.
        apply_user_runtime(agent, session, chat_id, surface="telegram")
        send_message(
            "Tool use is on."
            if value == "on"
            else "Tool use is off. Lumi will answer without tools."
        )
        return True

    if cmd in {"/permissions", "/approvals"}:
        if str(chat_id) != str(OWNER_ID):
            send_message("This command is owner-only.")
            return True
        cfg = get_app_runtime_config().copy()
        current = bool(cfg.get("require_tool_approvals", True))
        value = args.strip().lower()
        if not value:
            send_message(
                "Tool approvals are currently "
                f"{'on' if current else 'off'}.\n\n"
                "Use /permissions on or /permissions off. Delete file and git stage/commit/push still require approval."
            )
            return True
        if value not in {"on", "off"}:
            send_message("Usage: /permissions on|off")
            return True
        cfg["require_tool_approvals"] = value == "on"
        save_app_runtime_config(cfg)
        send_message(
            f"Tool approvals: {value}. "
            "Delete file and git stage/commit/push still require approval."
        )
        return True

    if cmd == "/help":
        lines = [
            "Commands:\n",
            "/chats - list & resume saved conversations",
            "/new - start a fresh conversation",
            "/stop - interrupt Lumi mid-task",
            "/tools - toggle tool call visibility",
            "/permissions - toggle tool approval prompts",
            "/status - show model, storage, index info",
            "/tasks - list autonomous background tasks",
            "/task <id> - show details for a specific task",
            "/help - this message",
            "/voice - toggle replies and switch Edge voices",
            "\nYou can also send a photo directly — Lumi will analyze it if the model supports vision.",
        ]
        if str(chat_id) == str(OWNER_ID):
            lines.append("/adduser - authorize a new user")
            lines.append("/removeuser - remove an authorized user")
            lines.append("/role - set a user's tool-access role (trusted/limited)")
            lines.append("/approve <id> [always] - approve a task's pending protected action (always = for the rest of that task)")
            lines.append("/deny <id> - refuse a task's pending protected action")
            lines.append("/model - choose the owner's Telegram model settings")
            lines.append("/workspace - pick or set the working directory (alias /dir)")
            lines.append("/safemode - toggle full machine access (approvals + file sandbox)")
            lines.append("/tooluse on|off - let Lumi use tools at all (on by default)")
            lines.append("/lumabot - agent or instant remote control")
            lines.append("/users - list authorized users")
        lines.append("/personality - view or change your Telegram personality override")
        send_message("\n".join(lines))
        return True

    if cmd == "/chats":
        owner_id = chat_owner_id(chat_id)
        chats = list_chats(limit=20, owner_id=owner_id)
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
                resume_chat(chats[pick]["id"], agent, session, telegram_chat_id=chat_id)
                apply_chat_runtime(agent, session, chat_id)
            else:
                send_message("Invalid number.")
        except ValueError:
            resume_chat(reply.strip(), agent, session, telegram_chat_id=chat_id)
            apply_chat_runtime(agent, session, chat_id)
        return True

    if cmd == "/new":
        owner_id = chat_owner_id(chat_id)
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages, owner_id=owner_id)
        session["chat_id"] = new_chat_id()
        session["title"] = ""
        session["first_message_sent"] = False
        system_msg = agent.messages[0] if agent.messages else None
        agent.messages = [system_msg] if system_msg else []
        session["messages"] = agent.messages
        set_active_chat(owner_id, session["chat_id"])
        apply_chat_runtime(agent, session, chat_id)
        send_message("New conversation started.")
        return True

    if cmd == "/status":
        health = agent.storage.check_health()
        index_status = agent.get_code_index_status()
        if index_status["state"] == "ready":
            index_display = f"{index_status['symbols']} symbols"
        elif index_status["state"] == "error":
            index_display = f"error: {index_status['error']}"
        else:
            index_display = index_status["state"]
        msg_count = len(agent.messages)
        model = agent.model or "not set"
        fallback = agent.fallback_model or "not set"
        chat_count = len(list_chats(limit=100, owner_id=chat_owner_id(chat_id)))
        user_count = len(ALLOWED_IDS)
        owner_suffix = ""
        if str(chat_id) == str(OWNER_ID):
            owner_cfg = get_owner_effective_config(agent)
            owner_suffix = (
                f"\nLocal mode: {'on' if owner_cfg['use_local_model'] else 'off'}"
                f"\nLocal model: {owner_cfg['local_model'] or 'not set'}"
                f"\nWorkspace: {agent.workspace_root}"
                f"\nSafe mode: {'on' if get_app_runtime_config().get('safe_mode', True) else 'off'}"
                f"\nLumaBot mode: {get_chat_lumabot_profile(session.get('chat_id'))}"
            )
        user_cfg = _get_user_config(chat_id)
        send_message(
            f"Status\n\n"
            f"Model: {model}\n"
            f"Fallback: {fallback}\n"
            f"Messages: {msg_count} in current conversation\n"
            f"Saved chats: {chat_count}\n"
            f"Index: {index_display}\n"
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

    if cmd in {"/adduser", "/removeuser", "/users", "/model", "/role", "/approve", "/deny", "/workspace", "/dir", "/safemode", "/lumabot"} and str(chat_id) != str(OWNER_ID):
        send_message("This command is owner-only.")
        return True

    if cmd == "/lumabot" and str(chat_id) == str(OWNER_ID):
        command_parts = args.strip().split(maxsplit=1)
        action = command_parts[0].lower() if command_parts else ""
        current = get_chat_lumabot_profile(session.get("chat_id"))
        if not action:
            send_message(
                f"LumaBot mode: {current.upper()}\n\n{REMOTE_HELP}",
                reply_markup=lumabot_remote_keyboard() if current == "remote" else None,
            )
            return True

        if action in {"on", "agent", "remote", "off"}:
            profile = "agent" if action == "on" else action
            set_chat_lumabot_profile(session.get("chat_id"), profile)
            apply_chat_runtime(agent, session, chat_id)
            if profile == "agent":
                send_message("LumaBot Agent mode ON. Natural language uses the configured LLM.")
            elif profile == "remote":
                send_message(
                    "LumaBot Remote mode ON. These controls bypass the LLM.",
                    reply_markup=lumabot_remote_keyboard(),
                )
            else:
                send_message("LumaBot mode OFF. Full LumaKit is restored.")
            return True

        if action not in {"stop", "park", "status", "help"} and current != "remote":
            send_message("Switch to Remote mode first with /lumabot remote.")
            return True
        result = execute_remote_command(args)
        prefix = f"LumaBot mode: {current.upper()}\n" if action == "status" else ""
        send_message(
            prefix + result["text"],
            reply_markup=lumabot_remote_keyboard() if current == "remote" else None,
        )
        return True

    if cmd == "/safemode" and str(chat_id) == str(OWNER_ID):
        cfg = get_app_runtime_config().copy()
        current = bool(cfg.get("safe_mode", True))
        value = args.strip().lower()
        if not value:
            send_message(
                f"Safe mode is {'ON' if current else 'OFF'}.\n\n"
                "ON: shell/python/file-delete/git actions always ask for approval, "
                "and file tools stay inside the workspace.\n"
                "OFF: full machine access for you — no forced approval prompts, file tools "
                "can go anywhere (secrets files stay blocked). Other users keep their limits.\n\n"
                "Usage: /safemode on|off"
            )
            return True
        if value not in {"on", "off"}:
            send_message("Usage: /safemode on|off")
            return True
        cfg["safe_mode"] = value == "on"
        save_app_runtime_config(cfg)
        if value == "off":
            hint = (
                "\nHeads up: /permissions is still on, so regular tools will still ask once. "
                "Use /permissions off for zero prompts."
                if bool(cfg.get("require_tool_approvals", True))
                else ""
            )
            send_message(
                "⚠️ Safe mode OFF. Shell, python, file deletes, and git pushes now run "
                "without approval prompts, and file tools can reach the whole machine. "
                "Secrets files (.env, tokens) stay blocked." + hint
            )
        else:
            send_message(
                "Safe mode ON. Protected tools require approval again and file tools are "
                "limited to the workspace."
            )
        return True

    if cmd in {"/workspace", "/dir"} and str(chat_id) == str(OWNER_ID):
        action = args.strip()

        if not action:
            options = _known_workspaces()
            lines = [f"Active working directory:\n{agent.workspace_root}\n", "Known workspaces:\n"]
            for i, p in enumerate(options, 1):
                marker = "  ← active" if p == agent.workspace_root else ""
                lines.append(f"{i}. {p}{marker}")
            lines.append("\nReply with a number, a full path, 'reset', or 'cancel'.")
            send_message("\n".join(lines))

            reply, _ = poll_for_reply(chat_id)
            choice = reply.strip()
            low = choice.lower()
            if low in ("cancel", "c", "n", "no"):
                send_message("Cancelled.")
                return True
            if low == "reset":
                _reset_owner_workspace(agent)
                return True
            if choice.lstrip("-").isdigit():
                pick = int(choice) - 1
                if 0 <= pick < len(options):
                    _set_owner_workspace(agent, options[pick])
                else:
                    send_message("Invalid number.")
                return True
            path = _resolve_workspace_input(agent, choice)
            if not path.is_dir():
                send_message(f"That's not a directory I can find: {path}")
                return True
            _set_owner_workspace(agent, path)
            return True

        subparts = action.split(maxsplit=1)
        verb = subparts[0].lower()

        if verb == "reset":
            _reset_owner_workspace(agent)
            return True

        if verb == "set" and len(subparts) < 2:
            send_message("Usage: /workspace set <path>")
            return True

        raw = subparts[1] if verb == "set" else action
        path = _resolve_workspace_input(agent, raw)
        if not path.is_dir():
            send_message(f"That's not a directory I can find: {path}")
            return True
        _set_owner_workspace(agent, path)
        return True

    if cmd in {"/approve", "/deny"} and str(chat_id) == str(OWNER_ID):
        from core import task_approvals
        parts = str(args or "").split()
        try:
            task_id = int(parts[0])
        except (IndexError, ValueError):
            send_message(f"Usage: {cmd} <task id> [always]  (see /tasks for ids)")
            return True
        scope = "task" if len(parts) > 1 and parts[1].lower() in {"always", "task", "all"} else "once"
        if cmd == "/approve":
            ok, reply = task_approvals.approve(task_id, scope=scope)
        else:
            ok, reply = task_approvals.deny(task_id)
        send_message(("✅ " if ok else "⚠️ ") + reply)
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
        from core.telegram_user_config import get_user_role
        lines = ["Authorized users:\n"]
        for uid in ALLOWED_IDS:
            name = _get_user_label(uid)
            tag = " (owner)" if uid == str(OWNER_ID) else f" ({get_user_role(uid)})"
            lines.append(f"- {name}{tag} (id: {uid})")
        send_message("\n".join(lines))
        return True

    if cmd == "/role" and str(chat_id) == str(OWNER_ID):
        from core.telegram_user_config import get_user_role, set_user_role
        others = [uid for uid in ALLOWED_IDS if uid != str(OWNER_ID)]
        if not others:
            send_message("No non-owner users yet. Roles only apply to other authorized users.")
            return True
        lines = ["Whose role do you want to change?\n"]
        for i, uid in enumerate(others, 1):
            lines.append(f"{i}. {_get_user_label(uid)} — currently {get_user_role(uid)} (id: {uid})")
        lines.append(
            "\nReply with '<number> trusted' or '<number> limited', or 'cancel'."
            "\n- trusted: full chat, no shell/python/file-write/git/task tools"
            "\n- limited: trusted minus browser automation and email"
        )
        send_message("\n".join(lines))

        reply, _ = poll_for_reply(chat_id)
        normalized = reply.strip().lower()
        if normalized in ("cancel", "c", "n", "no"):
            send_message("Cancelled.")
            return True
        parts = normalized.split()
        if len(parts) == 2 and parts[1] in ("trusted", "limited"):
            try:
                pick = int(parts[0]) - 1
            except ValueError:
                pick = -1
            if 0 <= pick < len(others):
                target = others[pick]
                new_role = set_user_role(target, parts[1])
                send_message(f"{_get_user_label(target)} is now '{new_role}'.")
                return True
        send_message("Invalid input. Use '<number> trusted' or '<number> limited'.")
        return True

    if cmd == "/removeuser" and str(chat_id) == str(OWNER_ID):
        removable = [uid for uid in ALLOWED_IDS if uid != str(OWNER_ID)]
        if not removable:
            send_message("No users to remove (can't remove the owner).")
            return True
        lines = ["Which user do you want to remove?\n"]
        for i, uid in enumerate(removable, 1):
            name = _get_user_label(uid)
            lines.append(f"{i}. {name} (id: {uid})")
        lines.append("\nReply with a number, or 'cancel'.")
        send_message("\n".join(lines))

        reply, _ = poll_for_reply(chat_id)
        if reply.lower() in ("cancel", "c", "n", "no"):
            send_message("Cancelled.")
            return True
        try:
            pick = int(reply) - 1
            if 0 <= pick < len(removable):
                removed_id = removable[pick]
                removed_name = _get_user_label(removed_id)
                ALLOWED_IDS.discard(removed_id)
                _save_allowed_ids()
                send_message(f"Removed {removed_name}.")
            else:
                send_message("Invalid number.")
        except ValueError:
            send_message("Invalid input.")
        return True

    if cmd == "/tasks":
        from core import task_store as _ts
        tasks = _ts.get_tasks_by_owner(str(chat_id), limit=20)
        if not tasks:
            send_message("No tasks found. Tell Lumi to work on something and it'll show up here.")
            return True
        status_emoji = {
            "planning": "🗂", "active": "⚙️", "blocked": "🔴",
            "done": "✅", "failed": "❌", "cancelled": "🚫",
        }
        lines = ["Your tasks:\n"]
        for t in tasks:
            em = status_emoji.get(t["status"], "❓")
            due = f"  due {t['due_at'][:10]}" if t.get("due_at") else ""
            plan = t.get("plan") or []
            step_str = f"  step {t['current_step']+1}/{len(plan)}" if plan and t["status"] == "active" else ""
            lines.append(f"{em} [{t['id']}] {t['title']}{due}{step_str}")
        lines.append("\nUse /task <id> for details.")
        send_message("\n".join(lines))
        return True

    if cmd == "/task":
        from core import task_store as _ts
        if not args:
            send_message("Usage: /task <id>")
            return True
        try:
            task_id = int(args.strip())
        except ValueError:
            send_message("Usage: /task <id>  (id must be a number)")
            return True
        task = _ts.get_task(task_id)
        if not task:
            send_message(f"Task {task_id} not found.")
            return True
        plan = task.get("plan") or []
        history = task.get("history") or []
        step_idx = task["current_step"]
        current = plan[step_idx]["description"] if plan and step_idx < len(plan) else "N/A"
        history_lines = []
        for h in history:
            if h.get("type") == "step_result":
                history_lines.append(
                    f"  Step {h.get('step_index',0)+1} [{h.get('verdict')}]: {h.get('summary','')[:80]}"
                )
        result_block = f"\n\nResult:\n{task['result']}" if task.get("result") else ""
        send_message(
            f"Task [{task_id}]: {task['title']}\n"
            f"Status: {task['status']}\n"
            f"Goal: {task['goal'][:200]}\n"
            f"Due: {task.get('due_at','none')}\n"
            f"Steps: {step_idx+1}/{len(plan) or '?'}\n"
            f"Current step: {current[:100]}\n"
            + ("\nHistory:\n" + "\n".join(history_lines) if history_lines else "")
            + result_block
        )
        return True

    if cmd == "/start":
        return True

    return False
