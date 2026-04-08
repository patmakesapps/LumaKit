import sys

from agent import Agent
from core.chat_store import make_title, new_chat_id, save_chat
from core.cli import render_storage_meter
from core.commands import handle_command
from core.reminder_checker import ReminderChecker

# Create an agent
verbose = "--verbose" in sys.argv
agent = Agent(verbose=verbose)

# Start the reminder checker (polls every 30 seconds)
reminders = ReminderChecker(interval=30)
reminders.start()

# Session state for chat persistence
session = {
    "chat_id": new_chat_id(),
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
        # Auto-save on exit
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)
        reminders.stop()
        print("\nGoodbye.")
        break

    if user_input.lower() in ("exit", "quit"):
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)
        reminders.stop()
        print("Goodbye.")
        break

    if not user_input:
        continue

    # Slash commands
    if user_input.startswith("/"):
        handle_command(user_input, agent, session)
        continue

    try:
        response = agent.ask_llm(user_input)
        content = response.get("message", {}).get("content", "")
        if content:
            print(f"\nLumi: {content}\n")

        # Auto-title from first message
        if not session["first_message_sent"]:
            session["title"] = make_title(user_input)
            session["first_message_sent"] = True

        # Auto-save after each exchange
        if session["first_message_sent"] and len(agent.messages) > 1:
            save_chat(session["chat_id"], session["title"], agent.messages)

        # Check storage milestones after each response
        milestone = agent.storage.check_milestone()
        if milestone:
            print(milestone)

        # Handle storage full
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
