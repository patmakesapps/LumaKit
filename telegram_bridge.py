"""Telegram bridge — lets you chat with LumaKit from your phone.

Run this instead of (or alongside) main.py:
    python telegram_bridge.py

Supports multiple users. Set TELEGRAM_ALLOWED_IDS in .env as a
comma-separated list of Telegram chat IDs. The first ID is the owner
and can use /adduser to authorize new users at runtime.
"""

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
from core import cli as cli_module
from core.chat_store import list_chats, load_chat, make_title, new_chat_id, save_chat
from core.cli import Spinner
from core.reminder_checker import ReminderChecker

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lumakit", "telegram_users.json")

# Load allowed users — .env seeds the list, telegram_users.json adds to it
_raw_ids = os.getenv("TELEGRAM_ALLOWED_IDS", "").strip()
_env_ids = set(id.strip() for id in _raw_ids.split(",") if id.strip())


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
OWNER_ID = list(_env_ids)[0] if _env_ids else None

# Per-user sessions: {chat_id: {"messages": [...], "chat_id": ..., "title": ..., ...}}
_sessions = {}

# Tracks which user is currently being served (for confirm/send routing)
_active_chat_id = {"value": None}

# Global offset tracker
_poll_offset = {"value": None}

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


def handle_telegram_command(text, agent, session, chat_id):
    """Handle /commands sent via Telegram. Returns True if handled."""
    cmd = text.strip().lower()

    if cmd == "/help":
        lines = [
            "Commands:\n",
            "/chats - list & resume saved conversations",
            "/new - start a fresh conversation",
            "/status - show model, storage, index info",
            "/help - this message",
        ]
        if str(chat_id) == str(OWNER_ID):
            lines.append("/adduser - authorize a new user")
            lines.append("/users - list authorized users")
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
            else:
                send_message("Invalid number.")
        except ValueError:
            _resume_chat(reply.strip(), agent, session)
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
        chat_count = len(list_chats(limit=100))
        user_count = len(ALLOWED_IDS)
        send_message(
            f"Status\n\n"
            f"Model: {model}\n"
            f"Messages: {msg_count} in current conversation\n"
            f"Saved chats: {chat_count}\n"
            f"Index: {sym_count} symbols\n"
            f"Storage: {health['total_display']} / {health['budget_display']} "
            f"({health['usage_percent']:.0f}%)\n"
            f"Users: {user_count} authorized"
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

    verbose = "--verbose" in sys.argv
    agent = Agent(verbose=verbose)

    # Start reminders — notify all authorized users
    def notify_telegram(reminder):
        for uid in ALLOWED_IDS:
            send_message(f"🔔 Reminder: {reminder['content']}", chat_id=uid)
        print(f"[reminder] {reminder['content']}")

    reminders = ReminderChecker(interval=30, notify=notify_telegram)
    reminders.start()

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
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]

            updates = telegram_api("getUpdates", params)

            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()

                if not text or not chat_id:
                    continue

                # Unauthorized user — track them for /adduser, reject
                if chat_id not in ALLOWED_IDS:
                    name = msg.get("from", {}).get("first_name", "Unknown")
                    _pending_users[chat_id] = name
                    send_message(
                        "Not authorized. Ask the household admin to run /adduser.",
                        chat_id=chat_id,
                    )
                    print(f"[unauthorized] {name} ({chat_id}): {text}")
                    continue

                # Set active user for send_message/confirm routing
                _active_chat_id["value"] = chat_id
                user_name = msg.get("from", {}).get("first_name", "?")

                # Get/create this user's session and swap in their history
                session = _get_session(chat_id)
                _swap_in(agent, session)

                print(f"[{user_name}] {text}")

                # Handle slash commands
                if text.startswith("/"):
                    if handle_telegram_command(text, agent, session, chat_id):
                        continue

                try:
                    response = agent.ask_llm(text)
                    reply = response.get("message", {}).get("content", "")
                    if reply:
                        send_message(reply)
                        print(f"[Lumi -> {user_name}] {reply[:200]}")
                    else:
                        send_message("(no response)")

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
            print("\nBridge stopped.")
            break
        except (socket.timeout, urllib.error.URLError):
            continue
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
