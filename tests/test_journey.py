from pathlib import Path

from test_trail import project_of

from flowmap.dataflow import trace
from flowmap.journey import build_journey
from flowmap.project import load_project

SHOP = Path(__file__).parent / "fixtures" / "shop"


def labelled(graph) -> dict[str, str]:
    return {n.id: ("START" if n.kind == "start" else n.label) for n in graph.nodes}


def edges(graph) -> list[str]:
    nm = labelled(graph)
    return sorted(f"{nm[e.source]} -> {nm[e.target]}" + (f" [{e.label}]" if e.label else "") for e in graph.edges)


def files(graph) -> dict[str, set[str]]:
    frames = {n.id: n.label for n in graph.nodes if n.kind == "file"}
    out: dict[str, set[str]] = {label: set() for label in frames.values()}
    for n in graph.nodes:
        if n.parent in frames and n.kind == "step":
            out[frames[n.parent]].add(n.label)
    return out


def test_journey_shows_each_function_the_data_passes_through(tmp_path):
    project = project_of(tmp_path)
    graph = build_journey(project, trace(project, "m.py::main", "s", "path"))
    assert files(graph) == {"m.py": {"main", "read", "clean", "save"}}
    # only forward arrows, so the map reads top-down; handing the data back is a mark on the box
    assert edges(graph) == sorted([
        "START -> main [path]",
        "main -> read [path]",
        "read -> clean [data]",
        "main -> save [rows]",
        "read -> open(path)",
        "read -> json.load(fh)",
        "save -> json.dump(rows, out)",
    ])
    returns = {n.label: n.detail["returns"] for n in graph.nodes if n.kind == "step"}
    assert returns == {"main": False, "read": True, "clean": True, "save": False}


def test_journey_across_files_groups_functions_by_file():
    project = load_project(SHOP)
    graph = build_journey(project, trace(project, "src/shop/cli.py::main", "s", "argv"))
    by_file = files(graph)
    assert by_file["src/shop/cli.py"] == {"main", "parse"}
    assert {"make_service", "OrderService()", "OrderService.place"} <= by_file["src/shop/orders.py"]
    assert by_file["src/shop/payments/gateway.py"] == {"Gateway.charge"}
    assert {"Store()", "Store.save"} <= by_file["src/shop/store.py"]
    assert "OrderService.place -> Gateway.charge [amount]" in edges(graph)
    io = {n.label for n in graph.nodes if n.kind == "io"}
    assert any(label.startswith("requests.post(") for label in io)
    assert any(label.startswith("self.conn.execute(") for label in io)
