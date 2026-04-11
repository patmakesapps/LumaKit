"""Telegram bridge — lets you chat with LumaKit from your phone.

Run this instead of (or alongside) main.py:
    python telegram_bridge.py

Supports multiple users. Set TELEGRAM_ALLOWED_IDS in .env as a
comma-separated list of Telegram chat IDs. The first ID is the owner
and can use /adduser to authorize new users at runtime.
"""

import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

from agent import Agent
from core import auth, cli as cli_module
from core.telegram_owner_config import load_owner_config, save_owner_config
from core.telegram_user_config import load_user_configs, save_user_configs
from tools.comms.react import set_react_context
from tools.memory.memory_tools import set_active_user
from core.chat_store import list_chats, load_chat, make_title, new_chat_id, save_chat
from core.cli import Spinner
from core.email_checker import EmailChecker
from core.heartbeat import Heartbeat
from core.reminder_checker import ReminderChecker

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lumakit", "telegram_users.json")

# Load allowed users — .env seeds the list, telegram_users.json adds to it
_raw_ids = os.getenv("TELEGRAM_ALLOWED_IDS", "").strip()
_env_id_list = [id.strip() for id in _raw_ids.split(",") if id.strip()]
_env_ids = set(_env_id_list)


def _load_users_file():
    """Load extra authorized users from the JSON file."""
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(str(uid) for uid in json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_users_file(ids):
    """Save authorized user IDs to JSON (atomic write)."""
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, USERS_FILE)  # atomic on all platforms


ALLOWED_IDS = _env_ids | _load_users_file()
OWNER_ID = _env_id_list[0] if _env_id_list else None
OWNER_CONFIG = load_owner_config()
USER_CONFIGS = load_user_configs()

# Per-user sessions: {chat_id: {"messages": [...], "chat_id": ..., "title": ..., ...}}
_sessions = {}

# Per-user tool visibility toggle
_show_tools = {}  # {chat_id: bool}

# Tracks which user is currently being served (for confirm/send routing)
_active_chat_id = {"value": None}

# Global offset tracker
_poll_offset = {"value": None}

# Buffer of updates peeked during tool runs — drained by main loop.
# When we peek for /stop mid-run, any non-/stop messages land here so they
# aren't lost.
_pending_updates = []

# Disable the spinner — it's just noise in bridge mode
Spinner.start = lambda self: self
Spinner.stop = lambda self: None


def telegram_api(method, params=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    if params:
        payload = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
    else:
        req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(text, chat_id=None):
    """Send a message to a user via Telegram."""
    chat_id = chat_id or _active_chat_id["value"]
    while text:
        chunk, text = text[:4096], text[4096:]
        telegram_api("sendMessage", {"chat_id": chat_id, "text": chunk})


def download_telegram_photo(file_id):
    """Download a photo from Telegram by file_id. Returns raw bytes or None."""
    try:
        file_info = telegram_api("getFile", {"file_id": file_id})
        file_path = file_info.get("result", {}).get("file_path")
        if not file_path:
            return None
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"[photo download error] {e}")
        return None


def poll_for_reply(chat_id=None):
    """Block until the specified user sends a reply. Returns (text, new_offset)."""
    chat_id = str(chat_id or _active_chat_id["value"])
    while True:
        try:
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != chat_id:
                    continue
                text = msg.get("text", "").strip()
                if text:
                    return text, _poll_offset["value"]
        except (socket.timeout, urllib.error.URLError):
            continue


def check_for_stop():
    """Non-blocking peek at Telegram updates for a /stop from the active user.

    Called by the agent between tool rounds. Any non-/stop updates seen
    during the peek are buffered in _pending_updates so the main poll loop
    can process them normally on the next iteration.

    Returns True if a /stop from the currently-active user was found.
    """
    chat_id = _active_chat_id["value"]
    if not chat_id:
        return False

    params = {"timeout": 0}
    if _poll_offset["value"] is not None:
        params["offset"] = _poll_offset["value"]

    try:
        updates = telegram_api("getUpdates", params).get("result", [])
    except Exception:
        return False

    if not updates:
        return False

    found_stop = False
    for update in updates:
        _poll_offset["value"] = update["update_id"] + 1
        msg = update.get("message", {})
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()
        if text.lower() == "/stop" and msg_chat_id == str(chat_id):
            found_stop = True
            try:
                send_message("Stopped.", chat_id=msg_chat_id)
            except Exception:
                pass
        else:
            # Leave this for the main loop to handle
            _pending_updates.append(update)
    return found_stop


