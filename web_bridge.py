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
from core.chat_store import (
    delete_chat,
    list_chats,
    load_chat,
    make_title,
    new_chat_id,
    save_chat,
)
from core.cli import Spinner
from core.paths import get_data_dir
from core import task_store, memory_store

# Disable the spinner — not useful in web mode
Spinner.start = lambda self: self
Spinner.stop = lambda self: None

PORT = int(os.getenv("LUMAKIT_WEB_PORT", "7865"))
WEB_DIR = Path(__file__).resolve().parent / "web"

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
_ws_confirm_events: dict[int, asyncio.Event] = {}
_ws_confirm_results: dict[int, bool] = {}


def _make_agent(ws_id: int, send_fn):
    """Create an Agent wired to push status/tool events over WebSocket."""

    import agent as agent_module
    from core import cli as cli_module

    agent = Agent(
        verbose="--verbose" in sys.argv,
        status_callback=lambda msg: send_fn({"type": "status", "text": msg}),
        check_interrupt=lambda: False,
    )

    # --- Patch confirm to go through WebSocket ---
    original_confirm = cli_module.confirm

    def ws_confirm(prompt):
        """Send a confirm request over WebSocket, block until client replies."""
        send_fn({"type": "confirm", "prompt": prompt})
        # Wait for the client's response (set by the WebSocket handler)
        event = _ws_confirm_events.get(ws_id)
        if event:
            event.wait(timeout=120)  # 2 min timeout
            event.clear()
            return _ws_confirm_results.get(ws_id, False)
        return True  # default to allow if something goes wrong

    cli_module.confirm = ws_confirm
    agent_module.confirm = ws_confirm

    # --- Patch tool call/result display ---
    def ws_show_tool_call(tool_name, inputs):
        detail = ""
        if "path" in inputs:
            detail = f" {inputs['path']}"
        elif "command" in inputs:
            detail = f" {inputs['command'][:80]}"
        send_fn({
            "type": "tool_call",
            "name": tool_name,
            "detail": detail,
            "args": {k: str(v)[:200] for k, v in inputs.items()},
        })

    def ws_show_tool_result(result):
        summary = ""
        if not result.get("success"):
            summary = result.get("error", "unknown error")
        else:
            data = result.get("data", {})
            if data.get("skipped"):
                summary = "skipped"
            elif "count" in data:
                summary = f"found {data['count']} result(s)"
            else:
                summary = "done"
        send_fn({"type": "tool_result", "name": "", "summary": summary})

    cli_module.show_tool_call = ws_show_tool_call
    cli_module.show_tool_result = ws_show_tool_result
    agent_module.show_tool_call = ws_show_tool_call
    agent_module.show_tool_result = ws_show_tool_result

    return agent


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    ws_id = id(ws)
    loop = asyncio.get_event_loop()

    # Thread-safe send helper: schedule the coroutine on the event loop
    def send_sync(msg: dict):
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    agent = _make_agent(ws_id, send_sync)
    _ws_confirm_events[ws_id] = threading.Event()

    # Session state
    chat_id = new_chat_id()
    title = ""
    first_message_sent = False

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

            # Client wants to stop the current run
            if msg_type == "stop":
                agent.interrupt_requested = True
                continue

            # Client wants to load a specific chat
            if msg_type == "load_chat":
                target_id = data.get("chat_id", "")
                loaded = load_chat(target_id)
                if loaded:
                    chat_id = loaded["id"]
                    title = loaded["title"]
                    first_message_sent = True
                    agent.messages = loaded["messages"]
                    await ws.send_json({
                        "type": "chat_loaded",
                        "chat_id": chat_id,
                        "title": title,
                        "messages": loaded["messages"],
                    })
                else:
                    await ws.send_json({"type": "error", "text": "Chat not found"})
                continue

            # Client wants a new chat
            if msg_type == "new_chat":
                chat_id = new_chat_id()
                title = ""
                first_message_sent = False
                agent.messages = [agent.build_system_message()]
                await ws.send_json({
                    "type": "chat_loaded",
                    "chat_id": chat_id,
                    "title": "",
                    "messages": [],
                })
                continue

            # Regular chat message
            if msg_type == "message":
                text = data.get("text", "").strip()
                if not text:
                    continue

                await ws.send_json({"type": "status", "text": "Lumi is thinking..."})

                # Run the blocking agent call in a thread
                def run_agent(prompt):
                    return agent.ask_llm(prompt)

                try:
                    response = await loop.run_in_executor(None, run_agent, text)
                    reply = response.get("message", {}).get("content", "")

                    if not first_message_sent:
                        title = make_title(text)
                        first_message_sent = True

                    save_chat(chat_id, title, agent.messages)

                    await ws.send_json({
                        "type": "response",
                        "text": reply,
                        "chat_id": chat_id,
                        "title": title,
                    })
                except Exception as e:
                    await ws.send_json({
                        "type": "error",
                        "text": f"Error: {e}",
                    })

    except WebSocketDisconnect:
        pass
    finally:
        _ws_confirm_events.pop(ws_id, None)
        _ws_confirm_results.pop(ws_id, None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print(f"\n=== LumaKit Web UI ===")
    print(f"Open http://localhost:{PORT} in your browser\n")

    # Auto-open browser after a short delay
    def open_browser():
        import time
        time.sleep(1.5)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
