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
    if kind == "ref":
        return ("ref", encoded["v"])
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
        def main(base):
            root = str(Path(base).parent)
            return root
    ''')
    sim = simulate(project, "m.py::main")
    assert variables(sim.frames[-1])["root"] == ("?", "str(Path(base).parent)", {})


# ── no limits: loops, break/continue, exceptions ──────────────────────────


LOOPS = '''
def handle(x): pass

def main():
    total = 0
    for n in range(10):
        if n == 7:
            break
        if n % 2:
            continue
        total += n
        handle(n)
    else:
        total = -1
    count = 0
    while count < 5:
        count += 1
    return total
'''


def test_loops_run_every_item_and_obey_break_and_continue(tmp_path):
    project = project_of(tmp_path, LOOPS)
    sim = simulate(project, "m.py::main")
    assert sim.status == "done"
    assert (variables(sim.frames[-1])["total"], variables(sim.frames[-1])["count"]) == (12, 5)
    handled = [variables(f)["n"] for f in sim.frames if f.node == node(build_flow(project, "m.py::main"), "handle").id]
    assert handled == [0, 2, 4, 6]


LONG = '''
def handle(x): pass

def main():
    total = 0
    for n in range(1000):
        total += n
        handle(n)
    return total
'''


def test_long_loops_are_computed_in_full_but_only_the_first_rounds_are_shown(tmp_path):
    from flowmap.simulate import VISIBLE_ROUNDS

    project = project_of(tmp_path, LONG)
    sim = simulate(project, "m.py::main")
    graph = build_flow(project, "m.py::main")
    assert variables(sim.frames[-1])["total"] == 499500
    assert len([f for f in sim.frames if f.node == node(graph, "handle").id]) == VISIBLE_ROUNDS
    assert any(f"{1000 - VISIBLE_ROUNDS} more" in f.note for f in sim.frames)


WHILE = '''
def step(): pass

def main(feed):
    while feed.more():
        step()
    return 1
'''


def test_a_loop_whose_condition_cannot_be_known_asks_each_round(tmp_path):
    project = project_of(tmp_path, WHILE)
    loop = next(n for n in build_flow(project, "m.py::main").nodes if n.kind == "loop").id
    asked = simulate(project, "m.py::main")
    assert asked.status == "choose" and asked.choice["node"] == f"{loop}@1"
    assert [o["value"] for o in asked.choice["options"]] == ["yes", "no"]
    twice = simulate(project, "m.py::main", choices={f"{loop}@1": "yes", f"{loop}@2": "yes", f"{loop}@3": "no"})
    assert twice.status == "done"
    assert len([f for f in twice.frames if f.node.endswith(node(build_flow(project, "m.py::main"), "step").id)]) == 2


ERRORS = '''
class ShopError(Exception):
    pass

def charge(amount):
    if amount <= 0:
        raise ShopError("amount must be positive")
    return amount * 2

def recover(): pass

def main(amount):
    closed = False
    try:
        paid = charge(amount)
    except ShopError as err:
        recover()
        paid = 0
    finally:
        closed = True
    rows = {"a": 1}
    try:
        missing = rows["b"]
    except KeyError:
        missing = None
    return paid
'''


def test_exceptions_are_caught_by_the_matching_handler(tmp_path):
    project = project_of(tmp_path, ERRORS)
    graph = build_flow(project, "m.py::main")
    sim = simulate(project, "m.py::main", inputs={"amount": -5})
    assert sim.status == "done"
    last = variables(sim.frames[-1])
    assert (last["paid"], last["closed"], last["missing"]) == (0, True, None)
    assert "err" not in last  # like Python, the name goes away after its except block
    inside = next(f for f in sim.frames if f.node == node(graph, "recover").id)
    assert variables(inside)["err"] == ("ShopError", {"message": "amount must be positive"})
    handler = node(graph, "except ShopError").id
    caught = next(f for f in sim.frames if f.node == handler)
    assert caught.edge == [node(graph, "try").id, handler]  # along the red error arrow
    assert caught.error == {"type": "ShopError", "message": "amount must be positive", "file": "m.py", "line": 7}


def test_no_exception_no_handler(tmp_path):
    project = project_of(tmp_path, ERRORS)
    sim = simulate(project, "m.py::main", inputs={"amount": 5})
    assert variables(sim.frames[-1])["paid"] == 10
    assert not any(f.node == node(build_flow(project, "m.py::main"), "except ShopError").id for f in sim.frames)


def test_an_uncaught_exception_ends_the_run_with_its_details(tmp_path):
    project = project_of(tmp_path, '''
        def main():
            values = [1, 2]
            return values[5]
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.status == "raised"
    assert sim.error == {"type": "IndexError", "message": "list index out of range", "file": "m.py", "line": 4}


