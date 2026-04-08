from pathlib import Path

from core.paths import get_display_path, get_repo_root


def get_move_path_tool():
    return {
        "name": "move_path",
        "description": "Move or rename a file or directory. If confirm is false, returns a preview without making changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_path": {
                    "type": "string",
                    "description": "Path of the file or directory to move",
                },
                "destination_path": {
                    "type": "string",
                    "description": "Target path for the move (use a directory path to move into that folder)",
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting existing destination (default: false)",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Execute the move if true, preview only if false (default: false)",
                },
            },
            "required": ["source_path", "destination_path"],
        },
        "execute": _move_path,
    }


def _resolve_path(raw: str) -> Path:
    """Resolve a path relative to the repo root. No fuzzy matching."""
    p = Path(raw.strip())
    if p.is_absolute():
        return p.resolve()
    return (get_repo_root() / p).resolve()


def _move_path(inputs):
    source_path = _resolve_path(inputs["source_path"])
    dest_raw = inputs["destination_path"]
    overwrite = inputs.get("overwrite", False)
    confirm = inputs.get("confirm", False)

    # Validate source exists
    if not source_path.exists():
        raise FileNotFoundError(
            f"Source path does not exist: {get_display_path(source_path)}"
        )

    # Determine if source is file or directory
    if source_path.is_file():
        kind = "file"
    elif source_path.is_dir():
        kind = "directory"
    else:
        raise ValueError(
            f"Source path is neither a file nor directory: {get_display_path(source_path)}"
        )

    # Resolve destination — if it's an existing directory, move source into it
    dest_path = _resolve_path(dest_raw)
    if dest_path.is_dir():
        dest_path = dest_path / source_path.name

    # Check for overwrite conflicts
    if dest_path.exists():
        if not overwrite:
            raise FileExistsError(
                f"Destination already exists: {get_display_path(dest_path)}. Set overwrite=true to replace it."
            )
        if dest_path.is_dir() and source_path.is_file():
            raise ValueError(
                f"Cannot overwrite a directory with a file: {get_display_path(dest_path)}"
            )
        if dest_path.is_file() and source_path.is_dir():
            raise ValueError(
                f"Cannot overwrite a file with a directory: {get_display_path(dest_path)}"
            )

    source_display = get_display_path(source_path)
    dest_display = get_display_path(dest_path)

    # Preview mode
    if not confirm:
        return {
            "source_path": source_display,
            "destination_path": dest_display,
            "kind": kind,
            "moved": False,
            "preview": True,
        }

    # Execute the move
    try:
        source_path.rename(dest_path)
    except OSError as e:
        raise RuntimeError(f"Failed to move {source_display} to {dest_display}: {e}")

    return {
        "source_path": source_display,
        "destination_path": dest_display,
        "kind": kind,
        "moved": True,
    }
