"""Flow diagrams: what a function does, from START to END, Mermaid-style.

A diagram keeps only what explains the flow:

- steps: calls into the project's own functions and classes,
- I/O: calls that move data into or out of the program,
- the branches, loops and error paths around them,
- the exits: returns (edges to END) and raises.

Edges carry the data a step hands on (the variables it assigns). Opening a
step nests the called function's own diagram inside it, so a flow can be
followed down through files and functions as deep as needed.

With a ``Trail`` (see ``flowmap.dataflow``) the same diagram is sliced to one
piece of data: only what touches it is drawn, and every call it enters is
opened.

Node ids come from source positions (``c12.8`` is the call at line 12, column
8; an opened step's nodes are prefixed with its id and a slash), so the same
node keeps its id whatever else is shown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from flowmap.ir import Call, Function, Handler, If, Item, Loop, Match, Raise, Return, Try, walk
from flowmap.project import Project

if TYPE_CHECKING:
    from flowmap.dataflow import Trail

_SIMPLE_VALUE = re.compile(r"[\w.]{1,40}")
_KEY_LETTER = {Call: "c", If: "d", Match: "m", Loop: "l", Try: "t", Raise: "r", Handler: "h"}

Port = tuple[str, str, str]  # (node id, edge label, edge kind) waiting for the next node


@dataclass
class Node:
    id: str
    kind: str  # start | end | step | io | use | decision | loop | try | handler | raise | group
    label: str
    parent: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    label: str = ""
    kind: str = "flow"  # flow | yes | no | case | error
    data: list[str] = field(default_factory=list)  # variables behind a data label ($n = a nested call's result)


@dataclass
class Graph:
    root: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


def item_key(item: Item | Handler) -> str:
    """Id of an item within its function, from its source position."""
    return f"{_KEY_LETTER[type(item)]}{item.line}.{item.col}"


def build_flow(
    project: Project,
    root: str,
    expanded: set[str] | frozenset[str] = frozenset(),
    depth: int = 0,
    start_label: str | None = None,
    trail: Trail | None = None,
) -> Graph:
    """Diagram of ``root``. Steps whose ids are in ``expanded``, and every step
    less than ``depth`` levels down, are opened in place. A ``trail`` replaces
    both: the diagram then shows one piece of data's path."""
    graph = Graph(root)

    def should_open(node: Node, level: int) -> bool:
        if trail is not None:
            return node.id in trail.opened
        return bool(node.detail.get("expandable")) and (node.id in expanded or level < depth)

    def add(fid: str, prefix: str, parent: str | None, path: frozenset[str], level: int) -> None:
        fragment = _Fragment(
            project, project.functions[fid], prefix, parent, path, start_label if parent is None else None, trail
        )
        graph.nodes.extend(fragment.nodes)
        graph.edges.extend(fragment.edges)
        for node in fragment.nodes:
            if node.kind == "step" and node.detail.get("target") and should_open(node, level):
                target = node.detail["target"]
                if target in path:
                    continue
                node.kind = "group"
                add(target, f"{node.id}/", node.id, path | {target}, level + 1)

    add(root, "", None, frozenset({root}), 0)
    return graph


def display_name(project: Project, fn: Function) -> str:
    if fn.name == "__init__" and fn.cls in project.classes:
        return f"{project.classes[fn.cls].name}()"
    if fn.name == "<main>":
        return fn.file
    return fn.qualname.replace(".<locals>.", ".")


def _visible(call: Call) -> str | None:
    if call.kind in ("project", "class"):
        return "step"
    if call.io and call.io[0] != "console":
        return "io"
    return None


def _shows(items: list[Item]) -> bool:
    """Whether a block has anything a diagram would draw."""
    return any(
        (isinstance(i, Call) and _visible(i))
        or isinstance(i, Raise)
        or (isinstance(i, Return) and i.kind == "return")
        for i in walk(items)
    )


