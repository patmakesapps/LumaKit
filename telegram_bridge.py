"""Telegram bridge — lets you chat with LumaKit from your phone.

Run this instead of (or alongside) main.py:
    python telegram_bridge.py

Supports multiple users. Set TELEGRAM_ALLOWED_IDS in .env as a
comma-separated list of Telegram chat IDs. The first ID is the owner
and can use /adduser to authorize new users at runtime.
"""

import socket
import sys
import time
import urllib.error

from dotenv import load_dotenv

load_dotenv()

from agent import Agent
from core import auth, cli as cli_module
from core.chat_store import make_title, save_chat
from core.cli import Spinner
from core.email_checker import EmailChecker
from core.heartbeat import Heartbeat
from core.reminder_checker import ReminderChecker
from core.task_runner import TaskRunner
from core.telegram_api import (
    download_telegram_file,
    download_telegram_photo,
    send_chat_action,
    telegram_api,
)
from core.telegram_commands import apply_chat_runtime, handle_telegram_command, swap_in
from core.telegram_io import check_for_stop, send_message, send_tts_reply, telegram_confirm
from core.telegram_speech import SpeechClient
from core.telegram_state import (
    ALLOWED_IDS,
    OWNER_ID,
    TOKEN,
    _active_chat_id,
    _get_session,
    _get_user_config,
    _pending_updates,
    _pending_users,
    _poll_offset,
    _sessions,
)
from tools.comms.react import set_react_context
from tools.memory.memory_tools import set_active_user

# Disable the spinner — it's just noise in bridge mode
Spinner.start = lambda self: self
Spinner.stop = lambda self: None

# ---------------------------------------------------------------------------
# Monkey-patch confirm and tool-call display into cli/agent modules
# ---------------------------------------------------------------------------
import agent as agent_module

cli_module.confirm = telegram_confirm
agent_module.confirm = telegram_confirm

_original_show_tool_call = cli_module.show_tool_call
_original_show_tool_result = cli_module.show_tool_result

_show_tools: dict = {}  # {chat_id: bool}


def _telegram_show_tool_call(tool_name, inputs):
    _original_show_tool_call(tool_name, inputs)
    chat_id = _active_chat_id["value"]
    if chat_id and _show_tools.get(chat_id):
        detail = ""
        if "path" in inputs:
            detail = f" {inputs['path']}"
        elif "command" in inputs:
            detail = f" {inputs['command'][:80]}"
        send_message(f"🔧 [{tool_name}]{detail}", chat_id=chat_id)


def _telegram_show_tool_result(result):
    _original_show_tool_result(result)
    chat_id = _active_chat_id["value"]
    if chat_id and _show_tools.get(chat_id):
        if not result.get("success"):
            send_message(f"❌ {result.get('error', 'unknown')}", chat_id=chat_id)
        else:
            data = result.get("data", {})
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

# Pass _show_tools into telegram_commands so /tools toggle works
import core.telegram_state as _ts
_ts._show_tools = _show_tools


# ---------------------------------------------------------------------------
# Reply helper — sends text or TTS depending on user preference
# ---------------------------------------------------------------------------