def telegram_confirm(prompt):
    """Replacement for cli.confirm() — polls Telegram directly for y/n."""
    send_message(f"⚠️ {prompt}\nReply y or n")
    chat_id = str(_active_chat_id["value"])

    while True:
        try:
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != chat_id:
                    continue
                text = msg.get("text", "").strip().lower()
                if text in ("y", "yes", "n", "no"):
                    return text in ("y", "yes")
        except (socket.timeout, urllib.error.URLError):
            continue


# Monkey-patch confirm everywhere it was imported
import agent as agent_module
cli_module.confirm = telegram_confirm
agent_module.confirm = telegram_confirm

# Monkey-patch show_tool_call/show_tool_result to forward to Telegram
_original_show_tool_call = cli_module.show_tool_call
_original_show_tool_result = cli_module.show_tool_result


def _telegram_show_tool_call(tool_name, inputs):
    _original_show_tool_call(tool_name, inputs)
    chat_id = _active_chat_id["value"]
    if chat_id and _show_tools.get(chat_id):
        detail = ""
        if "path" in inputs:
            detail = f" {inputs['path']}"
        elif "command" in inputs:
            cmd = inputs["command"]
            detail = f" {cmd[:80]}"
        send_message(f"🔧 [{tool_name}]{detail}", chat_id=chat_id)


def _telegram_show_tool_result(result):
    _original_show_tool_result(result)
    chat_id = _active_chat_id["value"]
    if chat_id and _show_tools.get(chat_id):
        if not result.get("success"):
            send_message(f"❌ {result.get('error', 'unknown')}", chat_id=chat_id)
        else:
            data = result.get("data", {})
            # Send a brief summary of the result
            if data.get("skipped"):
                send_message("⏭ skipped", chat_id=chat_id)
            elif "saved" in data:
                send_message(f"✅ saved (id:{data.get('id', '?')})", chat_id=chat_id)
            elif "updated" in data:
                send_message(f"✅ updated (id:{data.get('id', '?')})", chat_id=chat_id)
            elif "count" in data:
                send_message(f"📋 found {data['count']} result(s)", chat_id=chat_id)


cli_module.show_tool_call = _telegram_show_tool_call
cli_module.show_tool_result = _telegram_show_tool_result
agent_module.show_tool_call = _telegram_show_tool_call
agent_module.show_tool_result = _telegram_show_tool_result


def _get_session(chat_id):
    """Get or create a session for a user."""
    chat_id = str(chat_id)
    if chat_id not in _sessions:
        _sessions[chat_id] = {
            "chat_id": new_chat_id(),
            "title": "",
            "first_message_sent": False,
            "messages": None,  # will copy from agent on first use
        }
    return _sessions[chat_id]


def _swap_in(agent, session):
    """Load a user's message history into the agent."""
    if session["messages"] is None:
        # First time — clone the system prompt
        session["messages"] = [agent.messages[0].copy()]
    agent.messages = session["messages"]


def _resume_chat(chat_id_str, agent, session):
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


def _save_allowed_ids():
    """Persist authorized users to JSON (never touches .env)."""
    _save_users_file(ALLOWED_IDS)


def _get_user_label(chat_id):
    """Get a display name for a chat ID from Telegram."""
    chat_id = str(chat_id)
    try:
        result = telegram_api("getChat", {"chat_id": chat_id})
        chat = result.get("result", {})
        first = chat.get("first_name", "")
        last = chat.get("last_name", "")
        return f"{first} {last}".strip() or chat_id
    except Exception:
        return chat_id


def _save_owner_config():
    """Persist owner-only Telegram runtime settings."""
    global OWNER_CONFIG
    OWNER_CONFIG = save_owner_config(OWNER_CONFIG)


def _get_user_config(chat_id):
    chat_id = str(chat_id)
    config = USER_CONFIGS.get(chat_id)
    if not config:
        config = {"personality_prompt": ""}
        USER_CONFIGS[chat_id] = config
    return config


def _save_user_configs():
    global USER_CONFIGS
    USER_CONFIGS = save_user_configs(USER_CONFIGS)


def _get_owner_effective_config(agent):
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


def _apply_chat_runtime(agent, session, chat_id):
    """Switch agent runtime config for the active Telegram user."""
    user_cfg = _get_user_config(chat_id)
    personality_prompt = user_cfg.get("personality_prompt") or None
    if str(chat_id) == str(OWNER_ID):
        config = _get_owner_effective_config(agent)
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


