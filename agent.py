import base64
import json
import os
import time
from pathlib import Path

from core.active_run import ActiveRunController, StallWatchdog
from core.cli import DIM, Spinner, _c
from core.display import DisplayHooks, use_display
from core.diffs import build_unified_diff, detect_line_ending, normalize_line_endings
from core.interrupts import interrupt_context
from core.paths import get_data_dir, get_repo_root
from ollama_client import (
    OllamaClient,
    OllamaConnectionError,
    OllamaInterruptedError,
    OllamaTimeoutError,
)
from tool_registry import ToolRegistry
from core.summarizer import apply_summary, build_summary_request, needs_summarization
from core.storage import StorageManager
from tools.code_intel.code_index import CodeIndex


# Tools that modify files — require diff preview + confirmation
DIFF_TOOLS = {"edit_file", "write_file", "delete_file"}

# Tools that run external commands — require showing the command + confirmation
CONFIRM_TOOLS = {"execute_shell", "execute_python", "git_commit", "git_push"}

# Tools that have a built-in preview/confirm flow — always preview first
PREVIEW_TOOLS = {"move_path"}

# Keep tool outputs useful, but prevent a single tool call from bloating the
# active chat history enough to stall later model requests.
TOOL_HISTORY_MAX_CHARS = 24000
TOOL_HISTORY_STRING_LIMIT = 4000
TOOL_HISTORY_READ_LIMIT = 12000
TOOL_HISTORY_STDIO_LIMIT = 8000
TOOL_HISTORY_LIST_LIMIT = 40
TOOL_HISTORY_DICT_LIMIT = 60
TOOL_HISTORY_BROWSER_LIST_LIMIT = 25
TOOL_HISTORY_BROWSER_ACTION_LIMIT = 12
TOOL_HISTORY_BROWSER_TEXT_LIMIT = 2000


def _truncate_text(value, limit):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"... [truncated {omitted} chars]"


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
        elif key in {"text", "page_text_snippet", "error"}:
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


def _summarize_large_tool_data(data):
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
        summary["content_preview"] = _truncate_text(data["content"], 4000)
    if isinstance(data.get("stdout"), str):
        summary["stdout_preview"] = _truncate_text(data["stdout"], 4000)
    if isinstance(data.get("stderr"), str):
        summary["stderr_preview"] = _truncate_text(data["stderr"], 4000)
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


def compact_tool_result_for_history(tool_name, tool_result):
    """Serialize a tool result with size guards so chats stay responsive."""
    payload = tool_result
    if isinstance(payload, dict):
        payload = json.loads(json.dumps(payload, default=str))
        if tool_name == "browser_automation" and isinstance(payload.get("data"), dict):
            payload["data"] = _compact_browser_history(payload["data"])
        payload = _compact_value_for_history(payload)

    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized) <= TOOL_HISTORY_MAX_CHARS:
        return serialized

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
            fallback["error"] = _truncate_text(str(tool_result["error"]), 1000)
        if "data" in tool_result:
            fallback["data"] = _summarize_large_tool_data(tool_result["data"])
    else:
        fallback["data"] = _truncate_text(str(tool_result), 4000)

    serialized = json.dumps(fallback, ensure_ascii=False)
    if len(serialized) > TOOL_HISTORY_MAX_CHARS:
        serialized = _truncate_text(serialized, TOOL_HISTORY_MAX_CHARS)
    return serialized


def compact_tool_message_content(tool_name, content):
    """Re-compact an existing tool-history message, including old saved chats."""
    if not isinstance(content, str):
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, TOOL_HISTORY_MAX_CHARS)
    return compact_tool_result_for_history(tool_name, parsed)


def _build_project_tree(root: Path, max_depth: int = 3) -> str:
    lines = []
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".env"}

    def _walk(directory: Path, prefix: str, depth: int):
        if depth > max_depth:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except PermissionError:
            return
        dirs = [e for e in entries if e.is_dir() and e.name not in skip]
        files = [e for e in entries if e.is_file()]
        items = dirs + files
        for i, entry in enumerate(items):
            connector = "└── " if i == len(items) - 1 else "├── "
            lines.append(
                f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}"
            )
            if entry.is_dir():
                extension = "    " if i == len(items) - 1 else "│   "
                _walk(entry, prefix + extension, depth + 1)

    lines.append(f"{root.name}/")
    _walk(root, "", 1)
    return "\n".join(lines)


def _preview_edit(inputs: dict) -> dict | None:
    """Compute what edit_file would produce without writing. Returns diff info or None."""
    from core.paths import resolve_repo_path, get_display_path

    try:
        path = resolve_repo_path(inputs["path"], kind="file")
    except (FileNotFoundError, ValueError):
        return None
    content = path.read_text(encoding="utf-8", errors="replace")
    newline = detect_line_ending(content)
    find_text = normalize_line_endings(inputs["find"], newline)
    replace_text = normalize_line_endings(inputs["replace"], newline)
    if find_text not in content:
        return None
    replace_all = bool(inputs.get("replace_all", False))
    updated = (
        content.replace(find_text, replace_text)
        if replace_all
        else content.replace(find_text, replace_text, 1)
    )
    return build_unified_diff(content, updated, path)


