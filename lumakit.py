from __future__ import annotations

import argparse
import atexit
import getpass
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
os.chdir(REPO_ROOT)

# Load env files before importing any surface/core module that reads env
# vars at import time (telegram_state, etc). User overrides win over repo .env.
from dotenv import load_dotenv  # noqa: E402

_user_env = Path.home() / ".lumakit" / "config.env"
if _user_env.exists():
    load_dotenv(_user_env)
load_dotenv(REPO_ROOT / ".env")

import uvicorn  # noqa: E402
from PIL import Image  # noqa: E402

from core.paths import get_data_dir  # noqa: E402
from core.service import LumaKitService  # noqa: E402
from surfaces import telegram as telegram_surface  # noqa: E402
from surfaces import web as web_surface  # noqa: E402

RUNTIME_STATE_FILE = get_data_dir() / "lumakit-runtime.json"
DAEMON_LOG_FILE = get_data_dir() / "lumakit-daemon.log"
DEFAULT_SERVICE_PATH = REPO_ROOT / "lumakit.service"
SHORTCUT_NAME = "LumaKit"
LINUX_SHORTCUT_ID = "lumakit.desktop"


def _runtime_state() -> dict | None:
    try:
        return json.loads(RUNTIME_STATE_FILE.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _write_runtime_state() -> None:
    payload = {
        "pid": os.getpid(),
        "port": web_surface.PORT,
        "url": web_surface.WEB_URL,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": str(REPO_ROOT),
    }
    RUNTIME_STATE_FILE.write_text(json.dumps(payload, indent=2))


def _clear_runtime_state(*, pid: int | None = None) -> None:
    state = _runtime_state()
    if pid is not None and state and state.get("pid") != pid:
        return
    try:
        RUNTIME_STATE_FILE.unlink()
    except (FileNotFoundError, OSError):
        pass


def _pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _pid_running_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_running_windows(pid: int) -> bool:
    try:
        import ctypes
    except Exception:
        return False

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False

    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _port_is_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _resolve_port(preferred: int, attempts: int = 20) -> int:
    """Return `preferred` if free, otherwise the next free port in range.

    Handles the common case where another process (e.g. NTKDaemon / Nahimic
    on Windows) is squatting the default port. If the user explicitly set
    LUMAKIT_WEB_PORT, we still walk forward — telling them "your chosen port
    is taken" is better than silently exiting.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        if _port_is_free(candidate):
            if offset:
                print(
                    f"Port {preferred} is in use — falling back to {candidate}. "
                    f"Set LUMAKIT_WEB_PORT to pin a specific port."
                )
            return candidate
    raise RuntimeError(
        f"Could not find a free port in {preferred}-{preferred + attempts - 1}. "
        "Set LUMAKIT_WEB_PORT to a known-free port."
    )


def _apply_port(port: int) -> None:
    """Update the web surface module-level port so prints, runtime state, and
    health checks all agree on the chosen value."""
    web_surface.PORT = port
    web_surface.WEB_URL = f"http://localhost:{port}"


def _health_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or web_surface.PORT}/api/health"


def _health_check(port: int | None = None, *, timeout: float = 1.5) -> dict | None:
    url = _health_url(port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    if payload.get("status") != "ok":
        return None
    return payload


def _wait_for_health(*, timeout: float = 30.0, port: int | None = None) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = _health_check(port)
        if payload:
            return payload
        time.sleep(0.5)
    return None


def _open_browser(url: str) -> None:
    opened = False
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print(f"Open {url} in your browser.")


def _stale_runtime_cleanup() -> None:
    state = _runtime_state()
    if not state:
        return
    if not _pid_running(state.get("pid")):
        _clear_runtime_state()


def _already_running_state() -> tuple[str, dict | None]:
    _stale_runtime_cleanup()

    state = _runtime_state()
    health = _health_check(state.get("port") if state else None)
    if health:
        url = state.get("url") if state else web_surface.WEB_URL
        return f"LumaKit is already running at {url}.", state or {"url": url, "port": web_surface.PORT}

    if state and _pid_running(state.get("pid")):
        return (
            "A LumaKit process is already running but the web health check is failing. "
            "Use `python -m lumakit status` or `python -m lumakit stop` before starting another one.",
            state,
        )

    return "", None


def _spawn_daemon(*, verbose: bool = False) -> None:
    cmd = [sys.executable, "-m", "lumakit", "serve"]
    if verbose:
        cmd.append("--verbose")

    with DAEMON_LOG_FILE.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            cmd,
            cwd=str(REPO_ROOT),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def _render_systemd_service(
    *,
    user: str,
    working_dir: Path,
    env_file: Path,
    python_executable: Path,
) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=LumaKit AI Agent",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={user}",
            f"WorkingDirectory={working_dir}",
            f"EnvironmentFile=-{env_file}",
            f"ExecStart={python_executable} -m lumakit serve",
            "Restart=on-failure",
            "RestartSec=10",
            "StandardOutput=journal",
            "StandardError=journal",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def _desktop_exec_arg(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9@%_+=:,./-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_linux_desktop_entry(*, python_executable: Path, working_dir: Path, icon_path: Path) -> str:
    exec_value = " ".join(
        _desktop_exec_arg(part)
        for part in (str(python_executable), "-m", "lumakit", "open")
    )
    return "\n".join(
        [
            "[Desktop Entry]",
            "Version=1.0",
            "Type=Application",
            f"Name={SHORTCUT_NAME}",
            "Comment=Start or reuse the LumaKit backend and open the web UI",
            f"Exec={exec_value}",
            f"Path={working_dir}",
            f"Icon={icon_path}",
            "Terminal=false",
            "StartupNotify=true",
            "Categories=Utility;Development;",
            "",
        ]
    )


def _write_linux_shortcut(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o755)


def _windows_desktop_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def _windows_programs_dir() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _windows_shell_candidates() -> list[list[str]]:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return [
        [str(system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")],
        ["powershell.exe"],
        ["pwsh.exe"],
    ]


def _ensure_windows_icon(source_png: Path) -> Path:
    icon_target = get_data_dir() / "lumakit.ico"
    if icon_target.exists() and icon_target.stat().st_mtime >= source_png.stat().st_mtime:
        return icon_target

    icon_target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source_png) as img:
        rgba = img.convert("RGBA")
        rgba.save(icon_target, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return icon_target


def _create_windows_shortcut(*, target: Path, python_executable: Path, working_dir: Path, icon_path: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join(
        [
            "$WshShell = New-Object -ComObject WScript.Shell",
            f"$Shortcut = $WshShell.CreateShortcut('{_ps_quote(str(target))}')",
            f"$Shortcut.TargetPath = '{_ps_quote(str(python_executable))}'",
            "$Shortcut.Arguments = '-m lumakit open'",
            f"$Shortcut.WorkingDirectory = '{_ps_quote(str(working_dir))}'",
            f"$Shortcut.IconLocation = '{_ps_quote(str(icon_path))},0'",
            "$Shortcut.Description = 'Start or reuse the LumaKit backend and open the web UI'",
            "$Shortcut.Save()",
        ]
    )
    errors = []
    for shell_cmd in _windows_shell_candidates():
        try:
            result = subprocess.run(
                [*shell_cmd, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stderr.strip():
                raise RuntimeError(result.stderr.strip())
            return
        except FileNotFoundError as exc:
            errors.append(f"{shell_cmd[0]}: {exc}")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            errors.append(f"{shell_cmd[0]}: {detail}")
        except Exception as exc:
            errors.append(f"{shell_cmd[0]}: {exc}")
    raise RuntimeError(" ; ".join(errors))


def _write_windows_cmd_shortcut(*, target: Path, python_executable: Path, working_dir: Path) -> None:
    content = "\r\n".join(
        [
            "@echo off",
            f'cd /d "{working_dir}"',
            f'"{python_executable}" -m lumakit open',
            "",
        ]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def command_shortcut_install(args) -> int:
    python_executable = Path(sys.executable).resolve()
    working_dir = REPO_ROOT
    icon_path = (REPO_ROOT / "photos" / "lumakit_cat_logo.png").resolve()

    if sys.platform.startswith("linux"):
        targets: list[Path] = []
        content = _render_linux_desktop_entry(
            python_executable=python_executable,
            working_dir=working_dir,
            icon_path=icon_path,
        )

        app_target = Path.home() / ".local" / "share" / "applications" / LINUX_SHORTCUT_ID
        _write_linux_shortcut(app_target, content)
        targets.append(app_target)

        print("Installed LumaKit shortcut(s):")
        for target in targets:
            print(f"  {target}")
        print("These launch `lumakit open` via the current Python environment.")
        print("On Linux this installs the app-menu launcher only.")
        return 0

    if os.name == "nt":
        shortcut_targets: list[Path] = []
        fallback_targets: list[Path] = []
        shortcut_error = None
        candidates = [_windows_desktop_dir() / f"{SHORTCUT_NAME}.lnk"]
        programs_dir = _windows_programs_dir()
        if programs_dir:
            candidates.append(programs_dir / f"{SHORTCUT_NAME}.lnk")

        try:
            windows_icon = _ensure_windows_icon(icon_path)
            for target in candidates:
                _create_windows_shortcut(
                    target=target,
                    python_executable=python_executable,
                    working_dir=working_dir,
                    icon_path=windows_icon,
                )
                shortcut_targets.append(target)
        except Exception as exc:
            shortcut_error = str(exc).strip() or repr(exc)
            shortcut_targets.clear()
            fallback_candidates = [_windows_desktop_dir() / f"{SHORTCUT_NAME}.cmd"]
            if programs_dir:
                fallback_candidates.append(programs_dir / f"{SHORTCUT_NAME}.cmd")
            for target in fallback_candidates:
                _write_windows_cmd_shortcut(
                    target=target,
                    python_executable=python_executable,
                    working_dir=working_dir,
                )
                fallback_targets.append(target)

        if shortcut_targets:
            print("Installed LumaKit shortcut(s):")
            for target in shortcut_targets:
                print(f"  {target}")
            print("These launch `lumakit open` via the current Python environment.")
            return 0

        print("PowerShell shortcut creation failed; wrote command launcher(s) instead:")
        if shortcut_error:
            print(f"  error: {shortcut_error}")
        for target in fallback_targets:
            print(f"  {target}")
        print("These still launch `lumakit open`, but may not show a custom app icon.")
        return 0

    print(f"Shortcut install is not supported on this platform: {sys.platform}")
    return 1


def _service_install_target(args) -> Path:
    if args.system:
        return Path("/etc/systemd/system") / f"{args.name}.service"
    return Path(args.output).expanduser().resolve(strict=False) if args.output else DEFAULT_SERVICE_PATH


def command_open(args) -> int:
    state = _runtime_state()
    health = _health_check(state.get("port") if state else None)
    url = (state or {}).get("url", web_surface.WEB_URL)
    if health:
        print(f"LumaKit is already running at {url}.")
        _open_browser(url)
        return 0

    message, running_state = _already_running_state()
    if running_state:
        print(message)
        return 1

    print("Starting LumaKit in the background...")
    _spawn_daemon(verbose=args.verbose)

    # Wait for the daemon to publish its chosen port (it may fall back off
    # the default if something else is squatting it), then health-check that.
    deadline = time.time() + args.timeout
    daemon_state = None
    while time.time() < deadline:
        candidate = _runtime_state()
        if candidate and candidate.get("port"):
            daemon_state = candidate
            break
        time.sleep(0.25)

    port = (daemon_state or {}).get("port")
    url = (daemon_state or {}).get("url", web_surface.WEB_URL)
    remaining = max(1.0, deadline - time.time())
    health = _wait_for_health(timeout=remaining, port=port)
    if not health:
        print(f"LumaKit did not become healthy in time. Check {DAEMON_LOG_FILE}.")
        return 1

    print(f"LumaKit is running at {url}.")
    _open_browser(url)
    return 0


def command_status(_args) -> int:
    _stale_runtime_cleanup()
    state = _runtime_state()
    port = state.get("port") if state else None
    health = _health_check(port)

    if health:
        url = state.get("url") if state else web_surface.WEB_URL
        print(f"status: running")
        print(f"url: {url}")
        print(f"pid: {state.get('pid') if state else 'unmanaged'}")
        print(f"telegram: {'configured' if telegram_surface.is_configured() else 'disabled'}")
        print(f"model: {health.get('model', 'unknown')}")
        return 0

    if state and _pid_running(state.get("pid")):
        print("status: unhealthy")
        print(f"pid: {state.get('pid')}")
        print(f"url: {state.get('url', web_surface.WEB_URL)}")
        print("health: failing")
        return 1

    print("status: stopped")
    print(f"url: {web_surface.WEB_URL}")
    return 1


def command_stop(args) -> int:
    _stale_runtime_cleanup()
    state = _runtime_state()
    if not state:
        print("LumaKit is not running.")
        return 0

    pid = state.get("pid")
    if not _pid_running(pid):
        _clear_runtime_state()
        print("LumaKit is not running.")
        return 0

    print(f"Stopping LumaKit (pid {pid})...")
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _pid_running(pid):
            _clear_runtime_state()
            print("LumaKit stopped.")
            return 0
        time.sleep(0.25)

    print("LumaKit is still shutting down. Check status again in a few seconds.")
    return 1


def command_service_install(args) -> int:
    target = _service_install_target(args)
    working_dir = Path(args.working_dir).expanduser().resolve() if args.working_dir else REPO_ROOT
    env_file = Path(args.env_file).expanduser().resolve(strict=False) if args.env_file else working_dir / ".env"
    python_executable = Path(args.python).expanduser().resolve() if args.python else Path(sys.executable).resolve()
    user = args.user or getpass.getuser()
    service_text = _render_systemd_service(
        user=user,
        working_dir=working_dir,
        env_file=env_file,
        python_executable=python_executable,
    )

    if target.exists() and not args.force and target.read_text(encoding="utf-8") != service_text:
        print(f"{target} already exists. Re-run with --force to overwrite it.")
        return 1

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(service_text, encoding="utf-8")
    except PermissionError:
        print(f"Permission denied writing {target}.")
        if args.system:
            print("Re-run this command with sudo, or omit --system and copy the file manually.")
        return 1

    print(f"Wrote {target}.")

    if args.system:
        print("Next steps:")
        print("  sudo systemctl daemon-reload")
        print(f"  sudo systemctl enable {args.name}.service")
        print(f"  sudo systemctl start {args.name}.service")
        return 0

    print("Install it with:")
    print(f"  sudo cp {target} /etc/systemd/system/{args.name}.service")
    print("  sudo systemctl daemon-reload")
    print(f"  sudo systemctl enable {args.name}.service")
    print(f"  sudo systemctl start {args.name}.service")
    return 0


def command_serve(args) -> int:
    message, running_state = _already_running_state()
    if running_state:
        print(message)
        return 1

    _apply_port(_resolve_port(web_surface.PORT))

    web_surface.configure_owner()

    service = LumaKitService()
    telegram_enabled = telegram_surface.is_configured()
    web_surface.register_surface(service, is_owner=not telegram_enabled)
    service.start()

    stop_event = threading.Event()
    telegram_thread = None
    if telegram_enabled:
        telegram_thread = threading.Thread(
            target=telegram_surface.run,
            kwargs={
                "service": service,
                "verbose": args.verbose,
                "owns_service": False,
                "stop_event": stop_event,
                "announce_start": True,
            },
            daemon=True,
            name="lumakit-telegram",
        )
        telegram_thread.start()

    config = uvicorn.Config(
        web_surface.app,
        host="0.0.0.0",
        port=web_surface.PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    def request_shutdown(_signum=None, _frame=None):
        stop_event.set()
        server.should_exit = True

    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    atexit.register(_clear_runtime_state, pid=os.getpid())
    _write_runtime_state()

    print("Starting LumaKit backend...")
    print(f"Web UI: {web_surface.WEB_URL}")
    print(f"Telegram: {'enabled' if telegram_enabled else 'disabled'}")

    try:
        server.run()
    finally:
        stop_event.set()
        service.stop()
        if telegram_thread:
            telegram_thread.join(timeout=6)
        _clear_runtime_state(pid=os.getpid())
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    return 0


def command_cli(args) -> int:
    # Import lazily so the env loading at the top of this module runs before
    # the CLI surface imports agent/core modules that read configuration.
    from surfaces.cli import main as cli_main

    cli_main(["--verbose"] if args.verbose else [])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumakit")
    subparsers = parser.add_subparsers(dest="command", required=True)

    cli = subparsers.add_parser("cli", help="start the terminal chat interface")
    cli.add_argument("--verbose", action="store_true", help="enable verbose agent logging")
    cli.set_defaults(func=command_cli)

    serve = subparsers.add_parser("serve", help="run the long-lived LumaKit backend")
    serve.add_argument("--verbose", action="store_true", help="enable verbose Telegram agent logging")
    serve.set_defaults(func=command_serve)

    open_cmd = subparsers.add_parser("open", help="start or reuse the backend, then open the web UI")
    open_cmd.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for health")
    open_cmd.add_argument("--verbose", action="store_true", help="start the backend with verbose Telegram logging")
    open_cmd.set_defaults(func=command_open)

    status = subparsers.add_parser("status", help="show backend health and runtime status")
    status.set_defaults(func=command_status)

    stop = subparsers.add_parser("stop", help="stop the running backend")
    stop.add_argument("--timeout", type=float, default=15.0, help="seconds to wait for shutdown")
    stop.set_defaults(func=command_stop)

    service = subparsers.add_parser("service", help="generate or install service files for always-on mode")
    service_subparsers = service.add_subparsers(dest="service_command", required=True)

    service_install = service_subparsers.add_parser("install", help="write a systemd unit for `lumakit serve`")
    service_install.add_argument("--name", default="lumakit", help="service name, default: lumakit")
    service_install.add_argument("--output", help="write the unit file to this path")
    service_install.add_argument(
        "--system",
        action="store_true",
        help="write directly to /etc/systemd/system/<name>.service",
    )
    service_install.add_argument("--user", help="system user that should own the service process")
    service_install.add_argument("--working-dir", help="working directory for the service")
    service_install.add_argument("--env-file", help="EnvironmentFile path for systemd")
    service_install.add_argument("--python", help="python executable to use in ExecStart")
    service_install.add_argument("--force", action="store_true", help="overwrite an existing unit file")
    service_install.set_defaults(func=command_service_install)

    shortcut = subparsers.add_parser("shortcut", help="install desktop/start-menu launchers for `lumakit open`")
    shortcut_subparsers = shortcut.add_subparsers(dest="shortcut_command", required=True)

    shortcut_install = shortcut_subparsers.add_parser(
        "install",
        help="install native launcher shortcuts for the current platform",
    )
    shortcut_install.set_defaults(func=command_shortcut_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
