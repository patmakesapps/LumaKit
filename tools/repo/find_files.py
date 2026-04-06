from pathlib import Path


def get_find_files_tool():
    return {
        'name': 'find_files',
        'description': 'Finds files by glob pattern',
        'inputSchema': {
            'properties': {
                'pattern': {'type': 'string', 'description': 'Glob pattern such as *.py or **/*.md'},
                'path': {'type': 'string', 'description': 'Directory to search (default current directory)'},
                'recursive': {'type': 'boolean', 'description': 'Whether to search subdirectories (default true)'}
            },
            'required': ['pattern']
        },
        'execute': _find_files
    }


def _find_files(inputs):
    target = Path(inputs.get('path', '.'))

    if not target.exists():
        raise FileNotFoundError(f"Directory not found: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {target}")

    pattern = inputs['pattern']
    recursive = bool(inputs.get('recursive', True))
    iterator = target.rglob(pattern) if recursive else target.glob(pattern)
    matches = [str(path) for path in sorted(iterator) if path.is_file()]

    return {
        'path': str(target.resolve()),
        'pattern': pattern,
        'recursive': recursive,
        'count': len(matches),
        'matches': matches
    }
