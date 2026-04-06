from pathlib import Path


def get_read_file_tool():
    return {
        'name': 'read_file',
        'description': 'Reads the content of a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'}
            },
            'required': ['path']
        },
        'execute': _read_file
    }


def _read_file(inputs):
    path = Path(inputs['path'])
    return path.read_text(encoding='utf-8', errors='replace')
