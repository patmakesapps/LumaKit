"""Web UI bridge — chat with LumaKit from your browser.

Run this alongside (or instead of) telegram_bridge.py:
    python web_bridge.py

Opens a web UI at http://localhost:7865.
"""

import asyncio
import json
import os
import sys
import threading
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

# Load config.env from ~/.lumakit/ first (user overrides), then repo-root .env
_user_env = Path.home() / ".lumakit" / "config.env"
if _user_env.exists():
    load_dotenv(_user_env)
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from agent import Agent
from core import auth as _auth
from core.chat_store import (
    delete_chat,
    list_chats,
    load_chat,
    make_title,
    new_chat_id,
    save_chat,
)
from core.cli import Spinner
from core.interface_context import set_interface
from core.paths import get_data_dir
from core.reminder_checker import ReminderChecker
from core.runtime_config import apply_user_runtime
from core.telegram_state import OWNER_ID
from core import task_store, memory_store
from tools.comms.react import set_react_context
from tools.memory.memory_tools import set_active_user as set_memory_active_user

# Disable the spinner — not useful in web mode
Spinner.start = lambda self: self
Spinner.stop = lambda self: None

PORT = int(os.getenv("LUMAKIT_WEB_PORT", "7865"))
WEB_DIR = Path(__file__).resolve().parent / "web"
WEB_USER_ID = str(OWNER_ID) if OWNER_ID else "web_owner"
WEB_MEDIA_DIR = get_data_dir() / "web_media"

app = FastAPI(title="LumaKit")


# ---------------------------------------------------------------------------
# Static files — serve the web/ directory
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


# Mount static files after explicit routes so /api paths aren't shadowed
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Serve photos (logos) from the repo
PHOTOS_DIR = Path(__file__).resolve().parent / "photos"
if PHOTOS_DIR.exists():
    app.mount("/photos", StaticFiles(directory=str(PHOTOS_DIR)), name="photos")

WEB_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(WEB_MEDIA_DIR)), name="media")


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    model = os.getenv("OLLAMA_MODEL", "unknown")
    return {"status": "ok", "model": model}


@app.get("/api/chats")
async def api_list_chats():
    return list_chats(limit=50)


@app.get("/api/chats/{chat_id}")
async def api_get_chat(chat_id: str):
    chat = load_chat(chat_id)
    if not chat:
        return JSONResponse({"error": "not found"}, status_code=404)
    return chat


@app.delete("/api/chats/{chat_id}")
async def api_delete_chat(chat_id: str):
    deleted = delete_chat(chat_id)
    return {"deleted": deleted}


