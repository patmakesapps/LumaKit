"""Per-agent UI hooks.

Each surface (CLI, Telegram, web, future connectors) constructs its own
DisplayHooks so multiple agents can coexist in one process without stomping
on module-level state.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

from core.cli import (
    confirm as _cli_confirm,
    render_diff as _cli_render_diff,
    show_tool_call as _cli_show_tool_call,
    show_tool_result as _cli_show_tool_result,
)


def _cli_show_diff(diff_text: str) -> None:
    print(_cli_render_diff(diff_text))


_active_display: ContextVar["DisplayHooks | None"] = ContextVar(
    "lumakit_display_hooks",
    default=None,
)


class DisplayHooks:
    def __init__(
        self,
        *,
        show_tool_call: Callable[[str, dict], None] | None = None,
        show_tool_result: Callable[[dict], None] | None = None,
        show_diff: Callable[[str], None] | None = None,
        status: Callable[[str], None] | None = None,
        confirm: Callable[[str], bool] | None = None,
        confirm_email: Callable[[dict, str | None], bool] | None = None,
    ):
        self.show_tool_call = show_tool_call or _cli_show_tool_call
        self.show_tool_result = show_tool_result or _cli_show_tool_result
        self.show_diff = show_diff or _cli_show_diff
        self.status = status or (lambda message: print(message))
        self.confirm = confirm or _cli_confirm
        self.confirm_email = confirm_email or (
            lambda preview, prompt=None: self.confirm(prompt or "Approve this email?")
        )


@contextmanager
def use_display(display: DisplayHooks):
    token = _active_display.set(display)
    try:
        yield
    finally:
        _active_display.reset(token)


def get_display() -> DisplayHooks:
    display = _active_display.get()
    return display or DisplayHooks()


def confirm(prompt: str = "Apply this change?") -> bool:
    return get_display().confirm(prompt)


def confirm_email(preview: dict, prompt: str | None = None) -> bool:
    return get_display().confirm_email(preview, prompt)


def status(message: str) -> None:
    if not message:
        return
    get_display().status(message)
