from datetime import datetime
import platform

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