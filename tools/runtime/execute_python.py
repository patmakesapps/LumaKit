import os
import subprocess
import tempfile


def get_execute_python_tool():
    return {
        'name': 'execute_python',
        'description': 'Executes Python code in a sandboxed environment and returns the output',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string'},
                'reason': {'type': 'string', 'description': 'Brief explanation of WHY this code needs to run and what it accomplishes'}
            },
            'required': ['code', 'reason']
        },
        'execute': _execute_python
    }


def _execute_python(inputs):
    code = inputs['code']

    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as file_handle:
            file_handle.write(code)
            temp_file = file_handle.name

        result = subprocess.run(
            ['python', temp_file],
            capture_output=True,
            text=True,
            timeout=10
        )

        os.unlink(temp_file)

        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
            'success': result.returncode == 0
        }
    except subprocess.TimeoutExpired:
        return {'error': 'Code execution timed out (10 second limit)', 'success': False}
    except Exception as error:
        return {'error': str(error), 'success': False}