def _send_owner_model_status(agent):
    cfg = _get_owner_effective_config(agent)
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
            _apply_chat_runtime(agent, session, chat_id)
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
            _apply_chat_runtime(agent, session, chat_id)
            send_message(f"Owner fallback model set to: {model_name}")
            return True

        if choice == "3":
            if not agent.local_model:
                send_message("OLLAMA_LOCAL_MODEL is not set in .env, so local mode can't be enabled.")
                return True
            OWNER_CONFIG["use_local_model"] = not bool(OWNER_CONFIG.get("use_local_model"))
            _save_owner_config()
            _apply_chat_runtime(agent, session, chat_id)
            state = "on" if OWNER_CONFIG["use_local_model"] else "off"
            send_message(
                f"Local model mode: {state}."
                + (f" Effective primary is now {agent.model}." if state == "on" else "")
            )
            return True

        if choice == "4":
            OWNER_CONFIG["primary_model"] = ""
            _save_owner_config()
            _apply_chat_runtime(agent, session, chat_id)
            send_message("Primary override reset.")
            return True

        if choice == "5":
            OWNER_CONFIG["fallback_model"] = ""
            _save_owner_config()
            _apply_chat_runtime(agent, session, chat_id)
            send_message("Fallback override reset.")
            return True

        if choice == "6":
            OWNER_CONFIG.update(
                {
                    "primary_model": "",
                    "fallback_model": "",
                    "use_local_model": False,
                }
            )
            _save_owner_config()
            _apply_chat_runtime(agent, session, chat_id)
            send_message("All model overrides reset.")
            return True

        send_message("Invalid choice. Reply with 1-7.")


def handle_telegram_command(text, agent, session, chat_id):
    """Handle /commands sent via Telegram. Returns True if handled."""
    raw = text.strip()
    parts = raw.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/stop":
        # If Lumi is actively running, the check_for_stop peek inside
        # agent.ask_llm catches /stop before it ever gets here. Reaching this
        # branch means there was nothing to stop.
        send_message("Nothing to stop — I wasn't working on anything.")
        return True

    if cmd == "/tools":
        current = _show_tools.get(str(chat_id), False)
        _show_tools[str(chat_id)] = not current
        state = "on" if not current else "off"
        send_message(f"Tool visibility: {state}")
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
                _resume_chat(chats[pick]["id"], agent, session)
                _apply_chat_runtime(agent, session, chat_id)
            else:
                send_message("Invalid number.")
        except ValueError:
            _resume_chat(reply.strip(), agent, session)
            _apply_chat_runtime(agent, session, chat_id)
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
            owner_cfg = _get_owner_effective_config(agent)
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
            f"Personality override: {'set' if user_cfg.get('personality_prompt') else 'not set'}"
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
            _apply_chat_runtime(agent, session, chat_id)
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
            _apply_chat_runtime(agent, session, chat_id)
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
                OWNER_CONFIG.update(
                    {
                        "primary_model": "",
                        "fallback_model": "",
                        "use_local_model": False,
                    }
                )
            else:
                send_message("Usage: /model reset primary|fallback|all")
                return True
            _save_owner_config()
            _apply_chat_runtime(agent, session, chat_id)
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
            _apply_chat_runtime(agent, session, chat_id)
            send_message("Your Telegram personality override was updated.")
            return True

        if prompt_action == "reset":
            user_cfg["personality_prompt"] = ""
            _save_user_configs()
            _apply_chat_runtime(agent, session, chat_id)
            send_message("Your Telegram personality override was cleared.")
            return True

        send_message(
            "Unknown personality command. Use /personality, /personality set <text>, or /personality reset."
        )
        return True

    # Owner-only: add a new user
    if cmd == "/adduser" and str(chat_id) == str(OWNER_ID):
        pending = _get_pending_users()
        if not pending:
            send_message("No new users have messaged the bot yet. "
                         "Have them send a message first, then try /adduser again.")
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
                send_message("Welcome! You can now chat with Lumi. "
                             "Send /help for commands.", chat_id=new_id)
            else:
                send_message("Invalid number.")
        except ValueError:
            send_message("Invalid input.")
        return True

    # Owner-only: list users
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


# Track unauthorized message attempts for /adduser
_pending_users = {}  # {chat_id: first_name}


def _get_pending_users():
    """Return list of (chat_id, name) for unauthorized users who tried to message."""
    return [(uid, name) for uid, name in _pending_users.items()]


