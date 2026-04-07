# Contributing to LumaKit

## Git workflow

Use short-lived branches off `main` for day-to-day work.

- Create a new branch from `main` for each discrete task
- Use clear branch names such as `feat/add-read-file-tool`, `fix/web-fetch-timeout`, or `docs/readme-update`
- Keep the branch focused so the change is easy to review and test
- Merge completed work back into `main`
- If `main` moves forward while you are working, merge `main` into your branch instead of rebasing
- Avoid a long-lived `iteration` branch unless you are intentionally collecting several related changes before merging

Typical flow:

```bash
git checkout main
git pull
git checkout -b feat/my-change
```

Later, if needed:

```bash
git checkout feat/my-change
git merge main
```

## Adding a tool

LumaKit auto-loads tools from the `tools/` directory. To add a new tool correctly:

1. Put the module in the right namespace:
   - `tools/repo/` for repository and filesystem work
   - `tools/runtime/` for shell, Python, or system information
   - `tools/web/` for HTTP and search features
2. Export a function named `get_<tool_name>_tool()`.
3. Return a tool definition dict with `name`, `description`, `inputSchema`, and `execute`.
4. Implement the executor as a normal Python function that accepts a single `inputs` dict.
5. Return JSON-serializable data.

`tool_registry.py` walks `tools/**/*.py`, skips `__init__.py`, imports each module, and registers every callable named `get_*_tool`. The exposed tool name comes from the returned dict, not the filename.

## Required tool shape

```python
def get_example_tool():
    return {
        "name": "example",
        "description": "Short, clear description of what the tool does.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"}
            },
            "required": ["value"]
        },
        "execute": _example
    }


def _example(inputs):
    value = inputs["value"]
    return {
        "value": value,
        "length": len(value)
    }
```

## Repo-specific guidance

- Use `core.paths.resolve_repo_path()` for user-provided repo paths.
- Use `core.paths.get_display_path()` when returning repo paths.
- Reuse `core.diffs` helpers for file-editing tools so results stay consistent.
- Raise normal Python exceptions for invalid local input. `ToolRegistry.execute()` will wrap them into a structured error response.
- Keep tools narrow. One tool should do one job well instead of trying to cover a whole workflow.
- Preserve existing naming style: lowercase snake_case for both filenames and tool names.

## Suggested checklist

- Add the module under `tools/<namespace>/`.
- Expose `get_<tool_name>_tool()`.
- Keep `inputSchema` explicit and minimal.
- Make the return value easy for the model to consume.
- Handle bad input and edge cases predictably.
- Run `python main.py` and confirm the tool is listed in `=== Available Tools ===`.

## Notes

- A top-level file in `tools/` ending with `_tools.py` is currently skipped by the loader, so prefer `tools/repo/`, `tools/runtime/`, or `tools/web/`.
- If a tool depends on environment variables or external services, document that in the description and the issue body before implementation starts.
