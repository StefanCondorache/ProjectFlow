import textwrap

from flowmap.flow import build_flow
from flowmap.project import load_project
from flowmap.simulate import simulate

SHOP = '''
import os

DEFAULTS = {"db": "shop.db", "retries": 3}


def load(path):
    cfg = dict(DEFAULTS)
    cfg["path"] = path
    cfg["user"] = os.getenv("USER")
    return cfg


class Store:
    def __init__(self, cfg):
        self.db = cfg["db"]
        self.rows = self.fresh()

    def fresh(self):
        return []

    def add(self, row):
        self.rows.append(row)


def main(argv=None):
    cfg = load("config.json")
    cfg["mode"] = "train"
    store = Store(cfg)
    names = ["a", "b"]
    for name in names:
        store.add(name.upper())
    if cfg["retries"] > 5:
        raise ValueError("too many")
    total = len(names) * cfg["retries"]
    return total
'''


def project_of(tmp_path, src: str):
    (tmp_path / "m.py").write_text(textwrap.dedent(src))
    return load_project(tmp_path)


def node(graph, label: str):
    return next(n for n in graph.nodes if n.label == label and n.kind not in ("start", "end"))


def decode(encoded):
    """Encoded value back to plain Python, for comparisons (test-only helper)."""
    kind = encoded["t"]
    if kind == "val":
        return encoded["v"]
    if kind == "dict":
        return {k: decode(v) for k, v in encoded["v"].items()}
    if kind == "list":
        return [decode(v) for v in encoded["v"]]
    if kind == "obj":
        return (encoded["cls"], {k: decode(v) for k, v in encoded["v"].items()})
    return ("?", encoded["from"], {k: decode(v) for k, v in encoded.get("v", {}).items()})


def variables(frame):
    return {name: decode(value) for name, value in frame.stack[-1]["vars"].items()}


def opened_shop(tmp_path):
    project = project_of(tmp_path, SHOP)
    closed = build_flow(project, "m.py::main")
    expanded = {node(closed, "load").id, node(closed, "Store()").id}
    return project, closed, expanded, simulate(project, "m.py::main", expanded=expanded)


def test_known_values_are_carried_through_opened_steps(tmp_path):
    _, _, _, sim = opened_shop(tmp_path)
    assert sim.status == "done"
    first, last = sim.frames[0], sim.frames[-1]
    assert (first.node, variables(first), first.changes["added"]) == ("s", {"argv": None}, ["argv"])
    assert last.node == "e"
    assert variables(last)["total"] == 6
    assert variables(last)["cfg"] == {
        "db": "shop.db",
        "retries": 3,
        "path": "config.json",
        "user": ("?", 'os.getenv("USER")', {}),
        "mode": "train",
    }
    # add() was not opened, yet what it does to the store still happens
    assert variables(last)["store"] == ("Store", {"db": "shop.db", "rows": ["A", "B"]})


def test_inside_an_opened_step_the_panel_shows_its_own_variables(tmp_path):
    _, closed, _, sim = opened_shop(tmp_path)
    load = node(closed, "load").id
    inside = [f for f in sim.frames if f.node == f"{load}/e"]
    assert len(inside) == 1
    assert [s["label"] for s in inside[0].stack] == ["main", "load"]
    assert variables(inside[0]) == {
        "path": "config.json",
        "cfg": {"db": "shop.db", "retries": 3, "path": "config.json", "user": ("?", 'os.getenv("USER")', {})},
    }


def test_what_the_code_adds_shows_up_as_added(tmp_path):
    _, closed, _, sim = opened_shop(tmp_path)
    at_store = next(f for f in sim.frames if f.node == node(closed, "Store()").id and not f.hop)
    assert at_store.changes["added"] == ["cfg.mode"]


def test_leaving_an_opened_step_hands_its_result_to_the_caller(tmp_path):
    _, closed, _, sim = opened_shop(tmp_path)
    load = node(closed, "load").id
    back = next(f for f in sim.frames if f.node == load and f.hop)
    assert back.changes["added"] == ["cfg"]
    assert [s["label"] for s in back.stack] == ["main"]


def test_loops_run_once_per_known_item(tmp_path):
    _, closed, _, sim = opened_shop(tmp_path)
    adds = [f for f in sim.frames if f.node == node(closed, "Store.add").id]
    assert [variables(f)["name"] for f in adds] == ["a", "b"]
    assert adds[1].hop  # back round the loop, not along a drawn arrow


def test_a_known_condition_takes_its_branch_without_asking(tmp_path):
    _, closed, _, sim = opened_shop(tmp_path)
    decision = next(f for f in sim.frames if f.node == node(closed, 'cfg["retries"] > 5').id)
    assert decision.note.endswith("→ no")


def test_every_move_follows_a_drawn_arrow_unless_marked_as_a_hop(tmp_path):
    project, _, expanded, sim = opened_shop(tmp_path)
    graph = build_flow(project, "m.py::main", expanded=expanded)
    drawn = {(e.source, e.target) for e in graph.edges}
    ids = {n.id for n in graph.nodes}
    assert all(f.node in ids for f in sim.frames)
    for frame in sim.frames[1:]:
        assert frame.hop or tuple(frame.edge) in drawn, frame


QUESTION = '''
def done(): pass

def main(flag):
    if flag:
        raise ValueError("stop")
    done()
'''


def test_an_unknown_condition_stops_and_asks(tmp_path):
    project = project_of(tmp_path, QUESTION)
    decision = node(build_flow(project, "m.py::main"), "flag").id
    sim = simulate(project, "m.py::main")
    assert sim.status == "choose"
    assert sim.choice == {
        "node": decision,
        "question": "flag",
        "options": [{"label": "yes", "value": "yes"}, {"label": "no", "value": "no"}],
    }
    assert sim.frames[-1].node == decision


def test_answers_continue_down_the_chosen_branch(tmp_path):
    project = project_of(tmp_path, QUESTION)
    graph = build_flow(project, "m.py::main")
    decision = node(graph, "flag").id
    raised = simulate(project, "m.py::main", choices={decision: "yes"})
    assert (raised.status, raised.frames[-1].node) == ("raised", node(graph, "raise ValueError").id)
    finished = simulate(project, "m.py::main", choices={decision: "no"})
    assert finished.status == "done"
    assert [f.node for f in finished.frames[-2:]] == [node(graph, "done").id, "e"]


READ = '''
import json

def read(p):
    with open(p) as fh:
        return json.load(fh)

def main():
    data = read("in.json")
'''


def test_data_from_outside_is_named_after_where_it_came_from(tmp_path):
    project = project_of(tmp_path, READ)
    closed = simulate(project, "m.py::main")
    assert variables(closed.frames[-1])["data"] == ("?", 'read("in.json")', {})
    read = node(build_flow(project, "m.py::main"), "read").id
    opened = simulate(project, "m.py::main", expanded={read})
    assert variables(opened.frames[-1])["data"] == ("?", "json.load(fh)", {})
    graph = build_flow(project, "m.py::main", expanded={read})
    assert node(graph, "json.load(fh)").id in {f.node for f in opened.frames}


def test_builtins_on_unknown_values_read_like_the_code(tmp_path):
    project = project_of(tmp_path, '''
        from pathlib import Path
        def main():
            root = str(Path(__file__).parent)
            return root
    ''')
    sim = simulate(project, "m.py::main")
    assert variables(sim.frames[-1])["root"] == ("?", "str(Path(__file__).parent)", {})
