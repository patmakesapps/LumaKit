"""Web UI surface — chat with LumaKit from your browser.

Run with:
    python -m surfaces.web

Opens a web UI at http://localhost:7865.
"""

import asyncio
import base64
import binascii
import contextvars
import json
import os
import re
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

# Load config.env from ~/.lumakit/ first (user overrides), then repo-root .env
_user_env = Path.home() / ".lumakit" / "config.env"
if _user_env.exists():
    load_dotenv(_user_env)
load_dotenv()

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from agent import Agent, timestamp_message
from core import auth as _auth
from core import web_auth
from core import email_draft_store
from core.chat_store import (
    delete_chat,
    get_active_chat,
    get_chat_lumabot_profile,
    get_chat_workspace,
    list_chats,
    load_chat,
    make_title,
    new_chat_id,
    save_chat,
    set_active_chat,
    set_chat_lumabot_profile,
    set_chat_workspace,
)
from core import notifications as notification_log
from core.display import DisplayHooks
from core.identity import WEB_USER_ID
from core.interface_context import set_interface
from core.paths import get_data_dir, set_workspace_root
from core.runtime_config import apply_user_runtime, get_effective_config_for_user
from core.service import LumaKitService, Surface
from core.telegram_state import OWNER_ID
from core import task_store, memory_store
from core.app_runtime_config import get_app_runtime_config, save_app_runtime_config
from ollama_client import OllamaClient
from tools.comms.email import send_preapproved
from tools.comms.react import set_react_context
from tools.lumabot.remote import REMOTE_HELP, execute_remote_action
from tools.memory.memory_tools import set_active_user as set_memory_active_user

PORT = int(os.getenv("LUMAKIT_WEB_PORT", "7865"))
_REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = _REPO_ROOT / "web"
WEB_MEDIA_DIR = get_data_dir() / "web_media"
WEB_URL = f"http://localhost:{PORT}"
MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(title="LumaKit")


@app.middleware("http")
async def _require_session_token(request: Request, call_next):
    """Every /api/* route requires the per-install session token (S-1)."""
    if request.url.path.startswith("/api/"):
        token = request.headers.get("x-lumakit-token") or request.query_params.get("token")
        if not web_auth.is_valid_token(token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


async def _authorize_websocket(ws: WebSocket) -> bool:
    """Reject a WebSocket handshake without a valid token or from a foreign
    Origin. Called before accept(); closing here refuses the upgrade."""
    token = ws.query_params.get("token") or ws.headers.get("x-lumakit-token")
    if web_auth.is_valid_token(token) and web_auth.origin_allowed(ws.headers.get("origin")):
        return True
    await ws.close(code=1008)
    return False


def _default_workspace() -> Path:
    return _REPO_ROOT.resolve(strict=False)


def _resolve_workspace_path(raw_path: str | None, *, base: str | Path | None = None) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        return _default_workspace()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        base_path = Path(base).expanduser() if base else _default_workspace()
        path = base_path / path
    resolved = path.resolve(strict=False)
    if not resolved.exists():
        raise FileNotFoundError(f"Workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise NotADirectoryError(f"Workspace is not a directory: {resolved}")
    return resolved


def _chat_workspace(chat_id: str | None) -> Path:
    if chat_id:
        saved = get_chat_workspace(chat_id, owner_id=WEB_USER_ID)
        if saved:
            try:
                return _resolve_workspace_path(saved)
            except (FileNotFoundError, NotADirectoryError):
                pass
    return _default_workspace()


def _pick_workspace_dialog(initial: str | Path | None) -> str | None:
    """Open a native folder picker on the server host. Returns the chosen path or None."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception:
        return None

    initial_dir = ""
    if initial:
        try:
            initial_dir = str(Path(initial).expanduser().resolve(strict=False))
        except Exception:
            initial_dir = ""

    root = tkinter.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        chosen = filedialog.askdirectory(
            title="Select working directory",
            initialdir=initial_dir or None,
            mustexist=True,
        )
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    chosen = (chosen or "").strip()
    return chosen or None


def _workspace_payload(path: str | Path) -> dict:
    root = Path(path).expanduser().resolve(strict=False)
    return {
        "workspace_path": str(root),
        "workspace_display": str(root),
    }


def _display_transcript(messages: list[dict] | None) -> list[dict]:
    """Return only messages that should be replayed in the chat UI."""
    transcript = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            transcript.append(dict(msg))
    return transcript


def _append_display_message(session: dict, role: str, content: str) -> None:
    text = str(content or "").strip()
    if not text:
        return
    session.setdefault("display_messages", []).append(
        timestamp_message({"role": role, "content": text})
    )


def _save_web_chat(session: dict) -> None:
    save_chat(
        session["chat_id"],
        session["title"],
        session["messages"],
        owner_id=WEB_USER_ID,
        display_messages=session.get("display_messages") or [],
    )


def _decode_image_payload(payload: dict | None) -> bytes | None:
    if not isinstance(payload, dict):
        return None
    data_url = str(payload.get("data_url") or "").strip()
    if not data_url:
        return None
    if "," in data_url:
        header, encoded = data_url.split(",", 1)
        if not header.lower().startswith("data:image/"):
            raise ValueError("Only image uploads are supported.")
    else:
        encoded = data_url
    image_data = base64.b64decode(encoded, validate=True)
    if len(image_data) > MAX_IMAGE_UPLOAD_BYTES:
        raise ValueError("Image upload is too large.")
    return image_data


# ---------------------------------------------------------------------------
# Static files — serve the web/ directory
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


# ---------------------------------------------------------------------------
# Shareable task artifact page (§6.3) — read-only result + timeline. Token
# gated like the API (the middleware only covers /api/*, so check here).
# ---------------------------------------------------------------------------

_TASK_PAGE_STATUS_COLORS = {
    "planning": "#8b8fa3", "active": "#2f6feb", "blocked": "#c9822b",
    "paused": "#8b8fa3", "done": "#2e9e5b", "failed": "#c94f4f",
    "cancelled": "#8b8fa3",
}


@app.get("/task/{task_id}")
async def task_page(task_id: int, request: Request):
    import html as _html

    token = request.query_params.get("token") or request.headers.get("x-lumakit-token")
    if not web_auth.is_valid_token(token):
        return HTMLResponse("<h1>401</h1><p>This task page requires the session token.</p>",
                            status_code=401)
    task = task_store.get_task(task_id)
    if not task:
        return HTMLResponse("<h1>404</h1><p>Task not found.</p>", status_code=404)

    esc = _html.escape
    status = str(task.get("status") or "unknown")
    color = _TASK_PAGE_STATUS_COLORS.get(status, "#8b8fa3")

    todos_html = ""
    for todo in task.get("plan") or []:
        mark = "✅" if todo.get("status") == "done" else ("🔄" if todo.get("status") == "in_progress" else "⬜")
        todos_html += f"<li>{mark} {esc(str(todo.get('description', '')))}</li>"

    pending = None
    constraints = task.get("constraints") or {}
    if isinstance(constraints, dict):
        pending = constraints.get("_pending_approval")
    pending_html = ""
    if isinstance(pending, dict):
        pending_html = (
            '<div class="callout">⏸ Waiting for owner approval to run '
            f"<code>{esc(str(pending.get('tool')))}</code>: "
            f"<code>{esc(str(pending.get('summary')))}</code></div>"
        )

    files_html = ""
    if isinstance(constraints, dict) and constraints.get("_files_changed"):
        files_changed = [str(p) for p in constraints["_files_changed"]]
        overflow = int(constraints.get("_files_changed_overflow") or 0)
        items = "".join(f"<li><code>{esc(p)}</code></li>" for p in files_changed)
        if overflow:
            items += f"<li class='muted'>+{overflow} more</li>"
        files_html = f"<h2>Files changed</h2><ul class='todos'>{items}</ul>"

    rows = ""
    for entry in (task.get("history") or [])[-120:]:
        kind = esc(str(entry.get("type") or entry.get("kind") or ""))
        detail = entry.get("detail") or entry.get("reason") or entry.get("tool") or ""
        stamp = esc(str(entry.get("timestamp") or ""))[:19].replace("T", " ")
        rows += (
            f"<tr><td class='ts'>{stamp}</td><td class='kind'>{kind}</td>"
            f"<td>{esc(str(detail))[:300]}</td></tr>"
        )

    result_html = ""
    if task.get("result"):
        result_html = f"<h2>Result</h2><pre class='result'>{esc(str(task['result']))}</pre>"

    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LumaKit — Task #{task_id}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 0; background: #101216; color: #e8eaf0; }}
 .wrap {{ max-width: 860px; margin: 0 auto; padding: 32px 20px 60px; }}
 h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
 .status {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
            background: {color}; color: #fff; font-size: .8rem; margin-left: 8px;
            vertical-align: middle; }}
 .muted {{ color: #9aa0b4; font-size: .9rem; }}
 .callout {{ background: #2a2313; border: 1px solid #c9822b; border-radius: 8px;
             padding: 12px 14px; margin: 16px 0; }}
 pre.result {{ background: #171a21; border: 1px solid #262b36; border-radius: 8px;
               padding: 14px; white-space: pre-wrap; word-break: break-word; }}
 ul.todos {{ list-style: none; padding-left: 0; }}
 ul.todos li {{ padding: 3px 0; }}
 table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
 td {{ padding: 5px 8px; border-top: 1px solid #22262f; vertical-align: top; }}
 td.ts {{ white-space: nowrap; color: #9aa0b4; }}
 td.kind {{ white-space: nowrap; color: #7aa2f7; }}
 code {{ background: #171a21; padding: 1px 5px; border-radius: 4px; }}
 h2 {{ font-size: 1.05rem; margin-top: 28px; }}
</style></head><body><div class="wrap">
<h1>{esc(str(task.get('title') or 'Task'))}<span class="status">{esc(status)}</span></h1>
<div class="muted">Task #{task_id} · created {esc(str(task.get('created_at') or ''))[:19].replace('T', ' ')}
 · workspace {esc(str(task.get('workspace_path') or 'default'))}</div>
<h2>Goal</h2><pre class="result">{esc(str(task.get('goal') or ''))}</pre>
{pending_html}
{result_html}
{files_html}
<h2>Todo list</h2><ul class="todos">{todos_html or '<li class="muted">No todo list yet.</li>'}</ul>
<h2>Activity timeline</h2>
<table>{rows or '<tr><td class="muted">No activity recorded yet.</td></tr>'}</table>
</div></body></html>"""
    return HTMLResponse(body)


# Mount static files after explicit routes so /api paths aren't shadowed
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Serve photos (logos) from the repo
PHOTOS_DIR = _REPO_ROOT / "photos"
if PHOTOS_DIR.exists():
    app.mount("/photos", StaticFiles(directory=str(PHOTOS_DIR)), name="photos")

WEB_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(WEB_MEDIA_DIR)), name="media")


