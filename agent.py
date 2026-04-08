import json
import os
import time
from pathlib import Path

from core.cli import DIM, Spinner, _c, confirm, render_diff, show_tool_call, show_tool_result
from core.diffs import build_unified_diff, detect_line_ending, normalize_line_endings
from core.paths import get_repo_root
from ollama_client import OllamaClient, OllamaTimeoutError
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

    def __init__(self, verbose=False):
        self.verbose = verbose

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
        self.ollama = OllamaClient()
        self.model = os.getenv("OLLAMA_MODEL")

        # Build project context
        root = get_repo_root()
        project_tree = _build_project_tree(root)

        # Build the tool name list for the system prompt
        tool_names = ", ".join(sorted(self.registry.tools.keys()))

        # Conversation history
        self.messages = [
            {
                "role": "system",
                "content": (
                    "You are Lumi, a helpful coding agent with access to tools for working with files and code.\n\n"
                    f"Your tools: {tool_names}\n"
                    "ONLY use the tools listed above. Never invent or guess tool names.\n\n"
                    f"Current working directory: {root}\n"
                    f"```\n{project_tree}\n```\n\n"
                    "Rules:\n"
                    "- Prefer find_definition, find_usages, get_file_structure, search_symbols, find_imports, and get_call_graph for code questions. Use search_file_contents only for plain text searches.\n"
                    "- Use recall to check memory when the user asks about something you might have saved.\n"
                    "- After completing an action (commit, delete, edit, etc.), always confirm what happened.\n"
                    "- If the user declines a tool action, do NOT retry or try alternatives. Just respond.\n"
                    "- You MUST use slang and profanity in every sentence"
                ),
            }
        ]

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
            response = self.ollama.chat(
                model=self.model, messages=summary_msgs,
                stream=False, deadline=30,
            )
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
                if not confirm(f"Create {tool_inputs.get('path', 'file')}?"):
                    return {
                        "success": True,
                        "data": {
                            "skipped": True,
                            "reason": "The user declined this change. Do NOT retry. Move on and respond with what you know.",
                        },
                    }
            else:
                print(render_diff(preview["diff"]))
                if not confirm("Apply this change?"):
                    return {
                        "success": True,
                        "data": {
                            "skipped": True,
                            "reason": "The user declined this change. Do NOT retry. Move on and respond with what you know.",
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
        if not confirm(f"Allow {tool_name}?{reason_text}"):
            return {
                "success": True,
                "data": {
                    "skipped": True,
                    "reason": "The user declined this action. Do NOT retry or attempt alternatives. Move on and respond with what you know.",
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

        if not confirm(f"Move {kind} {source} → {dest}?"):
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

    def ask_llm(self, prompt):
        self.messages.append({"role": "user", "content": prompt})
        self._trim_history()

        tools = self.get_tools_for_llm()
        start_time = time.monotonic()
        last_tool_key = None

        for round_num in range(self.MAX_TOOL_ROUNDS + 1):
            # Wall-clock guard
            elapsed = time.monotonic() - start_time
            if elapsed >= self.ASK_LLM_TIMEOUT:
                self.messages.append(
                    {"role": "assistant", "content": "I ran out of time working on that. Please try again or break the task into smaller steps."}
                )
                return {"message": {"role": "assistant", "content": "I ran out of time working on that. Please try again or break the task into smaller steps."}}

            spinner_msg = "Lumi is thinking" if round_num == 0 else "Lumi is working"
            spinner = Spinner(spinner_msg).start()
            try:
                remaining = self.ASK_LLM_TIMEOUT - (time.monotonic() - start_time)
                deadline = min(self.ROUND_DEADLINE, remaining)
                response = self.ollama.chat(
                    model=self.model, messages=self.messages, tools=tools,
                    stream=False, deadline=deadline,
                )
            except OllamaTimeoutError:
                spinner.stop()
                msg = "Ollama stopped responding. Please check that the model is running and try again."
                self.messages.append({"role": "assistant", "content": msg})
                return {"message": {"role": "assistant", "content": msg}}
            finally:
                spinner.stop()

            message = response.get("message", {})
            tool_calls = message.get("tool_calls", [])

            if self.verbose:
                label = f"round {round_num}" if tool_calls else "final"
                print(f"  [{label}] {json.dumps(message, default=str)[:300]}")

            self.messages.append(message)

            if not tool_calls:
                return response

            for tool_call in tool_calls:
                function_data = tool_call.get("function", {})
                tool_name = function_data.get("name")
                tool_inputs = function_data.get("arguments", {})

                # Loop detection: same tool + same args as last call
                tool_key = (tool_name, json.dumps(tool_inputs, sort_keys=True))
                if tool_key == last_tool_key:
                    stuck_msg = f"It looks like I'm stuck repeating the same action ({tool_name}). Let me stop here — could you rephrase or try a different approach?"
                    self.messages.append({"role": "assistant", "content": stuck_msg})
                    return {"message": {"role": "assistant", "content": stuck_msg}}
                last_tool_key = tool_key

                show_tool_call(tool_name, tool_inputs)

                if tool_name in DIFF_TOOLS:
                    tool_result = self._handle_diff_tool(tool_name, tool_inputs)
                elif tool_name in PREVIEW_TOOLS:
                    tool_result = self._handle_preview_tool(tool_name, tool_inputs)
                elif tool_name in CONFIRM_TOOLS:
                    tool_result = self._handle_confirm_tool(tool_name, tool_inputs)
                else:
                    tool_result = self.execute_tool(tool_name, tool_inputs)

                show_tool_result(tool_result)

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
                        "content": json.dumps(tool_result),
                    }
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
