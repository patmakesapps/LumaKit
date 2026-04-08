# Title

Make system prompt and model configurable

# Suggested labels

`enhancement`, `core`

# Body

## Problem

The system prompt is embedded directly in `Agent.__init__`. If you want to support different personalities, models, or use cases, you need to modify the source code. This limits flexibility for users who want to customize behavior.

## Proposed solution

- Extract the system prompt into a configurable file or parameter.
- Allow the model name to be passed as a configuration option.
- Support loading system prompts from external files (e.g. `prompts/default.txt`).

## Implementation notes

- Add a `config` dict or dataclass that `Agent.__init__` reads from.
- Support a `--system-prompt` CLI flag pointing to a prompt file.
- Support a `--model` CLI flag to override the default model.
- Fall back to the current embedded prompt if no custom prompt is provided.
- Consider a simple `config.json` or `.lumakit.yaml` for persistent configuration.

## Acceptance criteria

- The agent can be started with a custom system prompt file.
- The model can be changed via CLI flag or config.
- Default behavior is unchanged when no config is provided.
- Invalid prompt files or model names produce clear errors.

## Out of scope

- Hot-reloading prompts mid-conversation
- A prompt marketplace or gallery
- Multi-model routing within a single session
