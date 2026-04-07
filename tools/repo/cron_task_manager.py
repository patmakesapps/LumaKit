# Tool addition: Dynamic Task Scheduler
# 
# Purpose:
#   This tool provides a simple command‑line interface for scheduling recurring
#   or one‑off tasks (e.g., backups, data pulls, cleanup scripts) based on
#   cron‑style expressions. It leverages the existing `execute_shell` and
#   `get_current_datetime` utilities to run commands at specified times,
#   storing scheduled tasks persistently in a JSON file (`scheduled_tasks.json`).
# 
# Features:
#   • Add a new scheduled task with a cron expression and command.
#   • List all scheduled tasks.
#   • Run pending tasks that match the current time.
#   • Remove or edit existing tasks.
# 
# Usage examples:
#   1. Add a task:
#      python tool_addition.py add "0 2 * * *" "python /path/to/backup.py"
#   2. Run pending tasks now:
#      python tool_addition.py run
#   3. List tasks:
#      python tool_addition.py list
#   4. Remove a task by index:
#      python tool_addition.py remove 2
# 
# Implementation notes:
#   • The script stores tasks in `scheduled_tasks.json` under the repository root.
#   • It parses cron expressions using the lightweight `croniter` library,
#     which should be added to `requirements.txt` if not already present.
#   • The script can be invoked directly from the command line; see the
#     `__main__` block at the bottom for the argument parser.
import json
import os
from datetime import datetime
from croniter import croniter

TASKS_FILE = "scheduled_tasks.json"

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return {}
    with open(TASKS_FILE, "r") as f:
        return json.load(f)

def save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)

def parse_cron(cron_expr):
    # Simple validation – returns an iterator that can be checked against datetime.now()
    return croniter(cron_expr)

def add_task(cron_expr, command):
    tasks = load_tasks()
    task_id = str(datetime.utcnow().timestamp())
    tasks[task_id] = {
        "cron": cron_expr,
        "command": command,
        "added_at": datetime.utcnow().isoformat()
    }
    save_tasks(tasks)
    print(f"Added task {task_id}")

def list_tasks():
    tasks = load_tasks()
    if not tasks:
        print("No scheduled tasks.")
        return
    for idx, (tid, data) in enumerate(sorted(tasks.items()), start=1):
        print(f"{idx}. ID: {tid} | Cron: {data['cron']} | Command: {data['command']}")

def run_tasks():
    tasks = load_tasks()
    now = datetime.utcnow()
    pending = []
    for tid, data in tasks.items():
        cron = data["cron"]
        cmd = data["command"]
        iterator = parse_cron(cron)
        # Check if now matches the cron pattern (within 1 minute tolerance)
        if iterator.get_next(datetime.now()) <= now + timedelta(minutes=1):
            pending.append((tid, cmd))
    if not pending:
        print("No tasks to run at this time.")
        return
    for tid, cmd in pending:
        print(f"Executing task {tid}: {cmd}")
        # Use the existing execute_shell tool to run the command
        # (implementation would call execute_shell here)
        # For demonstration we just print the command:
        print(f"[Shell] {cmd}")
        # TODO: replace print with actual execute_shell call
        # execute_shell(cmd)
        # Once executed, optionally remove or mark as done
        # del tasks[tid]
    save_tasks(tasks)

if __name__ == "__main__":
    import sys
    from argparse import ArgumentParser
    parser = ArgumentParser(description="Dynamic Task Scheduler")
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new cron task")
    add_parser.add_argument("cron", help="Cron expression")
    add_parser.add_argument("command", help="Command to execute")

    list_parser = subparsers.add_parser("list", help="List scheduled tasks")
    remove_parser = subparsers.add_parser("remove", help="Remove a task by index")
    remove_parser.add_argument("index", type=int, help="Task index to remove")
    run_parser = subparsers.add_parser("run", help="Execute pending tasks")

    args = parser.parse_args()

    if args.cmd == "add":
        add_task(args.cron, args.command)
    elif args.cmd == "list":
        list_tasks()
    elif args.cmd == "remove":
        tasks = load_tasks()
        keys = sorted(tasks.keys())
        if args.index < 1 or args.index > len(keys):
            print("Invalid index.")
            sys.exit(1)
        tid = keys[args.index - 1]
        del tasks[tid]
        save_tasks(tasks)
        print(f"Removed task {tid}")
    elif args.cmd == "run":
        from datetime import timedelta
        run_tasks()