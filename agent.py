from tool_registry import ToolRegistry
from ollama_client import OllamaClient

class Agent:
    def __init__(self):
         # Initialize the tool registry and auto-load all tools from the tools folder
        self.registry = ToolRegistry()
        self.registry.load_tools_from_folder()

        # Initialize Ollama Client
        self.ollama = OllamaClient()
        self.model = "gemma4:e4b"

    def get_available_tools(self):
        # Return a list of all available tools with their name and description
        return self.registry.list()

    def execute_tool(self, tool_name, inputs):
        # Execute a specific tool by name with the given inputs
        # Returns a dict with success status and either data or error
        return self.registry.execute(tool_name, inputs)   

    def get_tools_for_llm(self):
        # Format tools into a schema that LLMs (Claude, OpenAI, etc.) can understand
        # This is used to tell the LLM what tools are available and how to call them
        result = []
        for tool_name in self.registry.tools.keys():
            tool = self.registry.get(tool_name)
            result.append({
                'name': tool['name'],
                'description': tool['description'],
                'input_schema': tool['inputSchema']
            })
        return result
    
    def ask_llm(self, prompt):
        response = self.ollama.chat(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            stream=False
        )

        return response.get("message", {}).get("content", "")
    
    def run_task(self, task_description):
        # Execute a task by preparing the task description and available tools
        # Tthis will send the task and tools to an LLM for decision-making
        print(f"\n=== Task: {task_description} ===")
        print(f"Available tools: {[t['name'] for t in self.get_available_tools()]}")
        return {
            'task': task_description,
            'tools_available': self.get_available_tools(),
            'tools_for_llm': self.get_tools_for_llm()
        }