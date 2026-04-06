from core.paths import get_display_path, resolve_repo_path


def get_read_file_tool():
    return {
        'name': 'read_file',
        'description': 'Read file contents.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'}
            },
            'required': ['path']
        },
        'execute': _read_file
    }


def _read_file(inputs):
    path = resolve_repo_path(inputs['path'], kind='file')
    return {
        'path': get_display_path(path),
        'content': path.read_text(encoding='utf-8', errors='replace')
    }