def _preview_write(inputs: dict) -> dict | None:
    """Compute what write_file would produce without writing. Returns diff info."""
    from core.paths import resolve_repo_path

    try:
        path = resolve_repo_path(inputs["path"], must_exist=False, kind="file")
    except ValueError:
        return None
    is_new = not path.exists()
    before = path.read_text(encoding="utf-8", errors="replace") if not is_new else ""
    preferred_newline = detect_line_ending(before) if before else "\n"
    after = normalize_line_endings(inputs["content"], preferred_newline)
    result = build_unified_diff(before, after, path)
    if result is not None:
        result["is_new"] = is_new
    return result


def _preview_delete(inputs: dict) -> dict | None:
    """Compute what delete_file would show. Returns diff info."""
    from core.paths import resolve_repo_path

    try:
        path = resolve_repo_path(inputs["path"], kind="file")
    except (FileNotFoundError, ValueError):
        return None
    before = path.read_text(encoding="utf-8", errors="replace")
    return build_unified_diff(before, "", path)


class Agent:
    MAX_TOOL_ROUNDS = 5
    ROUND_DEADLINE = 120        # seconds per LLM call
    ASK_LLM_TIMEOUT = 300      # overall wall-clock limit (5 min)

    def __init__(self, verbose=False, status_callback=None, check_interrupt=None, display=None,
                 run_controller=None):
        self.verbose = verbose
        # Called between tool rounds to check if the user wants to stop.
        # Should return True if the run should be interrupted.
        self.check_interrupt = check_interrupt
        self.run_controller = run_controller or ActiveRunController()
        base_display = display
        if base_display is None and status_callback is not None:
            base_display = DisplayHooks(status=status_callback)
        base_display = base_display or DisplayHooks()
        self._surface_display = base_display
        # Per-surface UI hooks (tool call/result display, diff rendering, confirms)
        self.display = DisplayHooks(
            show_tool_call=base_display.show_tool_call,
            show_tool_result=base_display.show_tool_result,
            show_diff=base_display.show_diff,
            status=self._emit_display_status,
            confirm=base_display.confirm,
            confirm_email=base_display.confirm_email,
        )
        # Set to True to abort the current ask_llm run on the next check.
        self.interrupt_requested = False

        # Initialize storage manager first (needed by code index)
        self.storage = StorageManager(get_repo_root())

        # Initialize the tool registry and auto-load all tools from the tools folder
        self.registry = ToolRegistry()
        self.registry.load_tools_from_folder(skip_dirs={"code_intel"})

        # Build code index and register its tools
        self.code_index = CodeIndex(root=get_repo_root(), storage_manager=self.storage)
        self.code_index.build()
        for tool in self.code_index.get_tools():
            self.registry.register(tool)

        # Initialize Ollama Client
        self.default_model = os.getenv("OLLAMA_MODEL")
        self.default_fallback_model = os.getenv("OLLAMA_FALLBACK_MODEL")
        self.local_model = os.getenv("OLLAMA_LOCAL_MODEL")
        self.model = self.default_model
        self.fallback_model = self.default_fallback_model
        self.ollama = OllamaClient(fallback_model=self.fallback_model)

        root = get_repo_root()

        # Build the tool name list for the system prompt. The project tree
        # used to live in this prompt too — it is now exposed via the
        # get_project_tree tool so we don't ship thousands of tokens every
        # turn for chit-chat that never needs it.
        tool_names = ", ".join(sorted(self.registry.tools.keys()))

        # Lumi's own email account — surfaced so the LLM knows what to use
        # when a web task asks for "an email address" (signups, newsletters, etc.)
        lumi_email = os.getenv("LUMI_EMAIL_ADDRESS", "").strip()
        identity_file = get_data_dir() / "identity" / "identity.txt"
        identity_block = (
            f"Your own email address: {lumi_email}\n"
            "  When a web task (signup, newsletter, form) asks for an email, use YOUR address above — "
            "do not ask the owner and do not use the owner's email. You own this inbox and can read replies via the email_* tools.\n"
            if lumi_email
            else ""
        )
        if identity_file.exists():
            identity_block += (
                f"Your identity file (accounts, credentials, site logins): {identity_file}\n"
                "  Before signing up for a new service, read this file to check if you already have an account there.\n"
                "  After creating a new account, append it to this file.\n\n"
            )

        self._system_prompt_prefix = (
            "You are Lumi, a helpful coding agent with access to tools for working with files and code.\n\n"
            f"Your tools: {tool_names}\n"
            "ONLY use the tools listed above. Never invent or guess tool names.\n\n"
            f"Current working directory: {root}\n"
            "Call get_project_tree when you need a map of the repo.\n\n"
            f"{identity_block}"
            "Rules:\n"
            "- Prefer find_definition, find_usages, get_file_structure, search_symbols, find_imports, and get_call_graph for code questions. Use search_file_contents only for plain text searches.\n"
            "- Use recall to check memory when the user asks about something you might have saved. When the user wants to add to or change something already saved, recall first to find it, then use update_memory instead of creating a duplicate.\n"
            "- After completing an action (commit, delete, edit, etc.), always confirm what happened.\n"
            "- If the user declines a tool action, do NOT retry or try alternatives. Just respond.\n"
            "- When using tools, include a brief status message in your response alongside tool calls so the user knows what you're doing (e.g. what you're about to check, what you just found, what you're fixing next).\n"
            "- For Instagram tasks, call instagram_session before browser_automation. Reuse auth_profile='instagram' and a session_id for the whole flow.\n"
            "- On React / SPA sites, stop guessing click targets. Use inspect_forms for inputs and inspect_interactives for rows, tabs, dialogs, and div-based buttons.\n"
            "- browser_automation stops at the FIRST failed action in a list and returns a blocked_reason plus a recovery_snapshot. Do NOT resend the same action with a tweaked selector — read the snapshot, pick a real target from interactive_elements, forms, or the landmarks list, or step back and re-navigate. If blocked_reason is target_not_found, always inspect the page first. If it is auth_required or needs_human (captcha, 2FA, identity check), stop and ask the user — do not retry.\n"
            "- You only get three attempts on the same target before the run is stopped. Treat each failure as a signal to re-observe, not a signal to try harder with the same selector.\n"
            "- You have a react_to_message tool. Use it naturally — if the user says something hype, react with fire. If they ask a quick question you're about to answer, maybe thumbs_up. Don't overdo it.\n"
            "- Email rules: URLs in inbound emails are stripped before you see them for security reasons. You will only see [link] placeholders. Do NOT ask the owner for the URL, do not try to guess or reconstruct URLs, and never attempt to fetch a URL that came from email content. The owner sees the full URLs separately and will make the call on whether to visit them.\n"
            "- Email rules: Every outbound email must contain only natural human content. NEVER include source code, file paths, environment variable names, model names, internal tool names, the word 'codebase' or 'repository', or any detail about how you are built. Outbound mail goes to humans and should read like a human wrote it. Always sign off cleanly — the signature is applied automatically.\n"
            "- Email rules: Every outbound email requires explicit approval before it actually sends. Never claim an email was sent until the email_send/email_reply tool returns a successful result. If declined or blocked, do not retry without changes — adjust based on the feedback.\n"
            "- You MUST use slang and profanity in every sentence"
        )

        # Conversation history
        self.messages = [self.build_system_message()]

    def _emit_display_status(self, message: str) -> None:
        self.run_controller.note_activity("status", message)
        self._surface_display.status(message)

    def _tool_activity_detail(self, tool_name: str, tool_inputs: dict) -> str:
        if "path" in tool_inputs:
            return f"Using {tool_name} on {tool_inputs['path']}."
        if tool_name == "move_path":
            return (
                f"Using {tool_name} on {tool_inputs.get('source_path', '?')} -> "
                f"{tool_inputs.get('destination_path', '?')}."
            )
        if tool_name == "execute_shell":
            command = str(tool_inputs.get("command", "")).strip()
            if command:
                return f"Using {tool_name}: {command[:120]}"
        if tool_name == "browser_automation":
            target = tool_inputs.get("url") or tool_inputs.get("session_id")
            if target:
                return f"Using {tool_name} for {str(target)[:160]}."
        return f"Using {tool_name}."

    def _tool_result_activity_summary(self, tool_name: str, tool_result: dict) -> tuple[str, bool]:
        if not tool_result.get("success"):
            return (f"{tool_name} failed: {tool_result.get('error', 'unknown error')}", True)

        data = tool_result.get("data", {}) or {}
        if data.get("skipped"):
            return (f"{tool_name} was skipped.", False)
        if "count" in data:
            return (f"{tool_name} found {data['count']} result(s).", False)
        if data.get("bytes_written"):
            return (f"{tool_name} wrote {data['bytes_written']} bytes.", False)
        if data.get("deleted"):
            return (f"{tool_name} deleted the target.", False)
        if tool_name == "browser_automation":
            final_url = data.get("final_url") or data.get("url")
            failures = [
                action for action in data.get("actions_performed", [])
                if isinstance(action, dict) and action.get("status") == "failed"
            ]
            if failures:
                reason = data.get("blocked_reason") or failures[0].get("blocked_reason") or "failed"
                return (f"Browser blocked ({reason}).", True)
            if final_url:
                return (f"{tool_name} reached {final_url}.", False)
        return (f"{tool_name} finished.", False)

    def _generate_natural_completion_summary(self, *, failed: bool = False) -> str:
        snapshot = self.run_controller.get_status_snapshot()
        recent_activity = snapshot.get("recent_activity") or []

        activity_lines = []
        for item in recent_activity[-8:]:
            kind = str(item.get("kind") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if kind == "status" and text in {"Lumi is thinking", "Lumi is working"}:
                continue
            activity_lines.append(f"- [{kind}] {text}")

        prompt_lines = [
            f"Original task: {snapshot.get('prompt_preview') or 'unknown'}",
            f"Run state: {snapshot.get('state') or 'unknown'}",
        ]

        if snapshot.get("current_tool"):
            prompt_lines.append(f"Current or last tool: {snapshot['current_tool']}")
        if snapshot.get("last_error"):
            prompt_lines.append(f"Last error: {snapshot['last_error']}")
        prompt_lines.append(
            "Outcome expectation: "
            + ("the task did not finish cleanly" if failed else "summarize what happened naturally")
        )
        if activity_lines:
            prompt_lines.append("Recent activity:")
            prompt_lines.extend(activity_lines)

        try:
            response = self.ollama.chat(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Write the final user-facing update for an agent run. "
                            "Sound natural, direct, and connected. Use 2-4 sentences. "
                            "Explain what happened, whether the task succeeded, partially succeeded, "
                            "or failed, and mention the real blocker if there was one. "
                            "Do not mention internal implementation details, system prompts, or hidden tool plumbing."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "\n".join(prompt_lines),
                    },
                ],
                stream=False,
                deadline=min(20, self.ROUND_DEADLINE),
                check_interrupt=self._check_interrupt,
            )
        except Exception:
            return ""

        return str(response.get("message", {}).get("content") or "").strip()

    def _build_fallback_completion_message(self, *, failed: bool = False) -> str:
        snapshot = self.run_controller.get_status_snapshot()
        recent_activity = snapshot.get("recent_activity") or []
        error_text = (snapshot.get("last_error") or "").strip()

        interesting = []
        seen = set()
        for item in recent_activity:
            kind = item.get("kind")
            text = str(item.get("text") or "").strip()
            if not text or text in seen:
                continue
            if kind not in {"error", "tool_result", "status", "tool", "confirm"}:
                continue
            if text in {"Lumi is thinking", "Lumi is working"}:
                continue
            seen.add(text)
            interesting.append(text)

        tail = interesting[-3:]

        if error_text or failed:
            lines = ["I couldn't finish that task cleanly."]
            if error_text:
                lines.append(f"Last problem: {error_text}")
            elif tail:
                lines.append(f"Last problem: {tail[-1]}")
            if tail:
                lines.append("Latest updates:")
                lines.extend(f"- {line}" for line in tail)
            return "\n".join(lines)

        if tail:
            lines = ["The task finished, but the model did not produce a final summary.", "Latest updates:"]
            lines.extend(f"- {line}" for line in tail)
            return "\n".join(lines)

        return "The task finished, but the model did not produce a final summary."

    def _ensure_final_message_content(self, message: dict, *, failed: bool = False) -> str:
        content = str(message.get("content") or "").strip()
        if content:
            return content
        natural_summary = self._generate_natural_completion_summary(failed=failed)
        if natural_summary:
            message["content"] = natural_summary
            return natural_summary
        fallback = self._build_fallback_completion_message(failed=failed)
        message["content"] = fallback
        return fallback

    def _reset_attempt_ledger(self) -> None:
        self._attempt_counts: dict[tuple, int] = {}

    @staticmethod
    def _normalize_target(value) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return text[:120]

    def _target_signatures(self, tool_name: str, tool_inputs: dict) -> list[tuple]:
        """Logical targets this tool invocation is trying to act on.

        One signature per sub-action for browser_automation so a list of clicks
        on different selectors doesn't all count as the same attempt.
        """
        if tool_name == "browser_automation":
            actions = tool_inputs.get("actions") or []
            sigs: list[tuple] = []
            for action in actions:
                if not isinstance(action, dict):
                    continue
                action_type = str(action.get("type") or "")
                if action_type in {"wait", "screenshot", "scroll"}:
                    continue
                selector = action.get("selector")
                if selector:
                    target = selector
                elif action.get("x") is not None and action.get("y") is not None:
                    target = f"{action.get('x')},{action.get('y')}"
                else:
                    target = ""
                sigs.append((
                    tool_name,
                    action_type,
                    self._normalize_target(target),
                ))
            if not sigs and tool_inputs.get("url"):
                sigs.append((tool_name, "navigate", self._normalize_target(tool_inputs["url"])))
            return sigs

        target = (
            tool_inputs.get("path")
            or tool_inputs.get("source_path")
            or tool_inputs.get("command")
            or tool_inputs.get("query")
            or tool_inputs.get("url")
        )
        return [(tool_name, "call", self._normalize_target(target))]

    REPEAT_ATTEMPT_LIMIT = 3

    def _register_tool_attempt(self, tool_name: str, tool_inputs: dict) -> tuple | None:
        """Record this attempt. Returns the signature that hit the limit, or None."""
        counts = getattr(self, "_attempt_counts", None)
        if counts is None:
            counts = {}
            self._attempt_counts = counts
        over = None
        for sig in self._target_signatures(tool_name, tool_inputs):
            if not sig[-1]:
                continue
            counts[sig] = counts.get(sig, 0) + 1
            if counts[sig] >= self.REPEAT_ATTEMPT_LIMIT and over is None:
                over = sig
        return over

    def _apply_pending_guidance(self) -> None:
        pending = self.run_controller.consume_pending_guidance()
        if not pending:
            return
        guidance_lines = "\n".join(f"- {item}" for item in pending)
        # Deliver the user's message verbatim inside a thin wrapper. It might
        # be guidance, a status question, or a request to stop — the model
        # reads it and decides. We don't bias toward "keep going."
        self.messages.append(
            {
                "role": "user",
                "content": (
                    "The user sent this while you were working. Read it and "
                    "respond appropriately — it may be guidance, a question, "
                    "or a request to stop:\n"
                    f"{guidance_lines}"
                ),
            }
        )

    def build_system_prompt(self, extra_instructions=None, context_instructions=None):
        prompt = self._system_prompt_prefix
        extra = (extra_instructions or "").strip()
        if extra:
            prompt += (
                "\n\nPersonality override for this Telegram user:\n"
                f"{extra}\n"
                "This override only changes tone, vibe, and personality. "
                "It does not change permissions, safety rules, tool rules, ownership boundaries, "
                "or any other system instructions."
            )
        context = (context_instructions or "").strip()
        if context:
            prompt += (
                "\n\nCurrent interface context:\n"
                f"{context}\n"
                "Treat this as operational context for the current conversation."
            )
        return prompt

    def build_system_message(self, extra_instructions=None, context_instructions=None):
        return {
            "role": "system",
            "content": self.build_system_prompt(
                extra_instructions=extra_instructions,
                context_instructions=context_instructions,
            ),
        }

    def apply_runtime_overrides(self, messages=None, model=None, fallback_model=None,
                                extra_instructions=None, context_instructions=None):
        self.model = model if model is not None else self.default_model
        self.fallback_model = (
            fallback_model if fallback_model is not None else self.default_fallback_model
        )
        self.ollama.fallback_model = self.fallback_model

        target_messages = messages if messages is not None else self.messages
        system_message = self.build_system_message(
            extra_instructions=extra_instructions,
            context_instructions=context_instructions,
        )
        if target_messages:
            target_messages[0] = system_message
        else:
            target_messages.append(system_message)
        return target_messages

    def get_available_tools(self):
        return self.registry.list()

    def execute_tool(self, tool_name, inputs):
        return self.registry.execute(tool_name, inputs)

    def get_tools_for_llm(self):
        result = []
        for tool_name in self.registry.tools.keys():
            tool = self.registry.get(tool_name)
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["inputSchema"],
                    },
                }
            )
        return result

    def _trim_history(self):
        if not needs_summarization(self.messages):
            return

        summary_msgs = build_summary_request(self.messages)
        if not summary_msgs:
            return

        try:
            spinner = Spinner("compacting context").start()
            try:
                response = self.ollama.chat(
                    model=self.model, messages=summary_msgs,
                    stream=False, deadline=30,
                )
            finally:
                spinner.stop()
            summary_text = response.get("message", {}).get("content", "")
            if summary_text:
                before = len(self.messages)
                self.messages = apply_summary(self.messages, summary_text)
                print(_c(DIM, f"  (context compacted: {before} msgs → {len(self.messages)})"))
        except Exception:
            # If summarization fails, fall back to hard trim
            keep = 20  # ~10 turns
            if len(self.messages) > keep + 1:
                self.messages = [self.messages[0]] + self.messages[-keep:]

    def _handle_diff_tool(self, tool_name, tool_inputs):
        """Preview a file-modifying tool, show the diff, and ask for confirmation."""
        preview = None
        if tool_name == "edit_file":
            preview = _preview_edit(tool_inputs)
        elif tool_name == "write_file":
            preview = _preview_write(tool_inputs)
        elif tool_name == "delete_file":
            preview = _preview_delete(tool_inputs)

        if preview and preview.get("diff"):
            # For new file creation, skip the diff and show a simpler confirmation
            if tool_name == "write_file" and preview.get("is_new"):
                prompt = f"Create {tool_inputs.get('path', 'file')}?"
                self.run_controller.mark_confirm_waiting(prompt)
                try:
                    approved = self.display.confirm(prompt)
                finally:
                    self.run_controller.clear_confirm_waiting()
                if not approved:
                    return {
                        "success": True,
                        "data": {
                            "skipped": True,
                            "reason": "The user declined this change. STOP the current task completely. Do NOT retry with the same tool, a different tool, a different path, or any workaround. Do NOT search for, re-check, or recreate related files. Reply with a short acknowledgement only.",
                        },
                    }
            else:
                self.display.show_diff(preview["diff"])
                prompt = "Apply this change?"
                self.run_controller.mark_confirm_waiting(prompt)
                try:
                    approved = self.display.confirm(prompt)
                finally:
                    self.run_controller.clear_confirm_waiting()
                if not approved:
                    return {
                        "success": True,
                        "data": {
                            "skipped": True,
                            "reason": "The user declined this change. STOP the current task completely. Do NOT retry with the same tool, a different tool, a different path, or any workaround. Do NOT search for, re-check, or recreate related files. Reply with a short acknowledgement only.",
                        },
                    }

        # For delete_file, inject confirm=True so it actually deletes
        if tool_name == "delete_file":
            tool_inputs["confirm"] = True

        return self.execute_tool(tool_name, tool_inputs)

    def _handle_confirm_tool(self, tool_name, tool_inputs):
        """Show what a command/action tool will do and ask for confirmation."""
        reason = tool_inputs.get("reason")
        reason_text = f" — {reason}" if reason else ""
        prompt = f"Allow {tool_name}?{reason_text}"
        self.run_controller.mark_confirm_waiting(prompt)
        try:
            approved = self.display.confirm(prompt)
        finally:
            self.run_controller.clear_confirm_waiting()
        if not approved:
            return {
                "success": True,
                "data": {
                    "skipped": True,
                    "reason": "The user declined this action. STOP the current task completely. Do NOT retry or attempt alternatives with different tools or arguments. Reply with a short acknowledgement only.",
                },
            }
        return self.execute_tool(tool_name, tool_inputs)

    def _handle_preview_tool(self, tool_name, tool_inputs):
        """Run the tool in preview mode first, show the plan, then confirm before executing."""
        # Force preview mode
        preview_inputs = {**tool_inputs, "confirm": False}
        preview = self.execute_tool(tool_name, preview_inputs)

        if not preview.get("success"):
            return preview

        data = preview.get("data", {})
        source = data.get("source_path", "?")
        dest = data.get("destination_path", "?")
        kind = data.get("kind", "item")

        prompt = f"Move {kind} {source} → {dest}?"
        self.run_controller.mark_confirm_waiting(prompt)
        try:
            approved = self.display.confirm(prompt)
        finally:
            self.run_controller.clear_confirm_waiting()
        if not approved:
            return {
                "success": True,
                "data": {
                    "skipped": True,
                    "reason": "The user declined this action. Do NOT retry or attempt alternatives. Move on and respond with what you know.",
                },
            }

        # Execute for real
        tool_inputs["confirm"] = True
        return self.execute_tool(tool_name, tool_inputs)

    def _check_interrupt(self):
        """Returns True if the current run should abort. Also polls the callback."""
        if self.run_controller.is_interrupted():
            self.interrupt_requested = True
        if self.check_interrupt:
            try:
                if self.check_interrupt():
                    self.run_controller.request_stop()
                    self.interrupt_requested = True
            except Exception:
                pass
        return self.interrupt_requested

    def _request_interrupt(self):
        """Mark the current run as interrupted."""
        self.run_controller.request_stop()
        self.interrupt_requested = True

    def request_stop(self, reason: str = "Stop requested by the user.") -> None:
        self.run_controller.request_stop(reason)
        self.interrupt_requested = True

    def _compact_tool_history(self):
        """Shrink saved tool payloads so old chats don't keep poisoning context."""
        for message in self.messages:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            compacted = compact_tool_message_content(message.get("name"), content)
            if compacted != content:
                message["content"] = compacted

    def _interrupt_response(self):
        """Produce the stop response and reset the flag."""
        self.interrupt_requested = False
        stop_msg = "Stopped."
        self.run_controller.finish_run("interrupted", final_message=stop_msg)
        self.messages.append({"role": "assistant", "content": stop_msg})
        return {"message": {"role": "assistant", "content": stop_msg}}

    def ask_llm(self, prompt):
        with use_display(self.display), interrupt_context(self._check_interrupt, self._request_interrupt):
            self.run_controller.start_run(prompt, kind="chat")
            watchdog = StallWatchdog(
                self.run_controller,
                notify=lambda text: self.display.status(text),
            )
            watchdog.start()

            def _finish(response, *, state="completed", final_message="", error=""):
                watchdog.stop()
                self.run_controller.finish_run(
                    state,
                    final_message=final_message,
                    error=error,
                )
                return response

            try:
                self.messages.append({"role": "user", "content": prompt})
                self._compact_tool_history()
                self._trim_history()

                # Clear any stale interrupt from a previous run
                self.interrupt_requested = False

                tools = self.get_tools_for_llm()
                start_time = time.monotonic()
                self._reset_attempt_ledger()

                for round_num in range(self.MAX_TOOL_ROUNDS + 1):
                    # User-requested stop
                    if self._check_interrupt():
                        return self._interrupt_response()
                    self._apply_pending_guidance()

                    # Wall-clock guard
                    elapsed = time.monotonic() - start_time
                    if elapsed >= self.ASK_LLM_TIMEOUT:
                        self.run_controller.note_activity(
                            "error",
                            f"Wall-clock limit of {self.ASK_LLM_TIMEOUT}s reached.",
                        )
                        msg = self._generate_natural_completion_summary(failed=True) or (
                            "I ran out of time working on that. Please try again "
                            "or break the task into smaller steps."
                        )
                        self.messages.append({"role": "assistant", "content": msg})
                        return _finish(
                            {"message": {"role": "assistant", "content": msg}},
                            state="failed",
                            error=msg,
                        )

                    spinner_msg = "Lumi is thinking" if round_num == 0 else "Lumi is working"
                    spinner = Spinner(spinner_msg).start()
                    self.run_controller.mark_model_round_start(round_num)
                    try:
                        remaining = self.ASK_LLM_TIMEOUT - (time.monotonic() - start_time)
                        deadline = min(self.ROUND_DEADLINE, remaining)
                        response = self.ollama.chat(
                            model=self.model,
                            messages=self.messages,
                            tools=tools,
                            stream=False,
                            deadline=deadline,
                            check_interrupt=self._check_interrupt,
                        )
                        self.run_controller.mark_model_round_end(round_num)
                    except OllamaInterruptedError:
                        self.run_controller.mark_model_round_end(round_num)
                        spinner.stop()
                        return self._interrupt_response()
                    except OllamaConnectionError as e:
                        self.run_controller.mark_model_round_end(round_num)
                        spinner.stop()
                        msg = str(e)
                        if self.ollama.last_model_used and self.ollama.last_model_used != self.model:
                            msg = f"Primary model unavailable, using fallback ({self.ollama.last_model_used}). " + msg
                        self.display.status(msg)
                        self.messages.append({"role": "assistant", "content": msg})
                        return _finish(
                            {"message": {"role": "assistant", "content": msg}},
                            state="failed",
                            error=msg,
                        )
                    except OllamaTimeoutError:
                        self.run_controller.mark_model_round_end(round_num)
                        spinner.stop()
                        msg = "Ollama stopped responding. Please check that the model is running and try again."
                        self.display.status(msg)
                        self.messages.append({"role": "assistant", "content": msg})
                        return _finish(
                            {"message": {"role": "assistant", "content": msg}},
                            state="failed",
                            error=msg,
                        )
                    finally:
                        spinner.stop()

                    # Notify if fallback model was used
                    if (self.ollama.last_model_used
                            and self.ollama.last_model_used != self.model
                            and round_num == 0):
                        print(_c(DIM, f"  (primary model unavailable, using fallback: {self.ollama.last_model_used})"))
                        self.display.status(
                            f"Primary model did not respond, so I switched to the fallback model {self.ollama.last_model_used}."
                        )

                    message = response.get("message", {})
                    tool_calls = message.get("tool_calls", [])

                    if self.verbose:
                        label = f"round {round_num}" if tool_calls else "final"
                        print(f"  [{label}] {json.dumps(message, default=str)[:300]}")

                    self.messages.append(message)

                    # Surface any text the model included alongside tool calls,
                    # unless it's effectively a restatement of the tool target
                    # the UI is already about to render as a chip, or the only
                    # tool calls are reactions — in which case the mid_text is
                    # about to become the final reply (see short-circuit below).
                    mid_text = (message.get("content") or "").strip()
                    reactions_only_preview = bool(tool_calls) and all(
                        (tc.get("function", {}) or {}).get("name") == "react_to_message"
                        for tc in tool_calls
                    )
                    if mid_text and tool_calls and not reactions_only_preview:
                        self.display.status(mid_text)

                    if not tool_calls:
                        final_text = self._ensure_final_message_content(
                            message,
                            failed=False,
                        )
                        return _finish(response, final_message=final_text)

                    # If every tool call in this round is a side-effect-only
                    # reaction AND the model already produced text, treat the
                    # text as the final reply instead of paying for another
                    # model round-trip just to say the same thing.
                    short_circuit_final = reactions_only_preview and bool(mid_text)

                    for tool_call in tool_calls:
                        # Check for stop before every tool call so long sequences
                        # (like a multi-step browser automation) can be aborted mid-flight.
                        if self._check_interrupt():
                            return self._interrupt_response()
                        self._apply_pending_guidance()

                        function_data = tool_call.get("function", {})
                        tool_name = function_data.get("name")
                        tool_inputs = function_data.get("arguments", {})

                        # Semantic loop detection: count attempts per logical
                        # target, not per exact argument blob. Three tries on
                        # the same (tool, action, target) → short-circuit.
                        over_limit = self._register_tool_attempt(tool_name, tool_inputs)
                        if over_limit is not None:
                            _, action_type, target = over_limit
                            target_label = target or "this step"
                            incident = (
                                f"Attempted `{tool_name}` ({action_type}) on "
                                f"{target_label} {self.REPEAT_ATTEMPT_LIMIT} times "
                                "without success; stopping to avoid a loop."
                            )
                            self.run_controller.note_activity("error", incident)
                            stuck_msg = self._generate_natural_completion_summary(failed=True) or (
                                f"I've tried `{tool_name}` ({action_type}) on "
                                f"{target_label} {self.REPEAT_ATTEMPT_LIMIT} times "
                                "and it keeps failing. I'm stopping here so we don't "
                                "loop — could you take a look or point me at a "
                                "different approach?"
                            )
                            self.messages.append({"role": "assistant", "content": stuck_msg})
                            return _finish(
                                {"message": {"role": "assistant", "content": stuck_msg}},
                                state="failed",
                                error=stuck_msg,
                            )

                        self.run_controller.mark_tool_start(
                            tool_name,
                            self._tool_activity_detail(tool_name, tool_inputs),
                        )
                        self.display.show_tool_call(tool_name, tool_inputs)

                        if tool_name in DIFF_TOOLS:
                            tool_result = self._handle_diff_tool(tool_name, tool_inputs)
                        elif tool_name in PREVIEW_TOOLS:
                            tool_result = self._handle_preview_tool(tool_name, tool_inputs)
                        elif tool_name in CONFIRM_TOOLS:
                            tool_result = self._handle_confirm_tool(tool_name, tool_inputs)
                        else:
                            tool_result = self.execute_tool(tool_name, tool_inputs)

                        if tool_result.get("interrupted") or self._check_interrupt():
                            return self._interrupt_response()

                        self.display.show_tool_result(tool_result)
                        summary, is_error = self._tool_result_activity_summary(tool_name, tool_result)
                        self.run_controller.mark_tool_end(tool_name, summary, error=is_error)

                        # Incrementally update code index when files change
                        if tool_name in ("edit_file", "write_file", "delete_file"):
                            changed_path = tool_inputs.get("path")
                            if changed_path and tool_result.get("success"):
                                self.code_index.update_file(changed_path)

                        if self.verbose:
                            print(f"  [tool result] {json.dumps(tool_result)[:200]}")

                        self.messages.append(
                            {
                                "role": "tool",
                                "name": tool_name,
                                "content": compact_tool_result_for_history(tool_name, tool_result),
                            }
                        )

                    # React-only round with existing text: finalize without
                    # another model call. Replace the placeholder content on
                    # the last assistant turn so transcripts still make sense.
                    if short_circuit_final:
                        if self.messages and self.messages[-1 - len(tool_calls)].get("role") == "assistant":
                            self.messages[-1 - len(tool_calls)]["content"] = mid_text
                        synthetic = {
                            "message": {"role": "assistant", "content": mid_text},
                        }
                        return _finish(synthetic, final_message=mid_text)

                final_message = response.get("message", {}) if isinstance(response, dict) else {}
                final_text = self._ensure_final_message_content(final_message, failed=False)
                return _finish(response, final_message=final_text)
            except Exception as exc:
                watchdog.stop()
                self.run_controller.finish_run("failed", error=str(exc))
                raise

    SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

    def ask_llm_with_image(self, prompt, image_data=None, image_path=None):
        """Send a message with an image to the LLM.

        Args:
            prompt: Text prompt to accompany the image.
            image_data: Raw image bytes (e.g. from Telegram download).
            image_path: Path to an image file on disk.
        """
        with use_display(self.display), interrupt_context(self._check_interrupt, self._request_interrupt):
            self.run_controller.start_run(prompt or "Image analysis", kind="vision")
            if image_path:
                path = Path(image_path)
                if not path.exists():
                    self.run_controller.finish_run(
                        "failed", error=f"File not found: {image_path}"
                    )
                    return {"message": {"role": "assistant",
                                        "content": f"File not found: {image_path}"}}
                if path.suffix.lower() not in self.SUPPORTED_IMAGE_EXTS:
                    error_msg = (
                        f"Unsupported image format: {path.suffix}\n"
                        f"Supported: {', '.join(sorted(self.SUPPORTED_IMAGE_EXTS))}"
                    )
                    self.run_controller.finish_run("failed", error=error_msg)
                    return {"message": {"role": "assistant",
                                        "content": error_msg}}
                image_data = path.read_bytes()

            if not image_data:
                self.run_controller.finish_run("failed", error="No image provided.")
                return {"message": {"role": "assistant",
                                    "content": "No image provided."}}

            b64 = base64.b64encode(image_data).decode("utf-8")
            prompt = prompt or "What do you see in this image?"

            # Clear any stale interrupt from a previous run
            self.interrupt_requested = False

            # Ollama vision format: message with "images" field
            self.messages.append({
                "role": "user",
                "content": prompt,
                "images": [b64],
            })
            self._compact_tool_history()
            self._trim_history()

            spinner = Spinner("Lumi is looking at the image").start()
            self.run_controller.mark_model_round_start(0)
            try:
                remaining = self.ASK_LLM_TIMEOUT
                deadline = min(self.ROUND_DEADLINE, remaining)
                response = self.ollama.chat(
                    model=self.model,
                    messages=self.messages,
                    stream=False,
                    deadline=deadline,
                    check_interrupt=self._check_interrupt,
                )
                self.run_controller.mark_model_round_end(0)
            except OllamaInterruptedError:
                self.run_controller.mark_model_round_end(0)
                spinner.stop()
                return self._interrupt_response()
            except OllamaConnectionError as e:
                self.run_controller.mark_model_round_end(0)
                spinner.stop()
                msg = str(e)
                self.messages.append({"role": "assistant", "content": msg})
                self.run_controller.finish_run("failed", error=msg)
                return {"message": {"role": "assistant", "content": msg}}
            except OllamaTimeoutError:
                self.run_controller.mark_model_round_end(0)
                spinner.stop()
                msg = "Ollama stopped responding while processing the image."
                self.messages.append({"role": "assistant", "content": msg})
                self.run_controller.finish_run("failed", error=msg)
                return {"message": {"role": "assistant", "content": msg}}
            except Exception as e:
                self.run_controller.mark_model_round_end(0)
                spinner.stop()
                error_str = str(e)
                # Detect vision-not-supported errors from Ollama
                if "does not support" in error_str.lower() or "vision" in error_str.lower():
                    msg = (f"The current model ({self.model}) doesn't support image analysis. "
                           f"Try switching to a vision model like llava or moondream.")
                else:
                    msg = f"Error processing image: {error_str}"
                self.messages.append({"role": "assistant", "content": msg})
                self.run_controller.finish_run("failed", error=msg)
                return {"message": {"role": "assistant", "content": msg}}
            finally:
                spinner.stop()

            # Notify if fallback was used
            if self.ollama.last_model_used and self.ollama.last_model_used != self.model:
                print(_c(DIM, f"  (primary model unavailable, using fallback: {self.ollama.last_model_used})"))

            message = response.get("message", {})
            self.messages.append(message)
            final_text = self._ensure_final_message_content(message, failed=False)
            self.run_controller.finish_run(
                "completed", final_message=final_text
            )
            return response

    def run_task(self, task_description):
        print(f"\n=== Task: {task_description} ===")
        print(f"Available tools: {[t['name'] for t in self.get_available_tools()]}")
        return {
            "task": task_description,
            "tools_available": self.get_available_tools(),
            "tools_for_llm": self.get_tools_for_llm(),
        }