# ── real data ─────────────────────────────────────────────────────────────


def test_inputs_fill_the_parameters_and_build_project_objects(tmp_path):
    project = project_of(tmp_path, '''
        from dataclasses import dataclass

        @dataclass
        class Order:
            name: str
            amount: float

        def price(order: Order, rate=2):
            total = order.amount * rate
            return total
    ''')
    sim = simulate(project, "m.py::price", inputs={"order": {"name": "x", "amount": 3}})
    assert variables(sim.frames[-1])["total"] == 6
    assert variables(sim.frames[0])["order"] == ("Order", {"name": "x", "amount": 3})


ARGS = '''
import argparse
def run(): pass

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pipeline", action="store_true")
    p.add_argument("--regions", default="us")
    p.add_argument("--days", type=int, default=1)
    args = p.parse_args()
    if args.pipeline:
        run()
    return args.days * 2
'''


def test_command_line_arguments_go_through_argparse(tmp_path):
    project = project_of(tmp_path, ARGS)
    sim = simulate(project, "m.py::main", argv=["--pipeline", "--regions", "eu", "--days", "3"])
    assert sim.status == "done"
    assert variables(sim.frames[-1])["args"] == ("Namespace", {"pipeline": True, "regions": "eu", "days": 3})
    assert node(build_flow(project, "m.py::main"), "run").id in {f.node for f in sim.frames}


def test_bad_command_line_arguments_exit_like_the_real_program(tmp_path):
    sim = simulate(project_of(tmp_path, ARGS), "m.py::main", argv=["--bogus"])
    assert sim.status == "raised"
    assert sim.error["type"] == "SystemExit" and "unrecognized arguments: --bogus" in sim.error["message"]


FILES = '''
import json
import os

def main():
    with open("config.json") as fh:
        cfg = json.load(fh)
    user = os.getenv("SHOP_USER", "nobody")
    with open("out.json", "w") as out:
        json.dump({"db": cfg["db"], "user": user}, out)
    print("saved", cfg["db"])
    return cfg["db"]
'''


def test_project_files_are_read_and_writes_are_only_recorded(tmp_path):
    (tmp_path / "config.json").write_text('{"db": "real.db"}')
    project = project_of(tmp_path, FILES)
    sim = simulate(project, "m.py::main", env={"SHOP_USER": "stefan"})
    last = variables(sim.frames[-1])
    assert (last["cfg"], last["user"]) == ({"db": "real.db"}, "stefan")
    outputs = [o for f in sim.frames for o in f.outputs]
    assert [(o["target"], o["how"]) for o in outputs] == [("out.json", "json.dump"), ("console", "print")]
    assert decode(outputs[0]["data"]) == {"db": "real.db", "user": "stefan"}
    assert not (tmp_path / "out.json").exists()


def test_files_next_to_the_code_are_found_through_dunder_file(tmp_path):
    (tmp_path / "settings.json").write_text('{"mode": "fast"}')
    project = project_of(tmp_path, '''
        import json
        from pathlib import Path

        def main():
            path = Path(__file__).parent / "settings.json"
            settings = json.loads(path.read_text())
            return settings["mode"]
    ''')
    assert variables(simulate(project, "m.py::main").frames[-1])["settings"] == {"mode": "fast"}


