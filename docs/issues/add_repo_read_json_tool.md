# Title

Add `read_json` repo tool

# Suggested labels

`tooling`, `repo-tool`, `good first issue`

# Body

## Problem

LumaKit can read raw file contents, but it has no structured JSON reader. A dedicated JSON tool would make it easier for the model to inspect package manifests, configs, lockfiles, and structured repo metadata without reparsing text every time.

## Proposed tool

- Tool name: `read_json`
- Namespace: `repo`
- File: `tools/repo/read_json.py`

## Required structure

The module should export `get_read_json_tool()` and return the standard tool dict with `name`, `description`, `inputSchema`, and `execute`. Follow [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the current loader contract.

## Suggested schema

- `path` (string, required)
- `max_chars` (number, optional)

## Implementation notes

- Resolve the target with `core.paths.resolve_repo_path()`.
- Return the display path with `core.paths.get_display_path()`.
- Parse the file with the standard library `json` module.
- Return a structured payload such as:
  - `path`
  - `data`
  - `keys`
  - `truncated`
- If `max_chars` is provided, truncate oversized serialized output and mark it clearly.
- Raise clear errors for invalid JSON, missing files, and non-file paths.

## Acceptance criteria

- The new file lives at `tools/repo/read_json.py`
- `python main.py` prints `read_json` in the available tool list
- Valid JSON files return parsed data
- Invalid JSON produces a clear error
- The tool response is small and predictable enough for model use

## Out of scope

- JSON5 support
- YAML/TOML parsing
- In-place JSON editing
