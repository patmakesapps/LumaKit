from __future__ import annotations

import sys
import threading
import time


# ANSI color codes
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _supports_color():
        return text
    return f"{code}{text}{RESET}"


def render_diff(diff_text: str) -> str:
    if not diff_text:
        return _c(DIM, "  (no changes)")
    lines = diff_text.splitlines()
    rendered = []
    for line in lines:
        if line.startswith("+++") or line.startswith("---"):
            rendered.append(_c(BOLD, line))
        elif line.startswith("@@"):
            rendered.append(_c(CYAN, line))
        elif line.startswith("+"):
            rendered.append(_c(GREEN, line))
        elif line.startswith("-"):
            rendered.append(_c(RED, line))
        else:
            rendered.append(_c(DIM, line))
    return "\n".join(rendered)


def show_tool_call(tool_name: str, inputs: dict) -> None:
    label = _c(YELLOW, f"  [{tool_name}]")
    detail = ""
    if "path" in inputs:
        detail = f" {inputs['path']}"
    elif "command" in inputs:
        cmd = inputs["command"]
        detail = f" {cmd[:80]}{'...' if len(cmd) > 80 else ''}"
    print(f"{label}{detail}")


def show_tool_result(result: dict) -> None:
    if not result.get("success"):
        print(_c(RED, f"  error: {result.get('error', 'unknown')}"))
        return
    data = result.get("data", {})
    if data.get("skipped"):
        print(_c(DIM, "  (skipped)"))
        return
    if "diff" in data and data["diff"]:
        print(render_diff(data["diff"]))
    # Show a one-line summary for common tool results
    summary = _summarize_result(data)
    if summary:
        print(_c(GREEN, f"  {summary}"))


def _summarize_result(data: dict) -> str:
    """Build a short one-line summary from tool result data."""
    if data.get("committed"):
        return f'committed: "{data.get("message", "")}"'
    if data.get("pushed"):
        return f'pushed to {data.get("branch", "remote")}'
    if data.get("pulled"):
        return f'pulled from {data.get("branch", "remote")}'
    if data.get("deleted"):
        return f'deleted {data.get("path", "file")}'
    if data.get("saved"):
        return f'memory saved (id:{data.get("id")})'
    if "replacements" in data:
        return f'{data["replacements"]} replacement(s) in {data.get("path", "file")}'
    if data.get("created") is True:
        return f'created {data.get("path", "file")} ({data.get("bytes_written", 0)} bytes)'
    if data.get("created") is False and "bytes_written" in data:
        return f'wrote {data.get("path", "file")} ({data.get("bytes_written", 0)} bytes)'
    if "added" in data:
        return f'staged {data.get("files", "files")}'
    if "status" in data and "command" in data:
        return "done"
    if "count" in data and "memories" in data:
        return f'found {data["count"]} memory(s)'
    if "content" in data and "path" in data and len(data) <= 3:
        return f'read {data["path"]}'
    return ""


class Spinner:
    """Animated spinner that runs in a background thread."""

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str = "Lumi is thinking"):
        self._message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = self.FRAMES[i % len(self.FRAMES)]
            text = f"\r{_c(CYAN, frame)} {_c(DIM, self._message)}"
            sys.stdout.write(text)
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.08)
        # Clear the spinner line
        sys.stdout.write("\r" + " " * (len(self._message) + 4) + "\r")
        sys.stdout.flush()

    def start(self) -> "Spinner":
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None

    def __enter__(self) -> "Spinner":
        return self.start()

    def __exit__(self, *_) -> None:
        self.stop()


def confirm(prompt: str = "Apply this change?") -> bool:
    try:
        answer = input(f"\n{_c(YELLOW, prompt)} {_c(DIM, '[y/n]')} ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
