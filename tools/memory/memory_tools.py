import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta

from core import memory_store
from core.runtime_config import get_effective_config_for_user
from ollama_client import OllamaClient


# Per-turn: who is currently talking to the agent. None in CLI mode.
_active_user: ContextVar[str | None] = ContextVar("lumakit_memory_active_user", default=None)


def set_active_user(chat_id):
    """Called by the bridge to mark who's currently talking to the agent."""
    _active_user.set(str(chat_id) if chat_id is not None else None)


def _get_active_user():
    return _active_user.get()


def _parse_simple_relative_time(value: str, now: datetime) -> str | None:
    match = re.fullmatch(
        r"(?:in\s+)?(?P<amount>\d+)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
        value.strip().lower(),
    )
    if not match:
        return None

    amount = int(match.group("amount"))
    if amount <= 0:
        return None

    unit = match.group("unit")
    if unit.startswith(("second", "sec")) or unit == "s":
        delta = timedelta(seconds=amount)
    elif unit.startswith(("minute", "min")) or unit == "m":
        delta = timedelta(minutes=amount)
    elif unit.startswith(("hour", "hr")) or unit == "h":
        delta = timedelta(hours=amount)
    elif unit.startswith("day") or unit == "d":
        delta = timedelta(days=amount)
    else:
        delta = timedelta(weeks=amount)

    return (now + delta).replace(microsecond=0).isoformat()


def _parse_notify_at(value: str) -> str | None:
    """Use a focused LLM call to convert any time expression to ISO datetime.
    Returns a valid ISO string or None if parsing failed."""
    if not value or not value.strip():
        return None

    value = value.strip()
    now = datetime.now()

    # Already valid ISO — skip the LLM call
    try:
        parsed = datetime.fromisoformat(value)
        return value if parsed > now else None
    except (ValueError, TypeError):
        pass

    parsed_relative = _parse_simple_relative_time(value, now)
    if parsed_relative:
        return parsed_relative

    # Ask the LLM to convert it using the same effective runtime model the
    # active user is already using in chat.
    now_iso = now.replace(microsecond=0).isoformat()
    timezone_name = datetime.now().astimezone().tzname() or "local time"
    active_user = _get_active_user()
    model_cfg = get_effective_config_for_user(active_user)
    prompt = (
        "Convert the reminder time below into a local ISO 8601 datetime.\n"
        f"Current local date and time: {now_iso}\n"
        f"Local timezone: {timezone_name}\n"
        f"Reminder expression: \"{value}\"\n\n"
        "Rules:\n"
        "- Respond with ONLY one datetime in this exact format: YYYY-MM-DDTHH:MM:SS\n"
        "- Always interpret the reminder as a FUTURE local time.\n"
        "- If the user gives only a time and that time has already passed today, use tomorrow.\n"
        "- If the user gives a weekday, choose the next future occurrence of that weekday.\n"
        "- If you cannot determine a valid future time, respond with INVALID.\n"
        "Examples:\n"
        "- \"in 5 minutes\" -> 2026-04-17T15:25:00\n"
        "- \"tomorrow at 9am\" -> 2026-04-18T09:00:00\n"
        "- \"Friday at 2\" -> 2026-04-24T14:00:00"
    )

    try:
        client = OllamaClient(fallback_model=model_cfg.get("fallback_model"))
        model = model_cfg.get("primary_model") or os.getenv("OLLAMA_MODEL")
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        result = response.get("message", {}).get("content", "").strip()
        if result == "INVALID":
            return None

        # Validate that the LLM actually returned a valid datetime
        parsed = datetime.fromisoformat(result)
        if parsed <= now:
            return None
        return result
    except (ValueError, TypeError, Exception):
        return None


def get_remember_tool():
    return {
        "name": "remember",
        "description": (
            "Save something to long-term memory. Use this when the user asks you to "
            "remember something, or when you learn an important fact, preference, or task. "
            "For reminders, set type to 'reminder' and set notify_at to when the user should "
            "be notified. Use natural language like '5 minutes', 'tomorrow at 9am', "
            "'next Tuesday at 3pm', etc."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "What to remember"},
                "type": {
                    "type": "string",
                    "enum": ["fact", "reminder", "preference", "task"],
                    "description": "Memory type (default: fact)",
                },
                "notify_at": {
                    "type": "string",
                    "description": "When to notify, in natural language (e.g. '5 minutes', 'tomorrow at 9am', 'next Friday at 2pm')",
                },
                "scope": {
                    "type": "string",
                    "enum": ["me", "everyone"],
                    "description": (
                        "Who this memory is for. Default 'me' (personal — only the "
                        "requester sees/gets it). Use 'everyone' ONLY when the user "
                        "explicitly asks for a family/group/household reminder — e.g. "
                        "'remind us', 'tell everyone', 'family reminder', 'remind the "
                        "whole house'. When in doubt, use 'me'."
                    ),
                },
            },
            "required": ["content"],
        },
        "execute": _remember,
    }


