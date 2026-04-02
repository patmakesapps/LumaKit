from agent import Agent
import json

# Create an agent
agent = Agent()

# Print available tools
print("=== Available Tools ===")
for tool in agent.get_available_tools():
    print(f"  - {tool['name']}")

# Test shell execution
print("\n=== Test: Execute Shell Command ===")
shell_result = agent.execute_tool('execute_shell', {'command': 'python --version'})
print("Output:")
print(shell_result['data']['stdout'])
if shell_result['data']['stderr']:
    print("Errors:")
    print(shell_result['data']['stderr'])
print(f"Success: {shell_result['data']['success']}")

# Test another command
print("\n=== Test: List directory ===")
shell_result = agent.execute_tool('execute_shell', {'command': 'dir' if agent.registry.tools else 'ls'})
print(shell_result['data']['stdout'][:500])