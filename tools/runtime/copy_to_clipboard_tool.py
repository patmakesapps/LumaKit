import pyperclip


def get_copy_to_clipboard_tool():
    return {
        'name': 'copy_to_clipboard',
        'description': 'Copy content to system clipboard',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'content': {'type': 'string', 'description': 'The text to copy to clipboard'}
            },
            'required': ['content']
        },
        'execute': _copy_to_clipboard
    }


def _copy_to_clipboard(inputs):
    content = inputs['content']
    pyperclip.copy(content)

    preview = content[:200] + "..." if len(content) > 200 else content

    return {
        "characters_copied": len(content),
        "preview": preview
    }
