from core.diffs import build_unified_diff, detect_line_ending, normalize_line_endings
from core.paths import get_display_path, resolve_repo_path


def get_edit_file_tool():
    return {
        'name': 'edit_file',
        'description': 'Finds and replaces text in a repo file with guardrails and returns the diff.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Repo-relative or absolute file path. Unique near-matches may be resolved automatically.'
                },
                'find': {'type': 'string', 'description': 'Exact text to replace.'},
                'replace': {'type': 'string', 'description': 'Replacement text.'},
                'replace_all': {'type': 'boolean', 'description': 'Replace all matches instead of exactly one'},
                'expected_occurrences': {'type': 'number', 'description': 'Optional exact number of matches expected before editing'}
            },
            'required': ['path', 'find', 'replace']
        },
        'execute': _edit_file
    }


def _edit_file(inputs):
    path = resolve_repo_path(inputs['path'], kind='file')
    content = path.read_text(encoding='utf-8', errors='replace')
    newline = detect_line_ending(content)
    find_text = normalize_line_endings(inputs['find'], newline)
    replace_text = normalize_line_endings(inputs['replace'], newline)
    occurrences = content.count(find_text)
    expected_occurrences = inputs.get('expected_occurrences')
    replace_all = bool(inputs.get('replace_all', False))

    if occurrences == 0:
        raise ValueError(f"Could not find the requested text in {get_display_path(path)}")

    if expected_occurrences is not None and occurrences != int(expected_occurrences):
        raise ValueError(
            f"Expected {int(expected_occurrences)} occurrence(s) in {get_display_path(path)}, found {occurrences}"
        )

    if occurrences > 1 and not replace_all:
        raise ValueError(
            f"Found {occurrences} matches in {get_display_path(path)}. "
            "Pass replace_all=true or provide a more specific find string."
        )

    updated_content = (
        content.replace(find_text, replace_text)
        if replace_all
        else content.replace(find_text, replace_text, 1)
    )
    replacements_made = occurrences if replace_all else 1
    path.write_text(updated_content, encoding='utf-8')

    return {
        'path': get_display_path(path),
        'replacements': replacements_made,
        **build_unified_diff(content, updated_content, path)
    }