def test_hidden_files_are_never_read(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    project = project_of(tmp_path, '''
        def main():
            with open(".env") as fh:
                text = fh.read()
            return text
    ''')
    assert variables(simulate(project, "m.py::main").frames[-1])["text"][0] == "?"


def test_settings_come_from_other_modules_and_the_given_environment(tmp_path):
    (tmp_path / "config.py").write_text('import os\nDB = os.getenv("DB", "local.db")\nHOME = os.environ["HOME"]\n')
    project = project_of(tmp_path, '''
        import config
        from config import DB

        def main():
            db = DB
            home = config.HOME
            return db
    ''')
    last = variables(simulate(project, "m.py::main", env={"DB": "prod.db", "HOME": "/home/x"}).frames[-1])
    assert (last["db"], last["home"]) == ("prod.db", "/home/x")
    unset = variables(simulate(project, "m.py::main", env={"DB": None}).frames[-1])
    assert unset["db"] == "local.db" and unset["home"][0] == "?"  # null: not set; missing: not known


def test_values_can_be_given_for_anything_from_outside(tmp_path):
    project = project_of(tmp_path, READ)
    read = node(build_flow(project, "m.py::main"), "read").id
    sim = simulate(project, "m.py::main", provided={read: {"rows": [1, 2]}})
    assert variables(sim.frames[-1])["data"] == {"rows": [1, 2]}


def test_pure_library_calls_are_computed(tmp_path):
    project = project_of(tmp_path, '''
        import json
        import math
        import os

        def main():
            data = json.loads('{"a": [1, 2, 3]}')
            root = math.sqrt(16)
            name = os.path.basename("/x/y/z.txt")
            return data["a"][2] + root
    ''')
    last = variables(simulate(project, "m.py::main").frames[-1])
    assert (last["data"], last["root"], last["name"]) == ({"a": [1, 2, 3]}, 4.0, "z.txt")


# ── start anywhere, and go inside the boxes ───────────────────────────────


CHAIN = '''
def step_a(x):
    return x + 1

def step_b(y):
    doubled = y * 10
    return doubled

def main(start):
    a = step_a(start)
    b = step_b(a)
    return b
'''


def test_a_simulation_can_start_at_any_node_with_given_data(tmp_path):
    from flowmap.simulate import expects

    project = project_of(tmp_path, CHAIN)
    at = node(build_flow(project, "m.py::main"), "step_b").id
    wanted = expects(project, "m.py::main", frozenset(), at)
    assert wanted["reached"] and set(wanted["vars"]) == {"start", "a"}
    sim = simulate(project, "m.py::main", start_at=at, inputs={"a": 4})
    assert sim.frames[0].node == at and sim.frames[0].edge is None
    assert variables(sim.frames[-1])["b"] == 40


def test_going_inside_opens_each_step_the_data_enters(tmp_path):
    project = project_of(tmp_path, CHAIN)
    graph = build_flow(project, "m.py::main")
    a, b = node(graph, "step_a").id, node(graph, "step_b").id
    sim = simulate(project, "m.py::main", inputs={"start": 1}, auto_open=True)
    assert {a, b} <= set(sim.expanded)
    inside_b = next(f for f in sim.frames if f.node == f"{b}/e")
    assert variables(inside_b) == {"y": 2, "doubled": 20}
    assert variables(sim.frames[-1])["b"] == 20


# ── what cannot be known is never an error ───────────────────────────────


def test_a_silent_step_that_may_return_early_gives_an_unknown_result(tmp_path):
    project = project_of(tmp_path, '''
        def pick(flag):
            if flag:
                return [1, 2]
            return None

        def same(flag):
            if flag:
                return 1
            else:
                return 1

        def main(flag):
            rows = pick(flag)
            one = same(flag)
            return rows
    ''')
    last = variables(simulate(project, "m.py::main").frames[-1])
    assert last["rows"][0] == "?" and last["one"] == 1


def test_unknown_parts_inside_known_containers_are_not_errors(tmp_path):
    project = project_of(tmp_path, '''
        def main(rows):
            pairs = [(a, b) for a, b in [rows, rows]]
            table = dict([rows])
            return pairs
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.status == "done", sim.error
    assert variables(sim.frames[-1])["table"][0] == "?"


def test_exiting_with_a_code_only_a_run_would_know_is_not_an_error(tmp_path):
    project = project_of(tmp_path, '''
        import sys
        def main(code):
            sys.exit(code)
    ''')
    assert simulate(project, "m.py::main").status == "done"


def test_after_a_loop_over_unknown_items_what_it_changed_is_not_known(tmp_path):
    project = project_of(tmp_path, '''
        class Box:
            def __init__(self):
                self.items = []
                self.name = "box"

        def main(rows):
            box = Box()
            count = 0
            seen = []
            for row in rows:
                count += 1
                seen.append(row)
                box.items.append(row)
            return count
    ''')
    last = variables(simulate(project, "m.py::main").frames[-1])
    assert last["count"] == ("?", "count after the loop over rows", {})
    assert last["seen"][0] == "?"
    assert last["box"] == ("Box", {"items": ("?", "box.items after the loop over rows", {}), "name": "box"})


def test_comprehension_variables_stay_inside_the_comprehension(tmp_path):
    project = project_of(tmp_path, '''
        def double(x):
            return x * 2

        def main():
            v = "mine"
            values = [double(v) for v in [1, 2, 3]]
            return values
    ''')
    last = variables(simulate(project, "m.py::main").frames[-1])
    assert last["v"] == "mine" and last["values"][0] == "?"


def test_a_call_inside_a_conditional_expression_runs_only_when_chosen(tmp_path):
    project = project_of(tmp_path, '''
        def main():
            settings = None
            pots = dict(settings) if settings else None
            return pots
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.status == "done" and variables(sim.frames[-1])["pots"] is None


def test_a_silent_step_that_only_may_fail_does_not_fail(tmp_path):
    project = project_of(tmp_path, '''
        def resolve(mode):
            if mode == "a":
                return 1
            if mode == "b":
                return 2
            raise ValueError(f"unknown mode {mode}")

        def main(mode):
            months = resolve(mode)
            return months
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.status == "done" and variables(sim.frames[-1])["months"][0] == "?"


def test_generators_hand_on_what_they_yield(tmp_path):
    project = project_of(tmp_path, '''
        def inner(block):
            for key, value in block.items():
                yield "g", key, value

        def outer(block):
            yield ("top", "x", 0)
            yield from inner(block)

        def main():
            names = []
            for group, key, value in outer({"a": 1, "b": 2}):
                names.append(key)
            return names
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.status == "done", sim.error
    assert variables(sim.frames[-1])["names"] == ["x", "a", "b"]


def test_a_change_at_a_place_only_a_run_would_know_makes_the_container_unknown(tmp_path):
    project = project_of(tmp_path, '''
        class Box:
            def __init__(self):
                self.slots = {"a": []}
                self.name = "box"

        def main(kind, x):
            out = {"a": [], "b": []}
            out[kind].append(x)
            seen = {}
            seen[kind] = x
            box = Box()
            box.slots[kind].append(x)
            return out
    ''')
    last = variables(simulate(project, "m.py::main").frames[-1])
    assert last["out"][0] == "?" and last["seen"][0] == "?"
    assert last["box"][1]["slots"][0] == "?" and last["box"][1]["name"] == "box"


def test_messages_show_what_is_known_of_them(tmp_path):
    project = project_of(tmp_path, '''
        def main(missing):
            count = 2
            print(f"checked {count} tables, missing {missing}")
            raise ValueError(f"Missing columns: {missing} ({count})")
    ''')
    sim = simulate(project, "m.py::main")
    assert sim.error["message"] == "Missing columns: ‹missing› (2)"
    printed = [o for f in sim.frames for o in f.outputs]
    assert decode(printed[0]["data"]) == "checked 2 tables, missing ‹missing›"


def test_logged_errors_read_as_their_message(tmp_path):
    project = project_of(tmp_path, '''
        import logging
        log = logging.getLogger("m")

        def main():
            try:
                raise KeyError("apple")
            except KeyError as err:
                log.warning("skipping: %s", err)
    ''')
    outputs = [o for f in simulate(project, "m.py::main").frames for o in f.outputs]
    assert [(o["target"], o["how"], decode(o["data"])) for o in outputs] == [("log", "warning", "skipping: 'apple'")]
