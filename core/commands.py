"""Slash command handlers for the LumaKit CLI."""

import json
import os
import sys
from pathlib import Path

from core.chat_store import delete_chat, list_chats, load_chat, make_title, new_chat_id, save_chat
from core.cli import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, _c, render_storage_meter
from core.menu import select_menu


def handle_command(command: str, agent, session: dict) -> bool:
    """Dispatch a slash command. Returns True if handled, False if not a command."""
    parts = command.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handlers = {
        "/help": cmd_help,
        "/chats": cmd_chats,
        "/new": cmd_new,
        "/status": cmd_status,
        "/config": cmd_config,
        "/clear": cmd_clear,
    }

    handler = handlers.get(cmd)
    if handler:
        handler(args, agent, session)
        return True
    else:
        print(_c(RED, f"  Unknown command: {cmd}"))
        print(_c(DIM, "  Type /help for available commands."))
        return True


def cmd_help(args: str, agent, session: dict):
    print(f"""
{_c(BOLD, '  LumaKit Commands')}

  {_c(CYAN, '/help')}                 Show this help
  {_c(CYAN, '/chats')}                List saved conversations
  {_c(CYAN, '/chats resume <id>')}    Resume a saved conversation
  {_c(CYAN, '/chats delete <id>')}    Delete a saved conversation
  {_c(CYAN, '/image <path> [prompt]')} Send an image to Lumi for analysis
  {_c(CYAN, '/new')}                  Start a new conversation
  {_c(CYAN, '/status')}               Show storage, index, and model info
  {_c(CYAN, '/config')}               View current configuration
  {_c(CYAN, '/config set <k> <v>')}   Update a config value
  {_c(CYAN, '/clear')}                Clear the screen
""")


def cmd_chats(args: str, agent, session: dict):
    chats = list_chats(limit=20)
    if not chats:
        print(_c(DIM, "  No saved conversations.\n"))
        return

    # Build menu items
    items = []
    for chat in chats:
        updated = chat["updated_at"][:16].replace("T", " ")
        items.append({
            "label": chat["title"],
            "sublabel": f"id: {chat['id']}  |  {updated}",
            "chat_id": chat["id"],
        })

    # Print blank lines so first render has space to overwrite
    total_lines = 2 + len(items) * 2 + 1
    print("\n" * total_lines)

    result = select_menu(items, title="Saved Conversations")
    if not result:
        return

    if result.get("action") == "select":
        _chats_resume(result["chat_id"], agent, session)
    elif result.get("action") == "delete":
        _chats_delete(result["chat_id"])


def _chats_resume(chat_id: str, agent, session: dict):
    chat = load_chat(chat_id)
    if not chat:
        print(_c(RED, f"  Conversation '{chat_id}' not found."))
        return

    # Save current conversation first
    _auto_save(agent, session)

    # Load the resumed conversation
    agent.messages = chat["messages"]
    session["chat_id"] = chat["id"]
    session["title"] = chat["title"]
    session["first_message_sent"] = True

    print(_c(GREEN, f"  Resumed: {chat['title']}"))
    print(_c(DIM, f"  {len(chat['messages'])} messages loaded.\n"))


def _chats_delete(chat_id: str):
    chat = load_chat(chat_id)
    title = chat["title"] if chat else chat_id
    if delete_chat(chat_id):
        print(_c(GREEN, f"  Deleted: {title}"))
    else:
        print(_c(RED, f"  Conversation '{chat_id}' not found."))


def cmd_new(args: str, agent, session: dict):
    # Save current conversation
    _auto_save(agent, session)

    # Reset
    session["chat_id"] = new_chat_id()
    session["title"] = ""
    session["first_message_sent"] = False

    # Rebuild messages with just the system prompt
    system_msg = agent.messages[0] if agent.messages else None
    agent.messages = [system_msg] if system_msg else []

    print(_c(GREEN, "  New conversation started.\n"))


