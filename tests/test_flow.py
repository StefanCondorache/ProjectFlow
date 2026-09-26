import textwrap

from flowmap.flow import build_flow
from flowmap.project import load_project


def project_of(tmp_path, src: str):
    (tmp_path / "m.py").write_text(textwrap.dedent(src))
    return load_project(tmp_path)


def names(graph) -> dict[str, str]:
    """Readable node names; START/END of an opened step carry its label."""
    labels = {n.id: n.label for n in graph.nodes}
    out = {}
    for n in graph.nodes:
        if n.kind in ("start", "end"):
            base = "START" if n.kind == "start" else "END"
            out[n.id] = f"{base}:{labels[n.parent]}" if n.parent else base
        else:
            out[n.id] = n.label
    return out


def edges(graph) -> list[str]:
    nm = names(graph)
    return sorted(f"{nm[e.source]} -> {nm[e.target]}" + (f" [{e.label}]" if e.label else "") for e in graph.edges)


def flow(tmp_path, src: str, fn: str = "main", **kw):
    return build_flow(project_of(tmp_path, src), f"m.py::{fn}", **kw)


def node(graph, label: str):
    return next(n for n in graph.nodes if n.label == label and n.kind not in ("start", "end"))


def test_steps_follow_execution_order_and_carry_the_data_they_produce(tmp_path):
    g = flow(tmp_path, '''
        def load(): return {}
        def fetch(cfg): return [cfg]
        def save(data): pass
        def main():
            cfg = load()
            data = fetch(cfg)
            save(data)
    ''')
    assert edges(g) == sorted(["START -> load", "load -> fetch [cfg]", "fetch -> save [data]", "save -> END"])
    carried = {(e.source, e.target): e.data for e in g.edges}
    assert carried[(node(g, "load").id, node(g, "fetch").id)] == ["cfg"]


def test_edges_keep_the_variables_behind_their_labels(tmp_path):
    g = flow(tmp_path, '''
        def inner(x): return x
        def outer(y): pass
        def main(a, b):
            outer(inner(a))
    ''')
    start = next(n for n in g.nodes if n.kind == "start")
    first = next(e for e in g.edges if e.source == start.id)
    assert (first.label, first.data) == ("a, b", ["a", "b"])
    from_inner = next(e for e in g.edges if e.source == node(g, "inner").id)
    assert from_inner.data == [] and from_inner.label == ""


def test_outside_calls_and_plain_assignments_are_left_out(tmp_path):
    g = flow(tmp_path, '''
        def main(items):
            n = len(items)
            total = n + 1
            print(total)
    ''')
    assert edges(g) == ["START -> END [items]"]


def test_if_else_branches_meet_again(tmp_path):
    g = flow(tmp_path, '''
        def a(): pass
        def b(): pass
        def c(): pass
        def main(flag):
            if flag:
                a()
            else:
                b()
            c()
    ''')
    assert edges(g) == sorted([
        "START -> flag [flag]",
        "flag -> a [yes]",
        "flag -> b [no]",
        "a -> c",
        "b -> c",
        "c -> END",
    ])
    assert node(g, "flag").kind == "decision"


def test_if_without_else_skips_straight_to_the_next_step(tmp_path):
    g = flow(tmp_path, '''
        def a(): pass
        def c(): pass
        def main(flag):
            if flag:
                a()
            c()
    ''')
    assert edges(g) == sorted(["START -> flag [flag]", "flag -> a [yes]", "flag -> c [no]", "a -> c", "c -> END"])


def test_early_return_goes_straight_to_end(tmp_path):
    g = flow(tmp_path, '''
        def a(): pass
        def main(x):
            if not x:
                return None
            a()
    ''')
    assert edges(g) == sorted(["START -> not x [x]", "not x -> END [yes]", "not x -> a [no]", "a -> END"])


def test_branch_with_nothing_to_show_is_dropped(tmp_path):
    g = flow(tmp_path, '''
        def a(): pass
        def main(x):
            if x:
                y = 1
            a()
    ''')
    assert edges(g) == ["START -> a [x]", "a -> END"]


def test_loop_is_a_frame_around_its_steps(tmp_path):
    g = flow(tmp_path, '''
        def process(it): pass
        def done(): pass
        def main(items):
            for it in items:
                process(it)
            done()
    ''')
    loop = node(g, "for it in items")
    assert loop.kind == "loop"
    assert node(g, "process").parent == loop.id
    assert edges(g) == sorted(["START -> for it in items [items]", "for it in items -> done", "done -> END"])


