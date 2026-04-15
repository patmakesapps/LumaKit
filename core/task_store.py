"""Persistent store for autonomous tasks.

Tasks live in the same memory/ directory as memories, in a separate
tasks.db file so they don't collide with the memory schema.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "memory" / "tasks.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            goal        TEXT NOT NULL,
            constraints TEXT NOT NULL DEFAULT '{}',
            status      TEXT NOT NULL DEFAULT 'planning',
            plan        TEXT NOT NULL DEFAULT '[]',
            current_step INTEGER NOT NULL DEFAULT 0,
            history     TEXT NOT NULL DEFAULT '[]',
            owner_chat_id TEXT,
            created_at  TEXT NOT NULL,
            due_at      TEXT,
            next_run_at TEXT,
            result      TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def create_task(
    title: str,
    goal: str,
    constraints: dict | None = None,
    owner_chat_id: str | None = None,
    due_at: str | None = None,
    start_at: str | None = None,
) -> int:
    """Insert a new task and return its id.

    start_at is when planning should kick off (defaults to now). Use this for
    tasks the user wants to begin at a specific future time — it's stored in
    next_run_at so the runner will skip the task until that moment.
    """
    conn = _connect()
    conn.execute(
        """INSERT INTO tasks
           (title, goal, constraints, status, plan, current_step,
            history, owner_chat_id, created_at, due_at, next_run_at)
           VALUES (?, ?, ?, 'planning', '[]', 0, '[]', ?, ?, ?, ?)""",
        (
            title,
            goal,
            json.dumps(constraints or {}),
            owner_chat_id,
            datetime.now().isoformat(),
            due_at,
            start_at or datetime.now().isoformat(),
        ),
    )
    conn.commit()
    task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return task_id


def set_plan(task_id: int, plan: list, next_run_at: str | None = None) -> None:
    """Store the generated plan and switch status to active."""
    conn = _connect()
    conn.execute(
        "UPDATE tasks SET plan=?, status='active', current_step=0, next_run_at=? WHERE id=?",
        (json.dumps(plan), next_run_at or datetime.now().isoformat(), task_id),
    )
    conn.commit()
    conn.close()


def append_history(task_id: int, entry: dict) -> None:
    """Append one entry to the task's history log."""
    conn = _connect()
    row = conn.execute("SELECT history FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return
    history = json.loads(row["history"])
    history.append({**entry, "timestamp": datetime.now().isoformat()})
    conn.execute("UPDATE tasks SET history=? WHERE id=?", (json.dumps(history), task_id))
    conn.commit()
    conn.close()


def advance_step(task_id: int, next_run_at: str) -> None:
    """Move to the next step and schedule its run time."""
    conn = _connect()
    conn.execute(
        "UPDATE tasks SET current_step = current_step + 1, next_run_at=? WHERE id=?",
        (next_run_at, task_id),
    )
    conn.commit()
    conn.close()


def update_task(task_id: int, **kwargs) -> None:
    """Generic field update. Caller passes column=value pairs."""
    if not kwargs:
        return
    cols = ", ".join(f"{k}=?" for k in kwargs)
    conn = _connect()
    conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*kwargs.values(), task_id))
    conn.commit()
    conn.close()


def complete_task(task_id: int, result: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE tasks SET status='done', result=? WHERE id=?",
        (result, task_id),
    )
    conn.commit()
    conn.close()


def fail_task(task_id: int, reason: str) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE tasks SET status='failed', result=? WHERE id=?",
        (reason, task_id),
    )
    conn.commit()
    conn.close()


def delete_task(task_id: int) -> bool:
    """Permanently delete a task. Returns False if it wasn't found."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_task(task_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return _deserialize(dict(row))


def get_tasks_by_owner(chat_id: str, limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE owner_chat_id=? ORDER BY created_at DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return [_deserialize(dict(r)) for r in rows]


def get_all_tasks(limit: int = 50) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_deserialize(dict(r)) for r in rows]


def get_due_tasks() -> list[dict]:
    """Return tasks that are ready for the runner to process.

    Includes:
    - Tasks in 'planning' state (need plan generated, next_run_at <= now)
    - Tasks in 'active' state where next_run_at <= now
    - Tasks in any non-terminal state where due_at has passed (for final report)
    """
    now = datetime.now().isoformat()
    conn = _connect()
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE status IN ('planning', 'active')
             AND next_run_at <= ?
           ORDER BY next_run_at ASC""",
        (now,),
    ).fetchall()
    conn.close()
    return [_deserialize(dict(r)) for r in rows]


def get_overdue_tasks() -> list[dict]:
    """Return tasks past their due_at that haven't been finalized."""
    now = datetime.now().isoformat()
    conn = _connect()
    rows = conn.execute(
        """SELECT * FROM tasks
           WHERE status NOT IN ('done', 'failed', 'cancelled')
             AND due_at IS NOT NULL
             AND due_at <= ?""",
        (now,),
    ).fetchall()
    conn.close()
    return [_deserialize(dict(r)) for r in rows]


def _deserialize(row: dict) -> dict:
    """Parse JSON fields back to Python objects."""
    for field in ("constraints", "plan", "history"):
        if isinstance(row.get(field), str):
            try:
                row[field] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                row[field] = {} if field == "constraints" else []
    return row
