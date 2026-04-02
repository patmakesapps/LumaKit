import subprocess
import tempfile
import os

def get_execute_python_tool():
    return {
        'name': 'execute_python',
        'description': 'Executes Python code in a sandboxed environment and returns the output',
        'inputSchema': {
            'properties': {
                'code': {'type': 'string'}
            },
            'required': ['code']
        },
        'execute': _execute_python
    }

def _execute_python(inputs):
    code = inputs['code']
    
    try:
        # Create a temporary file for the code
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(code)
            temp_file = f.name
        
        # Execute the code in a subprocess (sandboxed)
        result = subprocess.run(
            ['python', temp_file],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        # Clean up
        os.unlink(temp_file)
        
        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
            'success': result.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {'error': 'Code execution timed out (10 second limit)', 'success': False}
    except Exception as e:
        return {'error': str(e), 'success': False}