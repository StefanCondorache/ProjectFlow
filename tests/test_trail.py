import textwrap

from flowmap.dataflow import trace
from flowmap.flow import build_flow
from flowmap.project import load_project

SRC = '''
import json
import logging

log = logging.getLogger(__name__)


def read(path):
    with open(path) as fh:
        raw = json.load(fh)
    return clean(raw)


def clean(data):
    kept = {k: v for k, v in data.items() if v}
    return kept


def save(rows, dest):
    with open(dest, "w") as out:
        json.dump(rows, out)


def unrelated():
    pass


def main(path, verbose):
    cfg = read(path)
    unrelated()
    log.info("loaded %s", cfg)
    if verbose:
        print("verbose")
    if cfg.get("dry"):
        return None
    save(cfg, "out.json")


def collect(rows):
    out = []
    for r in rows:
        out.append(r)
    return out
'''


def project_of(tmp_path):
    (tmp_path / "m.py").write_text(textwrap.dedent(SRC))
    return load_project(tmp_path)


def names(graph) -> dict[str, str]:
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


def node(graph, label):
    return next(n for n in graph.nodes if n.label == label and n.kind not in ("start", "end"))


def trail_graph(project, root, at, var):
    return build_flow(project, root, trail=trace(project, root, at, var))


def test_parameter_followed_through_every_function_it_enters(tmp_path):
    g = trail_graph(project_of(tmp_path), "m.py::main", "s", "path")
    assert edges(g) == sorted([
        "START -> read [path]",
        "START:read -> open(path) [path]",
        "open(path) -> json.load(fh) [fh]",
        "json.load(fh) -> clean [raw]",
        "START:clean -> END:clean [data]",
        "clean -> END:read [result]",
        'read -> cfg.get("dry") [cfg]',
        "cfg.get(\"dry\") -> END [yes]",
        "cfg.get(\"dry\") -> save [no]",
        "START:save -> json.dump(rows, out) [rows]",
        "json.dump(rows, out) -> END:save",
        "save -> END",
    ])
    assert node(g, "read").kind == node(g, "clean").kind == node(g, "save").kind == "group"
    assert node(g, "json.dump(rows, out)").kind == "io"


def test_steps_that_do_not_touch_the_data_are_hidden(tmp_path):
    g = trail_graph(project_of(tmp_path), "m.py::main", "s", "path")
    labels = {n.label for n in g.nodes}
    assert not labels & {"unrelated", "verbose", 'open(dest, "w")', 'log.info("loaded %s", cfg)'}


def test_trail_from_a_step_output_does_not_open_the_step_that_made_it(tmp_path):
    project = project_of(tmp_path)
    read_step = node(build_flow(project, "m.py::main"), "read")
    g = trail_graph(project, "m.py::main", read_step.id, "cfg")
    assert node(g, "read").kind == "step"
    assert node(g, "read").detail["trail"] == "origin"
    assert "START -> read" in edges(g)
    assert 'read -> cfg.get("dry") [cfg]' in edges(g)
    assert node(g, "save").kind == "group"


def test_trail_can_start_inside_an_opened_step(tmp_path):
    project = project_of(tmp_path)
    opened = build_flow(project, "m.py::main", expanded={node(build_flow(project, "m.py::main"), "read").id})
    load = node(opened, "json.load(fh)")
    g = trail_graph(project, "m.py::main", load.id, "raw")
    visible = {n.label for n in g.nodes}
    assert "open(path)" not in visible  # before the data existed
    assert {"json.load(fh)", "clean", "save", "json.dump(rows, out)"} <= visible
    assert node(g, "read").kind == "group"


def test_data_put_into_a_container_keeps_flowing(tmp_path):
    g = trail_graph(project_of(tmp_path), "m.py::collect", "s", "rows")
    loop, append = node(g, "for r in rows"), node(g, "out.append(r)")
    assert append.kind == "use" and append.parent == loop.id
    assert "for r in rows -> END [out]" in edges(g)


def test_node_ids_do_not_depend_on_what_is_shown(tmp_path):
    project = project_of(tmp_path)
    full = build_flow(project, "m.py::main")
    sliced = trail_graph(project, "m.py::main", "s", "path")
    assert node(full, "save").id == node(sliced, "save").id
