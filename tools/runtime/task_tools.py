"""Tools the main conversational agent uses to manage autonomous tasks."""

from __future__ import annotations

from datetime import datetime

from core import task_store
from core.paths import get_repo_root


def _fmt_task(task: dict) -> str:
    status_emoji = {
        "planning": "🗂",
        "active":   "⚙️",
        "blocked":  "🔴",
        "done":     "✅",
        "failed":   "❌",
        "cancelled":"🚫",
    }
    emoji = status_emoji.get(task["status"], "❓")
    due = task.get("due_at", "")
    due_str = f"  due: {due[:10]}" if due else ""
    plan = task.get("plan") or []
    step_str = f"  step {task['current_step']+1}/{len(plan)}" if plan and task["status"] == "active" else ""
    return f"{emoji} [{task['id']}] {task['title']}{due_str}{step_str}"


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------

def get_create_task_tool():
    return {
        "name": "create_task",
        "description": (
            "Create a new autonomous background task for Lumi to work on independently. "
            "Lumi plans the task, then works it start-to-finish, running steps back-to-back "
            "with no artificial delays — it only pauses when it genuinely must wait for "
            "something external (a build/deploy, a reply, a time window), and resumes on its "
            "own. It reports back when the task is done (or at the due date if one is set). "
            "The task automatically captures the current active workspace and will keep "
            "using that directory for planning and execution even if the user navigates away. "
            "Use this ONLY when the user explicitly asks for a background task, or the job is "
            "clearly long-running: many steps, hours of work, or waiting on something external. "
            "Do NOT use it for anything you can do right now with your other tools (creating a "
            "folder, editing a file, running a command, answering a question) — just do those. "
            "IMPORTANT — start_at vs due_at: if the user says 'start at 5pm' / 'begin tomorrow morning' / "
            "'kick off at X', that's start_at (when planning begins). If they say 'by 5pm' / 'deadline is X' / "
            "'have it done by X', that's due_at (the deadline). They are different — don't put a start time "
            "into due_at or the task will be treated as already overdue."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short name for the task (e.g. 'Flip $100 into $1k')",
                },
                "goal": {
                    "type": "string",
                    "description": "Full description of what to accomplish",
                },
                "start_at": {
                    "type": "string",
                    "description": (
                        "ISO 8601 time when Lumi should begin working on this task "
                        "(e.g. '2026-04-15T17:00:00'). Omit to start immediately. "
                        "Use this when the user specifies a start time."
                    ),
                },
                "due_at": {
                    "type": "string",
                    "description": (
                        "ISO 8601 deadline — when the task must be finished by "
                        "(e.g. '2026-04-19T23:59:00'). Optional. This is NOT a start time."
                    ),
                },
                "budget": {
                    "type": "string",
                    "description": "Budget or resource constraint (e.g. '$100', '2 hours'). Optional.",
                },
                "notes": {
                    "type": "string",
                    "description": "Any extra context or constraints for this task. Optional.",
                },
                "owner_chat_id": {
                    "type": "string",
                    "description": "Telegram chat_id to notify. Leave blank to use the active user.",
                },
            },
            "required": ["title", "goal"],
        },
        "execute": _create_task,
    }


def _create_task(inputs: dict) -> dict:
    from tools.memory.memory_tools import _get_active_user
    constraints: dict = {}
    if inputs.get("budget"):
        constraints["budget"] = inputs["budget"]
    if inputs.get("notes"):
        constraints["notes"] = inputs["notes"]

    owner = inputs.get("owner_chat_id") or str(_get_active_user() or "")
    start_at = inputs.get("start_at") or None
    workspace_path = str(get_repo_root().resolve(strict=False))
    # Remember which chat the task came from so its lifecycle events (approval
    # requests, completion) land back in that conversation.
    if owner:
        try:
            from core.chat_store import get_active_chat
            origin_chat = get_active_chat(owner)
        except Exception:
            origin_chat = None
        if origin_chat:
            constraints["_origin_chat"] = str(origin_chat)

    task_id = task_store.create_task(
        title=inputs["title"],
        goal=inputs["goal"],
        constraints=constraints,
        owner_chat_id=owner or None,
        due_at=inputs.get("due_at") or None,
        start_at=start_at,
        workspace_path=workspace_path,
    )

    if start_at:
        msg = (
            f"Task created (id: {task_id}). I'll start working on it at {start_at} "
            f"and ping you with updates. Use /tasks to check status anytime."
        )
    else:
        msg = (
            f"Task created (id: {task_id}). I'll plan and start working on it in the background "
            f"and will ping you with updates. Use /tasks to check status anytime."
        )

    return {"task_id": task_id, "workspace_path": workspace_path, "message": msg}


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

def get_list_tasks_tool():
    return {
        "name": "list_tasks",
        "description": "List autonomous background tasks, optionally filtered to the active user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "all_users": {
                    "type": "boolean",
                    "description": "If true, show tasks for all users. Default: only the active user.",
                },
            },
        },
        "execute": _list_tasks,
    }