def has_content(fn: Function) -> bool:
    """Whether opening a call to ``fn`` would show anything."""
    for item in walk(fn.body):
        if isinstance(item, Call) and _visible(item) or isinstance(item, Raise):
            return True
        if isinstance(item, (If, Loop, Try, Match)) and _shows([item]):
            return True
    return False


def _data_label(names: list[str]) -> str:
    return ", ".join("result" if n.startswith("$") else n for n in dict.fromkeys(names))


class _Fragment:
    """The diagram of one function body."""

    def __init__(
        self,
        project: Project,
        fn: Function,
        prefix: str,
        parent: str | None,
        path: frozenset[str],
        start_label: str | None,
        trail: Trail | None,
    ):
        self.project = project
        self.fn = fn
        self.prefix = prefix
        self.path = path
        self.trail = trail
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.taken: set[str] = set()
        self.carries: dict[str, list[str]] = {}  # node id -> variables its outgoing flow edges carry
        self.end = f"{prefix}e"

        skip_self = bool(fn.cls and fn.params and "staticmethod" not in fn.decorators)
        params = [p.name for p in fn.params[1 if skip_self else 0 :]]
        inner = parent is not None
        start = self._node(
            "start",
            start_label or display_name(project, fn),
            parent,
            key="s",
            inner=inner,
            function=fn.id,
            file=fn.file,
            line=fn.line,
            params=params,
            doc=fn.doc,
        )
        carried = trail.names.get(start, []) if trail is not None else params
        self.carries[start] = list(carried)
        ports = self._seq(fn.body, [(start, _data_label(carried), "flow")], parent)
        self.nodes.append(Node(self.end, "end", "END", parent, {"inner": inner}))
        self._connect(ports, self.end)

    # building blocks

    def _node(self, kind: str, label: str, parent: str | None, key: str, **detail) -> str:
        nid = f"{self.prefix}{key}"
        while nid in self.taken:  # same position twice: keep ids unique
            nid += "~"
        self.taken.add(nid)
        detail.setdefault("at", self.fn.file)  # the file this node's own code is in
        if self.trail is not None:
            detail["trail"] = "origin" if nid == self.trail.origin else "on"
        self.nodes.append(Node(nid, kind, label, parent, detail))
        return nid

    def _connect(self, ports: list[Port], target: str, fallback_label: str = "") -> None:
        for source, label, kind in ports:
            data = self.carries.get(source, []) if kind == "flow" and label else []
            self.edges.append(Edge(source, target, label or fallback_label, kind, list(data)))

    def _seq(self, items: list[Item], ports: list[Port], parent: str | None) -> list[Port]:
        for item in items:
            ports = self._item(item, ports, parent)
        return ports

    def _kept(self, item: Item | Handler) -> bool:
        if self.trail is not None:
            return f"{self.prefix}{item_key(item)}" in self.trail.keep
        if isinstance(item, Call):
            return _visible(item) is not None
        if isinstance(item, Loop):
            return _shows(item.body)
        if isinstance(item, Handler):
            return _shows(item.body)
        if isinstance(item, Raise):
            return True
        return _shows([item])

    def _carried(self, nid: str, call: Call) -> str:
        """Label for the edge leaving a call: the data it hands on."""
        if self.trail is not None:
            self.carries[nid] = list(self.trail.names.get(nid, []))
        else:
            self.carries[nid] = [d for d in call.defs if not d.startswith("$")]
        return _data_label(self.carries[nid])

    # items

    def _item(self, item: Item, ports: list[Port], parent: str | None) -> list[Port]:
        if isinstance(item, Call):
            if not self._kept(item):
                return ports
            nid = self._call(item, parent)
            self._connect(ports, nid)
            return [(nid, self._carried(nid, item), "flow")]

        if isinstance(item, If):
            if not self._kept(item):
                return ports
            decision = self._node("decision", item.cond, parent, item_key(item), line=item.line)
            self._connect(ports, decision)
            yes = self._seq(item.then, [(decision, "yes", "yes")], parent)
            no = self._seq(item.orelse, [(decision, "no", "no")], parent)
            return yes + no

        if isinstance(item, Match):
            if not self._kept(item):
                return ports
            decision = self._node("decision", f"match {item.subject}", parent, item_key(item), line=item.line)
            self._connect(ports, decision)
            out: list[Port] = []
            for case in item.cases:
                out += self._seq(case.body, [(decision, case.pattern, "case")], parent)
            if not any(c.pattern.split(" if ")[0].strip() == "_" for c in item.cases):
                out.append((decision, "no match", "case"))
            return out

        if isinstance(item, Loop):
            if not self._kept(item):
                return ports
            frame = self._node("loop", item.header, parent, item_key(item), line=item.line, loop=item.kind)
            self._connect(ports, frame)
            self._seq(item.body, [], frame)
            exits: list[Port] = [(frame, "", "flow")]
            return self._seq(item.orelse, exits, parent) if item.orelse else exits

        if isinstance(item, Try):
            if not self._kept(item):
                return ports
            frame = self._node("try", "try", parent, item_key(item), line=item.line)
            self._connect(ports, frame)
            self._seq(item.body, [], frame)
            exits = [(frame, "", "flow")]
            for handler in item.handlers:
                if not self._kept(handler):
                    continue
                label = f"except {handler.types}" if handler.types else "except"
                box = self._node("handler", label, parent, item_key(handler), line=handler.line)
                self.edges.append(Edge(frame, box, handler.types or "error", "error"))
                if self._seq(handler.body, [], box):
                    exits.append((box, "", "flow"))
            if item.orelse:
                exits = self._seq(item.orelse, exits, parent)
            if item.final:
                exits = self._seq(item.final, exits, parent)
            return exits

        if isinstance(item, Return):
            if item.kind != "return":
                return ports
            value = item.value if _SIMPLE_VALUE.fullmatch(item.value) else ""
            self._connect(ports, self.end, value)
            return []

        if isinstance(item, Raise):
            if not self._kept(item):
                return ports
            name = item.exc.split("(", 1)[0].strip()
            label = f"raise {name}" if name else "re-raise"
            nid = self._node("raise", label, parent, item_key(item), line=item.line, text=item.exc)
            self._connect(ports, nid)
            return []

        return ports  # Assign: data only

    def _call(self, call: Call, parent: str | None) -> str:
        key = item_key(call)
        if self.trail is not None and f"{self.prefix}{key}" in self.trail.uses:
            return self._node("use", call.text, parent, key, line=call.line, text=call.text, external=call.external)
        if call.kind not in ("project", "class"):
            return self._io(call, parent, key)
        return self._step(call, parent, key)

    def _step(self, call: Call, parent: str | None, key: str) -> str:
        project = self.project
        common = dict(
            line=call.line,
            text=call.text,
            confidence=call.confidence,
            args=[a.text for a in call.args],
            defs=[d for d in call.defs if not d.startswith("$")],
        )
        if call.kind == "class":
            cls = project.classes.get(call.target or "")
            label = f"{cls.name}()" if cls else call.callee
            return self._node(
                "step", label, parent, key, target=None, cls=call.target, file=cls.file if cls else None,
                doc=cls.doc if cls else None, expandable=False, recursive=False, **common,
            )
        fn = project.functions[call.target]
        doc = fn.doc
        if doc is None and fn.name == "__init__" and fn.cls in project.classes:
            doc = project.classes[fn.cls].doc
        recursive = fn.id in self.path
        return self._node(
            "step",
            display_name(project, fn),
            parent,
            key,
            target=fn.id,
            file=fn.file,
            def_line=fn.line,
            doc=doc,
            expandable=not recursive and has_content(fn),
            recursive=recursive,
            **common,
        )

    def _io(self, call: Call, parent: str | None, key: str) -> str:
        return self._node(
            "io",
            call.text,
            parent,
            key,
            io=list(call.io) if call.io else ["", ""],
            external=call.external,
            line=call.line,
            text=call.text,
            defs=[d for d in call.defs if not d.startswith("$")],
        )
