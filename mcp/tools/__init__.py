import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def register(mcp, get_user_id, fs_factory) -> None:
    from .guide import register as register_guide
    from .search import register as register_search
    from .read import register as register_read
    from .write import register as register_write
    from .delete import register as register_delete
    from .lint import register as register_lint
    from .wiki_read import register as register_wiki_read

    register_guide(mcp, get_user_id, fs_factory)
    register_search(mcp, get_user_id, fs_factory)
    register_read(mcp, get_user_id, fs_factory)
    register_wiki_read(mcp, get_user_id, fs_factory)
    register_write(mcp, get_user_id, fs_factory)
    register_delete(mcp, get_user_id, fs_factory)
    register_lint(mcp, get_user_id, fs_factory)
