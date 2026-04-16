import os
from pathlib import Path

from core.paths import get_data_dir


DEFAULT_BUDGET_MB = 50
WARN_THRESHOLD = 0.8  # warn at 80% of budget


MILESTONES = [25, 50, 75]


class StorageManager:
    """Tracks disk usage of LumaKit's persistent stores and warns when space is low."""

    def __init__(self, project_root: Path, budget_mb: float = DEFAULT_BUDGET_MB):
        self.project_root = project_root
        self.budget_bytes = int(budget_mb * 1024 * 1024)
        self._last_milestone: int | None = None  # last milestone % we alerted on

        # Known storage locations
        data = get_data_dir()
        self.stores = {
            "memory.db": data / "memory" / "memory.db",
            "index_cache": data / "code_index.json",
        }

    def get_usage(self) -> dict[str, dict]:
        """Return size info for each store."""
        usage = {}
        for name, path in self.stores.items():
            size = 0
            if path.exists():
                if path.is_file():
                    size = path.stat().st_size
                elif path.is_dir():
                    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            usage[name] = {
                "path": str(path),
                "size_bytes": size,
                "size_display": _format_size(size),
            }
        return usage

    def get_total_bytes(self) -> int:
        return sum(info["size_bytes"] for info in self.get_usage().values())

    def check_health(self) -> dict:
        """Check storage health. Returns status + details."""
        usage = self.get_usage()
        total = sum(info["size_bytes"] for info in usage.values())
        ratio = total / self.budget_bytes if self.budget_bytes > 0 else 0

        if ratio >= 1.0:
            status = "critical"
        elif ratio >= WARN_THRESHOLD:
            status = "warning"
        else:
            status = "ok"

        return {
            "status": status,
            "total_bytes": total,
            "total_display": _format_size(total),
            "budget_display": _format_size(self.budget_bytes),
            "usage_percent": round(ratio * 100, 1),
            "stores": usage,
        }

    def check_milestone(self) -> str | None:
        """Return a meter string if we just crossed a milestone (25/50/75%), else None."""
        health = self.check_health()
        pct = health["usage_percent"]

        # Find which milestone we're at
        current_milestone = None
        for m in MILESTONES:
            if pct >= m:
                current_milestone = m

        if current_milestone is None:
            return None

        # Only alert once per milestone
        if current_milestone == self._last_milestone:
            return None

        self._last_milestone = current_milestone

        from core.cli import render_storage_meter
        return render_storage_meter(
            pct, health["total_display"], health["budget_display"]
        )

    def check_full(self) -> dict | None:
        """If storage is full, return breakdown with suggestions. Else None."""
        health = self.check_health()
        if health["status"] != "critical":
            return None

        # Rank stores by size, suggest clearing the biggest
        stores_by_size = sorted(
            health["stores"].items(),
            key=lambda x: x[1]["size_bytes"],
            reverse=True,
        )
        biggest_name, biggest_info = stores_by_size[0]

        return {
            "total_display": health["total_display"],
            "budget_display": health["budget_display"],
            "stores": health["stores"],
            "suggestion": biggest_name,
            "suggestion_size": biggest_info["size_display"],
        }

    def is_write_allowed(self) -> bool:
        """Check if we're under budget. Use before writing new cache data."""
        health = self.check_health()
        return health["status"] != "critical"

    def format_warning(self) -> str | None:
        """Return a user-facing warning string if storage is above threshold, else None."""
        health = self.check_health()
        if health["status"] == "ok":
            return None

        lines = []
        if health["status"] == "critical":
            lines.append(f"Storage limit reached: {health['total_display']} / {health['budget_display']} ({health['usage_percent']}%)")
        else:
            lines.append(f"Storage usage high: {health['total_display']} / {health['budget_display']} ({health['usage_percent']}%)")

        lines.append("Breakdown:")
        for name, info in health["stores"].items():
            if info["size_bytes"] > 0:
                lines.append(f"  {name}: {info['size_display']}")

        lines.append("Use 'clear_storage' to free space.")
        return "\n".join(lines)


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
