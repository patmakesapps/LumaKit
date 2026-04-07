import sys

from agent import Agent
from core.reminder_checker import ReminderChecker

# Create an agent
verbose = "--verbose" in sys.argv
agent = Agent(verbose=verbose)

# Start the reminder checker (polls every 60 seconds)
reminders = ReminderChecker(interval=30)
reminders.start()

print("\n=== LumaKit CLI ===")
print("Type 'exit' to quit.\n")

while True:
    try:
        user_input = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        reminders.stop()
        print("\nGoodbye.")
        break

    if user_input.lower() in ["exit", "quit"]:
        reminders.stop()
        print("Goodbye.")
        break

    if not user_input:
        continue

    try:
        response = agent.ask_llm(user_input)
        content = response.get("message", {}).get("content", "")
        if content:
            print(f"\nLumi: {content}\n")
    except Exception as e:
        print(f"\nError: {e}\n")