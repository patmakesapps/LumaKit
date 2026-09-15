"""Single source of truth for which tools always require user approval.

Both the interactive agent (agent.py) and the autonomous task runner
(core/task_runner.py) import from here so the security policy cannot drift
between them.

Two distinct policies:

- Interactive surfaces (web/CLI/Telegram): tools in ALWAYS_CONFIRM_TOOLS
  prompt for approval even when the global ``require_tool_approvals`` toggle
  is off. Arbitrary-execution tools are unconditionally included — a denylist
  of "dangerous commands" is unwinnable, so the approval prompt is the
  control, not command matching.

- Autonomous tasks (no human present to prompt): tools in
  AUTONOMOUS_REFUSED_TOOLS are refused outright. Shell/python execution is
  currently allowed inside tasks but screened by PROTECTED_SHELL_COMMAND_RE;
  the planned cross-surface approval round-trip will replace that screen with
  a real remote approval gate.
"""

from __future__ import annotations

import re

# Tools that must never run without an explicit human approval, regardless of
# the global require_tool_approvals setting (S-4).
ALWAYS_CONFIRM_TOOLS = frozenset({
    "delete_file",
    "git_add",
    "git_commit",
    "git_push",
    "execute_shell",
    "execute_python",
    "run_command",
    "lumabot_reboot",
    "lumabot_poweroff",
    "lumabot_start_autonomy",
})

# Tools an autonomous task may never execute (it has no way to ask).
AUTONOMOUS_REFUSED_TOOLS = frozenset({
    "delete_file",
    "git_add",
    "git_commit",
    "git_push",
    "lumabot_reboot",
    "lumabot_poweroff",
    "lumabot_start_autonomy",
})

# Screens shell commands issued from inside autonomous tasks. This is a
# best-effort filter, not a security boundary — interactive runs rely on the
# unconditional approval prompt instead.
PROTECTED_SHELL_COMMAND_RE = re.compile(
    r"\bgit\s+(add|commit|push)\b|\b(rm|del|erase|unlink)\b",
    re.IGNORECASE,
)

# Tools that accept a raw command string worth screening.
_COMMAND_TOOLS = {"execute_shell", "run_command"}

# Protected actions the owner may pre-approve for ONE whole task (scoped
# standing grants). Anything not granted still pauses the task for a one-shot
# approval. Keys are stable ids used by the API, the UI, and task constraints
# (``_allowed_actions``). These categories mirror AUTONOMOUS_REFUSED_TOOLS and
# PROTECTED_SHELL_COMMAND_RE — keep them in sync.
TASK_ACTION_GRANTS: dict[str, dict] = {
    "git_commit": {
        "label": "Git add & commit",
        "hint": "Stage files and create commits in the workspace repo.",
        "tools": frozenset({"git_add", "git_commit"}),
        "shell": re.compile(r"\bgit\s+(add|commit)\b", re.IGNORECASE),
        "risky": False,
    },
    "git_push": {
        "label": "Git push",
        "hint": "Push commits to a remote. Hard to take back.",
        "tools": frozenset({"git_push"}),
        "shell": re.compile(r"\bgit\s+push\b", re.IGNORECASE),
        "risky": True,
    },
    "delete_files": {
        "label": "Delete files",
        "hint": "Remove files or folders (delete_file, rm, del).",
        "tools": frozenset({"delete_file"}),
        "shell": re.compile(r"\b(rm|del|erase|unlink)\b", re.IGNORECASE),
        "risky": True,
    },
    "lumabot_power": {
        "label": "LumaBot power & autonomy",
        "hint": "Reboot or power off the robot, or start its autonomy mode.",
        "tools": frozenset({"lumabot_reboot", "lumabot_poweroff", "lumabot_start_autonomy"}),
        "shell": None,
        "risky": True,
    },
}


def task_action_key(tool_name: str | None, tool_inputs: dict) -> str | None:
    """Which pre-approvable action a protected call falls under, if any."""
    command = command_text_from_inputs(tool_inputs or {}) if tool_name in _COMMAND_TOOLS else ""
    for key, spec in TASK_ACTION_GRANTS.items():
        if tool_name in spec["tools"]:
            return key
        if command and spec["shell"] is not None and spec["shell"].search(command):
            return key
    return None


def normalize_task_actions(values) -> list[str]:
    """Validated, de-duplicated grant keys; unknown keys are dropped."""
    out: list[str] = []
    for value in values or []:
        key = str(value).strip()
        if key in TASK_ACTION_GRANTS and key not in out:
            out.append(key)
    return out


def task_action_catalog() -> list[dict]:
    return [
        {"key": key, "label": spec["label"], "hint": spec["hint"], "risky": bool(spec["risky"])}
        for key, spec in TASK_ACTION_GRANTS.items()
    ]


def task_action_label(key: str) -> str:
    spec = TASK_ACTION_GRANTS.get(key)
    return spec["label"] if spec else str(key)


