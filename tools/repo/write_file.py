from core.diffs import build_unified_diff, detect_line_ending, normalize_line_endings
from core.paths import get_display_path, resolve_repo_path


def get_write_file_tool():
    return {
        'name': 'write_file',
        'description': 'Writes content to a file in the repo and returns a diff summary.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Repo-relative or absolute file path. Unique near-matches may be resolved automatically.'
                },
                'content': {'type': 'string'}
            },
            'required': ['path', 'content']
        },
        'execute': _write_file
    }


def _write_file(inputs):
    path = resolve_repo_path(inputs['path'], must_exist=False, kind='file')
    existed_before = path.exists()
    before = path.read_text(encoding='utf-8', errors='replace') if existed_before else ''
    preferred_newline = detect_line_ending(before) if before else '\n'
    after = normalize_line_endings(inputs['content'], preferred_newline)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after, encoding='utf-8')
    return {
        'path': get_display_path(path),
        'created': not existed_before,
        'bytes_written': path.stat().st_size,
        **build_unified_diff(before, after, path)
    }
