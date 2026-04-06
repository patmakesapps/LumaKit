from core.paths import get_display_path, resolve_repo_path


def get_read_file_tool():
    return {
        'name': 'read_file',
        'description': 'Reads a file from the repo. Path resolution is forgiving and can match a unique hidden-dot filename such as .env.example from env.example.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Repo-relative or absolute file path. Unique near-matches may be resolved automatically.'
                }
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
