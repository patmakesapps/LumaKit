import sys
from pathlib import Path

import pytest

# Make the repo root importable regardless of how pytest is invoked.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_task_traces(tmp_path, monkeypatch):
    """Task runner tests use throwaway databases whose task ids restart at 1,
    so traces must never land in the real ~/.lumakit/traces/."""
    from core import task_trace

    traces = tmp_path / "traces"
    traces.mkdir(exist_ok=True)
    monkeypatch.setattr(task_trace, "traces_dir", lambda: traces)
    yield traces


@pytest.fixture()
def workspace(tmp_path):
    """A temp workspace set as the active workspace root."""
    from core.paths import set_workspace_root

    ws = tmp_path / "ws"
    ws.mkdir()
    set_workspace_root(ws)
    return ws
