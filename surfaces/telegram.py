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

from agent import Agent, timestamp_message
from core import auth
from core.chat_store import make_title, save_chat, set_active_chat
from core.cli import Spinner, show_tool_call as _cli_show_tool_call, show_tool_result as _cli_show_tool_result
from core.display import DisplayHooks
from core import email_draft_store
from core.interface_context import set_interface
from core.service import LumaKitService, Surface
from core.telegram_api import (
    download_telegram_file,
    download_telegram_photo,
    send_chat_action,
    telegram_api,
)
from core.telegram_commands import apply_chat_runtime, handle_telegram_command, swap_in
from core.telegram_io import edit_message_text, send_message, send_tts_reply, telegram_confirm
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


def _tool_detail(tool_name, inputs):
    if tool_name in {"read_file", "write_file", "edit_file", "delete_file"}:
        return inputs.get("path", "")
    if tool_name == "move_path":
        return f"{inputs.get('source_path', '?')} -> {inputs.get('destination_path', '?')}"
    if tool_name == "execute_shell":
        return (inputs.get("command") or "")[:120]
    if tool_name == "execute_python":
        return "python snippet"
    if tool_name in {"web_search", "fetch_url"}:
        return (inputs.get("query") or inputs.get("url") or "")[:120]
    if tool_name == "browser_automation":
        return (inputs.get("url") or inputs.get("session_id") or "browser task")[:120]
    return ""


def _pretty_tool_name(tool_name):
    return str(tool_name or "tool").replace("_", " ")


def _tool_status(tool_name, inputs):
    phrases = {
        "read_file": "Reading",
        "edit_file": "Editing",
        "write_file": "Writing",
        "delete_file": "Deleting",
        "list_directory": "Listing",
        "search_file_contents": "Searching",
        "find_files": "Searching",
        "execute_shell": "Running",
        "execute_python": "Running",
        "web_search": "Searching the web",
        "fetch_url": "Fetching",
        "browser_automation": "Working in the browser",
    }
    phrase = phrases.get(tool_name, f"Using {_pretty_tool_name(tool_name)}")
    detail = _tool_detail(tool_name, inputs)
    if detail:
        return f"{phrase} {detail}."
    return f"{phrase}."


def _tool_result_summary(tool_name, result):
    if not result.get("success"):
        return f"That failed: {result.get('error', 'unknown error')}"

    data = result.get("data", {}) or {}
    if data.get("skipped"):
        return "Skipped."
    if tool_name == "browser_automation":
        final_url = data.get("final_url") or data.get("url")
        failures = [
            action for action in data.get("actions_performed", [])
            if isinstance(action, dict) and action.get("status") == "failed"
        ]
        if failures:
            first = failures[0]
            reason = data.get("blocked_reason") or first.get("blocked_reason") or "failed"
            selector = first.get("selector")
            target = f" on {selector}" if selector else ""
            return f"Browser step blocked ({reason}){target}."
        if final_url:
            return f"Browser step finished at {final_url}"
        return "Browser step finished."
    if "count" in data:
        return f"Found {data['count']} result(s)."
    if data.get("saved"):
        return f"Saved item {data.get('id', '?')}."
    if data.get("updated"):
        return f"Updated item {data.get('id', '?')}."
    if data.get("deleted"):
        return "Deleted it."
    if data.get("bytes_written"):
        return f"Wrote {data['bytes_written']} bytes."
    return "Finished that step."


# Tools whose start is worth announcing on Telegram. Quick reads/listings stay
# silent so the LLM's own narration carries the conversation.
_NARRATE_TOOL_START = {
    "execute_shell",
    "execute_python",
    "web_search",
    "fetch_url",
    "browser_automation",
    "instagram_session",
}


def _telegram_show_tool_call(tool_name, inputs):
    _cli_show_tool_call(tool_name, inputs)
    chat_id = _active_chat_id["value"]
    chat_key = str(chat_id) if chat_id is not None else None
    if not chat_key or not _show_tools.get(chat_key, True):
        return
    if tool_name not in _NARRATE_TOOL_START:
        return
    send_message(_tool_status(tool_name, inputs), chat_id=chat_id)


def _telegram_show_tool_result(result):
    _cli_show_tool_result(result)
    chat_id = _active_chat_id["value"]
    chat_key = str(chat_id) if chat_id is not None else None
    if not chat_key or not _show_tools.get(chat_key, True):
        return
    # Only surface results when something went wrong — successes stay silent so
    # the LLM's own reply does the talking.
    if result.get("success") and not (result.get("data") or {}).get("skipped"):
        return
    tool_name = result.get("toolName") or result.get("tool_name") or "tool"
    send_message(_tool_result_summary(tool_name, result), chat_id=chat_id)


_stream_state = {
    "chat_id": None,
    "message_id": None,
    "text": "",
    "last_sent_len": 0,
    "last_edit_at": 0.0,
}


