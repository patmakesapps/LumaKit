import pyperclip


def get_read_from_clipboard_tool():
    return {
        'name': 'read_from_clipboard',
        'description': (
            'Read current clipboard contents. '
            'Only works when running on a desktop with a display (not headless/server mode).'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'required': []
        },
        'execute': _read_from_clipboard
    }


def _read_from_clipboard(inputs):
    try:
        content = pyperclip.paste()
    except Exception as e:
        return {
            "success": False,
            "error": f"Clipboard unavailable (no display or clipboard tool installed): {e}",
        }
    preview = content[:200] + "..." if len(content) > 200 else content
    return {
        "success": True,
        "characters_read": len(content),
        "content": content,
        "preview": preview,
    }
