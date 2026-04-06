import json
import os

from ollama_client import OllamaClient
from tool_registry import ToolRegistry


class Agent:
    def __init__(self):
        # Initialize the tool registry and auto-load all tools from the tools folder
        self.registry = ToolRegistry()
        self.registry.load_tools_from_folder()

        # Initialize Ollama Client
        self.ollama = OllamaClient()
        self.model = os.getenv("OLLAMA_MODEL", "gemma4:e4b")

    def get_available_tools(self):
        # Return a list of all available tools with their name and description
        return self.registry.list()

    def execute_tool(self, tool_name, inputs):
        # Execute a specific tool by name with the given inputs
        # Returns a dict with success status and either data or error
        return self.registry.execute(tool_name, inputs)

    def get_tools_for_llm(self):
        # Format tools into Ollama's expected tool schema
        result = []
        for tool_name in self.registry.tools.keys():
            tool = self.registry.get(tool_name)
            result.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["inputSchema"]
                }
            })
        return result

    def ask_llm(self, prompt):
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant with access to external tools. "
                    "Use tools when they help you answer accurately or complete a task. "
                    "If a tool is not needed, respond directly."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ]

        response = self.ollama.chat(
            model=self.model,
            messages=messages,
            tools=self.get_tools_for_llm(),
            stream=False
        )

        message = response.get("message", {})
        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            return response

        messages.append(message)

        for tool_call in tool_calls:
            function_data = tool_call.get("function", {})
            tool_name = function_data.get("name")
            tool_inputs = function_data.get("arguments", {})

            tool_result = self.execute_tool(tool_name, tool_inputs)

            messages.append({
                "role": "tool",
                "name": tool_name,
                "content": json.dumps(tool_result)
            })

        final_response = self.ollama.chat(
            model=self.model,
            messages=messages,
            tools=self.get_tools_for_llm(),
            stream=False
        )

        return final_response

    def run_task(self, task_description):
        # Execute a task by preparing the task description and available tools
        # This will send the task and tools to an LLM for decision-making
        print(f"\n=== Task: {task_description} ===")
        print(f"Available tools: {[t['name'] for t in self.get_available_tools()]}")
        return {
            "task": task_description,
            "tools_available": self.get_available_tools(),
            "tools_for_llm": self.get_tools_for_llm()
        }
