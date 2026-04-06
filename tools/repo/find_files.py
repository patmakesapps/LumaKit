from core.paths import get_display_path, get_repo_root, resolve_repo_path


def get_find_files_tool():
    return {
        'name': 'find_files',
        'description': 'Find files by glob pattern.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'pattern': {'type': 'string'},
                'path': {'type': 'string'}
            },
            'required': ['pattern']
        },
        'execute': _find_files
    }


def _find_files(inputs):
    target = resolve_repo_path(inputs['path'], kind='directory') if inputs.get('path') else get_repo_root()

    if not target.exists():
        raise FileNotFoundError(f"Directory not found: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {target}")

    pattern = inputs['pattern']
    recursive = bool(inputs.get('recursive', True))
    iterator = target.rglob(pattern) if recursive else target.glob(pattern)
    matches = [get_display_path(path) for path in sorted(iterator) if path.is_file()]

    return {
        'path': get_display_path(target),
        'pattern': pattern,
        'recursive': recursive,
        'count': len(matches),
        'matches': matches
    }
