import json
import os
from pathlib import Path

from core.cli import Spinner, confirm, render_diff, show_tool_call, show_tool_result
from core.diffs import build_unified_diff, detect_line_ending, normalize_line_endings
from core.paths import get_repo_root
from ollama_client import OllamaClient
from tool_registry import ToolRegistry


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
    HISTORY_TURNS = 5

    def __init__(self, verbose=False):
        self.verbose = verbose

        # Initialize the tool registry and auto-load all tools from the tools folder
        self.registry = ToolRegistry()
        self.registry.load_tools_from_folder()

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
                    "- Always read a file before editing it.\n"
                    "- Use recall to check memory when the user asks about something you might have saved.\n"
                    "- After completing an action (commit, delete, edit, etc.), always confirm what happened.\n"
                    "- If the user declines a tool action, do NOT retry or try alternatives. Just respond.\n"
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
        max_msgs = 1 + self.HISTORY_TURNS * 2
        if len(self.messages) > max_msgs:
            self.messages = [self.messages[0]] + self.messages[
                -self.HISTORY_TURNS * 2 :
            ]

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

        for round_num in range(self.MAX_TOOL_ROUNDS + 1):
            spinner_msg = "Lumi is thinking" if round_num == 0 else "Lumi is working"
            spinner = Spinner(spinner_msg).start()
            try:
                response = self.ollama.chat(
                    model=self.model, messages=self.messages, tools=tools, stream=False
                )
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