def _env_runtime_defaults():
    from core.providers import default_fallback_model, default_model
    return {
        "primary_model": default_model(),
        "fallback_model": default_fallback_model(),
        "local_model": str(os.getenv("OLLAMA_LOCAL_MODEL", "") or "").strip(),
    }


def _discover_ollama_models():
    from core.providers import resolve_provider_name
    if resolve_provider_name() != "ollama":
        # Remote providers have no local model registry to enumerate.
        return [], None
    try:
        payload = OllamaClient(request_timeout=10).tags(request_timeout=10)
        models = payload.get("models", []) if isinstance(payload, dict) else []
        names = sorted(
            {
                str(item.get("name", "") or "").strip()
                for item in models
                if isinstance(item, dict) and str(item.get("name", "") or "").strip()
            }
        )
        return names, None
    except Exception as exc:
        return [], str(exc)


def _settings_payload():
    from core import restart
    from core.providers import (
        VALID_PROVIDERS,
        api_key_is_set,
        provider_default_model,
        resolve_provider_name,
    )
    env_cfg = _env_runtime_defaults()
    drifted_env_vars = restart.env_drift()
    effective = get_effective_config_for_user(WEB_USER_ID)
    app_cfg = get_app_runtime_config()
    installed_models, model_error = _discover_ollama_models()
    setup_required = not bool(effective.get("primary_model"))
    provider = resolve_provider_name()

    def _model_source(legacy_override: str, saved_choice: str) -> str:
        if legacy_override:
            return "app override"
        if saved_choice:
            return "your choice"
        return "provider default"

    saved_models = app_cfg.get("provider_models") or {}
    saved_fallbacks = app_cfg.get("provider_fallback_models") or {}
    primary_source = _model_source(app_cfg.get("primary_model"), saved_models.get(provider, ""))
    fallback_source = _model_source(app_cfg.get("fallback_model"), saved_fallbacks.get(provider, ""))
    return {
        "llm_provider": provider,
        # Never echo keys — only whether one is configured, per provider, so
        # the UI can tell "key already in .env" apart from "key needed" while
        # the user is switching the dropdown.
        "api_key_set": api_key_is_set(provider),
        "api_keys_set": {
            p: api_key_is_set(p) for p in ("anthropic", "openai", "xai")
        },
        # Per-provider model memory for the provider card: the user's saved
        # choices, and each provider's default for placeholder text.
        "provider_models": dict(app_cfg.get("provider_models") or {}),
        "provider_fallback_models": dict(app_cfg.get("provider_fallback_models") or {}),
        "provider_default_models": {p: provider_default_model(p) for p in VALID_PROVIDERS},
        "model": effective.get("primary_model", ""),
        "fallback_model": effective.get("fallback_model", ""),
        "model_source": primary_source,
        "fallback_model_source": fallback_source,
        "require_tool_approvals": bool(app_cfg.get("require_tool_approvals", True)),
        "tools_enabled": bool(app_cfg.get("tools_enabled", True)),
        "data_dir": str(get_data_dir()),
        "app_primary_model": app_cfg.get("primary_model", ""),
        "app_fallback_model": app_cfg.get("fallback_model", ""),
        "env_primary_model": env_cfg["primary_model"],
        "env_fallback_model": env_cfg["fallback_model"],
        "local_model": effective.get("local_model", "") or env_cfg["local_model"],
        "setup_required": setup_required,
        "installed_models": installed_models,
        "installed_models_error": model_error,
        # .env/config.env was edited after the backend started — these vars
        # (names only, never values) won't apply until a restart (§6.3).
        "restart_required": bool(drifted_env_vars),
        "restart_reasons": drifted_env_vars,
        "restart_supported": restart.restart_supported(),
    }


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    cfg = get_effective_config_for_user(WEB_USER_ID)
    return {
        "status": "ok",
        "model": cfg.get("primary_model") or "not configured",
        "setup_required": not bool(cfg.get("primary_model")),
        "repo_root": str(_REPO_ROOT),
        "struqt_tools": (_REPO_ROOT / "tools" / "struqt" / "struqt_tools.py").exists(),
    }


