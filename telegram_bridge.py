"""Telegram bridge — lets you chat with LumaKit from your phone.

Run this instead of (or alongside) main.py:
    python telegram_bridge.py

It polls Telegram for messages, feeds them to the agent, and sends
the response back through the bot.
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
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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


def send_message(text):
    """Send a message to the user via Telegram."""
    while text:
        chunk, text = text[:4096], text[4096:]
        telegram_api("sendMessage", {"chat_id": CHAT_ID, "text": chunk})


def poll_for_reply(offset=None):
    """Block until the user sends a reply. Returns (text, new_offset)."""
    if offset is None:
        offset = _poll_offset["value"]
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                _poll_offset["value"] = offset
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue
                text = msg.get("text", "").strip()
                if text:
                    return text, offset
        except (socket.timeout, urllib.error.URLError):
            continue


# Global offset tracker so confirm can poll directly
_poll_offset = {"value": None}


def telegram_confirm(prompt):
    """Replacement for cli.confirm() — polls Telegram directly for y/n."""
    send_message(f"⚠️ {prompt}\nReply y or n")

    # Poll Telegram directly (this runs inside ask_llm, so the main loop is blocked)
    while True:
        try:
            params = {"timeout": 30}
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
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


def _resume_chat(chat_id, agent, session):
    """Load a saved conversation into the agent."""
    chat = load_chat(chat_id)
    if not chat:
        send_message(f"Chat '{chat_id}' not found.")
        return
    # Save current conversation first
    if session["first_message_sent"] and len(agent.messages) > 1:
        save_chat(session["chat_id"], session["title"], agent.messages)
    agent.messages = chat["messages"]
    session["chat_id"] = chat["id"]
    session["title"] = chat["title"]
    session["first_message_sent"] = True
    send_message(f"✅ Resumed: {chat['title']} ({len(chat['messages'])} messages)")


def handle_telegram_command(text, agent, session):
    """Handle /commands sent via Telegram. Returns True if handled."""
    cmd = text.strip().lower()

    if cmd == "/help":
        send_message(
            "Commands:\n\n"
            "/chats - list & resume saved conversations\n"
            "/new - start a fresh conversation\n"
            "/status - show model, storage, index info\n"
            "/help - this message"
        )
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

        # Wait for their pick
        reply, _ = poll_for_reply(_poll_offset["value"])
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
        send_message("New conversation started.")
        return True

    if cmd == "/status":
        health = agent.storage.check_health()
        sym_count = len(agent.code_index.table.all_symbols())
        msg_count = len(agent.messages)
        model = agent.model or "not set"
        chat_count = len(list_chats(limit=100))
        send_message(
            f"Status\n\n"
            f"Model: {model}\n"
            f"Messages: {msg_count} in current conversation\n"
            f"Saved chats: {chat_count}\n"
            f"Index: {sym_count} symbols\n"
            f"Storage: {health['total_display']} / {health['budget_display']} "
            f"({health['usage_percent']:.0f}%)"
        )
        return True

    # Ignore /start (Telegram's built-in)
    if cmd == "/start":
        return True

    return False


def main():
    if not TOKEN or not CHAT_ID:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
        sys.exit(1)

    verbose = "--verbose" in sys.argv
    agent = Agent(verbose=verbose)

    # Start reminders with Telegram notifications
    def notify_telegram(reminder):
        send_message(f"🔔 Reminder: {reminder['content']}")
        print(f"[reminder] {reminder['content']}")

    reminders = ReminderChecker(interval=30, notify=notify_telegram)
    reminders.start()

    # Chat persistence
    session = {
        "chat_id": new_chat_id(),
        "title": "",
        "first_message_sent": False,
    }

    # Start from the latest update so we don't replay old messages
    try:
        boot = telegram_api("getUpdates", {"timeout": 0})
        if boot.get("result"):
            _poll_offset["value"] = boot["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print("Telegram bridge running. Send messages to your bot.")

    while True:
        try:
            params = {"timeout": 30}  # long-poll
            if _poll_offset["value"] is not None:
                params["offset"] = _poll_offset["value"]

            updates = telegram_api("getUpdates", params)

            for update in updates.get("result", []):
                _poll_offset["value"] = update["update_id"] + 1
                msg = update.get("message", {})

                # Only accept messages from the authorized chat
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                text = msg.get("text", "").strip()
                if not text:
                    continue

                print(f"[Telegram] {text}")

                # Handle slash commands
                if text.startswith("/"):
                    if handle_telegram_command(text, agent, session):
                        continue

                try:
                    response = agent.ask_llm(text)
                    reply = response.get("message", {}).get("content", "")
                    if reply:
                        send_message(reply)
                        print(f"[Lumi] {reply[:200]}")
                    else:
                        send_message("(no response)")

                    # Auto-title from first message
                    if not session["first_message_sent"]:
                        session["title"] = make_title(text)
                        session["first_message_sent"] = True

                    # Auto-save after each exchange
                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(
                            session["chat_id"], session["title"], agent.messages
                        )

                except Exception as e:
                    error_msg = f"Error: {e}"
                    send_message(error_msg)
                    print(f"[error] {error_msg}")

        except KeyboardInterrupt:
            # Save on exit
            if session["first_message_sent"] and len(agent.messages) > 1:
                save_chat(session["chat_id"], session["title"], agent.messages)
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
