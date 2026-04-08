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

# Shared state so the confirm hook can receive replies
_pending_confirm = {"waiting": False, "answer": None}


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


def poll_for_reply(offset):
    """Block until the user sends a reply. Returns (text, new_offset)."""
    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            updates = telegram_api("getUpdates", params)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue
                text = msg.get("text", "").strip()
                if text:
                    return text, offset
        except (socket.timeout, urllib.error.URLError):
            continue


def telegram_confirm(prompt):
    """Replacement for cli.confirm() — asks via Telegram and waits for y/n."""
    send_message(f"⚠️ {prompt} [y/n]")
    _pending_confirm["waiting"] = True
    # Wait for the answer to be filled in by the main loop
    while _pending_confirm["answer"] is None:
        time.sleep(0.1)
    answer = _pending_confirm["answer"]
    _pending_confirm["waiting"] = False
    _pending_confirm["answer"] = None
    return answer


# Monkey-patch confirm so the agent's y/n prompts go through Telegram
cli_module.confirm = telegram_confirm


def handle_telegram_command(text, agent, session, offset):
    """Handle /commands sent via Telegram. Returns (handled, new_offset)."""
    cmd = text.strip().lower()

    if cmd == "/chats":
        chats = list_chats(limit=20)
        if not chats:
            send_message("No saved conversations.")
            return True, offset
        lines = ["📋 Saved conversations:\n"]
        for i, chat in enumerate(chats, 1):
            updated = chat["updated_at"][:16].replace("T", " ")
            lines.append(f"{i}. {chat['title']}\n   id: {chat['id']}  |  {updated}")
        lines.append("\nReply with /resume <id> to load one.")
        send_message("\n".join(lines))
        return True, offset

    if cmd.startswith("/resume"):
        parts = cmd.split(maxsplit=1)
        if len(parts) < 2:
            send_message("Usage: /resume <chat_id>")
            return True, offset
        chat_id = parts[1].strip()
        chat = load_chat(chat_id)
        if not chat:
            send_message(f"Chat '{chat_id}' not found.")
            return True, offset
        # Save current conversation first
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)
        # Load the resumed conversation
        agent.messages = chat["messages"]
        session["chat_id"] = chat["id"]
        session["title"] = chat["title"]
        session["first_message_sent"] = True
        send_message(f"✅ Resumed: {chat['title']} ({len(chat['messages'])} messages)")
        return True, offset

    if cmd == "/new":
        # Save current conversation
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)
        # Reset
        session["chat_id"] = new_chat_id()
        session["title"] = ""
        session["first_message_sent"] = False
        system_msg = agent.messages[0] if agent.messages else None
        agent.messages = [system_msg] if system_msg else []
        send_message("🆕 New conversation started.")
        return True, offset

    return False, offset


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
    offset = None
    try:
        boot = telegram_api("getUpdates", {"timeout": 0})
        if boot.get("result"):
            offset = boot["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print("Telegram bridge running. Send messages to your bot.")

    while True:
        try:
            params = {"timeout": 30}  # long-poll
            if offset is not None:
                params["offset"] = offset

            updates = telegram_api("getUpdates", params)

            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})

                # Only accept messages from the authorized chat
                if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                    continue

                text = msg.get("text", "").strip()
                if not text:
                    continue

                # If we're waiting for a y/n confirmation answer
                if _pending_confirm["waiting"]:
                    _pending_confirm["answer"] = text.lower() in ("y", "yes")
                    continue

                print(f"[Telegram] {text}")

                # Handle slash commands
                if text.startswith("/"):
                    handled, offset = handle_telegram_command(
                        text, agent, session, offset
                    )
                    if handled:
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
