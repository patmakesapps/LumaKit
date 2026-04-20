from core.paths import get_repo_root


def get_get_project_tree_tool():
    return {
        'name': 'get_project_tree',
        'description': (
            'Return a directory tree of the current project (depth-limited). '
            'Call this when you need a map of the repo before navigating files.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'max_depth': {
                    'type': 'integer',
                    'description': 'Max tree depth (default 3).',
                },
            },
        },
        'execute': _execute,
    }


def _execute(inputs):
    # Local import to avoid a circular import at module load.
    from agent import _build_project_tree

    max_depth = int(inputs.get('max_depth') or 3)
    root = get_repo_root()
    return {
        'root': str(root),
        'tree': _build_project_tree(root, max_depth=max_depth),
    }
