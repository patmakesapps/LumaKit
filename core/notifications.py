"""Cross-surface notification log.

Records every routed notification (reminders, tasks, etc.) so a user who gets
pinged on Telegram while away can still see what they missed when they open
the web UI later.

`shown_on_web` is the replay flag: web replays unshown notifications on
WebSocket connect and then flips the flag so they don't repeat on refresh.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from core.paths import get_data_dir


DB_PATH = get_data_dir() / "memory" / "memory.db"


def _connect():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            content TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            shown_on_web INTEGER NOT NULL DEFAULT 0,
            meta_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
    if "meta_json" not in existing:
        conn.execute("ALTER TABLE notifications ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
    conn.commit()
    return conn


def _decode_rows(rows) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        raw_meta = item.pop("meta_json", "{}") or "{}"
        try:
            item["meta"] = json.loads(raw_meta)
        except json.JSONDecodeError:
            item["meta"] = {}
        out.append(item)
    return out


def log(content: str, label: str = "", user_id: str | None = None, meta: dict | None = None) -> int:
    conn = _connect()
    conn.execute(
        "INSERT INTO notifications (user_id, content, label, created_at, meta_json) VALUES (?, ?, ?, ?, ?)",
        (
            str(user_id) if user_id else None,
            content,
            label or "",
            datetime.now().isoformat(),
            json.dumps(meta or {}, separators=(",", ":")),
        ),
    )
    conn.commit()
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return row_id


def unshown_for_web(user_id: str | None, within_hours: int = 24, limit: int = 20) -> list[dict]:
    """Recent notifications not yet replayed on web.

    user_id=None returns broadcast/family notifications too. Otherwise, filters
    to this user's own + unscoped (chat_id NULL) entries.
    """
    conn = _connect()
    cutoff = (datetime.now() - timedelta(hours=within_hours)).isoformat()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE shown_on_web = 0 AND created_at >= ? "
            "AND (user_id = ? OR user_id IS NULL) "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, str(user_id), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE shown_on_web = 0 AND created_at >= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    conn.close()
    return _decode_rows(rows)


def claim_unshown_for_web(user_id: str | None, within_hours: int = 24, limit: int = 20) -> list[dict]:
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    cutoff = (datetime.now() - timedelta(hours=within_hours)).isoformat()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE shown_on_web = 0 AND created_at >= ? "
            "AND (user_id = ? OR user_id IS NULL) "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, str(user_id), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE shown_on_web = 0 AND created_at >= ? "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    if rows:
        conn.executemany(
            "UPDATE notifications SET shown_on_web = 1 WHERE id = ?",
            [(int(row["id"]),) for row in rows],
        )
    conn.commit()
    conn.close()
    return _decode_rows(rows)


def mark_shown_on_web(ids: list[int]) -> None:
    if not ids:
        return
    conn = _connect()
    conn.executemany(
        "UPDATE notifications SET shown_on_web = 1 WHERE id = ?",
        [(int(i),) for i in ids],
    )
    conn.commit()
    conn.close()


def recent(user_id: str | None = None, limit: int = 50) -> list[dict]:
    """Recent notifications for display (web API)."""
    conn = _connect()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? OR user_id IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return _decode_rows(rows)
