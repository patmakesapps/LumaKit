import os
import importlib

# Tool Registry...
class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        self.tools[tool['name']] = tool    

    def get(self, name):
        return self.tools.get(name)
    
    def list(self):
        return [
            {
                'name': tool['name'],
                'description': tool ['description']
            }
            for tool in self.tools.values()
        ]
    
    def validate_inputs(self, inputs, schema):
        if 'required' in schema:
            for field in schema['required']:
                if field not in inputs:
                    raise ValueError(f"Missing required input: {field}")
                
    def execute(self, name, inputs=None):
        if inputs is None:
            inputs = {}

        tool = self.get(name)
        if tool is None:
            return {'success': False, 'error': f"Tool not found: {name}"}

        try:
            self.validate_inputs(inputs, tool['inputSchema'])
            result = tool['execute'](inputs)
            return {'success': True, 'data': result}
        except Exception as e:
            return {'success': False, 'error': str(e), 'toolName': name}    

    def load_tools_from_folder(self, folder_path='tools'):
        if not os.path.exists(folder_path):
            print(f"Tools folder not found: {folder_path}")
            return

        for filename in os.listdir(folder_path):
            if filename.endswith('_tools.py') and filename != '__init__.py':
                module_name = filename[:-3]
                module = importlib.import_module(f'tools.{module_name}')

                for attr_name in dir(module):
                    if attr_name.startswith('get_') and attr_name.endswith('_tool'):
                       tool_func = getattr(module, attr_name)
                       tool = tool_func()
                       self.register(tool)
                       print(f"Loaded tool: {tool['name']}")




# Create a registry
registry = ToolRegistry()

# Auto-load tools from the tools folder
registry.load_tools_from_folder()

# Test it
print("\n=== Available Tools ===")
for tool in registry.list():
    print(f"  - {tool['name']}: {tool['description']}")

print("\n=== Test: Write File ===")
result = registry.execute('write_file', {'path': 'test_output.txt', 'content': 'Hello, this is a test file.'})
print(result)

print("\n=== Test: Read File ===")
result = registry.execute('read_file', {'path': 'test_output.txt'})
print(f"Success: {result['success']}")
print(f"Content: {result['data']}")

print("\n=== Test: Edit File ===")
result = registry.execute('edit_file', {'path': 'test_output.txt', 'find': 'test', 'replace': 'EDITED'})
print(result)

print("\n=== Test: Read Edited File ===")
result = registry.execute('read_file', {'path': 'test_output.txt'})
print(f"Content: {result['data']}")