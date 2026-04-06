from core.diffs import build_unified_diff
from core.paths import get_display_path, resolve_repo_path


def get_delete_file_tool():
    return {
        'name': 'delete_file',
        'description': 'Deletes a repo file. By default it previews the deletion diff; it only deletes when confirm is true.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Repo-relative or absolute file path. Unique near-matches may be resolved automatically.'
                },
                'confirm': {'type': 'boolean', 'description': 'Must be true to perform the deletion'}
            },
            'required': ['path']
        },
        'execute': _delete_file
    }


def _delete_file(inputs):
    path = resolve_repo_path(inputs['path'], kind='file')
    before = path.read_text(encoding='utf-8', errors='replace')
    diff_data = build_unified_diff(before, '', path)

    if not bool(inputs.get('confirm', False)):
        return {
            'path': get_display_path(path),
            'would_delete': True,
            'requires_confirmation': True,
            'size': path.stat().st_size,
            **diff_data
        }

    try:
        path.unlink()
    except PermissionError as error:
        raise PermissionError(
            f"Deletion was confirmed but the OS denied removing {get_display_path(path)}: {error}"
        ) from error

    return {
        'path': get_display_path(path),
        'deleted': True,
        **diff_data
    }
