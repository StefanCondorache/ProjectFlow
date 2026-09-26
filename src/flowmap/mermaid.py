"""Export a flow diagram as Mermaid flowchart text."""

from __future__ import annotations

from collections import defaultdict

from flowmap.flow import Graph, Node

_FRAMES = {"loop", "try", "handler", "group", "file"}
_IO_VERBS = {"in": "reads", "out": "writes", "inout": "uses"}
# Mermaid keywords such as "end" cannot be class names, hence the mapping.
_CLASS_OF = {"start": "terminal", "end": "terminal", "io": "io", "decision": "decision", "raise": "failure"}
_CLASS_DEFS = [
    "  classDef terminal fill:#1f2937,stroke:#1f2937,color:#ffffff",
    "  classDef io fill:#ecfeff,stroke:#0e7490,color:#164e63",
    "  classDef decision fill:#fffbeb,stroke:#b45309,color:#78350f",
    "  classDef failure fill:#fef2f2,stroke:#b91c1c,color:#7f1d1d",
]


def _esc(text: str) -> str:
    return text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")


def _shape(node: Node) -> str:
    label = _esc(node.label)
    if node.kind in ("start", "end"):
        return f'(["{label}"])'
    if node.kind == "decision":
        return f'{{"{label}"}}'
    if node.kind == "io":
        verb = _IO_VERBS.get(node.detail.get("io", ["", ""])[1], "uses")
        return f'[/"{verb}: {label}"/]'
    if node.kind == "raise":
        return f'>"{label}"]'
    file = node.detail.get("file")
    return f'["{label}<br/>{_esc(file)}"]' if file else f'["{label}"]'


def _title(node: Node) -> str:
    if node.kind == "group" and node.detail.get("file"):
        return f"{node.label} · {node.detail['file']}"
    return node.label


def to_mermaid(graph: Graph) -> str:
    ids = {n.id: f"n{i}" for i, n in enumerate(graph.nodes)}
    children: dict[str | None, list[Node]] = defaultdict(list)
    for node in graph.nodes:
        children[node.parent].append(node)

    lines = ["flowchart TD"]

    def emit(parent: str | None, indent: str) -> None:
        for node in children.get(parent, []):
            if node.kind in _FRAMES:
                lines.append(f'{indent}subgraph {ids[node.id]}["{_esc(_title(node))}"]')
                lines.append(f"{indent}  direction TB")
                emit(node.id, indent + "  ")
                lines.append(f"{indent}end")
            else:
                lines.append(f"{indent}{ids[node.id]}{_shape(node)}")

    emit(None, "  ")
    for edge in graph.edges:
        arrow = "-.->" if edge.kind in ("error", "return") else "-->"
        label = f'|"{_esc(edge.label)}"|' if edge.label else ""
        lines.append(f"  {ids[edge.source]} {arrow}{label} {ids[edge.target]}")

    lines += _CLASS_DEFS
    members: dict[str, list[str]] = defaultdict(list)
    for node in graph.nodes:
        if node.kind in _CLASS_OF:
            members[_CLASS_OF[node.kind]].append(ids[node.id])
    for cls, node_ids in members.items():
        lines.append(f"  class {','.join(node_ids)} {cls}")
    return "\n".join(lines) + "\n"
