import pytest

from flowmap.navigation import browse_inventory as browse
from flowmap.project import load_project


@pytest.fixture
def inventory(tmp_path):
    (tmp_path / "Alpha").mkdir()
    (tmp_path / "src/nested-empty").mkdir(parents=True)
    (tmp_path / "src/app.ts").write_text("export const app = 1")
    (tmp_path / "src/notes.md").write_text("notes")
    (tmp_path / "A.md").write_text("readme")
    (tmp_path / "z.custom").write_text("notes")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules/ignored.js").write_text("ignored")
    return load_project(tmp_path).inventory


def test_browse_lists_only_direct_children_with_folders_first(inventory):
    assert browse(inventory) == {
        "path": "", "total": 4, "offset": 0, "limit": 40,
        "items": [
            {"path": "Alpha", "label": "Alpha", "category": "folder", "language": ""},
            {"path": "src", "label": "src", "category": "folder", "language": ""},
            {"path": "A.md", "label": "A.md", "category": "document", "language": "markdown"},
            {"path": "z.custom", "label": "z.custom", "category": "document", "language": "unknown"},
        ],
    }


@pytest.mark.parametrize("path", ["src", "src/app.ts", "./src//"])
def test_browse_normalizes_file_focus_to_its_parent(inventory, path):
    assert browse(inventory, path) == {
        "path": "src", "total": 3, "offset": 0, "limit": 40,
        "items": [
            {"path": "src/nested-empty", "label": "nested-empty", "category": "folder", "language": ""},
            {"path": "src/app.ts", "label": "app.ts", "category": "module", "language": "typescript"},
            {"path": "src/notes.md", "label": "notes.md", "category": "document", "language": "markdown"},
        ],
    }


def test_browse_root_file_returns_project_root(inventory):
    page = browse(inventory, "z.custom")
    assert page["path"] == "" and page["total"] == 4
    assert page["items"][-1]["path"] == "z.custom"


def test_browse_inventoried_empty_folder(inventory):
    assert browse(inventory, "src/nested-empty") == {
        "path": "src/nested-empty", "items": [], "total": 0, "offset": 0, "limit": 40,
    }


def test_browse_empty_project_root(tmp_path):
    assert browse(load_project(tmp_path).inventory) == {
        "path": "", "items": [], "total": 0, "offset": 0, "limit": 40,
    }


@pytest.mark.parametrize("path", ["missing", "../src", "src/../src", "/src", "node_modules", "src/\0"])
def test_browse_rejects_unknown_ignored_or_unsafe_paths(inventory, path):
    with pytest.raises(FileNotFoundError):
        browse(inventory, path)


def test_browse_preserves_unicode_and_comma_names(tmp_path):
    (tmp_path / "lib,common").mkdir()
    (tmp_path / "lib,common/订单.custom").write_text("notes")
    inventory = load_project(tmp_path).inventory
    assert browse(inventory)["items"] == [
        {"path": "lib,common", "label": "lib,common", "category": "folder", "language": ""},
    ]
    assert browse(inventory, "lib,common/订单.custom")["items"] == [
        {"path": "lib,common/订单.custom", "label": "订单.custom", "category": "document", "language": "unknown"},
    ]


def test_browse_reaches_every_file_beyond_the_map_limit(tmp_path):
    (tmp_path / "wide").mkdir()
    for i in range(240):
        (tmp_path / "wide" / f"file{i:03}.custom").write_text("notes")
    inventory = load_project(tmp_path).inventory
    pages = [browse(inventory, "wide", offset=offset) for offset in range(0, 240, 40)]
    assert all(page["total"] == 240 and len(page["items"]) == 40 for page in pages)
    assert [item["path"] for page in pages for item in page["items"]] == [
        f"wide/file{i:03}.custom" for i in range(240)
    ]
    assert pages[-1]["offset"] == 200
    assert pages[-1]["items"][-1]["path"] == "wide/file239.custom"
    assert len(browse(inventory, "wide", limit=100)["items"]) == 100


def test_browse_offset_beyond_total_keeps_pagination_metadata(inventory):
    assert browse(inventory, "src", offset=20, limit=2) == {
        "path": "src", "items": [], "total": 3, "offset": 20, "limit": 2,
    }


@pytest.mark.parametrize("offset,limit", [(-1, 40), (1.5, 40), (True, 40), ("1", 40), (0, 0), (0, -1), (0, 101), (0, 1.5), (0, False)])
def test_browse_rejects_invalid_pagination(inventory, offset, limit):
    with pytest.raises(ValueError):
        browse(inventory, offset=offset, limit=limit)
