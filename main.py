from agent import Agent
import json

# Create an agent
agent = Agent()

# Run a task
result = agent.run_task("Search the web for information")

# Print the tools available to the LLM
print("\n=== Tools for LLM ===")
print(json.dumps(agent.get_tools_for_llm(), indent=2))

# Test the web search tool
print("\n=== Tools for LLM===")
search_result = agent.execute_tool('web_search', {'query': 'York Hoist'})
print(json.dumps(search_result, indent=2))