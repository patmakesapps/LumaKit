# Tool Registry...

import importlib
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

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
        base_path = Path(folder_path)

        if not base_path.exists():
            print(f"Tools folder not found: {folder_path}")
            return

        search_root = base_path.parent if base_path.parent != Path('') else Path('.')

        for module_path in sorted(base_path.rglob('*.py')):
            if module_path.name == '__init__.py':
                continue
            if module_path.parent == base_path and module_path.name.endswith('_tools.py'):
                continue

            import_path = ".".join(module_path.with_suffix('').relative_to(search_root).parts)
            module = importlib.import_module(import_path)

            for attr_name in dir(module):
                if attr_name.startswith('get_') and attr_name.endswith('_tool'):
                    tool_func = getattr(module, attr_name)
                    tool = tool_func()
                    self.register(tool)



