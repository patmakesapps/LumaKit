import os
import platform
import subprocess


def get_execute_shell_tool():
    return {
        'name': 'execute_shell',
        'description': 'Executes shell commands and returns the output',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {'type': 'string'},
                'timeout': {'type': 'number', 'description': 'Timeout in seconds (default 600)'},
                'reason': {'type': 'string', 'description': 'Brief explanation of WHY this command needs to run and what it accomplishes'}
            },
            'required': ['command', 'reason']
        },
        'execute': _execute_shell
    }


def _execute_shell(inputs):
    command = inputs['command']
    timeout = inputs.get('timeout', 600)

    try:
        shell = platform.system() == 'Windows'

        # Run commands from the project directory, not system root
        cwd = os.getcwd()

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=shell,
            cwd=cwd
        )

        return {
            'stdout': result.stdout,
            'stderr': result.stderr,
            'returncode': result.returncode,
            'success': result.returncode == 0,
            'command': command
        }
    except subprocess.TimeoutExpired:
        return {'error': f'Command timed out ({timeout} second limit)', 'success': False, 'command': command}
    except Exception as error:
        return {'error': str(error), 'success': False, 'command': command}
