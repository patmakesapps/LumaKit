from core.paths import get_display_path, get_repo_root, resolve_repo_path


def get_list_directory_tool():
    return {
        'name': 'list_directory',
        'description': 'Lists files and folders in a directory',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Directory to inspect (default current directory)'},
                'recursive': {'type': 'boolean', 'description': 'Whether to include nested files and folders'}
            }
        },
        'execute': _list_directory
    }


def _list_directory(inputs):
    target = resolve_repo_path(inputs['path'], kind='directory') if inputs.get('path') else get_repo_root()

    if not target.exists():
        raise FileNotFoundError(f"Directory not found: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {target}")

    recursive = bool(inputs.get('recursive', False))
    iterator = target.rglob('*') if recursive else target.iterdir()
    entries = []

    for entry in sorted(iterator, key=lambda value: (not value.is_dir(), str(value).lower())):
        item = {
            'name': entry.name,
            'path': get_display_path(entry),
            'type': 'directory' if entry.is_dir() else 'file'
        }
        if entry.is_file():
            item['size'] = entry.stat().st_size
        entries.append(item)

    return {
        'path': get_display_path(target),
        'recursive': recursive,
        'count': len(entries),
        'entries': entries
    }
