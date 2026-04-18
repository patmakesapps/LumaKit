"""Deliver an image to the current user in their active interface."""

from __future__ import annotations

from tools.comms.delivery import deliver_image_to_current_user, resolve_image_path


def get_send_photo_user_tool():
    return {
        "name": "send_photo_user",
        "description": (
            "Send an existing image file to the current user in the interface they are "
            "using right now. In the web UI it appears inline in the chat. In Telegram "
            "it is sent as a Telegram photo. Prefer this over send_photo_telegram when "
            "the user asks to see an image or screenshot in the current conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the image file to send.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional caption to include with the image.",
                },
            },
            "required": ["path"],
        },
        "execute": _send_photo_user,
    }


def _send_photo_user(inputs):
    raw_path = inputs.get("path", "")
    if not raw_path:
        return {"error": "path is required"}

    path = resolve_image_path(raw_path)
    caption = inputs.get("caption", "")
    return deliver_image_to_current_user(path, caption=caption)

