import os
import subprocess
import tempfile
import time

from core.interrupts import OperationInterrupted, raise_if_interrupted


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
    temp_file = None

    try:
        raise_if_interrupted()

        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as file_handle:
            file_handle.write(code)
            temp_file = file_handle.name

        proc = subprocess.Popen(
            ['python', temp_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            start_new_session=(os.name != 'nt'),
        )
        deadline = time.monotonic() + 10

        while True:
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                return {
                    'stdout': stdout,
                    'stderr': stderr,
                    'returncode': proc.returncode,
                    'success': proc.returncode == 0
                }
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    _terminate_process(proc)
                    raise subprocess.TimeoutExpired('python', 10)
                try:
                    raise_if_interrupted()
                except OperationInterrupted:
                    _terminate_process(proc)
                    raise OperationInterrupted('Python execution interrupted by /stop.')
    except subprocess.TimeoutExpired:
        return {'error': 'Code execution timed out (10 second limit)', 'success': False}
    except OperationInterrupted:
        raise
    except Exception as error:
        return {'error': str(error), 'success': False}
    finally:
        if temp_file:
            try:
                os.unlink(temp_file)
            except OSError:
                pass


def _terminate_process(proc):
    if proc.poll() is not None:
        return

    try:
        proc.terminate()
        proc.wait(timeout=1)
    except Exception:
        pass

    if proc.poll() is not None:
        return

    try:
        proc.kill()
    except Exception:
        pass

    try:
        proc.communicate(timeout=1)
    except Exception:
        pass
