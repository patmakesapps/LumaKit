import os
from datetime import datetime

from core import memory_store
from ollama_client import OllamaClient


# Set by the bridge before each user message. None in CLI mode.
_active_user = {"value": None}


def set_active_user(chat_id):
    """Called by the bridge to mark who's currently talking to the agent."""
    _active_user["value"] = str(chat_id) if chat_id is not None else None


def _parse_notify_at(value: str) -> str | None:
    """Use a focused LLM call to convert any time expression to ISO datetime.
    Returns a valid ISO string or None if parsing failed."""
    if not value or not value.strip():
        return None

    # Already valid ISO — skip the LLM call
    try:
        datetime.fromisoformat(value)
        return value
    except (ValueError, TypeError):
        pass

    # Ask the LLM to convert it
    now = datetime.now().isoformat()
    prompt = (
        f"The current date and time is: {now}\n"
        f"Convert this to an ISO 8601 datetime string: \"{value}\"\n"
        "Respond with ONLY the datetime string, nothing else. Example: 2026-04-08T14:30:00"
    )

    try:
        client = OllamaClient()
        model = os.getenv("OLLAMA_MODEL")
        response = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        result = response.get("message", {}).get("content", "").strip()

        # Validate that the LLM actually returned a valid datetime
        datetime.fromisoformat(result)
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
    active = _active_user["value"]
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
    active = _active_user["value"]
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
    active = _active_user["value"]
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
