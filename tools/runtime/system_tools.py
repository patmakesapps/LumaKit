from datetime import datetime
import platform
import subprocess

def get_current_datetime_tool():
    return {
        "name": "get_current_datetime",
        "description": "Returns the current date and time in the system's timezone",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "enum": ["full", "date_only", "time_only", "iso"],
                    "description": "Format for the output (default: full)"
                }
            },
            "required": []
        },
        "execute": _get_current_datetime
    }


def _get_current_datetime(inputs):
    format_type = inputs.get("format", "full")
    now = datetime.now()

    formats = {
        "full": now.strftime("%A, %B %d, %Y at %I:%M:%S %p"),
        "date_only": now.strftime("%Y-%m-%d"),
        "time_only": now.strftime("%H:%M:%S"),
        "iso": now.isoformat()

    }

    return {
        "datetime": formats[format_type],
        "timezone": _get_timezone(),
        "format": format_type,
        "unix_timestamp": int(now.timestamp())
    }


def _get_timezone():
    """Get timezone name in a cross-platform way"""
    import time
    if platform.system() == "Windows":
        return time.tzname[time.daylight]
    else:
        # Unix/Linux/Mac
        return time.strftime("%Z")


def get_reboot_system_tool():
    return {
        "name": "reboot_system",
        "description": "Restarts the Lumi process. Use when something is broken and a fresh start would help (e.g. after config changes, memory issues, or unrecoverable errors). Lumi will restart immediately — send a farewell message before calling this.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why a restart is needed"
                }
            },
            "required": ["reason"]
        },
        "execute": _reboot_system
    }


def _reboot_system(inputs):
    import os
    import sys
    import threading

    reason = inputs["reason"]

    def _restart():
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # Short delay so the tool result can be returned before the process is replaced
    t = threading.Timer(2.0, _restart)
    t.daemon = True
    t.start()

    return {
        "success": True,
        "message": f"Restarting in 2 seconds. Reason: {reason}"
    }