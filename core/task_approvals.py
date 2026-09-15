"""Cross-surface approval round-trip for autonomous tasks (§6.3).

When a running task wants a protected tool (shell command on the denylist,
git write, delete), it no longer gets a flat refusal: the task pauses as
``blocked`` with a pending-approval record, the owner is pinged on whatever
surface they use, and they can approve or deny from Telegram (/approve N,
/deny N) or the web UI. Approval mints a one-shot grant for that exact
action; the task resumes and re-runs it.
"""

from __future__ import annotations

import json
from datetime import datetime

from core import task_store
from core.approval_policy import (
    command_text_from_inputs,
    normalize_task_actions,
    task_action_key,
    task_action_label,
)

# A grant is only valid for this long after approval (safety: a stale grant
# shouldn't authorize an action days later).
GRANT_TTL_MINUTES = 60


def _constraints(task: dict) -> dict:
    constraints = task.get("constraints")
    if isinstance(constraints, str):
        try:
            constraints = json.loads(constraints)
        except (json.JSONDecodeError, TypeError):
            constraints = {}
    return dict(constraints or {})


def _save_constraints(task_id: int, constraints: dict, **fields) -> None:
    task_store.update_task(task_id, constraints=json.dumps(constraints), **fields)


def action_summary(tool: str, inputs: dict) -> str:
    command = command_text_from_inputs(inputs or {})
    if command:
        return command[:300]
    path = (inputs or {}).get("path") or (inputs or {}).get("source_path")
    if path:
        return str(path)[:300]
    try:
        return json.dumps(inputs or {}, default=str)[:300]
    except Exception:
        return str(inputs)[:300]


def pending_approval(task: dict) -> dict | None:
    pending = _constraints(task).get("_pending_approval")
    return pending if isinstance(pending, dict) else None


# --- Scoped standing grants: "allow X for the rest of this task" ------------

def allowed_actions(task: dict) -> list[str]:
    return normalize_task_actions(_constraints(task).get("_allowed_actions"))


def set_allowed_actions(task_id: int, keys) -> list[str]:
    """Replace the task's standing grants. Returns the normalized list."""
    task = task_store.get_task(task_id)
    if not task:
        return []
    keys = normalize_task_actions(keys)
    constraints = _constraints(task)
    before = normalize_task_actions(constraints.get("_allowed_actions"))
    if keys:
        constraints["_allowed_actions"] = keys
    else:
        constraints.pop("_allowed_actions", None)
    _save_constraints(task_id, constraints)
    added = [k for k in keys if k not in before]
    removed = [k for k in before if k not in keys]
    if added or removed:
        task_store.append_history(task_id, {
            "type": "permissions_changed",
            "detail": "; ".join(
                [f"allowed {task_action_label(k)}" for k in added]
                + [f"revoked {task_action_label(k)}" for k in removed]
            ),
            "allowed": keys,
        })
    return keys


def grant_action(task_id: int, key: str) -> bool:
    task = task_store.get_task(task_id)
    if not task or key not in normalize_task_actions([key]):
        return False
    current = allowed_actions(task)
    if key in current:
        return True
    set_allowed_actions(task_id, current + [key])
    return True


def request_approval(task: dict, tool: str, inputs: dict) -> dict:
    """Record a pending approval and block the task. Returns the record."""
    task_id = task["id"]
    action_key = task_action_key(tool, inputs)
    record = {
        "tool": tool,
        "summary": action_summary(tool, inputs),
        "requested_at": datetime.now().isoformat(),
        "action_key": action_key,
        "action_label": task_action_label(action_key) if action_key else "",
    }
    constraints = _constraints(task)
    constraints["_pending_approval"] = record
    _save_constraints(task_id, constraints, status="blocked")
    task_store.append_history(task_id, {
        "type": "approval_requested",
        "tool": tool,
        "detail": record["summary"],
    })
    return record


def _resume_with_guidance(task_id: int, guidance: str) -> None:
    task = task_store.get_task(task_id)
    if not task:
        return
    messages = list(task.get("messages") or [])
    if messages:
        messages.append({"role": "user", "content": guidance})
        task_store.save_session(task_id, messages)
    task_store.update_task(
        task_id, status="active", next_run_at=datetime.now().isoformat()
    )


