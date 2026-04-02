from agent import Agent
import json

# Create an agent
agent = Agent()

# Print available tools
print("=== Available Tools ===")
for tool in agent.get_available_tools():
    print(f"  - {tool['name']}")

# Test Python execution
print("\n=== Test: Execute Python ===")
python_code = """
import math

result = math.factorial(5)
print(f"5! = {result}")

data = [1, 2, 3, 4, 5]
print(f"Sum: {sum(data)}")
print(f"Average: {sum(data) / len(data)}")
"""

exec_result = agent.execute_tool('execute_python', {'code': python_code})
print("Output:")
print(exec_result['data']['stdout'])
if exec_result['data']['stderr']:
    print("Errors:")
    print(exec_result['data']['stderr'])
print(f"Success: {exec_result['data']['success']}")