# Toolbox...

def get_read_file_tool():
    #Read File Tool
    return {
        'name': 'read_file',
        'description': 'Reads the content of a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'}
            },
            'required': ['path']
        },
        'execute': lambda inputs: open(inputs['path'], 'r').read()
    }

def get_write_file_tool():
    # Write File Tool
    return {
        'name': 'write_file',
        'description': 'Writes content to a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string'}
            },
            'required': ['path', 'content']
        },
        'execute': lambda inputs: open(inputs['path'], 'w').write(inputs['content']) or f"Wrote to {inputs['path']}"
    }

def _edit_file_execute(inputs):
    # Edit File Tool
    with open(inputs['path'], 'r') as f:
        content = f.read()
    
    new_content = content.replace(inputs['find'], inputs['replace'])
    
    with open(inputs['path'], 'w') as f:
        f.write(new_content)
    
    return f"Edited {inputs['path']}"

def get_edit_file_tool():
    return {
        'name': 'edit_file',
        'description': 'Finds and replaces text in a file',
        'inputSchema': {
            'properties': {
                'path': {'type': 'string'},
                'find': {'type': 'string'},
                'replace': {'type': 'string'}
            },
            'required': ['path', 'find', 'replace']
        },
        'execute': _edit_file_execute
    }