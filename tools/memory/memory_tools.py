from core import memory_store


def get_remember_tool():
    return {
        "name": "remember",
        "description": (
            "Save something to long-term memory. Use this when the user asks you to "
            "remember something, or when you learn an important fact, preference, or task. "
            "Set type to 'reminder' and provide notify_at for time-based reminders."
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
                    "description": "ISO datetime for reminders (e.g. 2026-04-08T09:00:00)",
                },
            },
            "required": ["content"],
        },
        "execute": _remember,
    }


def _remember(inputs):
    content = inputs["content"]
    memory_type = inputs.get("type", "fact")
    notify_at = inputs.get("notify_at")
    row_id = memory_store.save(content, memory_type, notify_at)
    return {"saved": True, "id": row_id, "type": memory_type}


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
