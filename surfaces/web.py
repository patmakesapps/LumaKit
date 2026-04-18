"""Web UI surface — chat with LumaKit from your browser.

Run with:
    python -m surfaces.web

Opens a web UI at http://localhost:7865.
"""

import asyncio
import contextvars
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
    get_active_chat,
    list_chats,
    load_chat,
    make_title,
    new_chat_id,
    save_chat,
    set_active_chat,
)
from core import notifications as notification_log
from core.cli import Spinner
from core.display import DisplayHooks
from core.interface_context import set_interface
from core.paths import get_data_dir
from core.runtime_config import apply_user_runtime
from core.service import LumaKitService, Surface
from core.telegram_state import OWNER_ID
from core import task_store, memory_store
from tools.comms.react import set_react_context
from tools.memory.memory_tools import set_active_user as set_memory_active_user

# Disable the spinner — not useful in web mode
Spinner.start = lambda self: self
Spinner.stop = lambda self: None

PORT = int(os.getenv("LUMAKIT_WEB_PORT", "7865"))
_REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = _REPO_ROOT / "web"
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
PHOTOS_DIR = _REPO_ROOT / "photos"
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


@app.get("/api/notifications")
async def api_list_notifications():
    return notification_log.recent(WEB_USER_ID, limit=50)


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


def _web_deliver(payload: dict) -> bool:
    """Deliver a routed notification to connected web clients.

    Reminders (label set) render as banner-style entries; everything else
    (heartbeat, email) renders as a plain assistant message.
    """
    content = payload.get("content", "")
    if not content:
        return False
    label = payload.get("label") or ""
    chat_id = str(payload.get("chat_id") or WEB_USER_ID)
    with _web_clients_lock:
        if payload.get("chat_id") is None and not label:
            # Owner-targeted (heartbeat/email): prefer owner's clients.
            callbacks = list(_web_clients.get(str(WEB_USER_ID), set()))
            if not callbacks:
                callbacks = [cb for clients in _web_clients.values() for cb in clients]
        elif payload.get("chat_id") is None:
            callbacks = [cb for clients in _web_clients.values() for cb in clients]
        else:
            callbacks = list(_web_clients.get(chat_id, set()))

    if not callbacks:
        return False

    if label:
        msg = {"type": "reminder", "text": content, "label": label}
    else:
        msg = {"type": "message", "text": content}
    for callback in callbacks:
        callback(msg)
    return True


def _web_inject_session(text: str) -> None:
    """Owner-session injection hook — not meaningful for the current web UI.

    Each websocket builds its own agent session on connect, so there's no
    persistent 'owner session' to append to. Left as a no-op for now; revisit
    once web supports a durable owner-session model.
    """
    return None


def _make_agent(ws_id: int, send_fn):
    """Create an Agent wired to push status/tool events over WebSocket."""

    # --- Tool call/result display ---
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

    # --- Capture the diff onto the pending tool context instead of printing ---
    def ws_show_diff(diff_text: str) -> None:
        ctx = _ws_tool_ctx.get(ws_id)
        if ctx is not None:
            ctx["diff"] = diff_text

    # --- Confirm goes through WebSocket with rich context ---
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

    display = DisplayHooks(
        show_tool_call=ws_show_tool_call,
        show_tool_result=ws_show_tool_result,
        show_diff=ws_show_diff,
        confirm=ws_confirm,
    )

    agent = Agent(
        verbose="--verbose" in sys.argv,
        status_callback=lambda msg: send_fn({"type": "status", "text": msg}),
        check_interrupt=lambda: False,
        display=display,
    )

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

    # Session state — try to resume the user's active chat (set by any
    # surface on its last activity). Falls back to a fresh chat if the
    # pointer is unset or the referenced chat has been deleted.
    resumed = None
    active_id = get_active_chat(WEB_USER_ID)
    if active_id:
        resumed = load_chat(active_id)
    if resumed:
        session = {
            "chat_id": resumed["id"],
            "title": resumed["title"],
            "first_message_sent": True,
            "messages": resumed["messages"],
        }
        agent.messages = resumed["messages"]
    else:
        session = {
            "chat_id": new_chat_id(),
            "title": "",
            "first_message_sent": False,
            "messages": agent.messages,
        }
    _prepare_web_turn(agent, session)
    set_active_chat(WEB_USER_ID, session["chat_id"])

    if resumed:
        await ws.send_json({
            "type": "chat_loaded",
            "chat_id": session["chat_id"],
            "title": session["title"],
            "messages": resumed["messages"],
        })

    # Replay notifications the user hasn't seen on web yet — bridges the
    # "got pinged on Telegram while away" gap.
    missed = notification_log.unshown_for_web(WEB_USER_ID)
    if missed:
        for n in missed:
            await ws.send_json({
                "type": "reminder",
                "text": n["content"],
                "label": n["label"] or "Reminder",
            })
        notification_log.mark_shown_on_web([n["id"] for n in missed])

    async def run_agent_request(text: str):
        """Run the agent in a worker thread and emit the response when it finishes.
        This is fired as a separate task so the receive loop stays alive and can
        process confirm_response / stop messages while the agent is working."""
        try:
            _prepare_web_turn(agent, session)
            # Snapshot the current ContextVars (auth, interface, memory user,
            # react context) so the worker thread sees them. run_in_executor
            # does NOT propagate contextvars by default.
            ctx = contextvars.copy_context()
            response = await loop.run_in_executor(None, ctx.run, agent.ask_llm, text)
            reply = response.get("message", {}).get("content", "")
            session["messages"] = agent.messages
            if not session["first_message_sent"]:
                session["title"] = make_title(text)
                session["first_message_sent"] = True
            save_chat(session["chat_id"], session["title"], session["messages"])
            set_active_chat(WEB_USER_ID, session["chat_id"])
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
                    set_active_chat(WEB_USER_ID, session["chat_id"])
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
                set_active_chat(WEB_USER_ID, session["chat_id"])
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

    # Web-only deployments run the service here; if Telegram is also configured
    # the Telegram bridge owns the service and this one stays idle so workers
    # don't double up.
    service = None
    if not OWNER_ID:
        service = LumaKitService()
        service.register_surface(Surface(
            name="web",
            deliver=_web_deliver,
            inject_session=_web_inject_session,
            is_owner=True,
        ))
        service.start()

    print(f"\n=== LumaKit Web UI ===")
    print(f"Open http://localhost:{PORT} in your browser\n")

    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()
    try:
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    finally:
        if service:
            service.stop()


if __name__ == "__main__":
    main()