def cmd_status(args: str, agent, session: dict):
    health = agent.storage.check_health()
    meter = render_storage_meter(
        health["usage_percent"], health["total_display"], health["budget_display"]
    )

    sym_count = len(agent.code_index.table.all_symbols())
    ref_count = len(agent.code_index.references)
    msg_count = len(agent.messages)
    model = agent.model or "not set"
    fallback = agent.fallback_model or "not set"
    chat_count = len(list_chats(limit=100))

    print(f"""
{_c(BOLD, '  LumaKit Status')}

{meter}

  {_c(CYAN, 'Model:')}          {model}
  {_c(CYAN, 'Fallback:')}       {fallback}
  {_c(CYAN, 'Messages:')}       {msg_count} in current conversation
  {_c(CYAN, 'Saved chats:')}    {chat_count}
  {_c(CYAN, 'Index:')}          {sym_count} symbols, {ref_count} references
  {_c(CYAN, 'Chat ID:')}        {session.get('chat_id', 'none')}
""")

    for name, info in health["stores"].items():
        if info["size_bytes"] > 0:
            print(f"  {_c(DIM, f'{name}:')} {info['size_display']}")
    print()


def cmd_config(args: str, agent, session: dict):
    config_path = agent.code_index.root / ".lumakit" / "config.json"

    if args.strip().lower().startswith("set"):
        _config_set(args, config_path, agent)
    else:
        _config_show(config_path, agent)


def _config_show(config_path: Path, agent):
    config = _load_config(config_path)

    print(f"\n{_c(BOLD, '  LumaKit Configuration')}")
    print(f"  {_c(DIM, str(config_path))}\n")

    defaults = _get_defaults(agent)
    for key, default in defaults.items():
        value = config.get(key, default)
        is_custom = key in config
        marker = _c(GREEN, "*") if is_custom else " "
        print(f"  {marker} {_c(CYAN, f'{key}:'):<35} {value}")

    print(f"\n{_c(DIM, '  * = customized  |  /config set <key> <value>')}\n")


def _config_set(args: str, config_path: Path, agent):
    # Parse: set <key> <value>
    parts = args.strip().split(maxsplit=2)
    if len(parts) < 3:
        print(_c(RED, "  Usage: /config set <key> <value>"))
        return

    key = parts[1]
    value = parts[2]

    # Type coercion
    int_keys = {"storage_budget_mb", "max_tool_rounds", "recent_turns"}
    bool_keys = {"auto_save_chats"}

    if key in int_keys:
        try:
            value = int(value)
        except ValueError:
            print(_c(RED, f"  {key} must be a number."))
            return
    elif key in bool_keys:
        value = value.lower() in ("true", "1", "yes")

    config = _load_config(config_path)
    config[key] = value

    config_path.parent.mkdir(exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    print(_c(GREEN, f"  Set {key} = {value}"))

    # Apply immediately where possible
    if key == "storage_budget_mb" and isinstance(value, int):
        agent.storage.budget_bytes = value * 1024 * 1024
    elif key == "max_tool_rounds" and isinstance(value, int):
        agent.MAX_TOOL_ROUNDS = value


def _load_config(config_path: Path) -> dict:
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _get_defaults(agent) -> dict:
    return {
        "model": agent.model or "(env: OLLAMA_MODEL)",
        "storage_budget_mb": agent.storage.budget_bytes // (1024 * 1024),
        "max_tool_rounds": agent.MAX_TOOL_ROUNDS,
        "auto_save_chats": True,
    }


def cmd_clear(args: str, agent, session: dict):
    os.system("cls" if sys.platform == "win32" else "clear")


def _auto_save(agent, session: dict):
    """Save the current conversation if it has content."""
    if not session.get("first_message_sent"):
        return
    chat_id = session.get("chat_id", "")
    title = session.get("title", "Untitled")
    if len(agent.messages) > 1:
        save_chat(chat_id, title, agent.messages)
