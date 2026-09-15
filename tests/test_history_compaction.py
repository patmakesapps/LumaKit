"""D-1/D-3: tool-result compaction rules."""

import json

from core.history_compaction import (
    TOOL_HISTORY_MAX_CHARS,
    compact_tool_message_content,
    compact_tool_result_for_history,
)


def test_small_results_pass_through():
    result = {"success": True, "data": {"content": "hello"}}
    out = compact_tool_result_for_history("read_file", result)
    assert json.loads(out) == result


def test_oversized_content_truncated():
    result = {"success": True, "data": {"content": "x" * 50_000}}
    out = compact_tool_result_for_history("read_file", result)
    assert len(out) <= TOOL_HISTORY_MAX_CHARS + 100
    assert "truncated" in out


def test_budget_parameterized():
    result = {"success": True, "data": {"content": "y" * 5_000}}
    tight = compact_tool_result_for_history("read_file", result, max_chars=1000)
    loose = compact_tool_result_for_history("read_file", result, max_chars=100_000)
    assert len(tight) <= 1100
    assert len(loose) > len(tight)  # same rules, different budget


def test_error_preserved_in_fallback():
    result = {"success": False, "error": "boom " * 2000, "data": {"content": "z" * 20_000}}
    out = compact_tool_result_for_history("execute_shell", result)
    assert "boom" in out
    assert len(out) <= TOOL_HISTORY_MAX_CHARS + 100


def test_recompact_existing_message():
    original = json.dumps({"success": True, "data": {"content": "w" * 50_000}})
    out = compact_tool_message_content("read_file", original)
    assert len(out) <= TOOL_HISTORY_MAX_CHARS + 100


def test_non_json_content_truncated_not_crashed():
    out = compact_tool_message_content("read_file", "plain text " * 5000)
    assert len(out) <= TOOL_HISTORY_MAX_CHARS + 100


def _pytest_like_output(failing_lines: int = 600) -> str:
    head = "============ test session starts ============\ncollected 5 items\n\n"
    body = "\n".join(f"tests/test_pricing.py:{i}: in test_total  assert 109 == 108" for i in range(failing_lines))
    tail = (
        "\n=========== short test summary info ===========\n"
        "FAILED tests/test_pricing.py::test_total_with_tax - AssertionError: assert 109 == 108\n"
        "========= 1 failed, 4 passed in 0.31s =========\n"
    )
    return head + body + tail


def test_command_output_keeps_the_verdict_at_the_tail():
    """The failing test name and pass/fail summary live at the END of pytest
    output. Head-only trimming used to drop them, letting the model guess."""
    result = {
        "success": False,
        "data": {
            "command": "python -m pytest -q",
            "cwd": "demo",
            "returncode": 1,
            "stdout": _pytest_like_output(),
            "stderr": "",
        },
    }
    out = compact_tool_result_for_history("run_command", result, max_chars=3000)
    assert len(out) <= 3100
    parsed = json.loads(out)  # never chopped mid-JSON
    text = json.dumps(parsed)
    assert "FAILED tests/test_pricing.py::test_total_with_tax" in text
    assert "1 failed, 4 passed" in text
    assert "test session starts" in text  # head kept too
    assert "[truncated" in text  # and the cut is visible
    data = parsed["data"]
    assert data["returncode"] == 1
    assert data["command"] == "python -m pytest -q"


def test_file_content_keeps_head_first():
    lines = "\n".join(f"line {i}" for i in range(2000))
    result = {"success": True, "data": {"path": "big.py", "content": lines}}
    out = compact_tool_result_for_history("read_file", result, max_chars=3000)
    parsed = json.loads(out)
    text = parsed["data"].get("content") or parsed["data"].get("content_preview")
    assert text.startswith("line 0\nline 1")
    assert "line 1999" in text


def test_fallback_is_valid_json_under_a_tight_budget():
    result = {
        "success": False,
        "error": "boom " * 3000,
        "data": {"stdout": "x" * 50_000, "stderr": "y" * 50_000, "returncode": 2},
    }
    for budget in (600, 1200, 2500):
        out = compact_tool_result_for_history("run_command", result, max_chars=budget)
        assert len(out) <= budget
        parsed = json.loads(out)
        assert parsed["success"] is False
        assert parsed["truncated"] is True
        assert "boom" in parsed["error"]


def test_tool_name_is_recorded_in_fallback():
    result = {"success": True, "data": {"content": "z" * 20_000}}
    out = compact_tool_result_for_history("read_file", result, max_chars=1000)
    assert json.loads(out)["tool"] == "read_file"
