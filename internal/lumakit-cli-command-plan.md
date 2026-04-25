# LumaKit CLI Command Plan

## Goal

Add a user-facing `lumakit cli` command so users can start the terminal chat
experience without knowing the internal `python -m surfaces.cli` module path.

## Current State

- Web users can start LumaKit through `lumakit open` or the desktop shortcut.
- The backend can be started with `lumakit serve`.
- The terminal chat surface exists at `python3 -m surfaces.cli`.
- There is no top-level `lumakit cli` command yet.

## Desired Behavior

- `python3 -m lumakit cli` starts the existing CLI surface.
- If package console scripts are installed, `lumakit cli` should do the same.
- The command should share the same env loading behavior as the rest of
  `lumakit.py`, including `~/.lumakit/config.env` before repo `.env`.
- The CLI should keep using the existing chat persistence and service startup
  behavior unless a separate refactor is needed.

## Implementation Notes

- Add a `cli` subcommand to the argparse command table in `lumakit.py`.
- Route that command to `surfaces.cli.main()`.
- Avoid importing `surfaces.cli` before env files are loaded.
- Confirm whether starting the CLI should also start Telegram/web-adjacent
  background workers. The existing `surfaces.cli.main()` starts
  `LumaKitService`, so this likely already works.
- Update README quickstart and command reference after implementation.

## Validation

- Run `python3 -m lumakit cli` from the repo.
- Confirm normal chat works.
- Confirm `/help`, `/status`, and `/config` still work.
- Confirm reminders/tasks still start from the CLI path.
- Confirm `python3 -m surfaces.cli` still works or decide if it becomes an
  internal-only fallback.
