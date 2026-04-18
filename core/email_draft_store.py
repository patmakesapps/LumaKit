from __future__ import annotations

import sqlite3
from datetime import datetime

from core.paths import get_data_dir


DB_PATH = get_data_dir() / "memory" / "memory.db"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_email_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_addr TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            from_label TEXT NOT NULL,
            uid INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def add_pending(to_addr: str, subject: str, body: str, from_label: str, uid: int | None = None) -> int:
    conn = _connect()
    conn.execute(
        "INSERT INTO pending_email_drafts (to_addr, subject, body, from_label, uid, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (to_addr, subject, body, from_label, uid, datetime.now().isoformat()),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def get_pending(draft_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM pending_email_drafts WHERE id = ?",
        (int(draft_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_latest_pending() -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM pending_email_drafts ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_pending(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM pending_email_drafts ORDER BY created_at DESC, id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def pop_pending(draft_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM pending_email_drafts WHERE id = ?",
        (int(draft_id),),
    ).fetchone()
    if not row:
        conn.close()
        return None
    conn.execute("DELETE FROM pending_email_drafts WHERE id = ?", (int(draft_id),))
    conn.commit()
    conn.close()
    return dict(row)


def pop_latest_pending() -> dict | None:
    latest = get_latest_pending()
    if not latest:
        return None
    return pop_pending(latest["id"])
