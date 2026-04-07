import subprocess


def get_git_init_tool():
    return {
        "name": "git_init",
        "description": "Initialize a new git repository in the current directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "bare": {
                    "type": "boolean",
                    "description": "Create a bare repository (default: false)"
                }
            },
            "required": []
        },
        "execute": _git_init
    }


def _run_git_command(command):
    """Execute a git command and return the result."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            shell=True
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _git_init(inputs):
    bare = inputs.get("bare", False)
    cmd = "git init --bare" if bare else "git init"
    result = _run_git_command(cmd)

    if not result["success"]:
        return {"error": result.get("stderr", "Init failed")}

    return {
        "initialized": True,
        "bare": bare,
        "output": result["stdout"]
    }


def get_git_status_tool():
    return {
        "name": "git_status",
        "description": "Get the current git status of the repository.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "execute": _git_status
    }


def get_git_commit_tool():
    return {
        "name": "git_commit",
        "description": "Stage all changes and commit with a message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Commit message"}
            },
            "required": ["message"]
        },
        "execute": _git_commit
    }


def get_git_push_tool():
    return {
        "name": "git_push",
        "description": "Push commits to remote repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch to push (default: current branch)"}
            },
            "required": []
        },
        "execute": _git_push
    }


def get_git_pull_tool():
    return {
        "name": "git_pull",
        "description": "Pull changes from remote repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch to pull (default: current branch)"}
            },
            "required": []
        },
        "execute": _git_pull
    }


def get_git_branch_tool():
    return {
        "name": "git_branch",
        "description": "List, create, or switch git branches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "switch"],
                    "description": "Action to perform"
                },
                "branch_name": {"type": "string", "description": "Branch name (required for create/switch)"}
            },
            "required": ["action"]
        },
        "execute": _git_branch
    }


def get_git_log_tool():
    return {
        "name": "git_log",
        "description": "View git commit history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "num_commits": {
                    "type": "number",
                    "description": "Number of commits to show (default: 10)"
                }
            },
            "required": []
        },
        "execute": _git_log
    }


def get_git_add_tool():
    return {
        "name": "git_add",
        "description": "Stage files for commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "files": {
                    "type": "string",
                    "description": "Files to stage (e.g., '.' for all, or specific paths)"
                }
            },
            "required": ["files"]
        },
        "execute": _git_add
    }


def _git_status(inputs):
    result = _run_git_command("git status --porcelain")
    if not result["success"]:
        return {"error": result.get("error", result.get("stderr", "Unknown error"))}

    return {
        "status": result["stdout"],
        "command": "git status"
    }


def _git_commit(inputs):
    message = inputs.get("message", "Auto-commit")
    result = _run_git_command(f'git commit -am "{message}"')

    if not result["success"]:
        return {"error": result.get("stderr", "Commit failed")}

    return {
        "committed": True,
        "message": message,
        "output": result["stdout"]
    }


def _git_push(inputs):
    branch = inputs.get("branch", "")
    cmd = f"git push {branch}".strip()
    result = _run_git_command(cmd)

    if not result["success"]:
        return {"error": result.get("stderr", "Push failed")}

    return {
        "pushed": True,
        "branch": branch or "current",
        "output": result["stdout"]
    }


def _git_pull(inputs):
    branch = inputs.get("branch", "")
    cmd = f"git pull {branch}".strip()
    result = _run_git_command(cmd)

    if not result["success"]:
        return {"error": result.get("stderr", "Pull failed")}

    return {
        "pulled": True,
        "branch": branch or "current",
        "output": result["stdout"]
    }


def _git_branch(inputs):
    action = inputs.get("action", "list")
    branch_name = inputs.get("branch_name", "")

    if action == "list":
        result = _run_git_command("git branch -a")
    elif action == "create":
        if not branch_name:
            return {"error": "branch_name required for create action"}
        result = _run_git_command(f"git branch {branch_name}")
    elif action == "switch":
        if not branch_name:
            return {"error": "branch_name required for switch action"}
        result = _run_git_command(f"git checkout {branch_name}")
    else:
        return {"error": f"Unknown action: {action}"}

    if not result["success"]:
        return {"error": result.get("stderr", f"{action} failed")}

    return {
        "action": action,
        "branch": branch_name or "N/A",
        "output": result["stdout"]
    }


def _git_log(inputs):
    num_commits = int(inputs.get("num_commits", 10))
    result = _run_git_command(f"git log --oneline -n {num_commits}")

    if not result["success"]:
        return {"error": result.get("stderr", "Log failed")}

    return {
        "commits": result["stdout"],
        "num_commits": num_commits
    }


def _git_add(inputs):
    files = inputs.get("files", ".")
    result = _run_git_command(f"git add {files}")

    if not result["success"]:
        return {"error": result.get("stderr", "Add failed")}

    return {
        "added": True,
        "files": files,
        "output": result["stdout"] or "Files staged"
    }