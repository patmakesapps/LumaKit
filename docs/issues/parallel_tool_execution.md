# Title

Add parallel tool execution support

# Suggested labels

`enhancement`, `core`, `performance`

# Body

## Problem

Tools execute sequentially in the tool-call loop. Some operations (like reading multiple files or running independent checks) could be parallelized for significantly faster responses, especially when the model requests multiple tool calls at once.

## Proposed solution

- Detect when multiple tool calls in a single response are independent.
- Execute independent tool calls concurrently using `asyncio` or a thread pool.
- Return results in the same order as requested.

## Implementation notes

- Modify the tool execution loop in the agent to support concurrent dispatch.
- Use `asyncio.gather()` if tools are async, or `concurrent.futures.ThreadPoolExecutor` for sync tools.
- Add a `parallel_safe` flag to each tool definition so only safe tools run concurrently.
- Tools that modify state (e.g. `write_file`, `git_commit`) should remain sequential by default.
- Add a `--no-parallel` flag to disable this for debugging.

## Acceptance criteria

- Independent tool calls in a single response execute concurrently.
- Results are returned in the correct order.
- State-modifying tools still execute sequentially unless explicitly marked safe.
- No race conditions in file access or git operations.
- Performance improvement is measurable on multi-tool responses.

## Out of scope

- Cross-response parallelism
- Distributed execution across multiple machines
- Automatic dependency analysis between tool calls
