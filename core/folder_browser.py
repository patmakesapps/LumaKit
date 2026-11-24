"""List folders for the web UI's own folder browser.

The workspace picker used to open a native Tk window on the computer running
LumaKit. On a kiosk or touchscreen that window can open behind LumaKit's own
full-screen window, so nothing seemed to happen. The page now browses folders
itself through this listing, which works the same by mouse or by touch.
"""

from __future__ import annotations

import os
import string
from pathlib import Path

MAX_FOLDERS = 500


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_drives() -> list[dict]:
    drives = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        if root.exists():
            drives.append({"name": f"{letter}:", "path": str(root)})
    return drives


def _parent_of(path: Path) -> str | None:
    """The folder one level up; '' means the list of drives on Windows."""
    if path.parent != path:
        return str(path.parent)
    return "" if _is_windows() else None


def list_folders(raw_path: str | None, *, show_hidden: bool = False) -> dict:
    """Return the subfolders of ``raw_path``.

    An empty path means the top: ``/`` on Linux and macOS, the list of drives
    on Windows. Raises FileNotFoundError, NotADirectoryError or
    PermissionError for a path that cannot be listed.
    """
    raw = str(raw_path or "").strip()
    if not raw:
        if _is_windows():
            return {"path": "", "parent": None, "folders": _windows_drives(), "truncated": False}
        raw = "/"

    path = Path(raw).expanduser().resolve(strict=False)
    if not path.exists():
        raise FileNotFoundError(f"Folder does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"Not a folder: {path}")

    folders = []
    with os.scandir(path) as entries:
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            folders.append({"name": entry.name, "path": str(path / entry.name)})

    folders.sort(key=lambda f: f["name"].casefold())
    truncated = len(folders) > MAX_FOLDERS
    return {
        "path": str(path),
        "parent": _parent_of(path),
        "folders": folders[:MAX_FOLDERS],
        "truncated": truncated,
    }


def shortcuts(*, repo_root: Path) -> list[dict]:
    """Places worth one tap from anywhere: the home folder and LumaKit's own."""
    places = [{"name": "Home", "path": str(Path.home())}]
    repo = str(repo_root.resolve(strict=False))
    if repo != places[0]["path"]:
        places.append({"name": "LumaKit", "path": repo})
    return places
