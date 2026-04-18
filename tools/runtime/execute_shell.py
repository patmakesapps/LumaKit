import os
import subprocess
import signal
import time

from core.interrupts import OperationInterrupted, raise_if_interrupted


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
        # Run commands from the project directory, not system root
        cwd = os.getcwd()
        raise_if_interrupted()

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            shell=True,
            cwd=cwd,
            start_new_session=(os.name != 'nt'),
        )

        deadline = time.monotonic() + float(timeout)

        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                return {
                    'stdout': stdout,
                    'stderr': stderr,
                    'returncode': proc.returncode,
                    'success': proc.returncode == 0,
                    'command': command,
                }
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate_process(proc)
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    raise_if_interrupted()
                except OperationInterrupted:
                    _terminate_process(proc)
                    raise OperationInterrupted('Command interrupted by /stop.')
    except subprocess.TimeoutExpired:
        return {'error': f'Command timed out ({timeout} second limit)', 'success': False, 'command': command}
    except OperationInterrupted:
        raise
    except Exception as error:
        return {'error': str(error), 'success': False, 'command': command}


def _terminate_process(proc):
    if proc.poll() is not None:
        return

    try:
        if os.name != 'nt':
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        pass

    if proc.poll() is not None:
        return

    try:
        if os.name != 'nt':
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass

    try:
        proc.communicate(timeout=1)
    except Exception:
        pass
