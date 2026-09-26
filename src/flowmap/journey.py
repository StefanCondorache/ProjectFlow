"""The journey of one piece of data: every function it passes through,
grouped by the file each function lives in.

Where the step diagram (``build_flow`` with a trail) shows everything that
happens to the data, the journey is the map: one box per function, one frame
per file, an arrow for "passes it in as ...", plus the I/O where it enters or
leaves the program. Arrows only point forward, so the map reads top-down; a
function that hands the data back to its caller is marked on its box. It stays
readable when the data touches half the project.
"""

from __future__ import annotations

from flowmap.dataflow import Context, Trail
from flowmap.flow import Edge, Graph, Node, display_name
from flowmap.project import Project


def _nearest_active(trail: Trail, prefix: str | None) -> Context | None:
    while prefix is not None:
        ctx = trail.contexts.get(prefix)
        if ctx is None:
            return None
        if ctx.active:
            return ctx
        prefix = ctx.parent
    return None


def build_journey(project: Project, trail: Trail) -> Graph:
    root = trail.contexts.get("")
    graph = Graph(root.function if root else "")
    var = "result" if trail.var.startswith("$") else trail.var
    start = Node("origin", "start", var, None, {"var": trail.var, "origin": trail.origin})
    graph.nodes.append(start)

    frames: dict[str, str] = {}
    boxes: dict[str, Node] = {}

    def frame(file: str) -> str:
        if file not in frames:
            frames[file] = f"file:{file}"
            graph.nodes.append(Node(frames[file], "file", file, None, {"file": file}))
        return frames[file]

    active = [ctx for ctx in trail.contexts.values() if ctx.active]
    for ctx in active:
        fn = project.functions[ctx.function]
        box = boxes.get(fn.id)
        if box is None:
            box = Node(
                f"fn:{fn.id}",
                "step",
                display_name(project, fn),
                frame(fn.file),
                {"target": fn.id, "file": fn.file, "def_line": fn.line, "doc": fn.doc, "receives": [], "returns": False},
            )
            boxes[fn.id] = box
            graph.nodes.append(box)
        box.detail["receives"] = sorted(set(box.detail["receives"]) | set(ctx.params))
        box.detail["returns"] = box.detail["returns"] or ctx.returns

    seen: set[tuple[str, str, str]] = set()

    def connect(source: str, target: str, label: str, kind: str, data: list[str]) -> None:
        if (source, target, kind) not in seen and source != target:
            seen.add((source, target, kind))
            graph.edges.append(Edge(source, target, label, kind, data))

    origin_prefix = trail.origin.rsplit("/", 1)[0] + "/" if "/" in trail.origin else ""
    origin = trail.contexts.get(origin_prefix)
    if origin is not None and origin.function in boxes:
        connect(start.id, boxes[origin.function].id, var, "flow", [trail.var])

    for ctx in active:
        caller = _nearest_active(trail, ctx.parent)
        if caller is None:
            continue
        here, there = boxes[ctx.function].id, boxes[caller.function].id
        connect(there, here, ", ".join(ctx.params), "flow", list(ctx.params))

    for ctx in active:
        fn = project.functions[ctx.function]
        for nid, text, io in ctx.io:
            if all(n.id != nid for n in graph.nodes):
                graph.nodes.append(Node(nid, "io", text, frame(fn.file), {"io": io, "at": fn.file, "text": text}))
            connect(boxes[fn.id].id, nid, "", "flow", [])
    return graph
