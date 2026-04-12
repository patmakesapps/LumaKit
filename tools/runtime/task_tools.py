"""Tools the main conversational agent uses to manage autonomous tasks."""

from __future__ import annotations

from datetime import datetime

from core import task_store


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
            "Lumi will plan, execute, and report back at the due date. "
            "Use this when the user gives a goal that takes hours or days to complete."
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
                "due_at": {
                    "type": "string",
                    "description": "ISO 8601 deadline (e.g. '2026-04-19T23:59:00'). Optional.",
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

    task_id = task_store.create_task(
        title=inputs["title"],
        goal=inputs["goal"],
        constraints=constraints,
        owner_chat_id=owner or None,
        due_at=inputs.get("due_at") or None,
    )

    return {
        "task_id": task_id,
        "message": (
            f"Task created (id: {task_id}). I'll plan and start working on it in the background "
            f"and will ping you with updates. Use /tasks to check status anytime."
        ),
    }


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
    if plan and step_idx < len(plan):
        current_step_desc = plan[step_idx].get("description", "")

    history_lines = []
    for h in history:
        if h.get("type") == "step_result":
            history_lines.append(
                f"Step {h.get('step_index', '?')+1} [{h.get('verdict')}]: {h.get('summary', '')}"
            )
        elif h.get("type") == "plan_generated":
            history_lines.append(f"Plan created: {len(h.get('steps', []))} steps")

    return {
        "id": task["id"],
        "title": task["title"],
        "goal": task["goal"],
        "status": task["status"],
        "due_at": task.get("due_at"),
        "current_step": f"{step_idx+1}/{len(plan)}" if plan else "N/A",
        "current_step_description": current_step_desc,
        "steps_total": len(plan),
        "history_summary": "\n".join(history_lines) or "No steps run yet.",
        "result": task.get("result") or "",
        "next_run_at": task.get("next_run_at") or "",
    }


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------

def get_cancel_task_tool():
    return {
        "name": "cancel_task",
        "description": "Cancel an autonomous background task by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id to cancel"},
            },
            "required": ["task_id"],
        },
        "execute": _cancel_task,
    }


def _cancel_task(inputs: dict) -> dict:
    task_id = int(inputs["task_id"])
    ok = task_store.cancel_task(task_id)
    if ok:
        return {"cancelled": True, "message": f"Task {task_id} cancelled."}
    return {"cancelled": False, "message": f"Task {task_id} not found or already finished."}
