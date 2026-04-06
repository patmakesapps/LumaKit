from pathlib import Path


def get_search_file_contents_tool():
    return {
        'name': 'search_file_contents',
        'description': 'Searches file contents for matching text',
        'inputSchema': {
            'properties': {
                'query': {'type': 'string', 'description': 'Text to search for'},
                'path': {'type': 'string', 'description': 'Directory to search (default current directory)'},
                'file_pattern': {'type': 'string', 'description': 'Glob pattern used to filter files (default *)'},
                'case_sensitive': {'type': 'boolean', 'description': 'Whether the search should be case-sensitive'},
                'recursive': {'type': 'boolean', 'description': 'Whether to search subdirectories (default true)'},
                'max_results': {'type': 'number', 'description': 'Maximum matches to return (default 50)'}
            },
            'required': ['query']
        },
        'execute': _search_file_contents
    }


def _search_file_contents(inputs):
    target = Path(inputs.get('path', '.'))

    if not target.exists():
        raise FileNotFoundError(f"Directory not found: {target}")
    if not target.is_dir():
        raise NotADirectoryError(f"Path is not a directory: {target}")

    query = inputs['query']
    file_pattern = inputs.get('file_pattern', '*')
    case_sensitive = bool(inputs.get('case_sensitive', False))
    recursive = bool(inputs.get('recursive', True))
    max_results = int(inputs.get('max_results', 50))
    iterator = target.rglob(file_pattern) if recursive else target.glob(file_pattern)

    needle = query if case_sensitive else query.lower()
    matches = []
    scanned_files = 0

    for file_path in sorted(iterator):
        if not file_path.is_file():
            continue

        scanned_files += 1

        try:
            with file_path.open('r', encoding='utf-8', errors='ignore') as handle:
                for line_number, line in enumerate(handle, start=1):
                    haystack = line if case_sensitive else line.lower()
                    if needle in haystack:
                        matches.append({
                            'path': str(file_path),
                            'line': line_number,
                            'content': line.rstrip()
                        })
                        if len(matches) >= max_results:
                            return {
                                'path': str(target.resolve()),
                                'query': query,
                                'file_pattern': file_pattern,
                                'recursive': recursive,
                                'case_sensitive': case_sensitive,
                                'scanned_files': scanned_files,
                                'count': len(matches),
                                'truncated': True,
                                'matches': matches
                            }
        except OSError:
            continue

    return {
        'path': str(target.resolve()),
        'query': query,
        'file_pattern': file_pattern,
        'recursive': recursive,
        'case_sensitive': case_sensitive,
        'scanned_files': scanned_files,
        'count': len(matches),
        'truncated': False,
        'matches': matches
    }
