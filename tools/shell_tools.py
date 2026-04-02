import subprocess
import platform

def get_execute_shell_tool():
    return {
        'name': 'execute_shell',
        'description': 'Executes shell commands and returns the output',
        'inputSchema': {
            'properties': {
                'command': {'type': 'string'}
            },
            'required': ['command']
        },
        'execute': _execute_shell

    }

def _execute_shell(inputs):
    command = inputs['command']

    try:
        # Use shell=True on Windows, shell=False on Unix
        shell = platform.system() == 'Windows'

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            shell=shell
              )
        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
            'success': result.returncode == 0,
            'command': command

        }

    except subprocess.TimeoutExpired:
        return {'error': 'Command timed out (30 second limit)', 'success': False, 'command': command}
    except Exception as e:
        return {'error': str(e), 'success': False, 'command': command}
    