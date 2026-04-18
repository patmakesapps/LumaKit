"""Per-agent UI hooks.

Each surface (CLI, Telegram, web, future connectors) constructs its own
DisplayHooks so multiple agents can coexist in one process without stomping
on module-level state.
"""

from __future__ import annotations

from typing import Callable

from core.cli import (
    confirm as _cli_confirm,
    render_diff as _cli_render_diff,
    show_tool_call as _cli_show_tool_call,
    show_tool_result as _cli_show_tool_result,
)


def _cli_show_diff(diff_text: str) -> None:
    print(_cli_render_diff(diff_text))


class DisplayHooks:
    def __init__(
        self,
        *,
        show_tool_call: Callable[[str, dict], None] | None = None,
        show_tool_result: Callable[[dict], None] | None = None,
        show_diff: Callable[[str], None] | None = None,
        confirm: Callable[[str], bool] | None = None,
    ):
        self.show_tool_call = show_tool_call or _cli_show_tool_call
        self.show_tool_result = show_tool_result or _cli_show_tool_result
        self.show_diff = show_diff or _cli_show_diff
        self.confirm = confirm or _cli_confirm