def _reset_stream_state():
    _stream_state.update({
        "chat_id": None,
        "message_id": None,
        "text": "",
        "last_sent_len": 0,
        "last_edit_at": 0.0,
    })


def _telegram_stream_delta(chunk: str) -> bool:
    chat_id = _active_chat_id["value"]
    if not chat_id or not chunk:
        return False
    if _get_user_config(chat_id).get("voice_replies"):
        return False

    now = time.monotonic()
    if _stream_state["chat_id"] != chat_id:
        _reset_stream_state()
        _stream_state["chat_id"] = chat_id

    _stream_state["text"] += chunk
    text = _stream_state["text"].strip()
    if not text:
        return True

    if _stream_state["message_id"] is None:
        if len(text) < 40 and "\n" not in text:
            return True
        payload = send_message(text, chat_id=chat_id)
        message_id = (payload or {}).get("result", {}).get("message_id")
        if message_id:
            _stream_state["message_id"] = message_id
            _stream_state["last_sent_len"] = len(text)
            _stream_state["last_edit_at"] = now
        return True

    grew_enough = len(text) - int(_stream_state["last_sent_len"] or 0) >= 80
    waited_enough = now - float(_stream_state["last_edit_at"] or 0.0) >= 1.0
    if grew_enough or waited_enough:
        edit_message_text(text, chat_id, _stream_state["message_id"])
        _stream_state["last_sent_len"] = len(text)
        _stream_state["last_edit_at"] = now
    return True


def _telegram_stream_end(text: str) -> None:
    chat_id = _stream_state.get("chat_id")
    message_id = _stream_state.get("message_id")
    final = (text or _stream_state.get("text") or "").strip()
    try:
        if chat_id and message_id and final:
            edit_message_text(final, chat_id, message_id)
        elif chat_id and final:
            send_message(final, chat_id=chat_id)
    finally:
        _reset_stream_state()


def _telegram_stream_cancel() -> None:
    chat_id = _stream_state.get("chat_id")
    message_id = _stream_state.get("message_id")
    try:
        if chat_id and message_id:
            edit_message_text("Working on it...", chat_id, message_id)
    finally:
        _reset_stream_state()