def _send_reply(reply, chat_id, user_name, speech_client):
    user_cfg = _get_user_config(chat_id)
    if user_cfg.get("voice_replies"):
        send_tts_reply(reply, chat_id=chat_id, speech_client=speech_client)
    else:
        send_message(reply)
    print(f"[Lumi -> {user_name}] {reply[:200]}")


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def main():
    if not TOKEN or not ALLOWED_IDS:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_IDS in .env first.")
        sys.exit(1)

    auth.set_owner(OWNER_ID)
    verbose = "--verbose" in sys.argv

    def telegram_status(msg):
        chat_id = _active_chat_id["value"]
        if chat_id:
            send_message(msg, chat_id=chat_id)
            try:
                send_chat_action(chat_id, "typing")
            except Exception:
                pass

    agent = Agent(
        verbose=verbose,
        status_callback=telegram_status,
        check_interrupt=check_for_stop,
    )
    speech_client = SpeechClient()

    # --- Reminders ---
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

    # --- Task runner ---
    def task_notify(msg: str, chat_id: str | None = None):
        target = chat_id or OWNER_ID or (list(ALLOWED_IDS)[0] if ALLOWED_IDS else None)
        if target:
            send_message(msg, chat_id=target)
            print(f"[task -> {target}] {msg[:120]}")

    task_runner = TaskRunner(interval=60, notify=task_notify)
    task_runner.start()

    # --- Heartbeat ---
    def heartbeat_send(msg):
        target = OWNER_ID or list(ALLOWED_IDS)[0]
        send_message(msg, chat_id=target)
        print(f"[heartbeat] {msg[:200]}")

    def heartbeat_inject_session(text):
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

    # --- Email checker ---
    def email_notify(msg):
        target = OWNER_ID or list(ALLOWED_IDS)[0]
        send_message(msg, chat_id=target)
        print(f"[email -> {target}] {msg[:200]}")

    def email_ask_llm(prompt):
        from ollama_client import OllamaClient
        from core.telegram_commands import get_owner_effective_config
        owner_cfg = get_owner_effective_config(agent)
        client = OllamaClient(fallback_model=owner_cfg["fallback_model"])
        response = client.chat(
            model=owner_cfg["primary_model"],
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            deadline=90,
        )
        return response.get("message", {}).get("content", "").strip()

    def email_inject_session(text):
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

    # Skip any updates that arrived before the bot started
    try:
        boot = telegram_api("getUpdates", {"timeout": 0})
        if boot.get("result"):
            _poll_offset["value"] = boot["result"][-1]["update_id"] + 1
    except Exception:
        pass

    print(f"Telegram bridge running. {len(ALLOWED_IDS)} authorized user(s).")

    while True:
        try:
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
                new_offset = update["update_id"] + 1
                if _poll_offset["value"] is None or new_offset > _poll_offset["value"]:
                    _poll_offset["value"] = new_offset

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                photo_list = msg.get("photo")
                has_photo = bool(photo_list)
                caption = msg.get("caption", "").strip()
                voice = msg.get("voice")
                audio = msg.get("audio")
                has_audio = bool(voice or audio)

                if (not text and not has_photo and not has_audio) or not chat_id:
                    continue

                # Unauthorized user
                if chat_id not in ALLOWED_IDS:
                    name = msg.get("from", {}).get("first_name", "Unknown")
                    _pending_users[chat_id] = name
                    send_message(
                        "Not authorized. Ask the household admin to run /adduser.",
                        chat_id=chat_id,
                    )
                    preview = text if text else ("[photo]" if has_photo else "[audio]")
                    print(f"[unauthorized] {name} ({chat_id}): {preview}")
                    continue

                _active_chat_id["value"] = chat_id
                user_name = msg.get("from", {}).get("first_name", "?")
                message_id = msg.get("message_id")
                set_react_context(chat_id, message_id)
                set_active_user(chat_id)
                auth.set_active_user(chat_id)
                heartbeat.notify_activity()

                session = _get_session(chat_id)
                swap_in(agent, session)
                apply_chat_runtime(agent, session, chat_id)

                # Photo
                if has_photo:
                    print(f"[{user_name}] [photo] {caption or '(no caption)'}")
                    file_id = photo_list[-1]["file_id"]
                    image_data = download_telegram_photo(file_id)
                    if not image_data:
                        send_message("Sorry, I couldn't download that photo. Please try again.")
                        continue
                    try:
                        try:
                            send_chat_action(chat_id, "typing")
                        except Exception:
                            pass
                        response = agent.ask_llm_with_image(prompt=caption or None, image_data=image_data)
                        reply = response.get("message", {}).get("content", "")
                        if reply:
                            _send_reply(reply, chat_id, user_name, speech_client)
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

                # Voice / audio
                if has_audio:
                    label = "[voice]" if voice else "[audio]"
                    print(f"[{user_name}] {label} {caption or '(no caption)'}")
                    media = voice or audio or {}
                    file_id = media.get("file_id")
                    file_name = media.get("file_name") or ("voice.ogg" if voice else "audio.bin")
                    audio_data, file_path = download_telegram_file(file_id)
                    if not audio_data:
                        send_message("Sorry, I couldn't download that audio. Please try again.")
                        continue
                    if not speech_client.can_transcribe:
                        send_message(
                            "Voice input isn't ready yet. Build whisper.cpp and point the bridge at the local model first."
                        )
                        continue
                    try:
                        try:
                            send_chat_action(chat_id, "typing")
                        except Exception:
                            pass
                        transcript = speech_client.transcribe(audio_data, filename=file_path or file_name)
                        effective_text = transcript
                        if caption:
                            effective_text = f"{caption}\n\nVoice transcript:\n{transcript}"
                        response = agent.ask_llm(effective_text)
                        reply = response.get("message", {}).get("content", "")
                        if reply:
                            _send_reply(reply, chat_id, user_name, speech_client)
                        session["messages"] = agent.messages
                        if not session["first_message_sent"]:
                            session["title"] = make_title(transcript)
                            session["first_message_sent"] = True
                        if session["first_message_sent"] and len(agent.messages) > 1:
                            save_chat(session["chat_id"], session["title"], agent.messages)
                    except Exception as e:
                        error_msg = f"Error processing audio: {e}"
                        send_message(error_msg)
                        print(f"[error] {error_msg}")
                    continue

                # Text
                print(f"[{user_name}] {text}")

                if _handle_pending_draft(text, chat_id):
                    continue

                if text.startswith("/"):
                    if handle_telegram_command(text, agent, session, chat_id, speech_client):
                        continue

                try:
                    try:
                        send_chat_action(chat_id, "typing")
                    except Exception:
                        pass
                    response = agent.ask_llm(text)
                    reply = response.get("message", {}).get("content", "")
                    if reply:
                        _send_reply(reply, chat_id, user_name, speech_client)
                    session["messages"] = agent.messages
                    if not session["first_message_sent"]:
                        session["title"] = make_title(text)
                        session["first_message_sent"] = True
                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(session["chat_id"], session["title"], agent.messages)
                except Exception as e:
                    error_msg = f"Error: {e}"
                    send_message(error_msg)
                    print(f"[error] {error_msg}")

        except KeyboardInterrupt:
            for cid, sess in _sessions.items():
                if sess["first_message_sent"] and sess["messages"] and len(sess["messages"]) > 1:
                    save_chat(sess["chat_id"], sess["title"], sess["messages"])
            reminders.stop()
            heartbeat.stop()
            email_checker.stop()
            task_runner.stop()
            print("\nBridge stopped.")
            break
        except (socket.timeout, urllib.error.URLError):
            continue
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
