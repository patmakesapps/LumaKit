"""Tool-result compaction for chat/task history (D-1/D-3).

Keeps tool outputs useful while preventing a single tool call from bloating
the stored history enough to stall later model requests. Both the interactive
agent and the task runner trim through here — one set of rules, parameterized
by budget.
"""

from __future__ import annotations

import json

TOOL_HISTORY_MAX_CHARS = 4000
TOOL_HISTORY_STRING_LIMIT = 2000
TOOL_HISTORY_READ_LIMIT = 2500
TOOL_HISTORY_STDIO_LIMIT = 2500
TOOL_HISTORY_LIST_LIMIT = 40
TOOL_HISTORY_DICT_LIMIT = 60
TOOL_HISTORY_BROWSER_LIST_LIMIT = 25
TOOL_HISTORY_BROWSER_ACTION_LIMIT = 12
TOOL_HISTORY_BROWSER_TEXT_LIMIT = 2000


# Share of the budget given to the END of a value when we keep both ends.
# Command output puts the important part last (pytest's FAILED lines and
# summary, compiler error totals, stack traces), so stdio is tail-heavy.
# File reads are what the caller asked to see from the top, so content is
# head-heavy with just enough tail to show where the file ends.
STDIO_TAIL_SHARE = 0.6
CONTENT_TAIL_SHARE = 0.2

# Keys whose strings keep both ends when trimmed, mapped to their tail share.
_KEEP_ENDS_KEYS = {
    "stdout": STDIO_TAIL_SHARE,
    "stderr": STDIO_TAIL_SHARE,
    "output": STDIO_TAIL_SHARE,
    "content": CONTENT_TAIL_SHARE,
}


def _truncate_text(value, limit):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"... [truncated {omitted} chars]"


def _truncate_keep_ends(value, limit, tail_share=STDIO_TAIL_SHARE):
    """Trim a long string to about *limit* chars keeping its head AND tail.

    Head-only truncation silently drops the part of command output the model
    most needs (the failure summary at the end), which lets it guess that a
    test run passed. Keeping both ends makes the cut visible and keeps the
    verdict.
    """
    if not isinstance(value, str) or len(value) <= limit:
        return value
    keep_tail = int(limit * tail_share)
    keep_head = max(0, limit - keep_tail)
    omitted = len(value) - keep_head - keep_tail
    marker = f"\n... [truncated {omitted} chars] ...\n"
    return value[:keep_head] + marker + (value[-keep_tail:] if keep_tail else "")


