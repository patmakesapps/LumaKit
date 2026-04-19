# Launcher Commands

LumaKit now has a unified launcher. These are the commands users should use most of the time.

## Recommended commands

Start or reuse the backend, then open the web UI:

```bash
python3 -m lumakit open
```

Check whether LumaKit is running:

```bash
python3 -m lumakit status
```

Stop the running backend:

```bash
python3 -m lumakit stop
```

Run the backend in the foreground without opening a browser:

```bash
python3 -m lumakit serve
```

## When to use each one

Use `python3 -m lumakit open` when:

- you want the normal user flow
- you want the web UI to open automatically
- you want LumaKit to start if it is not already running

Use `python3 -m lumakit serve` when:

- you want to watch logs in the terminal
- you are debugging startup issues
- you do not want the launcher to open a browser automatically

Use `python3 -m lumakit status` when:

- you want to confirm whether the backend is already up
- you want to check the web URL quickly

Use `python3 -m lumakit stop` when:

- you are done testing
- you want to ensure there is no background LumaKit process still running

## Clean testing flow

If you want to test manually without the boot-time service interfering:

```bash
sudo systemctl stop lumakit
python3 -m lumakit stop
python3 -m lumakit open
```

If something looks wrong, switch to foreground mode:

```bash
python3 -m lumakit serve
```

That will show startup and Telegram errors directly in the terminal.

## Always-on mode

If you want LumaKit to start automatically on boot, use the systemd service described in [autostart.md](autostart.md).

The service should run:

```bash
python3 -m lumakit serve
```

That is the always-on path.

## Surface-specific debug commands

These still exist for direct debugging, but they are no longer the recommended normal entrypoints:

```bash
python3 -m surfaces.web
python3 -m surfaces.telegram
python3 -m surfaces.cli
```
