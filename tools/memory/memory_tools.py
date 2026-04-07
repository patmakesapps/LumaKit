import os
from datetime import datetime

from core import memory_store
from ollama_client import OllamaClient


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
            },
            "required": ["content"],
        },
        "execute": _remember,
    }


def _remember(inputs):
    content = inputs["content"]
    memory_type = inputs.get("type", "fact")
    raw_notify = inputs.get("notify_at", "")

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

    row_id = memory_store.save(content, memory_type, notify_at)
    result = {"saved": True, "id": row_id, "type": memory_type}
    if notify_at:
        result["notify_at"] = notify_at
    return result


def get_recall_tool():
    return {
        "name": "recall",
        "description": (
            "Search long-term memory. Use this when the user asks about something "
            "you might have saved before, or when you need context from past conversations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "limit": {"type": "number", "description": "Max results (default: 10)"},
            },
            "required": ["query"],
        },
        "execute": _recall,
    }


def _recall(inputs):
    query = inputs["query"]
    limit = int(inputs.get("limit", 10))
    results = memory_store.search(query, limit)
    return {"count": len(results), "memories": results}


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
    memory_store.delete(memory_id)
    return {"deleted": True, "id": memory_id}