def _compact_browser_history(data):
    if not isinstance(data, dict):
        return data

    def _trim_browser_elements(elements):
        limited_elements = []
        for element in elements[:TOOL_HISTORY_BROWSER_LIST_LIMIT]:
            if isinstance(element, dict):
                trimmed = {}
                for key in (
                    "tag",
                    "type",
                    "id",
                    "name",
                    "placeholder",
                    "aria_label",
                    "data_testid",
                    "text",
                    "required",
                    "suggested_selector",
                    "css_path",
                    "href",
                    "role",
                    "x",
                    "y",
                    "width",
                    "height",
                    "needs_coordinate_click",
                    "error",
                ):
                    if key not in element:
                        continue
                    value = element[key]
                    if isinstance(value, str):
                        value = _truncate_text(value, 200)
                    trimmed[key] = value
                limited_elements.append(trimmed)
            else:
                limited_elements.append(_truncate_text(str(element), 300))
        return limited_elements

    def _trim_browser_snapshot(snapshot):
        if not isinstance(snapshot, dict):
            return snapshot
        trimmed = {}
        for key in ("url", "title"):
            if isinstance(snapshot.get(key), str):
                trimmed[key] = _truncate_text(snapshot[key], 300)
        if isinstance(snapshot.get("page_text_snippet"), str):
            trimmed["page_text_snippet"] = _truncate_text(
                snapshot["page_text_snippet"], TOOL_HISTORY_BROWSER_TEXT_LIMIT
            )
        interactive_elements = snapshot.get("interactive_elements")
        if isinstance(interactive_elements, list):
            trimmed["interactive_elements"] = _trim_browser_elements(interactive_elements)
            if len(interactive_elements) > TOOL_HISTORY_BROWSER_LIST_LIMIT:
                trimmed["interactive_elements_truncated"] = (
                    len(interactive_elements) - TOOL_HISTORY_BROWSER_LIST_LIMIT
                )
        forms = snapshot.get("forms")
        if isinstance(forms, list):
            trimmed["forms"] = _trim_browser_elements(forms)
            if len(forms) > TOOL_HISTORY_BROWSER_LIST_LIMIT:
                trimmed["forms_truncated"] = len(forms) - TOOL_HISTORY_BROWSER_LIST_LIMIT
        return trimmed

    compact = dict(data)
    actions = compact.get("actions_performed")
    if isinstance(actions, list):
        limited_actions = []
        for action in actions[:TOOL_HISTORY_BROWSER_ACTION_LIMIT]:
            if not isinstance(action, dict):
                limited_actions.append(action)
                continue

            entry = dict(action)
            if isinstance(entry.get("text"), str):
                entry["text"] = _truncate_text(
                    entry["text"], TOOL_HISTORY_BROWSER_TEXT_LIMIT
                )

            links = entry.get("links")
            if isinstance(links, list):
                limited_links = []
                for link in links[:TOOL_HISTORY_BROWSER_LIST_LIMIT]:
                    if isinstance(link, dict):
                        limited_links.append(
                            {
                                "text": _truncate_text(str(link.get("text", "")), 200),
                                "href": _truncate_text(str(link.get("href", "")), 300),
                            }
                        )
                    else:
                        limited_links.append(_truncate_text(str(link), 300))
                entry["links"] = limited_links
                if len(links) > len(limited_links):
                    entry["links_truncated"] = len(links) - len(limited_links)

            elements = entry.get("elements")
            if isinstance(elements, list):
                entry["elements"] = _trim_browser_elements(elements)
                if len(elements) > len(entry["elements"]):
                    entry["elements_truncated"] = len(elements) - len(entry["elements"])

            landmarks = entry.get("landmarks")
            if isinstance(landmarks, list):
                entry["landmarks"] = landmarks[:TOOL_HISTORY_BROWSER_LIST_LIMIT]

            if isinstance(entry.get("recovery_hint"), str):
                entry["recovery_hint"] = _truncate_text(entry["recovery_hint"], 300)

            if isinstance(entry.get("recovery_snapshot"), dict):
                entry["recovery_snapshot"] = _trim_browser_snapshot(entry["recovery_snapshot"])

            limited_actions.append(entry)

        compact["actions_performed"] = limited_actions
        if len(actions) > len(limited_actions):
            compact["actions_truncated"] = len(actions) - len(limited_actions)

    if isinstance(compact.get("page_observation"), dict):
        compact["page_observation"] = _trim_browser_snapshot(compact["page_observation"])

    for key in ("page_text_snippet", "error"):
        if isinstance(compact.get(key), str):
            compact[key] = _truncate_text(
                compact[key], TOOL_HISTORY_BROWSER_TEXT_LIMIT
            )
    for key in ("url", "final_url", "page_title", "final_title", "screenshot_path"):
        if isinstance(compact.get(key), str):
            compact[key] = _truncate_text(compact[key], 300)

    return compact


def _compact_value_for_history(value, path=()):
    key = path[-1] if path else ""

    if isinstance(value, str):
        limit = TOOL_HISTORY_STRING_LIMIT
        if key == "content":
            limit = TOOL_HISTORY_READ_LIMIT
        elif key in {"stdout", "stderr"}:
            limit = TOOL_HISTORY_STDIO_LIMIT
        if key in _KEEP_ENDS_KEYS:
            return _truncate_keep_ends(value, limit, _KEEP_ENDS_KEYS[key])
        if key in {"text", "page_text_snippet", "error"}:
            limit = TOOL_HISTORY_BROWSER_TEXT_LIMIT
        elif key in {"href", "url", "final_url", "selector", "suggested_selector"}:
            limit = 300
        return _truncate_text(value, limit)

    if isinstance(value, list):
        limit = TOOL_HISTORY_LIST_LIMIT
        if key in {"links", "elements"}:
            limit = TOOL_HISTORY_BROWSER_LIST_LIMIT
        elif key == "actions_performed":
            limit = TOOL_HISTORY_BROWSER_ACTION_LIMIT
        items = [
            _compact_value_for_history(item, path + (str(i),))
            for i, item in enumerate(value[:limit])
        ]
        if len(value) > limit:
            items.append({"_truncated_items": len(value) - limit})
        return items

    if isinstance(value, dict):
        items = list(value.items())
        compact = {}
        for key_name, item in items[:TOOL_HISTORY_DICT_LIMIT]:
            compact[str(key_name)] = _compact_value_for_history(
                item, path + (str(key_name),)
            )
        if len(items) > TOOL_HISTORY_DICT_LIMIT:
            compact["_truncated_keys"] = len(items) - TOOL_HISTORY_DICT_LIMIT
        return compact

    return value


