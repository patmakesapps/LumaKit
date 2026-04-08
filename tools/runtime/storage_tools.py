from core.paths import get_repo_root
from core.storage import StorageManager


def _get_manager():
    return StorageManager(get_repo_root())


def get_check_storage_tool():
    return {
        "name": "check_storage",
        "description": (
            "Check disk usage of LumaKit's persistent stores (memory.db, index cache). "
            "Shows total usage, budget, and per-store breakdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
        "execute": lambda inputs: _get_manager().check_health(),
    }


def get_clear_storage_tool():
    return {
        "name": "clear_storage",
        "description": (
            "Free disk space by clearing LumaKit storage. "
            "Specify which store to clear: 'index_cache' to rebuild code index, "
            "'old_memories' to delete memories older than a given number of days, "
            "or 'all' to clear everything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "What to clear: 'index_cache', 'old_memories', or 'all'",
                },
                "older_than_days": {
                    "type": "integer",
                    "description": "For old_memories: delete memories older than this many days (default 30)",
                },
            },
            "required": ["target"],
        },
        "execute": _clear_storage,
    }


def _clear_storage(inputs):
    target = inputs["target"]
    manager = _get_manager()
    before = manager.get_total_bytes()
    cleared = []

    if target in ("index_cache", "all"):
        cache_path = manager.stores["index_cache"]
        if cache_path.exists():
            size = cache_path.stat().st_size
            cache_path.unlink()
            cleared.append(f"index_cache ({_fmt(size)})")

    if target in ("old_memories", "all"):
        import sqlite3
        from datetime import datetime, timedelta

        days = int(inputs.get("older_than_days", 30))
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        db_path = manager.stores["memory.db"]

        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute(
                "DELETE FROM memories WHERE created_at < ?", (cutoff,)
            )
            deleted_count = cursor.rowcount
            conn.execute("VACUUM")
            conn.commit()
            conn.close()
            if deleted_count > 0:
                cleared.append(f"{deleted_count} memories older than {days} days")

    after = manager.get_total_bytes()
    freed = before - after

    return {
        "cleared": cleared if cleared else ["nothing to clear"],
        "freed": _fmt(freed) if freed > 0 else "0 B",
        "current_usage": manager.check_health(),
    }


def _fmt(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
