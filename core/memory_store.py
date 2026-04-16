import sqlite3
from datetime import datetime

from core.paths import get_data_dir

DB_PATH = get_data_dir() / "memory" / "memory.db"


def _connect():
    """Open a connection and create the table if it doesn't exist yet."""
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            type TEXT DEFAULT 'fact',
            created_at TEXT NOT NULL,
            notify_at TEXT,
            chat_id TEXT,
            created_by TEXT
        )
    """)
    # Migrate older DBs that predate the scope/authorship columns.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "chat_id" not in existing:
        conn.execute("ALTER TABLE memories ADD COLUMN chat_id TEXT")
    if "created_by" not in existing:
        conn.execute("ALTER TABLE memories ADD COLUMN created_by TEXT")
    conn.commit()
    return conn


def save(content, memory_type="fact", notify_at=None, chat_id=None, created_by=None):
    """Store a memory. chat_id=None means family/universal. Returns the new row's id."""
    conn = _connect()
    conn.execute(
        "INSERT INTO memories (content, type, created_at, notify_at, chat_id, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (content, memory_type, datetime.now().isoformat(), notify_at, chat_id, created_by),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def _scope_clause(active_user):
    """Build a WHERE fragment limiting results to the caller's visible scope.

    active_user=None (CLI) sees everything. Otherwise: own personal + all family.
    """
    if active_user is None:
        return "", ()
    return " AND (chat_id = ? OR chat_id IS NULL)", (str(active_user),)


def search(query, limit=10, active_user=None):
    """Find memories whose content contains the query string, scoped to the caller."""
    conn = _connect()
    clause, params = _scope_clause(active_user)
    rows = conn.execute(
        f"SELECT * FROM memories WHERE content LIKE ?{clause} "
        "ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", *params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent(limit=5, active_user=None):
    """Return the N most recent memories visible to the caller."""
    conn = _connect()
    clause, params = _scope_clause(active_user)
    rows = conn.execute(
        f"SELECT * FROM memories WHERE 1=1{clause} "
        "ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_by_id(memory_id):
    """Fetch a single memory by id (used for auth checks). Returns dict or None."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ?", (memory_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update(memory_id, content, notify_at=None):
    """Update a memory's content (and optionally notify_at) by id. Returns True if found and updated."""
    conn = _connect()
    if notify_at is not None:
        cursor = conn.execute(
            "UPDATE memories SET content = ?, notify_at = ? WHERE id = ?",
            (content, notify_at, memory_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE memories SET content = ? WHERE id = ?",
            (content, memory_id),
        )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def delete(memory_id):
    """Delete a memory by id."""
    conn = _connect()
    conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    conn.close()


def get_due_reminders():
    """Return all reminders where notify_at is in the past (i.e. they're due)."""
    conn = _connect()
    now = datetime.now().isoformat()
    rows = conn.execute(
        "SELECT * FROM memories WHERE type = 'reminder' AND notify_at <= ? ORDER BY notify_at",
        (now,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