def _summarize_large_tool_data(data, preview_limit: int = 2000):
    if not isinstance(data, dict):
        return _compact_value_for_history(data, ("data",))

    summary = {}
    for key in (
        "path",
        "count",
        "status",
        "created",
        "deleted",
        "bytes_written",
        "replacements",
        # run_command / background commands: the model must still see what
        # ran and whether it succeeded even when the output is huge.
        "command",
        "cwd",
        "returncode",
        "exit_code",
        "error_type",
        "stdout_truncated",
        "stderr_truncated",
        "stdout_path",
        "stderr_path",
        "page_title",
        "final_title",
        "url",
        "final_url",
        "screenshot_path",
        "error",
        "site",
        "failed_action_count",
        "completed_with_failures",
        "blocked_reason",
        "blocked_on_step",
        "skipped_remaining_actions",
    ):
        if key in data:
            summary[key] = _compact_value_for_history(data[key], ("data", key))

    if isinstance(data.get("content"), str):
        summary["content_preview"] = _truncate_keep_ends(
            data["content"], preview_limit, CONTENT_TAIL_SHARE
        )
    for key in ("stdout", "stderr", "output"):
        if isinstance(data.get(key), str) and data[key]:
            summary[f"{key}_preview"] = _truncate_keep_ends(
                data[key], preview_limit, STDIO_TAIL_SHARE
            )
    if isinstance(data.get("page_text_snippet"), str):
        summary["page_text_snippet"] = _truncate_text(
            data["page_text_snippet"], TOOL_HISTORY_BROWSER_TEXT_LIMIT
        )

    actions = data.get("actions_performed")
    if isinstance(actions, list):
        summary["actions_performed"] = _compact_browser_history(
            {"actions_performed": actions}
        )["actions_performed"]

    if isinstance(data.get("page_observation"), dict):
        summary["page_observation"] = _compact_browser_history(
            {"page_observation": data["page_observation"]}
        )["page_observation"]

    if not summary:
        summary["available_keys"] = list(data.keys())[:20]

    return summary


def compact_tool_result_for_history(tool_name, tool_result, max_chars: int = TOOL_HISTORY_MAX_CHARS):
    """Serialize a tool result with size guards so chats stay responsive.

    *max_chars* parameterizes the overall budget so both the interactive agent
    (default) and the task runner (tighter budget) use identical trimming
    rules (D-3).
    """
    payload = tool_result
    if isinstance(payload, dict):
        payload = json.loads(json.dumps(payload, default=str))
        if tool_name == "browser_automation" and isinstance(payload.get("data"), dict):
            payload["data"] = _compact_browser_history(payload["data"])
        payload = _compact_value_for_history(payload)

    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    def _build_fallback(preview_limit: int) -> dict:
        fallback = {
            "success": bool(tool_result.get("success")) if isinstance(tool_result, dict) else True,
            "tool": tool_name,
            "truncated": True,
            "note": (
                "Tool output was trimmed before being stored in chat history to keep "
                "later model calls responsive."
            ),
        }
        if isinstance(tool_result, dict):
            if "error" in tool_result:
                fallback["error"] = _truncate_text(
                    str(tool_result["error"]), min(1000, preview_limit)
                )
            if "data" in tool_result:
                fallback["data"] = _summarize_large_tool_data(
                    tool_result["data"], preview_limit
                )
        else:
            fallback["data"] = _truncate_keep_ends(str(tool_result), max(preview_limit, 200) * 2)
        return fallback

    # Shrink the previews until the summary fits, rather than chopping the
    # JSON string mid-way — a chopped result is unparseable and reads to the
    # model as a tool crash.
    preview_limit = 2000
    while True:
        serialized = json.dumps(_build_fallback(preview_limit), ensure_ascii=False)
        if len(serialized) <= max_chars or preview_limit <= 200:
            break
        preview_limit //= 2

    if len(serialized) > max_chars:
        # Last resort: a minimal, still-valid record with whatever error text fits.
        minimal = {
            "success": bool(tool_result.get("success")) if isinstance(tool_result, dict) else True,
            "tool": tool_name,
            "truncated": True,
            "note": "Tool output was too large to keep; only the outcome was stored.",
        }
        if isinstance(tool_result, dict) and "error" in tool_result:
            room = max_chars - len(json.dumps(minimal, ensure_ascii=False)) - 40
            if room > 20:
                minimal["error"] = _truncate_text(str(tool_result["error"]), room)
        serialized = json.dumps(minimal, ensure_ascii=False)
    return serialized


def compact_tool_message_content(tool_name, content, max_chars: int = TOOL_HISTORY_MAX_CHARS):
    """Re-compact an existing tool-history message, including old saved chats."""
    if not isinstance(content, str):
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, max_chars)
    return compact_tool_result_for_history(tool_name, parsed, max_chars=max_chars)
