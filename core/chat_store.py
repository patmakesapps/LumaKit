import json
import sqlite3
import uuid
from datetime import datetime

from core.paths import get_data_dir


DB_PATH = get_data_dir() / "memory" / "memory.db"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            owner_id TEXT,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            messages TEXT NOT NULL
        )
    """)
    # Per-user "active chat" pointer — lets any surface resume the current
    # conversation on connect so Telegram ↔ web feels continuous.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_chats (
            user_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE conversations ADD COLUMN owner_id TEXT")
        columns.add("owner_id")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated ON conversations(owner_id, updated_at)")
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version < 1:
        conn.execute("""
            UPDATE conversations
               SET owner_id = (
                   SELECT user_id
                     FROM active_chats
                    WHERE active_chats.chat_id = conversations.id
                    ORDER BY active_chats.updated_at DESC
                    LIMIT 1
               )
             WHERE owner_id IS NULL
               AND EXISTS (
                   SELECT 1 FROM active_chats WHERE active_chats.chat_id = conversations.id
               )
        """)
        conn.execute("PRAGMA user_version = 1")
    conn.commit()
    return conn


def set_active_chat(user_id: str, chat_id: str) -> None:
    """Mark this chat as the user's current active conversation."""
    if not user_id or not chat_id:
        return
    conn = _connect()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO active_chats (user_id, chat_id, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, updated_at = excluded.updated_at",
        (str(user_id), str(chat_id), now),
    )
    conn.commit()
    conn.close()


def get_active_chat(user_id: str) -> str | None:
    """Return the user's current active chat id, or None if never set."""
    if not user_id:
        return None
    conn = _connect()
    row = conn.execute(
        "SELECT chat_id FROM active_chats WHERE user_id = ?", (str(user_id),)
    ).fetchone()
    conn.close()
    return row["chat_id"] if row else None


def save_chat(chat_id: str, title: str, messages: list[dict], owner_id: str | None = None) -> str:
    """Save or update a conversation. Returns the chat id."""
    conn = _connect()
    now = datetime.now().isoformat()
    messages_json = json.dumps(messages, default=str)
    owner = str(owner_id) if owner_id is not None else None

    existing = conn.execute(
        "SELECT id FROM conversations WHERE id = ?", (chat_id,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ?, messages = ?, owner_id = COALESCE(?, owner_id) WHERE id = ?",
            (title, now, messages_json, owner, chat_id),
        )
    else:
        conn.execute(
            "INSERT INTO conversations (id, owner_id, title, created_at, updated_at, messages) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, owner, title, now, now, messages_json),
        )

    conn.commit()
    conn.close()
    return chat_id


def load_chat(chat_id: str, owner_id: str | None = None) -> dict | None:
    """Load a conversation by id. Returns dict with id, title, messages, etc."""
    conn = _connect()
    if owner_id is None:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (chat_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND owner_id = ?",
            (chat_id, str(owner_id)),
        ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": json.loads(row["messages"]),
    }


def list_chats(limit: int = 20, owner_id: str | None = None) -> list[dict]:
    """List recent conversations, newest first."""
    conn = _connect()
    if owner_id is None:
        rows = conn.execute(
            "SELECT id, owner_id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, owner_id, title, created_at, updated_at FROM conversations WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ?",
            (str(owner_id), limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_chat(chat_id: str, owner_id: str | None = None) -> bool:
    """Delete a conversation. Returns True if it existed."""
    conn = _connect()
    if owner_id is None:
        cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (chat_id,))
    else:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND owner_id = ?",
            (chat_id, str(owner_id)),
        )
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def iter_chats_with_messages(owner_id: str | None = None) -> list[dict]:
    """Return saved conversations with messages for read-only search."""
    conn = _connect()
    if owner_id is None:
        rows = conn.execute(
            "SELECT id, owner_id, title, created_at, updated_at, messages FROM conversations ORDER BY updated_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, owner_id, title, created_at, updated_at, messages FROM conversations WHERE owner_id = ? ORDER BY updated_at DESC",
            (str(owner_id),),
        ).fetchall()
    conn.close()

    chats = []
    for row in rows:
        try:
            messages = json.loads(row["messages"])
        except (TypeError, json.JSONDecodeError):
            messages = []
        chats.append({
            "id": row["id"],
            "owner_id": row["owner_id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "messages": messages,
        })
    return chats


def new_chat_id() -> str:
    """Generate a short chat id."""
    return uuid.uuid4().hex[:8]


def make_title(first_message: str) -> str:
    """Auto-generate a title from the first user message."""
    title = first_message.strip().replace("\n", " ")
    if len(title) > 50:
        title = title[:47] + "..."
    return title
