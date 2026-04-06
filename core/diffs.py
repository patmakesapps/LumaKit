from __future__ import annotations

import difflib
from pathlib import Path

from core.paths import get_display_path


MAX_DIFF_CHARS = 4000


def truncate_text(value: str, limit: int = MAX_DIFF_CHARS) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit] + "\n... [diff truncated]", True


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def normalize_line_endings(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", newline)


def build_unified_diff(before: str, after: str, path: Path, context_lines: int = 3) -> dict:
    display_path = get_display_path(path)
    diff_text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{display_path}",
            tofile=f"b/{display_path}",
            n=context_lines,
        )
    )
    truncated_diff, truncated = truncate_text(diff_text)
    return {
        "path": display_path,
        "diff": truncated_diff,
        "diff_truncated": truncated,
        "has_changes": bool(diff_text),
    }
