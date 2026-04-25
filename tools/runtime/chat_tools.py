"""Tools for managing saved conversations (chat history)."""

from __future__ import annotations

from core import chat_store
from core.identity import chat_owner_id
from tools.memory.memory_tools import _get_active_user


def _active_owner_id() -> str | None:
    active = _get_active_user()
    return chat_owner_id(active) if active is not None else None


def _coerce_limit(value, default: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _make_snippet(content: str, match_index: int, query_len: int, radius: int = 80) -> str:
    start = max(0, match_index - radius)
    end = min(len(content), match_index + query_len + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return prefix + content[start:end].replace("\n", " ").strip() + suffix


def get_list_chats_tool():
    return {
        "name": "list_chats",
        "description": (
            "List saved past conversations. Returns each chat's id, title, and last-updated date. "
            "Use this to find a chat id before deleting it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max results (default: 20)"},
            },
        },
        "execute": _list_chats,
    }


def _list_chats(inputs: dict) -> dict:
    limit = _coerce_limit(inputs.get("limit"), 20, 100)
    chats = chat_store.list_chats(limit=limit, owner_id=_active_owner_id())
    if not chats:
        return {"chats": [], "count": 0, "message": "No saved chats."}
    lines = [f"[{c['id']}] {c['title']} (updated {c['updated_at'][:10]})" for c in chats]
    return {"chats": chats, "count": len(chats), "message": "\n".join(lines)}


def get_delete_chat_tool():
    return {
        "name": "delete_chat",
        "description": (
            "Permanently delete a saved conversation by its id. "
            "Always run list_chats first to find the id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat id to delete"},
            },
            "required": ["chat_id"],
        },
        "execute": _delete_chat,
    }


def _delete_chat(inputs: dict) -> dict:
    chat_id = str(inputs["chat_id"])
    if chat_store.delete_chat(chat_id, owner_id=_active_owner_id()):
        return {"deleted": True, "chat_id": chat_id, "message": f"Chat {chat_id} deleted."}
    return {"deleted": False, "message": f"Chat '{chat_id}' not found."}


def get_deep_memory_tool():
    return {
        "name": "deep_memory",
        "description": (
            "Search this user's saved raw conversation history for a keyword or phrase. "
            "Use this when recall doesn't find something the user mentioned before."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword or phrase to search for"},
                "limit": {"type": "number", "description": "Max matching messages to return (default: 10)"},
            },
            "required": ["query"],
        },
        "execute": _deep_memory,
    }


def _deep_memory(inputs: dict) -> dict:
    query = str(inputs.get("query", "")).strip()
    if not query:
        return {"count": 0, "matches": [], "error": "query is required"}

    limit = _coerce_limit(inputs.get("limit"), 10, 50)
    owner_id = _active_owner_id()
    query_lower = query.lower()
    matches = []

    for chat in chat_store.iter_chats_with_messages(owner_id=owner_id):
        for message in chat.get("messages", []):
            content = message.get("content", "") if isinstance(message, dict) else ""
            if not isinstance(content, str):
                continue
            idx = content.lower().find(query_lower)
            if idx < 0:
                continue
            matches.append({
                "chat_id": chat["id"],
                "title": chat["title"],
                "role": message.get("role"),
                "timestamp": message.get("timestamp"),
                "updated_at": chat.get("updated_at"),
                "snippet": _make_snippet(content, idx, len(query)),
            })
            if len(matches) >= limit:
                return {
                    "count": len(matches),
                    "matches": matches,
                    "scoped": owner_id is not None,
                }

    return {
        "count": len(matches),
        "matches": matches,
        "scoped": owner_id is not None,
    }