@app.get("/api/tasks")
async def api_list_tasks():
    return task_store.get_all_tasks(limit=50)


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: int):
    task = task_store.get_task(task_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    return task


@app.get("/api/memories")
async def api_list_memories():
    return memory_store.get_recent(limit=50)


@app.get("/api/settings")
async def api_get_settings():
    return {
        "model": os.getenv("OLLAMA_MODEL", ""),
        "fallback_model": os.getenv("OLLAMA_FALLBACK_MODEL", ""),
        "data_dir": str(get_data_dir()),
    }


# ---------------------------------------------------------------------------
# WebSocket chat
# ---------------------------------------------------------------------------

# Per-connection state for the confirm/deny flow
_ws_confirm_events: dict[int, threading.Event] = {}
_ws_confirm_results: dict[int, bool] = {}
# Per-connection scratchpad for the tool currently being announced (captures
# tool_name + args from show_tool_call and the diff from render_diff so they
# can be attached to the next confirm event).
_ws_tool_ctx: dict[int, dict] = {}
_web_clients_lock = threading.RLock()
_web_clients: dict[str, set] = {}


def _tool_detail(tool_name: str, inputs: dict) -> str:
    """Human-readable one-liner for a tool invocation."""
    if tool_name in ("edit_file", "write_file", "read_file", "delete_file"):
        return inputs.get("path", "")
    if tool_name == "execute_shell":
        return (inputs.get("command") or "")[:160]
    if tool_name == "execute_python":
        return (inputs.get("code") or "")[:160]
    if tool_name == "move_path":
        src = inputs.get("source_path", "?")
        dst = inputs.get("destination_path", "?")
        return f"{src} \u2192 {dst}"
    if "path" in inputs:
        return inputs["path"]
    if "query" in inputs:
        return inputs["query"][:160]
    return ""


def _tool_status(tool_name: str, inputs: dict) -> str:
    """Telegram-style narration for what Lumi is currently doing."""
    verbs = {
        "read_file": "Reading",
        "edit_file": "Editing",
        "write_file": "Writing",
        "delete_file": "Deleting",
        "list_directory": "Listing",
        "search_files": "Searching",
        "grep_search": "Searching",
        "execute_shell": "Running",
        "execute_python": "Running Python",
        "web_search": "Searching the web",
        "fetch_url": "Fetching",
        "save_memory": "Saving memory",
        "save_task": "Saving task",
        "move_path": "Moving",
    }
    verb = verbs.get(tool_name, tool_name.replace("_", " "))
    target = _tool_detail(tool_name, inputs)
    if target:
        return f"Lumi is {verb.lower()} {target}..."
    return f"Lumi is using {verb.lower()}..."


def _prepare_web_turn(agent: Agent, session: dict):
    """Apply the same per-turn runtime and identity setup used in Telegram."""
    _auth.set_active_user(WEB_USER_ID)
    set_memory_active_user(WEB_USER_ID)
    set_react_context(None, None)
    set_interface("web", WEB_USER_ID)
    session["messages"] = agent.messages
    apply_user_runtime(agent, session, WEB_USER_ID, surface="web")


def _register_web_client(user_id: str, send_fn):
    with _web_clients_lock:
        _web_clients.setdefault(str(user_id), set()).add(send_fn)


def _unregister_web_client(user_id: str, send_fn):
    with _web_clients_lock:
        clients = _web_clients.get(str(user_id))
        if not clients:
            return
        clients.discard(send_fn)
        if not clients:
            _web_clients.pop(str(user_id), None)


def _notify_web(reminder: dict) -> bool:
    """Deliver reminders to connected web clients in web-only setups."""
    target = str(reminder.get("chat_id") or WEB_USER_ID)
    with _web_clients_lock:
        if reminder.get("chat_id") is None:
            callbacks = [cb for clients in _web_clients.values() for cb in clients]
        else:
            callbacks = list(_web_clients.get(target, set()))

    if not callbacks:
        return False

    payload = {
        "type": "reminder",
        "text": reminder["content"],
        "label": "Family reminder" if reminder.get("chat_id") is None else "Reminder",
    }
    for callback in callbacks:
        callback(payload)
    return True


def _make_agent(ws_id: int, send_fn):
    """Create an Agent wired to push status/tool events over WebSocket."""

    import agent as agent_module
    from core import cli as cli_module

    agent = Agent(
        verbose="--verbose" in sys.argv,
        status_callback=lambda msg: send_fn({"type": "status", "text": msg}),
        check_interrupt=lambda: False,
    )

    # --- Patch tool call/result display ---
    def ws_show_tool_call(tool_name, inputs):
        # Stash the tool context so the very next confirm() can describe what
        # is being approved (tool name, args, path). Clear any stale diff.
        _ws_tool_ctx[ws_id] = {
            "tool_name": tool_name,
            "args": {k: str(v)[:400] for k, v in inputs.items()},
            "detail": _tool_detail(tool_name, inputs),
            "path": inputs.get("path") or inputs.get("source_path"),
            "diff": None,
        }
        if tool_name == "react_to_message":
            return
        send_fn({
            "type": "tool_call",
            "name": tool_name,
            "detail": _ws_tool_ctx[ws_id]["detail"],
        })
        # Telegram-style narration so the user sees what Lumi is doing
        send_fn({"type": "status", "text": _tool_status(tool_name, inputs)})

    def ws_show_tool_result(result):
        ctx = _ws_tool_ctx.get(ws_id) or {}
        tool_name = ctx.get("tool_name", "")
        if not result.get("success"):
            summary = result.get("error", "unknown error")
            is_error = True
        else:
            data = result.get("data", {})
            if (
                tool_name == "react_to_message"
                and data.get("reacted")
                and data.get("emoji")
            ):
                send_fn({
                    "type": "reaction",
                    "emoji": data["emoji"],
                })
                _ws_tool_ctx.pop(ws_id, None)
                send_fn({"type": "status", "text": "Lumi is working..."})
                return
            if (
                tool_name in {"send_photo_user", "screenshot_user"}
                and data.get("sent")
                and data.get("interface") == "web"
                and data.get("url")
            ):
                send_fn({
                    "type": "image",
                    "url": data["url"],
                    "caption": data.get("caption", ""),
                })
                _ws_tool_ctx.pop(ws_id, None)
                send_fn({"type": "status", "text": "Lumi is working..."})
                return
            if data.get("skipped"):
                summary = "skipped"
            elif "count" in data:
                summary = f"found {data['count']} result(s)"
            else:
                summary = "done"
            is_error = False
        send_fn({
            "type": "tool_result",
            "name": tool_name,
            "summary": summary,
            "error": is_error,
        })
        _ws_tool_ctx.pop(ws_id, None)
        # The monkey-patched Spinner is silent, so without an explicit status
        # the UI looks frozen while the LLM decides what to do next.
        send_fn({"type": "status", "text": "Lumi is working..."})

    cli_module.show_tool_call = ws_show_tool_call
    cli_module.show_tool_result = ws_show_tool_result
    agent_module.show_tool_call = ws_show_tool_call
    agent_module.show_tool_result = ws_show_tool_result

    # --- Patch render_diff to capture (not print) the diff text ---
    def ws_render_diff(diff_text: str) -> str:
        ctx = _ws_tool_ctx.get(ws_id)
        if ctx is not None:
            ctx["diff"] = diff_text
        return ""  # suppress stdout output

    cli_module.render_diff = ws_render_diff
    agent_module.render_diff = ws_render_diff

    # --- Patch confirm to go through WebSocket with rich context ---
    def ws_confirm(prompt):
        """Send a confirm request over WebSocket, block until client replies."""
        ctx = _ws_tool_ctx.get(ws_id) or {}
        send_fn({
            "type": "confirm",
            "prompt": prompt,
            "tool_name": ctx.get("tool_name"),
            "args": ctx.get("args") or {},
            "detail": ctx.get("detail") or "",
            "path": ctx.get("path"),
            "diff": ctx.get("diff"),
        })
        event = _ws_confirm_events.get(ws_id)
        if event:
            event.wait(timeout=300)
            event.clear()
            return _ws_confirm_results.get(ws_id, False)
        return True  # default to allow if something goes wrong

    cli_module.confirm = ws_confirm
    agent_module.confirm = ws_confirm

    return agent


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    ws_id = id(ws)
    loop = asyncio.get_event_loop()
    ws_closed = {"v": False}

    # Thread-safe send helper: schedule the coroutine on the event loop.
    # Swallows errors when the socket is already closed so a background agent
    # thread doesn't spam the server log after the user reloads.
    def send_sync(msg: dict):
        if ws_closed["v"]:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)
        except Exception:
            pass

    _auth.set_active_user(WEB_USER_ID)
    _register_web_client(WEB_USER_ID, send_sync)

    agent = _make_agent(ws_id, send_sync)
    # If the client disconnects, abort the agent loop at the next check
    agent.check_interrupt = lambda: ws_closed["v"]
    _ws_confirm_events[ws_id] = threading.Event()

    # Session state
    session = {
        "chat_id": new_chat_id(),
        "title": "",
        "first_message_sent": False,
        "messages": agent.messages,
    }
    _prepare_web_turn(agent, session)

    async def run_agent_request(text: str):
        """Run the agent in a worker thread and emit the response when it finishes.
        This is fired as a separate task so the receive loop stays alive and can
        process confirm_response / stop messages while the agent is working."""
        try:
            _prepare_web_turn(agent, session)
            response = await loop.run_in_executor(None, agent.ask_llm, text)
            reply = response.get("message", {}).get("content", "")
            session["messages"] = agent.messages
            if not session["first_message_sent"]:
                session["title"] = make_title(text)
                session["first_message_sent"] = True
            save_chat(session["chat_id"], session["title"], session["messages"])
            if not ws_closed["v"]:
                await ws.send_json({
                    "type": "response",
                    "text": reply,
                    "chat_id": session["chat_id"],
                    "title": session["title"],
                })
        except Exception as e:
            if not ws_closed["v"]:
                try:
                    await ws.send_json({"type": "error", "text": f"Error: {e}"})
                except Exception:
                    pass

    agent_task: asyncio.Task | None = None

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "text": "Invalid JSON"})
                continue

            msg_type = data.get("type", "")

            # Client confirms or denies a tool action
            if msg_type == "confirm_response":
                _ws_confirm_results[ws_id] = data.get("approved", False)
                event = _ws_confirm_events.get(ws_id)
                if event:
                    event.set()
                continue

            # Explicit stop button (legacy — UI now uses /stop instead)
            if msg_type == "stop":
                agent.interrupt_requested = True
                ev = _ws_confirm_events.get(ws_id)
                if ev and not ev.is_set():
                    _ws_confirm_results[ws_id] = False
                    ev.set()
                await ws.send_json({"type": "status", "text": "Stopping..."})
                continue

            # Load a specific chat
            if msg_type == "load_chat":
                if agent_task and not agent_task.done():
                    await ws.send_json({"type": "error", "text": "Finish or stop the current run before switching chats."})
                    continue
                target_id = data.get("chat_id", "")
                loaded = load_chat(target_id)
                if loaded:
                    session["chat_id"] = loaded["id"]
                    session["title"] = loaded["title"]
                    session["first_message_sent"] = True
                    agent.messages = loaded["messages"]
                    session["messages"] = agent.messages
                    _prepare_web_turn(agent, session)
                    await ws.send_json({
                        "type": "chat_loaded",
                        "chat_id": session["chat_id"],
                        "title": session["title"],
                        "messages": loaded["messages"],
                    })
                else:
                    await ws.send_json({"type": "error", "text": "Chat not found"})
                continue

            # New chat
            if msg_type == "new_chat":
                if agent_task and not agent_task.done():
                    await ws.send_json({"type": "error", "text": "Finish or stop the current run before starting a new chat."})
                    continue
                session["chat_id"] = new_chat_id()
                session["title"] = ""
                session["first_message_sent"] = False
                agent.messages = [agent.build_system_message()]
                session["messages"] = agent.messages
                _prepare_web_turn(agent, session)
                await ws.send_json({
                    "type": "chat_loaded",
                    "chat_id": session["chat_id"],
                    "title": "",
                    "messages": [],
                })
                continue

            # Regular chat message
            if msg_type == "message":
                text = data.get("text", "").strip()
                if not text:
                    continue

                # Telegram-style /stop: interrupt the current run
                if text.lower() in ("/stop", "stop"):
                    agent.interrupt_requested = True
                    # Unblock any pending confirm so the thread can wind down
                    ev = _ws_confirm_events.get(ws_id)
                    if ev and not ev.is_set():
                        _ws_confirm_results[ws_id] = False
                        ev.set()
                    await ws.send_json({"type": "status", "text": "Stopping..."})
                    continue

                if agent_task and not agent_task.done():
                    await ws.send_json({
                        "type": "status",
                        "text": "Lumi is still working on the previous message. Wait for the reply or send /stop.",
                    })
                    continue

                await ws.send_json({"type": "status", "text": "Lumi is thinking..."})

                # Spawn as a task so the receive loop keeps running — this is
                # what lets confirm_response / stop messages get processed while
                # the agent is working.
                agent_task = asyncio.create_task(run_agent_request(text))
                continue

    except WebSocketDisconnect:
        pass
    finally:
        ws_closed["v"] = True
        _unregister_web_client(WEB_USER_ID, send_sync)
        # Cancel any in-flight agent task
        if agent_task and not agent_task.done():
            agent.interrupt_requested = True
        # If the agent is blocked on a confirm, unblock it so the thread can exit
        ev = _ws_confirm_events.get(ws_id)
        if ev:
            _ws_confirm_results[ws_id] = False
            ev.set()
        _ws_confirm_events.pop(ws_id, None)
        _ws_confirm_results.pop(ws_id, None)
        _ws_tool_ctx.pop(ws_id, None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    _auth.set_owner(WEB_USER_ID)
    reminders = None
    if not OWNER_ID:
        reminders = ReminderChecker(interval=30, notify=_notify_web)
        reminders.start()

    print(f"\n=== LumaKit Web UI ===")
    print(f"Open http://localhost:{PORT} in your browser\n")

    # Auto-open browser after a short delay
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    finally:
        if reminders:
            reminders.stop()


if __name__ == "__main__":
    main()