def approve(task_id: int, scope: str = "once") -> tuple[bool, str]:
    """Grant the pending action and resume the task.

    scope="once" mints a one-shot grant for that exact action. scope="task"
    additionally allows that whole category of action (e.g. git add & commit)
    for the rest of this task, so it never pauses for it again.
    """
    task = task_store.get_task(task_id)
    if not task:
        return False, f"Task {task_id} not found."
    pending = pending_approval(task)
    if not pending:
        return False, f"Task {task_id} has no pending approval."
    scope = "task" if str(scope or "").lower() in {"task", "always", "all"} else "once"

    constraints = _constraints(task)
    constraints.pop("_pending_approval", None)
    grants = constraints.get("_approved_grants")
    grants = list(grants) if isinstance(grants, list) else []
    grants.append({
        "tool": pending.get("tool"),
        "summary": pending.get("summary"),
        "granted_at": datetime.now().isoformat(),
    })
    constraints["_approved_grants"] = grants
    action_key = pending.get("action_key") or task_action_key(pending.get("tool"), {})
    if scope == "task" and action_key:
        allowed = normalize_task_actions(constraints.get("_allowed_actions"))
        if action_key not in allowed:
            allowed.append(action_key)
        constraints["_allowed_actions"] = allowed
    _save_constraints(task_id, constraints)
    task_store.append_history(task_id, {
        "type": "approval_granted",
        "tool": pending.get("tool"),
        "detail": pending.get("summary"),
        "scope": scope,
    })
    if scope == "task" and action_key:
        label = task_action_label(action_key)
        _resume_with_guidance(
            task_id,
            f"The owner APPROVED running {pending.get('tool')} "
            f"({pending.get('summary')}) and allowed '{label}' for the rest of this "
            "task — run that action again now and continue; you won't be paused "
            "for that kind of action again in this task.",
        )
        return True, (
            f"Approved. Task {task_id} will run {pending.get('tool')} and continue, "
            f"and '{label}' is allowed for the rest of this task."
        )
    _resume_with_guidance(
        task_id,
        f"The owner APPROVED running {pending.get('tool')} "
        f"({pending.get('summary')}). This is a one-time approval — run that "
        "exact action again now and continue the task.",
    )
    return True, (
        f"Approved. Task {task_id} will run {pending.get('tool')} and continue."
    )


def deny(task_id: int) -> tuple[bool, str]:
    """Refuse the pending action and resume the task without it."""
    task = task_store.get_task(task_id)
    if not task:
        return False, f"Task {task_id} not found."
    pending = pending_approval(task)
    if not pending:
        return False, f"Task {task_id} has no pending approval."

    constraints = _constraints(task)
    constraints.pop("_pending_approval", None)
    _save_constraints(task_id, constraints)
    task_store.append_history(task_id, {
        "type": "approval_denied",
        "tool": pending.get("tool"),
        "detail": pending.get("summary"),
    })
    _resume_with_guidance(
        task_id,
        f"The owner DENIED running {pending.get('tool')} "
        f"({pending.get('summary')}). Do NOT retry it or work around it with a "
        "different tool. Adjust the plan, or finish honestly and report what "
        "was blocked.",
    )
    return True, f"Denied. Task {task_id} will continue without that action."


def consume_grant(task: dict, tool: str, inputs: dict) -> bool:
    """True if this protected action may run without pausing.

    Standing grants first: if the owner allowed this category of action for
    the whole task, it runs (and the use is logged in the timeline). Otherwise
    a one-shot grant must match exactly: same tool, and for command tools the
    same command string (compared via the summary). Consumed grants are removed.
    """
    constraints = _constraints(task)
    action_key = task_action_key(tool, inputs)
    if action_key and action_key in normalize_task_actions(constraints.get("_allowed_actions")):
        task_store.append_history(task["id"], {
            "type": "standing_grant_used",
            "tool": tool,
            "detail": action_summary(tool, inputs),
            "action": action_key,
        })
        return True

    grants = constraints.get("_approved_grants")
    if not isinstance(grants, list) or not grants:
        return False

    now = datetime.now()
    summary = action_summary(tool, inputs)
    remaining: list = []
    matched = False
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        try:
            granted_at = datetime.fromisoformat(str(grant.get("granted_at")))
            expired = (now - granted_at).total_seconds() > GRANT_TTL_MINUTES * 60
        except (ValueError, TypeError):
            expired = True
        if expired:
            continue  # drop silently
        if not matched and grant.get("tool") == tool and grant.get("summary") == summary:
            matched = True
            continue  # consume
        remaining.append(grant)

    if matched or len(remaining) != len(grants):
        constraints["_approved_grants"] = remaining
        _save_constraints(task["id"], constraints)
        task["constraints"] = constraints
    return matched
