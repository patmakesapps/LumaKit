import json
import os

from ollama_client import OllamaClient
from tool_registry import ToolRegistry


class Agent:
    MAX_TOOL_ROUNDS = 5
    HISTORY_TURNS = 5

    def __init__(self, verbose=False):
        self.verbose = verbose

        # Initialize the tool registry and auto-load all tools from the tools folder
        self.registry = ToolRegistry()
        self.registry.load_tools_from_folder()

        # Initialize Ollama Client
        self.ollama = OllamaClient()
        self.model = os.getenv("OLLAMA_MODEL", "gemma4:e4b")

        # Conversation history
        self.messages = [
            {
                "role": "system",
                "content": (
                    "You are Lumi, a helpful agent with access to tools for working with files and code. "
                    "When the user asks you to create, edit, read, or find files, use the appropriate tool immediately. "
                    "Do not ask for clarification if you can make a reasonable decision yourself. "
                ),
            }
        ]

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
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["inputSchema"],
                    },
                }
            )
        return result

    def _trim_history(self):
        # Keep system prompt + last N turns (each turn = user msg + assistant msg)
        max_msgs = 1 + self.HISTORY_TURNS * 2
        if len(self.messages) > max_msgs:
            self.messages = [self.messages[0]] + self.messages[
                -self.HISTORY_TURNS * 2 :
            ]

    def ask_llm(self, prompt):
        self.messages.append({"role": "user", "content": prompt})
        self._trim_history()

        tools = self.get_tools_for_llm()

        for round_num in range(self.MAX_TOOL_ROUNDS + 1):
            response = self.ollama.chat(
                model=self.model, messages=self.messages, tools=tools, stream=False
            )

            message = response.get("message", {})
            tool_calls = message.get("tool_calls", [])

            if self.verbose:
                label = f"round {round_num}" if tool_calls else "final"
                print(f"  [{label}] {json.dumps(message, default=str)[:300]}")

            self.messages.append(message)

            if not tool_calls:
                return response

            for tool_call in tool_calls:
                function_data = tool_call.get("function", {})
                tool_name = function_data.get("name")
                tool_inputs = function_data.get("arguments", {})
                tool_inputs = self._resolve_content(tool_name, tool_inputs)

                if self.verbose:
                    print(
                        f"  [tool call] {tool_name}({json.dumps(tool_inputs, indent=2)[:200]})"
                    )

                tool_result = self.execute_tool(tool_name, tool_inputs)

                if self.verbose:
                    print(f"  [tool result] {json.dumps(tool_result)[:200]}")

                self.messages.append(
                    {
                        "role": "tool",
                        "name": tool_name,
                        "content": json.dumps(tool_result),
                    }
                )

        return response

    def _resolve_content(self, tool_name, inputs):
        """If a tool has needs_content_generation and no content was provided,
        make a separate LLM call to generate the file content."""
        tool = self.registry.get(tool_name)
        if not tool or not tool.get("needs_content_generation"):
            return inputs

        if inputs.get("content"):
            return inputs

        path = inputs.get("path", "file")
        language = inputs.get("language", "")
        hint = f" in {language}" if language else ""

        gen_prompt = f"Generate starter code{hint} for a file named '{path}'. Output ONLY the raw file content. No markdown fences, no explanation."

        if self.verbose:
            print(f"  [generating content] {gen_prompt}")

        response = self.ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": gen_prompt}],
            stream=False,
        )

        generated = response.get("message", {}).get("content", "").strip()

        # Strip markdown fences if the model included them anyway
        if generated.startswith("```"):
            lines = generated.split("\n")
            lines = lines[1:]  # drop opening fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            generated = "\n".join(lines)

        if generated:
            inputs = {**inputs, "content": generated}
            inputs.pop("language", None)

        return inputs

    def run_task(self, task_description):
        # Execute a task by preparing the task description and available tools
        # This will send the task and tools to an LLM for decision-making
        print(f"\n=== Task: {task_description} ===")
        print(f"Available tools: {[t['name'] for t in self.get_available_tools()]}")
        return {
            "task": task_description,
            "tools_available": self.get_available_tools(),
            "tools_for_llm": self.get_tools_for_llm(),
        }