def _list_tasks(inputs: dict) -> dict:
    from tools.memory.memory_tools import _get_active_user
    all_users = inputs.get("all_users", False)

    if all_users:
        tasks = task_store.get_all_tasks(limit=30)
    else:
        user = _get_active_user()
        tasks = task_store.get_tasks_by_owner(str(user), limit=20) if user else task_store.get_all_tasks(limit=30)

    if not tasks:
        return {"tasks": [], "message": "No tasks found."}

    lines = [_fmt_task(t) for t in tasks]
    return {
        "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in tasks],
        "message": "\n".join(lines),
        "count": len(tasks),
    }


# ---------------------------------------------------------------------------
# get_task_status
# ---------------------------------------------------------------------------

def get_get_task_status_tool():
    return {
        "name": "get_task_status",
        "description": "Get full details and history for a specific autonomous task by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id"},
            },
            "required": ["task_id"],
        },
        "execute": _get_task_status,
    }


def _get_task_status(inputs: dict) -> dict:
    task = task_store.get_task(int(inputs["task_id"]))
    if not task:
        return {"error": f"Task {inputs['task_id']} not found."}

    plan = task.get("plan") or []
    history = task.get("history") or []
    step_idx = task["current_step"]

    current_step_desc = ""
    current_step_obj: dict = {}
    if plan and step_idx < len(plan):
        current_step_obj = plan[step_idx] or {}
        current_step_desc = current_step_obj.get("description", "")

    history_lines = []
    for h in history:
        if h.get("type") == "step_result":
            history_lines.append(
                f"Step {h.get('step_index', '?')+1} [{h.get('verdict')}]: {h.get('summary', '')}"
            )
        elif h.get("type") == "plan_generated":
            history_lines.append(f"Plan created: {len(h.get('steps', []))} steps")
        elif h.get("type") == "step_retry":
            history_lines.append(
                f"Step {h.get('step_index', '?')+1} retry #{h.get('attempt', '?')}: "
                f"{h.get('reason', '')[:120]}"
            )

    # Derive a human-readable detail of *why* the task is in its current state.
    # The chat agent should quote this verbatim instead of inventing schedule
    # text, since next_run_at moves silently on retry.
    from core.task_approvals import pending_approval
    pending = pending_approval(task) if task["status"] == "blocked" else None
    runtime_retries = int(current_step_obj.get("runtime_retries", 0) or 0)
    last_runtime_error = current_step_obj.get("last_runtime_error") or ""
    next_run_at = task.get("next_run_at") or ""
    if task["status"] == "active" and runtime_retries > 0 and last_runtime_error:
        state_detail = (
            f"Step {step_idx + 1} is waiting to retry after a runtime error "
            f"(attempt {runtime_retries}). Reason: {last_runtime_error[:200]}. "
            f"Next retry at {next_run_at}."
        )
    elif task["status"] == "active":
        state_detail = (
            f"Step {step_idx + 1} of {len(plan)} is scheduled to run at {next_run_at}."
            if plan else "Active but no plan yet."
        )
    elif task["status"] == "planning":
        state_detail = f"Plan is being generated. Next attempt at {next_run_at}."
    elif task["status"] == "blocked" and pending:
        state_detail = (
            f"Blocked: waiting for the owner to approve running {pending.get('tool')} "
            f"({pending.get('summary')}). The task runner resumes it automatically once "
            "they approve or deny — by replying yes/no in chat or with the buttons on "
            "the task card / Tasks panel. Do not do this work yourself."
        )
    elif task["status"] == "blocked":
        state_detail = "Blocked waiting for user input."
    elif task["status"] == "paused":
        state_detail = "Paused by user."
    else:
        state_detail = task["status"]

    return {
        "id": task["id"],
        "title": task["title"],
        "goal": task["goal"],
        "status": task["status"],
        "state_detail": state_detail,
        "pending_approval": pending,
        "due_at": task.get("due_at"),
        "workspace_path": task.get("workspace_path") or "",
        "current_step": f"{step_idx+1}/{len(plan)}" if plan else "N/A",
        "current_step_description": current_step_desc,
        "current_step_runtime_retries": runtime_retries,
        "current_step_last_error": last_runtime_error[:300],
        "steps_total": len(plan),
        "history_summary": "\n".join(history_lines) or "No steps run yet.",
        "result": task.get("result") or "",
        "next_run_at": next_run_at,
    }


# ---------------------------------------------------------------------------
# delete_task
# ---------------------------------------------------------------------------

def get_delete_task_tool():
    return {
        "name": "delete_task",
        "description": (
            "Permanently delete an autonomous background task by id. "
            "Use when the user wants to cancel or remove a task. "
            "Run list_tasks first if you need to find the id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id to delete"},
            },
            "required": ["task_id"],
        },
        "execute": _delete_task,
    }


def _delete_task(inputs: dict) -> dict:
    task_id = int(inputs["task_id"])
    ok = task_store.delete_task(task_id)
    if ok:
        return {"deleted": True, "message": f"Task {task_id} deleted."}
    return {"deleted": False, "message": f"Task {task_id} not found."}