def test_try_except_has_an_error_path(tmp_path):
    g = flow(tmp_path, '''
        def risky(): pass
        def recover(): pass
        def finish(): pass
        def main():
            try:
                risky()
            except ValueError:
                recover()
            finish()
    ''')
    tried, handler = node(g, "try"), node(g, "except ValueError")
    assert node(g, "risky").parent == tried.id
    assert node(g, "recover").parent == handler.id
    assert edges(g) == sorted([
        "START -> try",
        "try -> except ValueError [ValueError]",
        "try -> finish",
        "except ValueError -> finish",
        "finish -> END",
    ])
    assert next(e for e in g.edges if e.target == handler.id).kind == "error"


def test_raise_ends_its_path(tmp_path):
    g = flow(tmp_path, '''
        def a(): pass
        def main(bad):
            if bad:
                raise ValueError("no")
            a()
    ''')
    assert node(g, "raise ValueError").kind == "raise"
    assert edges(g) == sorted(["START -> bad [bad]", "bad -> raise ValueError [yes]", "bad -> a [no]", "a -> END"])


def test_data_entering_the_program_is_an_io_step(tmp_path):
    g = flow(tmp_path, '''
        import json
        def use(cfg): pass
        def main(path):
            with open(path) as fh:
                cfg = json.load(fh)
            use(cfg)
    ''')
    assert node(g, "json.load(fh)").kind == "io"
    assert node(g, "json.load(fh)").detail["io"] == ["file", "in"]
    assert (node(g, "json.load(fh)").detail["at"], node(g, "json.load(fh)").detail["line"]) == ("m.py", 6)
    assert edges(g) == sorted([
        "START -> open(path) [path]",
        "open(path) -> json.load(fh) [fh]",
        "json.load(fh) -> use [cfg]",
        "use -> END",
    ])


def test_match_has_one_path_per_case(tmp_path):
    g = flow(tmp_path, '''
        def go(): pass
        def idle(): pass
        def main(cmd):
            match cmd:
                case "go":
                    go()
                case _:
                    idle()
    ''')
    assert edges(g) == sorted([
        "START -> match cmd [cmd]",
        'match cmd -> go ["go"]',
        "match cmd -> idle [_]",
        "go -> END",
        "idle -> END",
    ])


SRC_NESTED = '''
    def helper(v): pass
    def inner(x):
        """Does the inner work."""
        helper(x)
    def main():
        inner(1)
'''


def test_opening_a_step_nests_the_called_function_inside_it(tmp_path):
    project = project_of(tmp_path, SRC_NESTED)
    closed = build_flow(project, "m.py::main")
    step = node(closed, "inner")
    assert step.kind == "step" and step.detail["expandable"]
    assert step.detail["doc"] == "Does the inner work."

    opened = build_flow(project, "m.py::main", expanded={step.id})
    group = node(opened, "inner")
    assert group.kind == "group"
    assert "START:inner -> helper [x]" in edges(opened)
    assert "helper -> END:inner" in edges(opened)
    assert node(opened, "helper").parent == group.id


def test_depth_opens_every_step_down_to_that_level(tmp_path):
    project = project_of(tmp_path, SRC_NESTED)
    assert node(build_flow(project, "m.py::main", depth=1), "inner").kind == "group"
    assert node(build_flow(project, "m.py::main", depth=0), "inner").kind == "step"


def test_recursive_call_cannot_be_opened_again(tmp_path):
    project = project_of(tmp_path, '''
        def walk(n):
            if n:
                walk(n - 1)
    ''')
    closed = build_flow(project, "m.py::walk")
    step = node(closed, "walk")
    assert step.detail["recursive"] and not step.detail["expandable"]
    assert node(build_flow(project, "m.py::walk", expanded={step.id}), "walk").kind == "step"


def test_constructor_step_and_entry_start_label(tmp_path):
    g = flow(tmp_path, '''
        class Store:
            """Keeps things."""
            def __init__(self, path):
                self.path = path
        def main():
            s = Store("x")
    ''', start_label="python m.py")
    step = node(g, "Store()")
    assert step.detail["target"] == "m.py::Store.__init__"
    assert step.detail["doc"] == "Keeps things."
    assert (step.detail["file"], step.detail["line"]) == ("m.py", 7)
    assert next(n for n in g.nodes if n.kind == "start").label == "python m.py"
