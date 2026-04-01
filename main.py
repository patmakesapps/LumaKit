from agent import Agent
import json

# Create an agent
agent = Agent()

# Run a task
result = agent.run_task("Read a file and tell me what's in it")

# Print the tools available to the LLM
print("\n=== Tools for LLM ===")
print(json.dumps(agent.get_tools_for_llm(), indent=2))