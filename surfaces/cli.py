"""CLI surface — terminal REPL.

Run with:
    python -m surfaces.cli
"""

import os
import subprocess
import sys
import tempfile

from agent import Agent
from core.chat_store import make_title, new_chat_id, save_chat
from core.cli import render_storage_meter
from core.commands import handle_command
from core.service import LumaKitService, Surface
from tools.memory.memory_tools import set_active_user as set_memory_active_user

CLI_USER_ID = "cli"


def grab_clipboard_image():
    """Read an image from the clipboard. Returns PNG bytes or None."""
    # Uses PowerShell + .NET on Windows — works with Snipping Tool, Print
    # Screen, browser copies, etc.
    tmp = os.path.join(tempfile.gettempdir(), "_lumakit_clip.png")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$img = [System.Windows.Forms.Clipboard]::GetImage();"
        f"if ($img) {{ $img.Save('{tmp}'); Write-Host 'OK' }}"
        " else { Write-Host 'NONE' }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=5,
        )
        if "OK" in result.stdout and os.path.exists(tmp):
            with open(tmp, "rb") as f:
                data = f.read()
            os.remove(tmp)
            return data if len(data) > 0 else None
    except Exception:
        pass
    return None


def _cli_deliver(payload: dict) -> bool:
    """Surface deliverer — prints notifications to the terminal."""
    content = payload.get("content", "")
    if not content:
        return False
    label = payload.get("label") or ""
    prefix = f"🔔 {label}: " if label else ""
    print(f"\nLumi: {prefix}{content}\n")
    return True


def _cli_status(msg: str) -> None:
    print(f"\nLumi: {msg}\n")


def main():
    verbose = "--verbose" in sys.argv
    set_memory_active_user(CLI_USER_ID)
    agent = Agent(verbose=verbose, status_callback=_cli_status)

    service = LumaKitService()
    service.register_surface(Surface(
        name="cli",
        deliver=_cli_deliver,
        is_owner=True,
    ))
    service.start()

    session = {
        "chat_id": new_chat_id(),
        "owner_id": CLI_USER_ID,
        "title": "",
        "first_message_sent": False,
    }

    print("\n=== LumaKit CLI ===")
    health = agent.storage.check_health()
    print(render_storage_meter(
        health["usage_percent"], health["total_display"], health["budget_display"]
    ))
    print("Type /help for commands, 'exit' to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            if session["first_message_sent"] and len(agent.messages) > 1:
                save_chat(session["chat_id"], session["title"], agent.messages, owner_id=CLI_USER_ID)
            service.stop()
            print("\nGoodbye.")
            break

        if user_input.lower() in ("exit", "quit"):
            if session["first_message_sent"] and len(agent.messages) > 1:
                save_chat(session["chat_id"], session["title"], agent.messages, owner_id=CLI_USER_ID)
            service.stop()
            print("Goodbye.")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            if user_input.lower().startswith("/p"):
                parts = user_input.split(maxsplit=1)
                img_prompt = parts[1] if len(parts) > 1 else None
                image_data = grab_clipboard_image()
                if not image_data:
                    print("  No image found on clipboard. Copy an image first, then try /p again.\n")
                    continue
                try:
                    response = agent.ask_llm_with_image(prompt=img_prompt, image_data=image_data)
                    content = response.get("message", {}).get("content", "")
                    if content:
                        print(f"\nLumi: {content}\n")
                    if not session["first_message_sent"]:
                        session["title"] = make_title(img_prompt or "Clipboard image")
                        session["first_message_sent"] = True
                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(session["chat_id"], session["title"], agent.messages, owner_id=CLI_USER_ID)
                except Exception as e:
                    print(f"\nError: {e}\n")
                continue

            if user_input.lower().startswith("/image"):
                parts = user_input.split(maxsplit=2)
                if len(parts) < 2:
                    print("Usage: /image <path> [optional prompt]")
                    continue
                img_path = parts[1].strip('"').strip("'")
                img_prompt = parts[2] if len(parts) > 2 else None
                try:
                    response = agent.ask_llm_with_image(prompt=img_prompt, image_path=img_path)
                    content = response.get("message", {}).get("content", "")
                    if content:
                        print(f"\nLumi: {content}\n")
                    if not session["first_message_sent"]:
                        session["title"] = make_title(f"Image: {img_path}")
                        session["first_message_sent"] = True
                    if session["first_message_sent"] and len(agent.messages) > 1:
                        save_chat(session["chat_id"], session["title"], agent.messages, owner_id=CLI_USER_ID)
                except Exception as e:
                    print(f"\nError: {e}\n")
                continue

            handle_command(user_input, agent, session)
            continue

        try:
            response = agent.ask_llm(user_input)
            content = response.get("message", {}).get("content", "")
            if content:
                print(f"\nLumi: {content}\n")

            if not session["first_message_sent"]:
                session["title"] = make_title(user_input)
                session["first_message_sent"] = True

            if session["first_message_sent"] and len(agent.messages) > 1:
                save_chat(session["chat_id"], session["title"], agent.messages, owner_id=CLI_USER_ID)

            milestone = agent.storage.check_milestone()
            if milestone:
                print(milestone)

            full_info = agent.storage.check_full()
            if full_info:
                print(f"\n  Storage full! {full_info['total_display']} / {full_info['budget_display']}")
                print(f"  Largest store: {full_info['suggestion']} ({full_info['suggestion_size']})")
                try:
                    answer = input(f"  Clear {full_info['suggestion']} to free space? [y/n] ").strip().lower()
                    if answer in ("y", "yes"):
                        from tools.runtime.storage_tools import _clear_storage
                        result = _clear_storage({"target": full_info["suggestion"]})
                        print(f"  Cleared: {', '.join(result['cleared'])} (freed {result['freed']})\n")
                    else:
                        print("  Skipped. Lumi will keep running but won't write new cache data.\n")
                except (EOFError, KeyboardInterrupt):
                    print()

        except Exception as e:
            print(f"\nError: {e}\n")


if __name__ == "__main__":
    main()
