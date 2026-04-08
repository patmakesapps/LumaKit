import pyperclip


def get_read_from_clipboard_tool():
    return {
        'name': 'read_from_clipboard',
        'description': 'Read current clipboard contents',
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'required': []
        },
        'execute': _read_from_clipboard
    }


def _read_from_clipboard(inputs):
    content = pyperclip.paste()

    preview = content[:200] + "..." if len(content) > 200 else content

    return {
        "characters_read": len(content),
        "content": content,
        "preview": preview
    }
