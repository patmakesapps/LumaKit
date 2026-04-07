import sys

from agent import Agent

# Create an agent
verbose = "--verbose" in sys.argv
agent = Agent(verbose=verbose)

# Print available tools
print("=== Available Tools ===")
for tool in agent.get_available_tools():
    print(f"  - {tool['name']}")

print("\n=== LumaKit CLI ===")
print("Type 'exit' to quit.\n")

while True:
    try:
        user_input = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye.")
        break

    if user_input.lower() in ["exit", "quit"]:
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