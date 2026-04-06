from pathlib import Path


def get_write_file_tool():
    return {
        'name': 'write_file',
        'description': 'Writes content to a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string'}
            },
            'required': ['path', 'content']
        },
        'execute': _write_file
    }


def _write_file(inputs):
    path = Path(inputs['path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inputs['content'], encoding='utf-8')
    return f"Wrote to {path}"
