from agent import Agent

# Create an agent
agent = Agent()

# Print available tools
print("=== Available Tools ===")
for tool in agent.get_available_tools():
    print(f"  - {tool['name']}")

# Print LumaKit chat 
print("\n=== LumaKit CLI ===")
print("Type 'exit' to quit.\n")

while True:
    user_input = input("You: ").strip()

    if user_input.lower() in ["exit", "quit"]:
        print("Goodbye.")
        break

    if not user_input:
        continue

    try:
        response = agent.ask_llm(user_input)
        print(f"\nAgent: {response}\n")
    except Exception as e:
        print(f"\nError: {e}\n")