from pathlib import Path

from flowmap.cli import main

SHOP = str(Path(__file__).parent / "fixtures" / "shop")


def test_list_shows_entries_numbered_in_rank_order(capsys):
    assert main([SHOP, "--list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 4
    assert lines[0].split()[0] == "1"
    assert "python -m shop" in "\n".join(lines[:3])
    assert lines[3].split()[1] == "shop"


def test_mermaid_for_an_entry_picked_by_number(capsys):
    assert main([SHOP, "--mermaid", "1"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")


def test_mermaid_top_down(capsys):
    assert main([SHOP, "--mermaid", "1", "--vertical"]) == 0
    assert capsys.readouterr().out.startswith("flowchart TD")


def test_mermaid_for_an_entry_picked_by_label_with_depth(capsys):
    assert main([SHOP, "--mermaid", "shop.worker", "--depth", "1"]) == 0
    out = capsys.readouterr().out
    assert "python -m shop.worker" in out
    assert "subgraph" in out  # work() was opened


def test_mermaid_for_any_function_id(capsys):
    assert main([SHOP, "--mermaid", "src/shop/orders.py::OrderService.place"]) == 0
    assert "Gateway.charge" in capsys.readouterr().out


def test_unknown_entry_is_an_error(capsys):
    assert main([SHOP, "--mermaid", "nope"]) == 2
    assert "no entry" in capsys.readouterr().err


def test_mermaid_map_works_without_entries(tmp_path, capsys):
    (tmp_path / "notes.custom").write_text("project notes")
    assert main([str(tmp_path), "--mermaid-map"]) == 0
    assert 'notes.custom' in capsys.readouterr().out


def test_mermaid_map_supports_focus_expansion_and_vertical(tmp_path, capsys):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.ts").write_text("export const value = 1")
    assert main([str(tmp_path), "--mermaid-map", "--expand", "dir:src", "--vertical"]) == 0
    text = capsys.readouterr().out
    assert text.startswith("flowchart TD") and "subgraph" in text and "app.ts" in text
    assert main([str(tmp_path), "--mermaid-map", "--focus", "src/app.ts"]) == 0
    assert 'app.ts' in capsys.readouterr().out
