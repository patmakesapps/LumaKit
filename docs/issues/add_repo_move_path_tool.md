# Title

Add `move_path` repo tool

# Suggested labels

`tooling`, `repo-tool`, `good first issue`

# Body

## Problem

LumaKit can read, write, edit, delete, search, and diff files, but it cannot rename or move a path inside the repo. That forces multi-step workarounds for common refactors.

## Proposed tool

- Tool name: `move_path`
- Namespace: `repo`
- File: `tools/repo/move_path.py`

## Required structure

The module should export `get_move_path_tool()` and return the standard tool dict:

- `name`
- `description`
- `inputSchema`
- `execute`

The executor should accept a single `inputs` dict and return JSON-serializable output. Follow [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for the current loader contract.

## Suggested schema

- `source_path` (string, required)
- `destination_path` (string, required)
- `overwrite` (boolean, optional, default `false`)
- `confirm` (boolean, optional, default `false`)

## Implementation notes

- Use `core.paths.resolve_repo_path()` to resolve the source path.
- Use `core.paths.get_display_path()` when returning paths.
- Support moving both files and directories if practical. If directory support complicates the first version, limit the initial implementation to files and state that clearly in the tool description.
- If `confirm` is false, return a preview payload instead of mutating anything.
- If the destination already exists and `overwrite` is false, raise a clear error.
- Return enough data for the model to understand what changed, for example:
  - `source_path`
  - `destination_path`
  - `moved`
  - `overwrote`

## Acceptance criteria

- The new file lives at `tools/repo/move_path.py`
- `python main.py` prints `move_path` in the available tool list
- A no-confirm call previews the move without mutating the repo
- A confirmed call moves the file to the requested destination
- Error cases are explicit for missing source, existing destination, and invalid paths

## Out of scope

- Cross-device moves outside the repo
- Batch moves
- Git-aware rename detection logic