_telegram_display = DisplayHooks(
    show_tool_call=_telegram_show_tool_call,
    show_tool_result=_telegram_show_tool_result,
    status=lambda msg: send_message(msg, chat_id=_active_chat_id["value"]) if _active_chat_id["value"] else None,
    stream_delta=_telegram_stream_delta,
    stream_end=_telegram_stream_end,
    stream_cancel=_telegram_stream_cancel,
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
    safe_reply = reply[:200].encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    try:
        print(f"[Lumi -> {user_name}] {safe_reply}")
    except UnicodeEncodeError:
        print(f"[Lumi -> {user_name}] {safe_reply.encode('ascii', errors='replace').decode('ascii')}")


def _poll_active_run_messages(agent: Agent) -> bool:
    """Peek Telegram updates mid-run and route them as control messages."""
    chat_id = _active_chat_id["value"]
    if not chat_id:
        return False

    params = {"timeout": 0}
    if _poll_offset["value"] is not None:
        params["offset"] = _poll_offset["value"]

    try:
        updates = telegram_api("getUpdates", params).get("result", [])
    except Exception:
        return agent.run_controller.is_interrupted()

    if not updates:
        return agent.run_controller.is_interrupted()

    for update in updates:
        _poll_offset["value"] = update["update_id"] + 1
        msg = update.get("message", {})
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if not msg_chat_id:
            continue

        if msg_chat_id != str(chat_id) or not text:
            _pending_updates.append(update)
            continue

        # Slash commands are still handled by the command dispatcher.
        if text.startswith("/"):
            _pending_updates.append(update)
            continue

        # Always forward the user's message — the model reads it and decides
        # whether it's a stop, a status question, or new guidance. No keyword
        # classifier sitting in front of the LLM.
        if not agent.run_controller.submit_guidance(text):
            _pending_updates.append(update)

    return agent.run_controller.is_interrupted()


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def is_configured() -> bool:
    return bool(TOKEN and ALLOWED_IDS)


def _persist_sessions() -> None:
    for cid, sess in _sessions.items():
        if sess["first_message_sent"] and sess["messages"] and len(sess["messages"]) > 1:
            save_chat(sess["chat_id"], sess["title"], sess["messages"], owner_id=str(cid))
            set_active_chat(str(cid), sess["chat_id"])


def _register_surface(service: LumaKitService, agent: Agent) -> None:
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
        session["messages"].append(timestamp_message({"role": "assistant", "content": text}))
        if not session["first_message_sent"]:
            session["title"] = make_title(text)
            session["first_message_sent"] = True
        save_chat(session["chat_id"], session["title"], session["messages"], owner_id=str(target))
        set_active_chat(str(target), session["chat_id"])

    service.register_surface(Surface(
        name="telegram",
        deliver=telegram_deliver,
        inject_session=telegram_inject_session,
        is_owner=True,
    ))


def run(
    *,
    service: LumaKitService | None = None,
    verbose: bool = False,
    owns_service: bool | None = None,
    stop_event=None,
    announce_start: bool = True,
):
    if owns_service is None:
        owns_service = service is None

    if not TOKEN or not ALLOWED_IDS:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_IDS in .env first.")
        sys.exit(1)

    auth.set_owner(OWNER_ID)

    agent = None
    agent = Agent(
        verbose=verbose,
        check_interrupt=lambda: _poll_active_run_messages(agent),
        display=_telegram_display,
    )
    speech_client = SpeechClient()
    service = service or LumaKitService()
    _register_surface(service, agent)
    if owns_service:
        service.start()

    _AFFIRM = {"yes", "y", "yep", "yeah", "send", "send it", "do it", "ok", "okay", "sure"}
    _DENY = {"no", "n", "nah", "skip", "cancel", "nope", "don't", "dont"}

    def _handle_pending_draft(text, chat_id):
        draft = email_draft_store.get_latest_pending()
        if not draft:
            return False
        if str(chat_id) != str(OWNER_ID):
            return False
        normalized = text.strip().lower()
        if normalized in _AFFIRM:
            from tools.comms.email import send_preapproved
            claimed = email_draft_store.pop_pending(draft["id"])
            if not claimed:
                send_message("That draft was already handled.", chat_id=chat_id)
                return True
            result = send_preapproved(claimed["to_addr"], claimed["subject"], claimed["body"])
            if result.get("sent"):
                send_message(f"✅ Sent to {claimed['from_label']}.", chat_id=chat_id)
            else:
                send_message(f"❌ Couldn't send: {result.get('error', 'unknown error')}", chat_id=chat_id)
            return True
        if normalized in _DENY:
            claimed = email_draft_store.pop_pending(draft["id"])
            if claimed:
                send_message("👍 Skipped. Draft discarded.", chat_id=chat_id)
            else:
                send_message("That draft was already handled.", chat_id=chat_id)
            return True
        return False

    # Skip any updates that arrived before the bot started
    try:
        boot = telegram_api("getUpdates", {"timeout": 0})
        if boot.get("result"):
            _poll_offset["value"] = boot["result"][-1]["update_id"] + 1
    except Exception:
        pass

    if announce_start:
        print(f"Telegram bridge running. {len(ALLOWED_IDS)} authorized user(s).")

        try:
            send_message("LumaKit is running.", chat_id=OWNER_ID)
        except Exception:
            pass

    poll_timeout = 5 if stop_event is not None else 30

    while not (stop_event and stop_event.is_set()):
        try:
            if _pending_updates:
                buffered = _pending_updates[:]
                _pending_updates.clear()
                updates = {"result": buffered}
            else:
                params = {"timeout": poll_timeout}
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
                        if reply and not response.get("streamed"):
                            _send_reply(reply, chat_id, user_name, speech_client)
                        session["messages"] = agent.messages
                        if not session["first_message_sent"]:
                            session["title"] = make_title(caption or "Photo")
                            session["first_message_sent"] = True
                        if session["first_message_sent"] and len(agent.messages) > 1:
                            save_chat(session["chat_id"], session["title"], agent.messages, owner_id=str(chat_id))
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
                        if reply and not response.get("streamed"):
                            _send_reply(reply, chat_id, user_name, speech_client)
                        session["messages"] = agent.messages
                        if not session["first_message_sent"]:
                            session["title"] = make_title(transcript)
                            session["first_message_sent"] = True
                        if session["first_message_sent"] and len(agent.messages) > 1:
                            save_chat(session["chat_id"], session["title"], agent.messages, owner_id=str(chat_id))
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
                    if reply and not response.get("streamed"):
                        _send_reply(reply, chat_id, user_name, speech_client)
                    session["messages"] = agent.messages
                    if not session["first_message_sent"]:
                        session["title"] = make_title(text)
                        session["first_message_sent"] = True
                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(session["chat_id"], session["title"], agent.messages, owner_id=str(chat_id))
                        set_active_chat(chat_id, session["chat_id"])
                except Exception as e:
                    error_msg = f"Error: {e}"
                    send_message(error_msg)
                    print(f"[error] {error_msg}")

        except KeyboardInterrupt:
            break
        except (socket.timeout, urllib.error.URLError):
            continue
        except Exception as e:
            print(f"[poll error] {e}")
            time.sleep(5)

    _persist_sessions()
    if owns_service:
        service.stop()
    if announce_start:
        print("\nBridge stopped.")


def main():
    run(verbose="--verbose" in sys.argv)


if __name__ == "__main__":
    main()
