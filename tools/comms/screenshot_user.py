"""Capture a screenshot and deliver it to the current user."""

from __future__ import annotations

from tools.comms.delivery import capture_screenshot_to_disk, deliver_image_to_current_user


def get_screenshot_user_tool():
    return {
        "name": "screenshot_user",
        "description": (
            "Take a screenshot of the current screen and deliver it to the current user "
            "in their active interface. In the web UI it appears inline in the chat. In "
            "Telegram it is sent as a Telegram photo. Prefer this over screenshot_telegram "
            "when replying to the current user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "caption": {
                    "type": "string",
                    "description": "Optional caption to include with the screenshot.",
                },
            },
            "required": [],
        },
        "execute": _screenshot_user,
    }


def _screenshot_user(inputs):
    caption = inputs.get("caption", "")
    path = capture_screenshot_to_disk()
    result = deliver_image_to_current_user(path, caption=caption)
    if result.get("sent"):
        result["captured_path"] = str(path)
    return result
