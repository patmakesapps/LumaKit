import subprocess

from core.diffs import build_unified_diff, truncate_text
from core.paths import get_display_path, get_repo_root, resolve_repo_path


def get_show_diff_tool():
    return {
        'name': 'show_diff',
        'description': 'Shows the current git diff for the repo or for a specific file.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Optional repo-relative or absolute file path to diff. Unique near-matches may be resolved automatically.'
                },
                'context_lines': {'type': 'number', 'description': 'Unified diff context lines (default 3)'}
            }
        },
        'execute': _show_diff
    }


def _git(args):
    return subprocess.run(
        ['git', *args],
        cwd=get_repo_root(),
        capture_output=True,
        text=True,
        check=False
    )


def _build_untracked_diff(path):
    content = path.read_text(encoding='utf-8', errors='replace')
    return build_unified_diff('', content, path)


def _show_diff(inputs):
    context_lines = int(inputs.get('context_lines', 3))
    path_input = inputs.get('path')
    path = resolve_repo_path(path_input, must_exist=False, kind='file') if path_input else None

    inside_repo = _git(['rev-parse', '--is-inside-work-tree'])
    if inside_repo.returncode != 0:
        raise RuntimeError('show_diff requires a git repository')

    status_args = ['status', '--short']
    diff_args = ['diff', f'-U{context_lines}', '--no-ext-diff']

    if path is not None:
        relative_path = get_display_path(path)
        status_args.extend(['--', relative_path])
        diff_args.extend(['--', relative_path])
    else:
        relative_path = None

    status_result = _git(status_args)
    diff_result = _git(diff_args)

    status_lines = [line for line in status_result.stdout.splitlines() if line.strip()]
    untracked_paths = [line[3:] for line in status_lines if line.startswith('?? ')]

    untracked_diffs = []
    for untracked_path in untracked_paths:
        untracked_file = get_repo_root() / untracked_path
        if untracked_file.is_file():
            untracked_diffs.append(_build_untracked_diff(untracked_file))

    diff_text = diff_result.stdout
    if untracked_diffs:
        extra_diffs = "\n".join(item['diff'] for item in untracked_diffs if item['diff'])
        if extra_diffs:
            diff_text = f"{diff_text}\n{extra_diffs}".strip()
    diff_text, diff_truncated = truncate_text(diff_text)

    return {
        'path': relative_path,
        'status': status_lines,
        'diff': diff_text,
        'diff_truncated': diff_truncated,
        'has_changes': bool(status_lines or diff_text),
    }
