"""Tools for managing saved conversations (chat history)."""

from __future__ import annotations

from core import chat_store


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
    limit = int(inputs.get("limit", 20))
    chats = chat_store.list_chats(limit=limit)
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
    if chat_store.delete_chat(chat_id):
        return {"deleted": True, "chat_id": chat_id, "message": f"Chat {chat_id} deleted."}
    return {"deleted": False, "message": f"Chat '{chat_id}' not found."}
