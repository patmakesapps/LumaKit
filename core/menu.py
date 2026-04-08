"""Interactive menu selector for the LumaKit CLI."""

import sys

from core.cli import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, _c


def _read_key():
    """Read a single keypress. Returns a string identifier."""
    if sys.platform == "win32":
        import msvcrt
        key = msvcrt.getwch()
        if key in ("\x00", "\xe0"):  # special key prefix
            key2 = msvcrt.getwch()
            if key2 == "H":
                return "up"
            elif key2 == "P":
                return "down"
            elif key2 == "S":
                return "delete"
            return "unknown"
        elif key == "\r":
            return "enter"
        elif key == "\x1b":
            return "escape"
        elif key == "q":
            return "escape"
        return key
    else:
        import tty
        import termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    elif ch3 == "B":
                        return "down"
                    elif ch3 == "3":
                        sys.stdin.read(1)  # consume ~
                        return "delete"
                return "escape"
            elif ch == "\r" or ch == "\n":
                return "enter"
            elif ch == "q":
                return "escape"
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def select_menu(items: list[dict], title: str = "Select") -> dict | None:
    """Show an interactive menu. Each item needs 'label' and 'sublabel' keys.

    Returns the selected item dict, or None if cancelled.
    Sets item['action'] to 'select' or 'delete' before returning.
    """
    if not items:
        print(_c(DIM, "  No items.\n"))
        return None

    selected = 0

    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            _render(items, selected, title)
            key = _read_key()

            if key == "up":
                selected = (selected - 1) % len(items)
            elif key == "down":
                selected = (selected + 1) % len(items)
            elif key == "enter":
                _clear_menu(items, title)
                items[selected]["action"] = "select"
                return items[selected]
            elif key == "delete":
                _clear_menu(items, title)
                items[selected]["action"] = "delete"
                return items[selected]
            elif key == "escape":
                _clear_menu(items, title)
                return None
    finally:
        # Show cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def _render(items: list[dict], selected: int, title: str):
    """Render the menu (overwriting previous render)."""
    # Move cursor up to overwrite previous render
    total_lines = 2 + len(items) * 2 + 1  # title + items + footer
    sys.stdout.write(f"\033[{total_lines}A\033[J")

    print(f"\n{_c(BOLD, f'  {title}')}")

    for i, item in enumerate(items):
        if i == selected:
            marker = _c(GREEN, " >")
            label = _c(GREEN, item["label"])
        else:
            marker = "  "
            label = f"  {item['label']}"
        print(f"{marker} {label}")
        print(f"     {_c(DIM, item.get('sublabel', ''))}")

    print(_c(DIM, "  [arrows] navigate  [enter] resume  [del] delete  [esc] back"))


def _clear_menu(items: list[dict], title: str):
    """Clear the menu from the screen."""
    total_lines = 2 + len(items) * 2 + 1
    sys.stdout.write(f"\033[{total_lines}A\033[J")
    sys.stdout.flush()
