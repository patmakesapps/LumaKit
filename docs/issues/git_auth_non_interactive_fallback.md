# Title

Fix git auth fallback for non-interactive environments

# Suggested labels

`bug`, `core`, `git`

# Body

## Problem

The `_run_git_interactive` fallback for authentication is clever, but it requires the CLI to be in an interactive terminal. If running in a non-interactive context (e.g. headless Raspberry Pi, systemd service, Docker container), this can cause the process to hang indefinitely waiting for user input.

## Proposed solution

- Detect whether the current environment is interactive before attempting interactive git auth.
- Provide a non-interactive fallback that fails gracefully with a clear error message.
- Support token-based auth as an alternative path.

## Implementation notes

- Check `sys.stdin.isatty()` before calling `_run_git_interactive`.
- If non-interactive, skip the interactive prompt and return a clear error explaining how to configure auth (e.g. set `GIT_TOKEN` env var or use a credential helper).
- Add support for `GIT_TOKEN` or `GITHUB_TOKEN` environment variables as a non-interactive auth method.
- Add a configurable timeout to interactive auth attempts so they don't hang forever.
- Log a warning when falling back from interactive to non-interactive auth.

## Acceptance criteria

- Git operations in non-interactive environments fail gracefully instead of hanging.
- A clear error message explains how to configure auth for headless use.
- Token-based auth works when the appropriate env var is set.
- Interactive auth still works as before when a TTY is available.
- A timeout prevents indefinite hangs.

## Out of scope

- SSH key management
- OAuth browser-based flows
- Multi-account git credential switching