def command_text_from_inputs(tool_inputs: dict) -> str:
    command = str(tool_inputs.get("command", "") or "")
    if not command and isinstance(tool_inputs.get("args"), list):
        command = " ".join(str(part) for part in tool_inputs.get("args", []))
    return command


def safe_mode_enabled() -> bool:
    from core.app_runtime_config import get_app_runtime_config

    return bool(get_app_runtime_config().get("safe_mode", True))


def unrestricted_filesystem_active() -> bool:
    """Safe mode is off AND the active user is entitled to full machine access.

    Non-owner Telegram users keep the workspace sandbox regardless of the
    safe-mode toggle; web/CLI are single-user owner surfaces.
    """
    if safe_mode_enabled():
        return False
    from core import auth
    from core.interface_context import get_interface

    if get_interface() != "telegram":
        return True
    return auth.is_owner_active()


def tool_always_requires_approval(tool_name: str, tool_inputs: dict) -> bool:
    """Interactive-surface policy: does this call prompt even with approvals off?"""
    if not safe_mode_enabled():
        return False
    if tool_name in ALWAYS_CONFIRM_TOOLS:
        return True
    if tool_name in _COMMAND_TOOLS:
        return bool(PROTECTED_SHELL_COMMAND_RE.search(command_text_from_inputs(tool_inputs)))
    return False


# ---------------------------------------------------------------------------
# Per-user tool scoping on shared surfaces (S-6).
#
# Telegram allows multiple chat IDs; only the owner should reach tools that
# execute code, mutate the repo, or touch secrets. Roles:
#   owner   — everything (the first TELEGRAM_ALLOWED_IDS / auth owner)
#   trusted — default for other allowed users; no execution/mutation tools
#   limited — trusted minus browser automation and task creation
# ---------------------------------------------------------------------------

ROLE_OWNER = "owner"
ROLE_TRUSTED = "trusted"
ROLE_LIMITED = "limited"
VALID_ROLES = (ROLE_OWNER, ROLE_TRUSTED, ROLE_LIMITED)

OWNER_ONLY_TOOLS = frozenset({
    # arbitrary execution
    "execute_shell",
    "execute_python",
    "run_command",
    "stop_background_command",
    # repo mutation
    "write_file",
    "edit_file",
    "delete_file",
    "apply_patch",
    "move_path",
    "set_workspace",
    # git writes
    "git_init",
    "git_add",
    "git_commit",
    "git_push",
    "git_pull",
    "git_branch",
    # system control / destructive maintenance
    "reboot_system",
    "restart_service",
    "lumabot_reboot",
    "lumabot_poweroff",
    "clear_storage",
    # secrets manager
    "lumalok_list_secrets",
    "lumalok_get_secret",
    "lumalok_add_secret",
    "lumalok_update_secret",
    # physical robot control
    "lumabot_drive",
    "lumabot_sequence",
    "lumabot_stop",
    "lumabot_start_autonomy",
    # the camera photographs the owner's home
    "lumabot_capture_photo",
    # tasks run autonomously with broader powers — creating/deleting them is
    # an escalation path for non-owners
    "create_task",
    "delete_task",
})

LIMITED_DENIED_TOOLS = OWNER_ONLY_TOOLS | frozenset({
    "browser_automation",
    "browse",
    "instagram_session",
    "email_send",
    "email_reply",
})


def denied_tools_for_role(role: str) -> frozenset[str]:
    role = str(role or "").strip().lower()
    if role == ROLE_OWNER:
        return frozenset()
    if role == ROLE_LIMITED:
        return LIMITED_DENIED_TOOLS
    return OWNER_ONLY_TOOLS


def active_surface_denied_tools() -> frozenset[str]:
    """Tools the current per-turn user may not use.

    Enforced only on shared surfaces (Telegram today). Web and CLI are
    single-user owner surfaces and stay unrestricted here.
    """
    from core import auth
    from core.interface_context import get_interface

    if get_interface() != "telegram":
        return frozenset()
    if auth.is_owner_active():
        return frozenset()
    from core.telegram_user_config import get_user_role

    return denied_tools_for_role(get_user_role(auth.get_active_user()))


def surface_tool_denial(tool_name: str) -> str | None:
    """Return a refusal message if the current user's role blocks this tool."""
    if tool_name in active_surface_denied_tools():
        return (
            f"'{tool_name}' is not permitted for your role. Ask the owner to "
            "run this, or to change your role with /role."
        )
    return None


def autonomous_tool_refusal(tool_name: str | None, tool_inputs: dict) -> str | None:
    """Task-runner policy: return a refusal message if this call may not run
    inside an autonomous task, else None."""
    if tool_name in AUTONOMOUS_REFUSED_TOOLS:
        return (
            f"{tool_name} requires explicit user approval and cannot run "
            "inside an autonomous task."
        )
    if tool_name in _COMMAND_TOOLS and PROTECTED_SHELL_COMMAND_RE.search(
        command_text_from_inputs(tool_inputs)
    ):
        return (
            f"{tool_name} requires explicit user approval and cannot run "
            "inside an autonomous task."
        )
    return None
