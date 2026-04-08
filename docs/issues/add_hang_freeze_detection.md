# Title

Add hang/freeze detection and graceful recovery to agent loop

# Suggested labels

`bug`, `resilience`, `agent-core`

# Body

## Problem

When Ollama hangs or stops responding mid-request, the CLI freezes indefinitely with no way out except Ctrl+C, which crashes the process. There is also no detection for tool call loops where the LLM repeats the same call with identical arguments.

## Current behavior

- `ollama_client.py` has a 120s request timeout, but if the connection stays open with no data, the CLI blocks silently.
- The `ask_llm` loop in `agent.py` runs up to `MAX_TOOL_ROUNDS` (5) rounds with no overall time limit.
- If the LLM calls the same tool with the same arguments repeatedly, the loop continues until rounds are exhausted.
- On any unrecoverable failure, the CLI crashes rather than returning control to the user.

## Proposed changes

### 1. Per-round timeout wrapper

Wrap `ollama.chat()` in a `concurrent.futures.ThreadPoolExecutor` with a configurable deadline. If the call exceeds the timeout, cancel it and return a graceful error message to the user instead of hanging.

### 2. Tool loop detection

Track consecutive tool calls. If the LLM calls the same tool with the same arguments twice in a row, break out of the loop and inform the user that the agent appears stuck.

### 3. Overall `ask_llm` timeout

Add a wall-clock limit across all rounds (e.g. 5 minutes). If exceeded, bail out with a message rather than spinning indefinitely.

## Affected files

- `agent.py` — main loop, timeout wrapper, loop detection
- `ollama_client.py` — may need adjustments to support cancellation

## Acceptance criteria

- A hanging Ollama request does not freeze the CLI indefinitely
- Repeated identical tool calls are detected and broken out of
- The user always gets a readable error message, never a raw crash
- Normal operation (fast responses, varied tool calls) is unaffected