@app.get("/api/chats")
async def api_list_chats():
    return list_chats(limit=50, owner_id=WEB_USER_ID)


@app.get("/api/chats/{chat_id}")
async def api_get_chat(chat_id: str):
    chat = load_chat(chat_id, owner_id=WEB_USER_ID)
    if not chat:
        return JSONResponse({"error": "not found"}, status_code=404)
    return chat


@app.delete("/api/chats/{chat_id}")
async def api_delete_chat(chat_id: str):
    deleted = delete_chat(chat_id, owner_id=WEB_USER_ID)
    return {"deleted": deleted}


@app.get("/api/tasks")
async def api_list_tasks():
    return task_store.get_all_tasks(limit=50)


@app.get("/api/tasks/runner/health")
async def api_runner_health():
    from core.task_runner import get_active_runner
    from datetime import datetime as _dt
    runner = get_active_runner()
    if not runner:
        return {"running": False, "last_tick_at": None, "seconds_since_tick": None}
    last = runner.get_last_tick_at()
    secs = (_dt.now() - last).total_seconds() if last else None
    return {
        "running": True,
        "last_tick_at": last.isoformat() if last else None,
        "seconds_since_tick": secs,
    }


@app.get("/api/tasks/actions")
async def api_task_action_catalog():
    """Protected actions a task can be pre-approved for (New Task form,
    task panel permissions)."""
    from core.approval_policy import task_action_catalog
    return task_action_catalog()


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: int):
    task = task_store.get_task(task_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    return task


_TASK_PATCHABLE = {"title", "goal", "due_at", "next_run_at"}


@app.post("/api/tasks")
async def api_create_task(payload: dict):
    title = str(payload.get("title", "") or "").strip()
    goal = str(payload.get("goal", "") or "").strip()
    if not title or not goal:
        return JSONResponse({"error": "title and goal are required"}, status_code=400)
    start_at = payload.get("start_at") or None
    due_at = payload.get("due_at") or None
    workspace_raw = payload.get("workspace_path") or None
    workspace_path = None
    if workspace_raw:
        try:
            workspace_path = str(_resolve_workspace_path(workspace_raw))
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    else:
        workspace_path = str(_default_workspace())
    from core.approval_policy import normalize_task_actions
    origin_chat = get_active_chat(WEB_USER_ID)
    constraints: dict = {}
    if origin_chat:
        constraints["_origin_chat"] = origin_chat
    allowed = normalize_task_actions(payload.get("allowed_actions"))
    if allowed:
        constraints["_allowed_actions"] = allowed
    task_id = task_store.create_task(
        title=title,
        goal=goal,
        constraints=constraints or None,
        owner_chat_id=WEB_USER_ID,
        due_at=due_at,
        start_at=start_at,
        workspace_path=workspace_path,
    )
    return task_store.get_task(task_id)


@app.patch("/api/tasks/{task_id}")
async def api_update_task(task_id: int, payload: dict):
    if not task_store.get_task(task_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    fields = {k: v for k, v in payload.items() if k in _TASK_PATCHABLE}
    permissions = payload.get("allowed_actions") if "allowed_actions" in payload else None
    if not fields and permissions is None:
        return JSONResponse({"error": "no editable fields provided"}, status_code=400)
    if fields:
        task_store.update_task(task_id, **fields)
    if permissions is not None:
        from core import task_approvals
        from core.approval_policy import TASK_ACTION_GRANTS
        keys = task_approvals.set_allowed_actions(task_id, permissions if isinstance(permissions, list) else [])
        if set(keys) == set(TASK_ACTION_GRANTS):
            # "Allow all" from the started card: retire that card's button.
            _resolve_task_cards(task_id, "allowed", event="started")
    return task_store.get_task(task_id)


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int):
    deleted = task_store.delete_task(task_id)
    return {"deleted": deleted}


@app.post("/api/tasks/{task_id}/pause")
async def api_pause_task(task_id: int):
    if not task_store.pause_task(task_id):
        return JSONResponse(
            {"error": "task is not in a pausable state"}, status_code=400
        )
    return task_store.get_task(task_id)


@app.post("/api/tasks/{task_id}/resume")
async def api_resume_task(task_id: int):
    if not task_store.resume_task(task_id):
        return JSONResponse(
            {"error": "task is not paused or blocked"}, status_code=400
        )
    return task_store.get_task(task_id)


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(task_id: int):
    if not task_store.cancel_task(task_id):
        return JSONResponse(
            {"error": "task is already in a terminal state"}, status_code=400
        )
    return task_store.get_task(task_id)


@app.post("/api/tasks/{task_id}/approve")
async def api_approve_task(task_id: int, payload: dict | None = None):
    from core import task_approvals
    scope = str((payload or {}).get("scope") or "once")
    ok, message = task_approvals.approve(task_id, scope=scope)
    if not ok:
        return JSONResponse({"error": message}, status_code=400)
    _resolve_task_cards(task_id, "allowed" if scope.lower() in {"task", "always", "all"} else "approved")
    return task_store.get_task(task_id)


@app.post("/api/tasks/{task_id}/deny")
async def api_deny_task(task_id: int):
    from core import task_approvals
    ok, message = task_approvals.deny(task_id)
    if not ok:
        return JSONResponse({"error": message}, status_code=400)
    _resolve_task_cards(task_id, "denied")
    return task_store.get_task(task_id)


@app.post("/api/tasks/{task_id}/restart")
async def api_restart_task(task_id: int):
    if not task_store.restart_task(task_id):
        return JSONResponse(
            {"error": "task can only be restarted from cancelled, failed, or done"},
            status_code=400,
        )
    return task_store.get_task(task_id)


@app.get("/api/memories")
async def api_list_memories():
    return memory_store.get_recent(limit=50)


@app.get("/api/notifications")
async def api_list_notifications():
    return notification_log.recent(WEB_USER_ID, limit=50)


@app.get("/api/notifications/unshown")
async def api_list_unshown_notifications():
    notifications = notification_log.claim_unshown_for_web(WEB_USER_ID, limit=50)
    return [_notification_to_web_event(item) for item in notifications]


@app.get("/api/settings")
async def api_get_settings():
    return _settings_payload()


@app.post("/api/settings")
async def api_update_settings(payload: dict):
    app_cfg = get_app_runtime_config()
    primary_model = str(payload.get("primary_model", app_cfg.get("primary_model", "")) or "").strip()
    fallback_model = str(payload.get("fallback_model", app_cfg.get("fallback_model", "")) or "").strip()
    require_tool_approvals = payload.get(
        "require_tool_approvals",
        app_cfg.get("require_tool_approvals", True),
    )
    # Master tool switch (composer button). Unlike approvals, turning this OFF
    # only makes Lumi less capable, never less safe — no confirmation gate.
    tools_enabled = payload.get("tools_enabled", app_cfg.get("tools_enabled", True))

    # Turning approvals OFF weakens a security control — require an explicit
    # second acknowledgement flag so it can't happen from a casual/accidental
    # (or scripted) settings write (S-3). Protected tools still confirm
    # regardless of this toggle (S-4).
    currently_required = bool(app_cfg.get("require_tool_approvals", True))
    if currently_required and not bool(require_tool_approvals):
        if not payload.get("confirm_disable_approvals"):
            return JSONResponse(
                {
                    "error": (
                        "Disabling tool approvals requires "
                        "confirm_disable_approvals=true."
                    )
                },
                status_code=400,
            )

    # Provider selection + API key (server-side only; never echoed back).
    from core.providers import VALID_PROVIDERS, resolve_provider_name, save_api_key

    llm_provider = str(
        payload.get("llm_provider", app_cfg.get("llm_provider", "")) or ""
    ).strip().lower()
    if llm_provider and llm_provider not in VALID_PROVIDERS:
        return JSONResponse({"error": f"unknown provider: {llm_provider}"}, status_code=400)
    api_key = str(payload.get("llm_api_key", "") or "").strip()
    if api_key:
        save_api_key(api_key)

    # Switching provider: legacy global model overrides chosen for the OLD
    # provider would be sent verbatim to the new provider's API and fail.
    if llm_provider and llm_provider != resolve_provider_name() and "primary_model" not in payload:
        primary_model = ""
        fallback_model = ""

    # Per-provider model memory: the provider card saves the model FOR that
    # provider, so switching back later restores the user's choice. Empty =
    # use the provider's default.
    provider_models = dict(app_cfg.get("provider_models") or {})
    provider_fallbacks = dict(app_cfg.get("provider_fallback_models") or {})
    target_provider = llm_provider or resolve_provider_name()
    if "llm_model" in payload:
        model_choice = str(payload.get("llm_model") or "").strip()
        if model_choice:
            provider_models[target_provider] = model_choice
        else:
            provider_models.pop(target_provider, None)
        # The provider card is the canonical model picker — drop the legacy
        # global override so it can't shadow the per-provider choice.
        primary_model = ""
    if "llm_fallback_model" in payload:
        fallback_choice = str(payload.get("llm_fallback_model") or "").strip()
        if fallback_choice:
            provider_fallbacks[target_provider] = fallback_choice
        else:
            provider_fallbacks.pop(target_provider, None)
        fallback_model = ""

    save_app_runtime_config(
        {
            "primary_model": primary_model,
            "fallback_model": fallback_model,
            "require_tool_approvals": require_tool_approvals,
            "tools_enabled": tools_enabled,
            "llm_provider": llm_provider,
            "provider_models": provider_models,
            "provider_fallback_models": provider_fallbacks,
        }
    )
    return _settings_payload()


@app.post("/api/restart")
async def api_restart():
    """Gracefully restart the backend (token-gated by the /api middleware).

    Provider/key/config changes only fully apply on a fresh process — the
    task runner and other surfaces cache their LLM clients, and .env is
    read once at startup. The serve loop drains connections, then respawns
    the daemon; the UI polls /api/health until it's back.
    """
    from core import restart

    if not restart.schedule_restart():
        return JSONResponse(
            {
                "error": (
                    "This run mode doesn't support self-restart. "
                    "Restart manually: lumakit stop, then lumakit open."
                )
            },
            status_code=503,
        )
    return {"ok": True, "restarting": True}


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
# Task websocket subscribers. Each entry is (send_callable, task_id_filter_or_None).
_task_ws_lock = threading.RLock()
_task_ws_clients: list[tuple] = []
_task_ws_subscribed = False


def _broadcast_task_event(event: dict) -> None:
    """Fan a task_store event out to every connected task websocket. Runs on
    the task_runner thread, so it must hand back to the event loop via the
    thread-safe sender registered with each client.
    """
    task_id = event.get("task_id")
    with _task_ws_lock:
        clients = list(_task_ws_clients)
    for send_fn, filter_id in clients:
        if filter_id is not None and filter_id != task_id:
            continue
        try:
            send_fn(event)
        except Exception:
            pass


def _ensure_task_ws_subscribed() -> None:
    global _task_ws_subscribed
    if _task_ws_subscribed:
        return
    task_store.subscribe(_broadcast_task_event)
    _task_ws_subscribed = True
_EMAIL_AFFIRM = {"yes", "y", "yep", "yeah", "send", "send it", "do it", "ok", "okay", "sure"}
_EMAIL_DENY = {"no", "n", "nah", "skip", "cancel", "nope", "don't", "dont"}


def _notification_to_web_event(notification: dict) -> dict:
    meta = notification.get("meta") or {}
    if notification.get("label"):
        event = {
            "type": "reminder",
            "text": notification.get("content", ""),
            "label": notification.get("label") or "Reminder",
        }
    else:
        event = {
            "type": "message",
            "text": notification.get("content", ""),
        }
        if meta.get("kind") == "task":
            event["task"] = dict(meta)
        if meta.get("kind"):
            event["kind"] = meta["kind"]
        if meta.get("email"):
            event["email"] = meta["email"]
    if notification.get("id") is not None:
        event["notification_id"] = notification["id"]
    if meta.get("draft_id") is not None:
        event["draft_id"] = meta["draft_id"]
    return event


def _handle_email_draft_action(action: str, draft_id: int | None = None) -> dict:
    current = email_draft_store.get_pending(draft_id) if draft_id is not None else email_draft_store.get_latest_pending()
    if not current:
        return {
            "type": "email_draft_result",
            "draft_id": draft_id,
            "approved": action == "approve",
            "ok": False,
            "text": "That draft was already handled.",
        }

    claimed = email_draft_store.pop_pending(current["id"])
    if not claimed:
        return {
            "type": "email_draft_result",
            "draft_id": current["id"],
            "approved": action == "approve",
            "ok": False,
            "text": "That draft was already handled.",
        }

    if action == "discard":
        return {
            "type": "email_draft_result",
            "draft_id": claimed["id"],
            "approved": False,
            "ok": True,
            "text": "Draft discarded.",
        }

    result = send_preapproved(claimed["to_addr"], claimed["subject"], claimed["body"])
    if result.get("sent"):
        return {
            "type": "email_draft_result",
            "draft_id": claimed["id"],
            "approved": True,
            "ok": True,
            "text": f"Sent to {claimed['from_label']}.",
        }
    return {
        "type": "email_draft_result",
        "draft_id": claimed["id"],
        "approved": True,
        "ok": False,
        "text": f"Couldn't send: {result.get('error', 'unknown error')}",
    }


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


def _tool_result_summary(tool_name: str, result: dict) -> tuple[str, bool]:
    if not result.get("success"):
        return result.get("error", "unknown error"), True

    data = result.get("data", {}) or {}
    if data.get("skipped"):
        return "Skipped.", False
    if tool_name == "browser_automation":
        failures = [
            action for action in data.get("actions_performed", [])
            if isinstance(action, dict) and action.get("status") == "failed"
        ]
        if failures:
            first = failures[0]
            reason = data.get("blocked_reason") or first.get("blocked_reason") or "failed"
            selector = first.get("selector")
            where = f" on {selector}" if selector else ""
            return f"Blocked ({reason}){where}.", True
        final_url = data.get("final_url") or data.get("url")
        if final_url:
            return f"Finished at {final_url}", False
        return "Browser task finished.", False
    if "count" in data:
        return f"Found {data['count']} result(s).", False
    if data.get("saved"):
        return f"Saved item {data.get('id', '?')}.", False
    if data.get("updated"):
        return f"Updated item {data.get('id', '?')}.", False
    if data.get("deleted"):
        return "Deleted it.", False
    if data.get("bytes_written"):
        return f"Wrote {data['bytes_written']} bytes.", False
    return "Finished that step.", False


def _prepare_web_turn(agent: Agent, session: dict):
    """Apply the same per-turn runtime and identity setup used in Telegram."""
    _auth.set_active_user(WEB_USER_ID)
    set_memory_active_user(WEB_USER_ID)
    set_react_context(None, None)
    set_interface("web", WEB_USER_ID)
    workspace = _resolve_workspace_path(session.get("workspace_path"))
    session["workspace_path"] = str(workspace)
    set_workspace_root(workspace)
    agent.set_workspace_root(workspace)
    session["messages"] = agent.messages
    apply_user_runtime(agent, session, WEB_USER_ID, surface="web")


# Live per-connection chat sessions, keyed by websocket id, so background
# events (task cards) can be appended to the transcript the user is looking
# at instead of only being pushed over the socket and lost on reload.
_web_sessions: dict[int, dict] = {}


def _register_web_client(user_id: str, send_fn):
    with _web_clients_lock:
        _web_clients.setdefault(str(user_id), set()).add(send_fn)


def _task_chat_id(meta: dict) -> str | None:
    """The web chat a task's events belong in: the chat it was created from,
    else the user's current chat."""
    task_id = meta.get("task_id")
    origin = None
    if task_id is not None:
        task = task_store.get_task(int(task_id))
        constraints = (task or {}).get("constraints") or {}
        if isinstance(constraints, str):
            try:
                constraints = json.loads(constraints)
            except (TypeError, json.JSONDecodeError):
                constraints = {}
        origin = (constraints or {}).get("_origin_chat")
    return str(origin) if origin else get_active_chat(WEB_USER_ID)


def _persist_task_event(meta: dict, text: str) -> str | None:
    """Write a task event into its chat transcript so it survives reload.
    Returns the chat id it landed in, or None if there was nowhere to put it."""
    chat_id = _task_chat_id(meta)
    if not chat_id:
        return None
    entry = timestamp_message({"role": "assistant", "content": text, "task": dict(meta)})
    live = [s for s in list(_web_sessions.values()) if s.get("chat_id") == chat_id]
    if live:
        for s in live:
            s.setdefault("display_messages", []).append(dict(entry))
        primary = next((s for s in live if s.get("first_message_sent")), None)
        if primary:
            _save_web_chat(primary)
        return chat_id
    chat = load_chat(chat_id, owner_id=WEB_USER_ID)
    if not chat:
        return None
    display = list(chat.get("display_messages") or [])
    display.append(entry)
    save_chat(
        chat["id"], chat["title"], chat["messages"],
        owner_id=WEB_USER_ID, display_messages=display,
    )
    return chat_id


def _resolve_task_cards(task_id: int, resolution: str, event: str = "approval") -> None:
    """Mark a task's card for *event* (approval by default, or the started
    card after "allow all") as resolved wherever it is stored (live sessions
    and the saved chat) and tell open clients."""
    def _mark(messages) -> bool:
        changed = False
        for m in messages or []:
            t = m.get("task") if isinstance(m, dict) else None
            if (
                t and int(t.get("task_id") or 0) == int(task_id)
                and t.get("event") == event and not t.get("resolution")
            ):
                t["resolution"] = resolution
                changed = True
        return changed

    touched: set[str] = set()
    for s in list(_web_sessions.values()):
        if _mark(s.get("display_messages")):
            touched.add(str(s.get("chat_id")))
            if s.get("first_message_sent"):
                _save_web_chat(s)
    chat_id = _task_chat_id({"task_id": task_id})
    if chat_id and chat_id not in touched:
        chat = load_chat(chat_id, owner_id=WEB_USER_ID)
        if chat:
            display = list(chat.get("display_messages") or [])
            if _mark(display):
                save_chat(
                    chat["id"], chat["title"], chat["messages"],
                    owner_id=WEB_USER_ID, display_messages=display,
                )
    with _web_clients_lock:
        callbacks = [cb for clients in _web_clients.values() for cb in clients]
    for cb in callbacks:
        cb({"type": "task_approval_resolved", "task_id": int(task_id),
            "resolution": resolution, "event": event})


# Plain replies that resolve a task's pending approval without a model call —
# the same idea as the email draft yes/no fast path.
_TASK_APPROVE = frozenset({
    "1", "yes", "y", "yep", "yeah", "yup", "ok", "okay", "approve", "approved",
    "allow", "go", "go ahead", "do it", "sure",
})
_TASK_DENY = frozenset({
    "2", "no", "n", "nope", "deny", "denied", "don't", "dont", "cancel", "skip", "refuse",
})
# Approve AND allow that kind of action for the rest of the task.
_TASK_APPROVE_ALWAYS = frozenset({
    "always", "yes always", "allow always", "allow for this task", "approve all", "allow all",
})
_TASK_CMD_RE = re.compile(r"^/(approve|deny)\s+(\d+)(?:\s+(always|task|all))?\s*$", re.IGNORECASE)


def _pending_approval_task() -> dict | None:
    """The most recent task blocked on an owner approval, if any."""
    from core.task_approvals import pending_approval
    candidates = [
        t for t in task_store.get_all_tasks(limit=50)
        if t.get("status") == "blocked" and pending_approval(t)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda t: int(t.get("id") or 0))


def _task_approval_reply(text: str) -> tuple[str, int, str] | None:
    """('approve'|'deny', task_id, scope) if this chat message is an answer
    to a pending task approval, else None. scope is 'once' or 'task'."""
    normalized = " ".join(str(text or "").strip().lower().split())
    match = _TASK_CMD_RE.match(normalized)
    if match:
        scope = "task" if match.group(3) else "once"
        return match.group(1).lower(), int(match.group(2)), scope
    if normalized in _TASK_APPROVE or normalized in _TASK_DENY or normalized in _TASK_APPROVE_ALWAYS:
        task = _pending_approval_task()
        if task:
            if normalized in _TASK_APPROVE_ALWAYS:
                return "approve", int(task["id"]), "task"
            return ("approve" if normalized in _TASK_APPROVE else "deny"), int(task["id"]), "once"
    return None


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
    web_user_id = payload.get("web_user_id")
    chat_id = str(web_user_id or payload.get("chat_id") or WEB_USER_ID)
    meta = payload.get("meta") or {}
    is_task_event = meta.get("kind") == "task"
    with _web_clients_lock:
        if label and web_user_id is None:
            callbacks = [cb for clients in _web_clients.values() for cb in clients]
        elif label:
            callbacks = list(_web_clients.get(chat_id, set()))
        elif payload.get("chat_id") is None:
            # Owner-targeted (heartbeat/email): prefer owner's clients.
            callbacks = list(_web_clients.get(str(WEB_USER_ID), set()))
            if not callbacks:
                callbacks = [cb for clients in _web_clients.values() for cb in clients]
        else:
            callbacks = list(_web_clients.get(chat_id, set()))

    persisted = None
    if is_task_event:
        # Task events become part of the chat transcript (cards), so they
        # survive reload even when no tab was open to receive the push.
        try:
            persisted = _persist_task_event(meta, content)
        except Exception as exc:
            from core import log
            log.warn("web", "could not persist task event into chat", exc)

    if not callbacks and not persisted:
        return False

    if label:
        msg = {"type": "reminder", "text": content, "label": label}
    else:
        msg = {"type": "message", "text": content}
        if meta.get("kind"):
            msg["kind"] = meta["kind"]
        if meta.get("email"):
            msg["email"] = meta["email"]
        if meta.get("draft_id") is not None:
            msg["draft_id"] = meta["draft_id"]
        if is_task_event:
            msg["task"] = dict(meta)
    for callback in callbacks:
        callback(msg)
    notification_id = payload.get("notification_id")
    if notification_id is not None:
        notification_log.mark_shown_on_web([notification_id])
    return True


def _web_inject_session(text: str) -> None:
    """Owner-session injection hook — not meaningful for the current web UI.

    Each websocket builds its own agent session on connect, so there's no
    persistent 'owner session' to append to. Left as a no-op for now; revisit
    once web supports a durable owner-session model.
    """
    return None


def configure_owner() -> None:
    """Set the owner identity used by the web runtime."""
    _auth.set_owner(WEB_USER_ID)


def register_surface(service: LumaKitService, *, is_owner: bool = True) -> None:
    """Register the web surface on a shared service instance."""
    service.register_surface(Surface(
        name="web",
        deliver=_web_deliver,
        inject_session=_web_inject_session,
        is_owner=is_owner,
    ))


def run_server(*, host: str | None = None, port: int = PORT, log_level: str = "warning") -> None:
    """Run the FastAPI server for the web surface.

    Binds loopback by default; set LUMAKIT_BIND_HOST to expose it (auth stays
    mandatory either way).
    """
    bind_host = host or web_auth.resolve_bind_host()
    print(f"\n=== LumaKit Web UI ===")
    print(f"Open {web_auth.tokenized_url(f'http://localhost:{port}')} in your browser\n")
    uvicorn.run(app, host=bind_host, port=port, log_level=log_level)


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

    def ws_show_tool_result(result):
        ctx = _ws_tool_ctx.get(ws_id) or {}
        tool_name = ctx.get("tool_name", "")
        summary, is_error = _tool_result_summary(tool_name, result)
        if result.get("success"):
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
                return
            if (
                tool_name in {"send_photo", "screenshot"}
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
                return
        send_fn({
            "type": "tool_result",
            "name": tool_name,
            "summary": summary,
            "error": is_error,
        })
        _ws_tool_ctx.pop(ws_id, None)

    # --- Capture the diff onto the pending tool context instead of printing ---
    def ws_show_diff(diff_text: str) -> None:
        ctx = _ws_tool_ctx.get(ws_id)
        if ctx is not None:
            ctx["diff"] = diff_text

    # --- Confirm goes through WebSocket with rich context ---
    def _await_confirm(payload: dict) -> bool:
        """Send a confirm request and block until the client replies.

        Fails CLOSED: a missing confirm event, a timeout, or any ambiguity is
        a denial — never a silent approval (S-4).
        """
        event = _ws_confirm_events.get(ws_id)
        if not event:
            return False
        # Drop any stale result/signal from a previous confirm before asking.
        event.clear()
        _ws_confirm_results.pop(ws_id, None)
        send_fn(payload)
        answered = event.wait(timeout=300)
        event.clear()
        if not answered:
            return False
        return bool(_ws_confirm_results.get(ws_id, False))

    def ws_confirm(prompt):
        ctx = _ws_tool_ctx.get(ws_id) or {}
        return _await_confirm({
            "type": "confirm",
            "prompt": prompt,
            "tool_name": ctx.get("tool_name"),
            "args": ctx.get("args") or {},
            "detail": ctx.get("detail") or "",
            "path": ctx.get("path"),
            "diff": ctx.get("diff"),
        })

    def ws_confirm_email(preview, prompt=None):
        ctx = _ws_tool_ctx.get(ws_id) or {}
        return _await_confirm({
            "type": "confirm",
            "kind": "email",
            "prompt": prompt or "Approve this email?",
            "tool_name": ctx.get("tool_name"),
            "args": ctx.get("args") or {},
            "detail": ctx.get("detail") or "",
            "path": ctx.get("path"),
            "diff": ctx.get("diff"),
            "email_preview": preview,
        })

    def ws_stream_delta(chunk):
        send_fn({"type": "stream_delta", "text": chunk})
        return True

    def ws_stream_end(text):
        send_fn({"type": "stream_end", "text": text})

    def ws_stream_cancel():
        send_fn({"type": "stream_cancel"})

    display = DisplayHooks(
        show_tool_call=ws_show_tool_call,
        show_tool_result=ws_show_tool_result,
        show_diff=ws_show_diff,
        status=lambda msg: send_fn({"type": "status", "text": msg}),
        stream_delta=ws_stream_delta,
        stream_end=ws_stream_end,
        stream_cancel=ws_stream_cancel,
        confirm=ws_confirm,
        confirm_email=ws_confirm_email,
    )

    agent = Agent(
        verbose="--verbose" in sys.argv,
        check_interrupt=lambda: False,
        display=display,
        enable_spinner=False,
    )

    return agent


@app.websocket("/ws")
async def websocket_chat(ws: WebSocket):
    if not await _authorize_websocket(ws):
        return
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
        resumed = load_chat(active_id, owner_id=WEB_USER_ID)
    if resumed:
        workspace = _chat_workspace(resumed["id"])
        session = {
            "chat_id": resumed["id"],
            "title": resumed["title"],
            "first_message_sent": True,
            "messages": resumed["messages"],
            "display_messages": _display_transcript(resumed.get("display_messages") or resumed["messages"]),
            "workspace_path": str(workspace),
        }
        agent.messages = resumed["messages"]
    else:
        workspace = _default_workspace()
        session = {
            "chat_id": new_chat_id(),
            "title": "",
            "first_message_sent": False,
            "messages": agent.messages,
            "display_messages": [],
            "workspace_path": str(workspace),
        }
    _prepare_web_turn(agent, session)
    set_active_chat(WEB_USER_ID, session["chat_id"])
    _web_sessions[ws_id] = session

    if resumed:
        await ws.send_json({
            "type": "chat_loaded",
            "chat_id": session["chat_id"],
            "title": session["title"],
            "messages": session["display_messages"],
            "lumabot_mode": session.get("lumabot_mode", "off"),
            **_workspace_payload(session["workspace_path"]),
        })
    else:
        await ws.send_json({
            "type": "workspace_updated",
            "lumabot_mode": session.get("lumabot_mode", "off"),
            **_workspace_payload(session["workspace_path"]),
        })

    # Replay notifications the user hasn't seen on web yet — bridges the
    # "got pinged on Telegram while away" gap.
    missed = notification_log.claim_unshown_for_web(WEB_USER_ID)
    if missed:
        for n in missed:
            await ws.send_json(_notification_to_web_event(n))

    async def run_agent_request(text: str, image_data: bytes | None = None):
        """Run the agent in a worker thread and emit the response when it finishes.
        This is fired as a separate task so the receive loop stays alive and can
        process confirm_response / stop messages while the agent is working."""
        try:
            _prepare_web_turn(agent, session)
            _append_display_message(
                session,
                "user",
                text or "What do you see in this image?",
            )
            # Snapshot the current ContextVars (auth, interface, memory user,
            # react context) so the worker thread sees them. run_in_executor
            # does NOT propagate contextvars by default.
            ctx = contextvars.copy_context()
            if image_data:
                response = await loop.run_in_executor(
                    None,
                    ctx.run,
                    agent.ask_llm_with_image,
                    text or None,
                    image_data,
                    None,
                )
            else:
                response = await loop.run_in_executor(None, ctx.run, agent.ask_llm, text)
            reply = response.get("message", {}).get("content", "")
            session["messages"] = agent.messages
            _append_display_message(session, "assistant", reply)
            if not session["first_message_sent"]:
                session["title"] = make_title(text or "Photo")
                session["first_message_sent"] = True
            _save_web_chat(session)
            set_active_chat(WEB_USER_ID, session["chat_id"])
            set_chat_workspace(session["chat_id"], session["workspace_path"], owner_id=WEB_USER_ID)
            snap = agent.run_controller.get_status_snapshot()
            run_state = snap.get("state") or "completed"
            run_error = snap.get("last_error") or ""
            if not ws_closed["v"]:
                await ws.send_json({
                    "type": "response",
                    "text": reply,
                    "chat_id": session["chat_id"],
                    "title": session["title"],
                    "model_requested": agent.model,
                    "model_used": agent.last_model_used or agent.ollama.last_model_used or agent.model,
                    "run_state": run_state,
                    "run_error": run_error,
                    "streamed": bool(response.get("streamed")),
                    "messages": session["display_messages"],
                    "lumabot_mode": session.get("lumabot_mode", "off"),
                    **_workspace_payload(session["workspace_path"]),
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

            if msg_type == "email_draft_action":
                action = data.get("action", "")
                if action not in {"approve", "discard"}:
                    await ws.send_json({"type": "error", "text": "Invalid email draft action"})
                    continue
                draft_id_raw = data.get("draft_id")
                try:
                    draft_id = int(draft_id_raw) if draft_id_raw is not None else None
                except (TypeError, ValueError):
                    await ws.send_json({"type": "error", "text": "Invalid draft id"})
                    continue
                await ws.send_json(_handle_email_draft_action(action, draft_id))
                continue

            # Explicit stop button (legacy — UI now uses /stop instead)
            if msg_type == "stop":
                agent.request_stop("Stop requested from the web UI.")
                ev = _ws_confirm_events.get(ws_id)
                if ev and not ev.is_set():
                    _ws_confirm_results[ws_id] = False
                    ev.set()
                await ws.send_json({"type": "status", "text": "Stopping..."})
                continue

            if msg_type == "pick_workspace":
                if agent_task and not agent_task.done():
                    await ws.send_json({"type": "workspace_error", "text": "Finish or stop the current run before changing workspace."})
                    continue
                base_hint = data.get("base") or session.get("workspace_path") or str(_default_workspace())
                try:
                    chosen = await asyncio.get_event_loop().run_in_executor(
                        None, _pick_workspace_dialog, base_hint
                    )
                except Exception as exc:
                    await ws.send_json({"type": "workspace_error", "text": f"Folder picker failed: {exc}"})
                    continue
                if not chosen:
                    continue
                await ws.send_json({"type": "workspace_picked", "path": chosen})
                continue

            if msg_type == "set_workspace":
                if agent_task and not agent_task.done():
                    await ws.send_json({"type": "workspace_error", "text": "Finish or stop the current run before changing workspace."})
                    continue
                raw_path = data.get("path", "")
                try:
                    workspace = _resolve_workspace_path(raw_path, base=session.get("workspace_path"))
                except (FileNotFoundError, NotADirectoryError, OSError) as exc:
                    await ws.send_json({"type": "workspace_error", "text": str(exc)})
                    continue

                session["workspace_path"] = str(workspace)
                set_workspace_root(workspace)
                agent.set_workspace_root(workspace)
                agent.apply_runtime_overrides(messages=agent.messages)
                session["messages"] = agent.messages
                set_chat_workspace(session["chat_id"], session["workspace_path"], owner_id=WEB_USER_ID)
                if session["first_message_sent"] and len(agent.messages) > 1:
                    _save_web_chat(session)
                await ws.send_json({
                    "type": "workspace_updated",
                    **_workspace_payload(session["workspace_path"]),
                })
                continue

            if msg_type == "lumabot_mode":
                if agent_task and not agent_task.done():
                    await ws.send_json({
                        "type": "error",
                        "text": "Finish or stop the current run before changing LumaBot mode.",
                    })
                    continue
                mode = str(data.get("mode", "")).lower()
                if mode not in {"off", "agent", "remote"}:
                    await ws.send_json({"type": "error", "text": "Invalid LumaBot mode value."})
                    continue
                set_chat_lumabot_profile(session["chat_id"], mode)
                _prepare_web_turn(agent, session)
                await ws.send_json({
                    "type": "lumabot_mode",
                    "mode": mode,
                    "text": {
                        "agent": "LumaBot Agent mode ON. Natural language uses the configured LLM.",
                        "remote": "LumaBot Remote mode ON. Controls now bypass the LLM.",
                        "off": "LumaBot mode OFF. Full LumaKit is restored.",
                    }[mode],
                })
                continue

            if msg_type == "lumabot_control":
                action = str(data.get("action", "")).lower()
                profile = get_chat_lumabot_profile(session["chat_id"])
                if action not in {"stop", "park", "status"} and profile != "remote":
                    await ws.send_json({
                        "type": "lumabot_control",
                        "ok": False,
                        "text": "Switch to LumaBot Remote mode first.",
                    })
                    continue
                try:
                    continuous = data.get("continuous", False)
                    if not isinstance(continuous, bool):
                        raise ValueError("continuous must be true or false")
                    result = execute_remote_action(
                        action,
                        direction=data.get("direction"),
                        duration_s=data.get("duration_s", 1.0),
                        speed=data.get("speed", 0.3),
                        continuous=continuous,
                    )
                except ValueError as error:
                    result = {"ok": False, "text": str(error)}
                if action in {"stop", "park"} and agent_task and not agent_task.done():
                    agent.request_stop("LumaBot emergency stop requested.")
                await ws.send_json({"type": "lumabot_control", **result})
                continue

            # Load a specific chat
            if msg_type == "load_chat":
                if agent_task and not agent_task.done():
                    await ws.send_json({"type": "error", "text": "Finish or stop the current run before switching chats."})
                    continue
                target_id = data.get("chat_id", "")
                loaded = load_chat(target_id, owner_id=WEB_USER_ID)
                if loaded:
                    workspace = _chat_workspace(loaded["id"])
                    session["chat_id"] = loaded["id"]
                    session["title"] = loaded["title"]
                    session["first_message_sent"] = True
                    session["workspace_path"] = str(workspace)
                    agent.messages = loaded["messages"]
                    session["messages"] = agent.messages
                    session["display_messages"] = _display_transcript(
                        loaded.get("display_messages") or loaded["messages"]
                    )
                    _prepare_web_turn(agent, session)
                    set_active_chat(WEB_USER_ID, session["chat_id"])
                    await ws.send_json({
                        "type": "chat_loaded",
                        "chat_id": session["chat_id"],
                        "title": session["title"],
                        "messages": session["display_messages"],
                        "lumabot_mode": session.get("lumabot_mode", "off"),
                        **_workspace_payload(session["workspace_path"]),
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
                session["workspace_path"] = str(_default_workspace())
                set_workspace_root(session["workspace_path"])
                agent.set_workspace_root(session["workspace_path"])
                agent.messages = [agent.build_system_message()]
                session["messages"] = agent.messages
                session["display_messages"] = []
                _prepare_web_turn(agent, session)
                set_active_chat(WEB_USER_ID, session["chat_id"])
                await ws.send_json({
                    "type": "chat_loaded",
                    "chat_id": session["chat_id"],
                    "title": "",
                    "messages": [],
                    "lumabot_mode": session.get("lumabot_mode", "off"),
                    **_workspace_payload(session["workspace_path"]),
                })
                continue

            # Regular chat message
            if msg_type == "message":
                text = data.get("text", "").strip()
                image_payload = data.get("image")
                try:
                    image_data = _decode_image_payload(image_payload)
                except (ValueError, binascii.Error):
                    await ws.send_json({"type": "error", "text": "Invalid image upload."})
                    continue

                if not text and not image_data:
                    continue

                if get_chat_lumabot_profile(session["chat_id"]) == "remote":
                    await ws.send_json({
                        "type": "lumabot_control",
                        "ok": False,
                        "text": REMOTE_HELP,
                    })
                    continue

                normalized = text.lower()
                task_reply = None if image_data else _task_approval_reply(text)
                if task_reply:
                    # "1" / "yes" / "/approve 3" answers the pending task
                    # approval directly — it must never reach the chat model,
                    # which would otherwise try to do the task's work itself.
                    from core import task_approvals
                    action, target_id, scope = task_reply
                    if action == "approve":
                        ok, message = task_approvals.approve(target_id, scope=scope)
                    else:
                        ok, message = task_approvals.deny(target_id)
                    if ok:
                        resolution = "denied" if action == "deny" else ("allowed" if scope == "task" else "approved")
                        _resolve_task_cards(target_id, resolution)
                        reply = message
                    else:
                        reply = f"Couldn't {action} task #{target_id}: {message}"
                    _append_display_message(session, "user", text)
                    _append_display_message(session, "assistant", reply)
                    if session.get("first_message_sent"):
                        _save_web_chat(session)
                    await ws.send_json({"type": "response", "text": reply, "run_state": "completed"})
                    continue
                if not image_data and normalized in _EMAIL_AFFIRM and email_draft_store.get_latest_pending():
                    await ws.send_json(_handle_email_draft_action("approve"))
                    continue
                if not image_data and normalized in _EMAIL_DENY and email_draft_store.get_latest_pending():
                    await ws.send_json(_handle_email_draft_action("discard"))
                    continue

                # No keyword/regex fast-path for Struqt: every request goes to the
                # model, which decides which struqt_* tools to call itself.

                if agent_task and not agent_task.done():
                    # Always forward the user's message — let the model read it
                    # and decide whether it's a stop, a status question, or
                    # new guidance. No keyword classifier.
                    if image_data:
                        await ws.send_json({"type": "error", "text": "Wait for the current run to finish before sending a photo."})
                        continue
                    if not agent.run_controller.submit_guidance(text):
                        # Run finished between the check and the submit — fall
                        # through and treat this as a fresh turn.
                        await ws.send_json({"type": "status", "text": "Lumi is thinking..."})
                        agent_task = asyncio.create_task(run_agent_request(text))
                    else:
                        _append_display_message(session, "user", text)
                        if session["first_message_sent"]:
                            _save_web_chat(session)
                    continue

                await ws.send_json({"type": "status", "text": "Lumi is looking at the image..." if image_data else "Lumi is thinking..."})

                # Spawn as a task so the receive loop keeps running — this is
                # what lets confirm_response / stop messages get processed while
                # the agent is working.
                agent_task = asyncio.create_task(run_agent_request(text, image_data))
                continue

    except WebSocketDisconnect:
        pass
    finally:
        ws_closed["v"] = True
        _web_sessions.pop(ws_id, None)
        _unregister_web_client(WEB_USER_ID, send_sync)
        # Cancel any in-flight agent task
        if agent_task and not agent_task.done():
            agent.request_stop("The web client disconnected.")
        # If the agent is blocked on a confirm, unblock it so the thread can exit
        ev = _ws_confirm_events.get(ws_id)
        if ev:
            _ws_confirm_results[ws_id] = False
            ev.set()
        _ws_confirm_events.pop(ws_id, None)
        _ws_confirm_results.pop(ws_id, None)
        _ws_tool_ctx.pop(ws_id, None)


# ---------------------------------------------------------------------------
# Task websocket — streams live activity and status changes to the web UI.
# Optional ?task_id query param scopes the stream to a single task.
# ---------------------------------------------------------------------------

@app.websocket("/ws/tasks")
async def websocket_tasks(ws: WebSocket):
    if not await _authorize_websocket(ws):
        return
    await ws.accept()
    _ensure_task_ws_subscribed()
    loop = asyncio.get_event_loop()
    ws_closed = {"v": False}

    filter_id: int | None = None
    raw_filter = ws.query_params.get("task_id")
    if raw_filter:
        try:
            filter_id = int(raw_filter)
        except ValueError:
            filter_id = None

    def send_event(event: dict) -> None:
        if ws_closed["v"]:
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send_json(event), loop)
        except Exception:
            pass

    entry = (send_event, filter_id)
    with _task_ws_lock:
        _task_ws_clients.append(entry)

    # Send an initial snapshot so the client can render before any event fires.
    if filter_id is not None:
        snapshot = task_store.get_task(filter_id)
        if snapshot:
            await ws.send_json({"type": "snapshot", "task": snapshot})
    else:
        await ws.send_json({"type": "snapshot", "tasks": task_store.get_all_tasks(limit=50)})

    try:
        # Keep the socket alive; ignore any inbound messages.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_closed["v"] = True
        with _task_ws_lock:
            try:
                _task_ws_clients.remove(entry)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    configure_owner()

    # Web-only deployments run the service here; if Telegram is also configured
    # the Telegram bridge owns the service and this one stays idle so workers
    # don't double up.
    service = None
    if not OWNER_ID:
        service = LumaKitService()
        register_surface(service, is_owner=True)
        service.start()

    try:
        run_server()
    finally:
        if service:
            service.stop()


if __name__ == "__main__":
    main()