def _remember(inputs):
    content = inputs["content"]
    memory_type = inputs.get("type", "fact")
    raw_notify = inputs.get("notify_at", "")
    scope = inputs.get("scope", "me")

    # For reminders, parse and validate the time
    if memory_type == "reminder" and raw_notify:
        notify_at = _parse_notify_at(raw_notify)
        if not notify_at:
            return {
                "saved": False,
                "error": f"Could not parse reminder time: '{raw_notify}'. Ask the user to clarify.",
            }
    else:
        notify_at = None

    # Resolve scope → chat_id. 'everyone' always broadcasts (chat_id=None).
    # 'me' stores the caller's id; in CLI mode (no active user) it stays None.
    active = _get_active_user()
    chat_id = None if scope == "everyone" else active
    created_by = active

    row_id = memory_store.save(
        content, memory_type, notify_at, chat_id=chat_id, created_by=created_by
    )
    result = {
        "saved": True,
        "id": row_id,
        "type": memory_type,
        "scope": "family" if chat_id is None else "personal",
    }
    if notify_at:
        result["notify_at"] = notify_at
        print(f"  [reminder scheduled] {notify_at} ({result['scope']})")
    return result


def get_recall_tool():
    return {
        "name": "recall",
        "description": (
            "Search long-term memory. Use this when the user asks about something "
            "you might have saved before, or when you need context from past conversations. "
            "Leave query empty to list ALL saved memories. Use short keywords to search "
            "(e.g. 'name', 'wifi', 'appointment') — not full sentences."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to search for. Leave empty to list all memories."},
                "limit": {"type": "number", "description": "Max results (default: 20)"},
            },
            "required": [],
        },
        "execute": _recall,
    }


def _recall(inputs):
    query = inputs.get("query", "").strip()
    limit = int(inputs.get("limit", 20))
    active = _get_active_user()
    if query:
        results = memory_store.search(query, limit, active_user=active)
    else:
        results = memory_store.get_recent(limit, active_user=active)
    return {"count": len(results), "memories": results}


def _check_owner(memory_id):
    """Return (memory, error_dict_or_None). Blocks edits of other users' memories."""
    memory = memory_store.get_by_id(memory_id)
    if not memory:
        return None, {"error": f"Memory {memory_id} not found"}
    active = _get_active_user()
    creator = memory.get("created_by")
    # CLI (no active user) and legacy rows (no creator) bypass the check.
    if active and creator and str(creator) != str(active):
        return memory, {"error": "You can only change memories you created."}
    return memory, None


def get_update_memory_tool():
    return {
        "name": "update_memory",
        "description": (
            "Update an existing memory by its id. Use this instead of creating "
            "a new memory when the user wants to add to or change something "
            "already saved (e.g. adding items to a list, updating a preference, "
            "or changing a reminder time). "
            "Always recall first to find the memory id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "Memory id to update"},
                "content": {"type": "string", "description": "The full updated content"},
                "notify_at": {
                    "type": "string",
                    "description": "New reminder time in natural language (e.g. '2:15pm', 'tomorrow at 9am'). Only needed if changing when a reminder fires.",
                },
            },
            "required": ["id", "content"],
        },
        "execute": _update_memory,
    }


def _update_memory(inputs):
    memory_id = int(inputs["id"])
    content = inputs["content"]
    raw_notify = inputs.get("notify_at", "")

    _, err = _check_owner(memory_id)
    if err:
        return {"updated": False, **err}

    notify_at = None
    if raw_notify:
        notify_at = _parse_notify_at(raw_notify)
        if not notify_at:
            return {"updated": False, "error": f"Could not parse reminder time: '{raw_notify}'"}

    updated = memory_store.update(memory_id, content, notify_at=notify_at)
    if not updated:
        return {"updated": False, "error": f"Memory {memory_id} not found"}
    result = {"updated": True, "id": memory_id}
    if notify_at:
        result["notify_at"] = notify_at
        print(f"  [reminder updated] {notify_at}")
    return result


def get_forget_tool():
    return {
        "name": "forget",
        "description": "Delete a memory by its id. Use when the user asks you to forget something.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "number", "description": "Memory id to delete"},
            },
            "required": ["id"],
        },
        "execute": _forget,
    }


def _forget(inputs):
    memory_id = int(inputs["id"])
    _, err = _check_owner(memory_id)
    if err:
        return {"deleted": False, **err}
    memory_store.delete(memory_id)
    return {"deleted": True, "id": memory_id}
