---
name: Add Tool
about: Propose a new LumaKit tool for contributors to implement
title: "Add <tool_name> tool"
labels: tooling
---

## Problem

Describe the capability gap this tool should close.

## Proposed tool

- Tool name: `<tool_name>`
- Namespace: `repo` / `runtime` / `web`
- File: `tools/<namespace>/<tool_name>.py`

## Required structure

- Export `get_<tool_name>_tool()`
- Return a dict with:
  - `name`
  - `description`
  - `inputSchema`
  - `execute`
- Implement the executor as `_tool_name(inputs)` or similar
- Return JSON-serializable output

See [`CONTRIBUTING.md`](/CONTRIBUTING.md) for the current loader contract and repo-specific rules.

## Acceptance criteria

- New module lives under the correct `tools/<namespace>/` folder
- `python main.py` lists the tool at startup
- The schema is specific enough for the model to call correctly
- Error handling is predictable and structured
- Any repo path inputs use the helpers in `core.paths`

## Notes

Include env vars, security constraints, response shape expectations, and any out-of-scope behavior.
