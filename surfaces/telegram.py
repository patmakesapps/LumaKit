"""Telegram surface — lets you chat with LumaKit from your phone.

Run with:
    python -m surfaces.telegram

Supports multiple users. Set TELEGRAM_ALLOWED_IDS in .env as a
comma-separated list of Telegram chat IDs. The first ID is the owner
and can use /adduser to authorize new users at runtime.
"""

import socket
import sys
import time
import urllib.error

from pathlib import Path
from dotenv import load_dotenv

# Load config.env from ~/.lumakit/ first (user overrides), then repo-root .env
_user_env = Path.home() / ".lumakit" / "config.env"
if _user_env.exists():
    load_dotenv(_user_env)
load_dotenv()  # repo-root .env — won't override keys already set

from agent import Agent
from core import auth
from core.chat_store import make_title, save_chat, set_active_chat
from core.cli import Spinner, show_tool_call as _cli_show_tool_call, show_tool_result as _cli_show_tool_result
from core.display import DisplayHooks
from core.interface_context import set_interface
from core.service import LumaKitService, Surface
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
# Telegram-specific DisplayHooks — forwards tool activity to the active chat
# ---------------------------------------------------------------------------

_show_tools: dict = {}  # {chat_id: bool}

# Pass _show_tools into telegram_commands so /tools toggle works
import core.telegram_state as _ts
_ts._show_tools = _show_tools


def _telegram_show_tool_call(tool_name, inputs):
    _cli_show_tool_call(tool_name, inputs)
    chat_id = _active_chat_id["value"]
    if chat_id and _show_tools.get(chat_id):
        detail = ""
        if "path" in inputs:
            detail = f" {inputs['path']}"
        elif "command" in inputs:
            detail = f" {inputs['command'][:80]}"
        send_message(f"🔧 [{tool_name}]{detail}", chat_id=chat_id)


def _telegram_show_tool_result(result):
    _cli_show_tool_result(result)
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


_telegram_display = DisplayHooks(
    show_tool_call=_telegram_show_tool_call,
    show_tool_result=_telegram_show_tool_result,
    confirm=telegram_confirm,
)


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
        display=_telegram_display,
    )
    speech_client = SpeechClient()

    # --- Surface wiring: service owns reminders/tasks/heartbeat/email ---
    def telegram_deliver(payload: dict) -> bool:
        content = payload.get("content", "")
        if not content:
            return False
        label = payload.get("label") or ""
        text = f"🔔 {label}: {content}" if label else content
        chat_id = payload.get("chat_id")
        if chat_id:
            send_message(text, chat_id=chat_id)
            print(f"[{label.lower() or 'notify'} -> {chat_id}] {content[:200]}")
            return True
        # Broadcast (family reminder): every allowed user.
        for uid in ALLOWED_IDS:
            send_message(text, chat_id=uid)
        print(f"[{label.lower() or 'notify'} broadcast] {content[:200]}")
        return True

    def telegram_inject_session(text: str) -> None:
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
        set_active_chat(str(target), session["chat_id"])

    service = LumaKitService()
    service.register_surface(Surface(
        name="telegram",
        deliver=telegram_deliver,
        inject_session=telegram_inject_session,
        is_owner=True,
    ))
    service.start()

    _AFFIRM = {"yes", "y", "yep", "yeah", "send", "send it", "do it", "ok", "okay", "sure"}
    _DENY = {"no", "n", "nah", "skip", "cancel", "nope", "don't", "dont"}

    def _handle_pending_draft(text, chat_id):
        draft = service.email.pending_draft
        if not draft:
            return False
        if str(chat_id) != str(OWNER_ID):
            return False
        normalized = text.strip().lower()
        if normalized in _AFFIRM:
            from tools.comms.email import send_preapproved
            result = send_preapproved(draft["to"], draft["subject"], draft["body"])
            service.email.clear_pending_draft()
            if result.get("sent"):
                send_message(f"✅ Sent to {draft['from_label']}.", chat_id=chat_id)
            else:
                send_message(f"❌ Couldn't send: {result.get('error', 'unknown error')}", chat_id=chat_id)
            return True
        if normalized in _DENY:
            service.email.clear_pending_draft()
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

    try:
        send_message("LumaKit is running.", chat_id=OWNER_ID)
    except Exception:
        pass

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
                set_interface("telegram", chat_id)
                service.notify_activity()

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
                            set_active_chat(chat_id, session["chat_id"])
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
                            set_active_chat(chat_id, session["chat_id"])
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
                        set_active_chat(chat_id, session["chat_id"])
                except Exception as e:
                    error_msg = f"Error: {e}"
                    send_message(error_msg)
                    print(f"[error] {error_msg}")

        except KeyboardInterrupt:
            for cid, sess in _sessions.items():
                if sess["first_message_sent"] and sess["messages"] and len(sess["messages"]) > 1:
                    save_chat(sess["chat_id"], sess["title"], sess["messages"])
                    set_active_chat(str(cid), sess["chat_id"])
            service.stop()
            print("\nBridge stopped.")
            break
        except (socket.timeout, urllib.error.URLError):
            continue
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