def main():
    if not TOKEN or not ALLOWED_IDS:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_IDS in .env first.")
        sys.exit(1)

    auth.set_owner(OWNER_ID)

    verbose = "--verbose" in sys.argv

    def telegram_status(msg):
        """Send progress updates to the active user mid-work."""
        chat_id = _active_chat_id["value"]
        if chat_id:
            send_message(msg, chat_id=chat_id)
            # Re-set typing indicator after sending the update
            try:
                telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            except Exception:
                pass

    agent = Agent(
        verbose=verbose,
        status_callback=telegram_status,
        check_interrupt=check_for_stop,
    )

    # Start reminders — personal pings the creator, family pings everyone
    def notify_telegram(reminder):
        target = reminder.get("chat_id")
        if target:
            send_message(f"🔔 Reminder: {reminder['content']}", chat_id=target)
            print(f"[reminder -> {target}] {reminder['content']}")
        else:
            for uid in ALLOWED_IDS:
                send_message(f"🔔 Family reminder: {reminder['content']}", chat_id=uid)
            print(f"[family reminder] {reminder['content']}")

    reminders = ReminderChecker(interval=30, notify=notify_telegram)
    reminders.start()

    # Start heartbeat — sends to owner by default
    def heartbeat_send(msg):
        target = OWNER_ID or list(ALLOWED_IDS)[0]
        send_message(msg, chat_id=target)
        print(f"[heartbeat] {msg[:200]}")

    def heartbeat_inject_session(text):
        """Append the heartbeat's outbound message to the owner's session
        so follow-up replies have the context of what Lumi just said."""
        target = OWNER_ID or (list(ALLOWED_IDS)[0] if ALLOWED_IDS else None)
        if not target:
            return
        session = _get_session(target)
        if session["messages"] is None:
            session["messages"] = [
                agent.build_system_message(
                    extra_instructions=_get_user_config(target).get("personality_prompt") or None
                )
            ]
        session["messages"].append({"role": "assistant", "content": text})
        if not session["first_message_sent"]:
            session["title"] = make_title(text)
            session["first_message_sent"] = True
        save_chat(session["chat_id"], session["title"], session["messages"])

    heartbeat = Heartbeat(
        send=heartbeat_send,
        interval=900,
        cooldown=3600,
        inject_session=heartbeat_inject_session,
    )
    heartbeat.start()

    # Start email checker — polls every 60s, triages new mail via LLM,
    # notifies owner on Telegram, and injects summaries into owner's session
    def email_notify(msg):
        target = OWNER_ID or list(ALLOWED_IDS)[0]
        send_message(msg, chat_id=target)
        print(f"[email -> {target}] {msg[:200]}")

    def email_ask_llm(prompt):
        """Side-channel LLM call that doesn't touch any user's session."""
        from ollama_client import OllamaClient
        owner_cfg = _get_owner_effective_config(agent)
        client = OllamaClient(fallback_model=owner_cfg["fallback_model"])
        response = client.chat(
            model=owner_cfg["primary_model"],
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            deadline=90,
        )
        return response.get("message", {}).get("content", "").strip()

    def email_inject_session(text):
        """Append a synthetic assistant message to the owner's session
        so 'reply to that' references work later."""
        target = OWNER_ID or list(ALLOWED_IDS)[0]
        if not target:
            return
        session = _get_session(target)
        if session["messages"] is None:
            session["messages"] = [
                agent.build_system_message(
                    extra_instructions=_get_user_config(target).get("personality_prompt") or None
                )
            ]
        session["messages"].append({"role": "assistant", "content": text})

    email_checker = EmailChecker(
        notify_owner=email_notify,
        ask_llm=email_ask_llm,
        inject_session=email_inject_session,
        interval=60,
    )
    email_checker.start()

    _AFFIRM = {"yes", "y", "yep", "yeah", "send", "send it", "do it", "ok", "okay", "sure"}
    _DENY = {"no", "n", "nah", "skip", "cancel", "nope", "don't", "dont"}

    def _handle_pending_draft(text, chat_id):
        """If a draft is pending approval, intercept the owner's yes/no.
        Returns True if the message was consumed."""
        draft = email_checker.pending_draft
        if not draft:
            return False
        if str(chat_id) != str(OWNER_ID):
            return False
        normalized = text.strip().lower()
        if normalized in _AFFIRM:
            from tools.comms.email import send_preapproved
            result = send_preapproved(draft["to"], draft["subject"], draft["body"])
            email_checker.clear_pending_draft()
            if result.get("sent"):
                send_message(f"✅ Sent to {draft['from_label']}.", chat_id=chat_id)
            else:
                send_message(f"❌ Couldn't send: {result.get('error', 'unknown error')}", chat_id=chat_id)
            return True
        if normalized in _DENY:
            email_checker.clear_pending_draft()
            send_message("👍 Skipped. Draft discarded.", chat_id=chat_id)
            return True
        return False

    # Start from the latest update so we don't replay old messages
    try:
        boot = telegram_api("getUpdates", {"timeout": 0})
        if boot.get("result"):
            _poll_offset["value"] = boot["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print(f"Telegram bridge running. {len(ALLOWED_IDS)} authorized user(s).")

    while True:
        try:
            # Drain any updates that were peeked mid-run (by check_for_stop)
            # before making a fresh network call.
            if _pending_updates:
                buffered = _pending_updates[:]
                _pending_updates.clear()
                updates = {"result": buffered}
            else:
                params = {"timeout": 30}
                if _poll_offset["value"] is not None:
                    params["offset"] = _poll_offset["value"]
                updates = telegram_api("getUpdates", params)

            for update in updates.get("result", []):
                # Never downgrade the offset — check_for_stop may have already
                # advanced it past updates we're now draining from the buffer.
                new_offset = update["update_id"] + 1
                if _poll_offset["value"] is None or new_offset > _poll_offset["value"]:
                    _poll_offset["value"] = new_offset
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()

                # Check for photo
                photo_list = msg.get("photo")
                has_photo = bool(photo_list)
                caption = msg.get("caption", "").strip()

                if not text and not has_photo or not chat_id:
                    continue

                # Unauthorized user — track them for /adduser, reject
                if chat_id not in ALLOWED_IDS:
                    name = msg.get("from", {}).get("first_name", "Unknown")
                    _pending_users[chat_id] = name
                    send_message(
                        "Not authorized. Ask the household admin to run /adduser.",
                        chat_id=chat_id,
                    )
                    print(f"[unauthorized] {name} ({chat_id}): {text or '[photo]'}")
                    continue

                # Set active user for send_message/confirm routing
                _active_chat_id["value"] = chat_id
                user_name = msg.get("from", {}).get("first_name", "?")
                message_id = msg.get("message_id")
                set_react_context(chat_id, message_id)
                set_active_user(chat_id)
                auth.set_active_user(chat_id)
                heartbeat.notify_activity()

                # Get/create this user's session and swap in their history
                session = _get_session(chat_id)
                _swap_in(agent, session)
                _apply_chat_runtime(agent, session, chat_id)

                # Handle photo messages
                if has_photo:
                    print(f"[{user_name}] [photo] {caption or '(no caption)'}")
                    # Grab the largest photo (last in the array)
                    file_id = photo_list[-1]["file_id"]
                    image_data = download_telegram_photo(file_id)
                    if not image_data:
                        send_message("Sorry, I couldn't download that photo. Please try again.")
                        continue
                    try:
                        try:
                            telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                        except Exception:
                            pass
                        response = agent.ask_llm_with_image(
                            prompt=caption or None, image_data=image_data
                        )
                        reply = response.get("message", {}).get("content", "")
                        if reply:
                            send_message(reply)
                            print(f"[Lumi -> {user_name}] {reply[:200]}")
                        session["messages"] = agent.messages
                        if not session["first_message_sent"]:
                            session["title"] = make_title(caption or "Photo")
                            session["first_message_sent"] = True
                        if session["first_message_sent"] and len(agent.messages) > 1:
                            save_chat(session["chat_id"], session["title"], agent.messages)
                    except Exception as e:
                        error_msg = f"Error processing photo: {e}"
                        send_message(error_msg)
                        print(f"[error] {error_msg}")
                    continue

                print(f"[{user_name}] {text}")

                # Intercept yes/no for a pending email draft before the agent sees it
                if _handle_pending_draft(text, chat_id):
                    continue

                # Handle slash commands
                if text.startswith("/"):
                    if handle_telegram_command(text, agent, session, chat_id):
                        continue

                try:
                    try:
                        telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                    except Exception:
                        pass
                    response = agent.ask_llm(text)
                    reply = response.get("message", {}).get("content", "")
                    if reply:
                        send_message(reply)
                        print(f"[Lumi -> {user_name}] {reply[:200]}")

                    # Sync messages back to session
                    session["messages"] = agent.messages

                    if not session["first_message_sent"]:
                        session["title"] = make_title(text)
                        session["first_message_sent"] = True

                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(
                            session["chat_id"], session["title"], agent.messages
                        )

                except Exception as e:
                    error_msg = f"Error: {e}"
                    send_message(error_msg)
                    print(f"[error] {error_msg}")

        except KeyboardInterrupt:
            # Save all sessions on exit
            for cid, sess in _sessions.items():
                if sess["first_message_sent"] and sess["messages"] and len(sess["messages"]) > 1:
                    save_chat(sess["chat_id"], sess["title"], sess["messages"])
            reminders.stop()
            heartbeat.stop()
            email_checker.stop()
            print("\nBridge stopped.")
            break
        except (socket.timeout, urllib.error.URLError):
            continue
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
