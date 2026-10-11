"""Load a project folder: discover its files, extract them, resolve calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flowmap.inventory import Inventory, build_inventory, contained_path
from flowmap.entries import Entry, find_commands, merge_entries
from flowmap.ir import ClassDef, Function, Module
from flowmap.lang import adapters


@dataclass
class Project:
    root: Path
    modules: dict[str, Module]
    functions: dict[str, Function]
    classes: dict[str, ClassDef]
    entries: list[Entry] = field(default_factory=list)
    resolvers: dict[str, object] = field(default_factory=dict)  # language -> its linker, for type questions
    inventory: Inventory = field(default_factory=Inventory)
    relationships: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.root.name


def load_project(root: str | Path) -> Project:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a project folder: {root}")
    inventory = build_inventory(root)
    langs = adapters()
    by_ext = {ext: adapter for adapter in langs for ext in adapter.extensions}
    modules: dict[str, Module] = {}
    for rel, item in inventory.files.items():
        adapter = by_ext.get(Path(rel).suffix)
        if adapter is None:
            continue
        try:
            source = contained_path(root, rel).read_bytes()
        except (OSError, ValueError):
            continue
        modules[rel] = adapter.extract(rel, source)
        item.analysis = "semantic"
    commands = find_commands(root)
    candidates: list[Entry] = []
    resolvers: dict[str, object] = {}
    for adapter in langs:
        own = {f: m for f, m in modules.items() if m.language == adapter.language}
        resolvers[adapter.language] = adapter.link(root, own)
        candidates += adapter.entries(root, own, commands)
    functions = {fid: fn for m in modules.values() for fid, fn in m.functions.items()}
    classes = {cid: c for m in modules.values() for cid, c in m.classes.items()}
    project = Project(root, modules, functions, classes, merge_entries(candidates, functions), resolvers, inventory)
    from flowmap.overview import extract_relationships

    project.relationships = extract_relationships(project)
    return project
