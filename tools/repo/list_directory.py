from pathlib import Path


def get_list_directory_tool():
    return {
        'name': 'list_directory',
        'description': 'Lists files and folders in a directory',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string', 'description': 'Directory to inspect (default current directory)'},
                'recursive': {'type': 'boolean', 'description': 'Whether to include nested files and folders'}
            }
        },
        'execute': _list_directory
    }


def _list_directory(inputs):
    target = Path(inputs.get('path', '.'))

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
            'path': str(entry),
            'type': 'directory' if entry.is_dir() else 'file'
        }
        if entry.is_file():
            item['size'] = entry.stat().st_size
        entries.append(item)

    return {
        'path': str(target.resolve()),
        'recursive': recursive,
        'count': len(entries),
        'entries': entries
    }
