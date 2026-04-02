from agent import Agent
import json

# Create an agent
agent = Agent()

# Print available tools
print("=== Available Tools ===")
for tool in agent.get_available_tools():
    print(f"  - {tool['name']}")

# Test shell execution with default timeout
print("\n=== Test: Shell Command (default timeout) ===")
shell_result = agent.execute_tool('execute_shell', {'command': 'python --version'})
print("Output:")
print(shell_result['data']['stdout'])
print(f"Success: {shell_result['data']['success']}")

# Test with custom timeout
print("\n=== Test: Shell Command (custom timeout) ===")
shell_result = agent.execute_tool('execute_shell', {
    'command': 'dir' if agent.registry.tools else 'ls',
    'timeout': 120
})
print(shell_result['data']['stdout'][:300])
print(f"Success: {shell_result['data']['success']}")