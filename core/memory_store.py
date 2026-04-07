import sqlite3
from datetime import datetime
from pathlib import Path

# Database lives at memory/memory.db in the project root
DB_PATH = Path(__file__).resolve().parent.parent / "memory" / "memory.db"


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
            notify_at TEXT
        )
    """)
    conn.commit()
    return conn


def save(content, memory_type="fact", notify_at=None):
    """Store a memory. Returns the new row's id."""
    conn = _connect()
    conn.execute(
        "INSERT INTO memories (content, type, created_at, notify_at) VALUES (?, ?, ?, ?)",
        (content, memory_type, datetime.now().isoformat(), notify_at),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def search(query, limit=10):
    """Find memories whose content contains the query string."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM memories WHERE content LIKE ? ORDER BY created_at DESC LIMIT ?",
        (f"%{query}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent(limit=5):
    """Return the N most recent memories."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
