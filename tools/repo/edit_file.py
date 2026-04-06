from pathlib import Path


def get_edit_file_tool():
    return {
        'name': 'edit_file',
        'description': 'Finds and replaces text in a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'},
                'find': {'type': 'string'},
                'replace': {'type': 'string'}
            },
            'required': ['path', 'find', 'replace']
        },
        'execute': _edit_file
    }


def _edit_file(inputs):
    path = Path(inputs['path'])
    content = path.read_text(encoding='utf-8', errors='replace')
    occurrences = content.count(inputs['find'])
    updated_content = content.replace(inputs['find'], inputs['replace'])
    path.write_text(updated_content, encoding='utf-8')

    if occurrences == 0:
        return f"No changes made to {path}"

    return f"Edited {path} ({occurrences} replacement(s))"
