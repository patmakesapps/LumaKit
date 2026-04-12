import pyperclip


def get_copy_to_clipboard_tool():
    return {
        'name': 'copy_to_clipboard',
        'description': (
            'Copy content to the system clipboard. '
            'Only works when running on a desktop with a display (not headless/server mode).'
        ),
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
    try:
        pyperclip.copy(content)
    except Exception as e:
        return {
            "success": False,
            "error": f"Clipboard unavailable (no display or clipboard tool installed): {e}",
        }
    preview = content[:200] + "..." if len(content) > 200 else content
    return {
        "success": True,
        "characters_copied": len(content),
        "preview": preview,
    }
