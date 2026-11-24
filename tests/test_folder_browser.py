"""The web UI's own workspace folder browser (core.folder_browser)."""

import os
from pathlib import Path

import pytest

from core import folder_browser


def _tree(tmp_path: Path) -> Path:
    # A folder of its own: conftest puts a traces folder in tmp_path.
    root = tmp_path / "tree"
    root.mkdir()
    (root / "beta").mkdir()
    (root / "Alpha").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("not a folder", encoding="utf-8")
    return root


def test_lists_only_folders_sorted_without_hidden(tmp_path):
    root = _tree(tmp_path).resolve()
    listing = folder_browser.list_folders(str(root))
    assert [f["name"] for f in listing["folders"]] == ["Alpha", "beta"]
    assert listing["folders"][0]["path"] == str(root / "Alpha")
    assert listing["path"] == str(root)
    assert listing["parent"] == str(root.parent)
    assert listing["truncated"] is False


def test_hidden_folders_on_request(tmp_path):
    listing = folder_browser.list_folders(str(_tree(tmp_path)), show_hidden=True)
    assert [f["name"] for f in listing["folders"]] == [".hidden", "Alpha", "beta"]


def test_missing_folder_and_file_are_refused(tmp_path):
    root = _tree(tmp_path)
    with pytest.raises(FileNotFoundError):
        folder_browser.list_folders(str(root / "nope"))
    with pytest.raises(NotADirectoryError):
        folder_browser.list_folders(str(root / "notes.txt"))


def test_empty_path_is_the_top(tmp_path):
    listing = folder_browser.list_folders("")
    if os.name == "nt":
        assert listing["path"] == "" and listing["parent"] is None
        assert listing["folders"], "expected at least one drive"
    else:
        assert listing["path"] == "/"
        assert listing["parent"] is None


def test_long_listing_is_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(folder_browser, "MAX_FOLDERS", 3)
    root = tmp_path / "many"
    root.mkdir()
    for i in range(5):
        (root / f"d{i}").mkdir()
    listing = folder_browser.list_folders(str(root))
    assert len(listing["folders"]) == 3
    assert listing["truncated"] is True


def test_shortcuts_name_home_and_lumakit(tmp_path):
    places = folder_browser.shortcuts(repo_root=tmp_path)
    assert places[0] == {"name": "Home", "path": str(Path.home())}
    assert places[1] == {"name": "LumaKit", "path": str(tmp_path.resolve())}
