"""Load a project folder: discover its files, extract them, resolve calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flowmap.discover import discover_files
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

    @property
    def name(self) -> str:
        return self.root.name


def load_project(root: str | Path) -> Project:
    root = Path(root).resolve()
    langs = adapters()
    by_ext = {ext: adapter for adapter in langs for ext in adapter.extensions}
    modules: dict[str, Module] = {}
    for rel in discover_files(root, extensions=tuple(by_ext)):
        try:
            source = (root / rel).read_bytes()
        except OSError:
            continue
        modules[rel] = by_ext[Path(rel).suffix].extract(rel, source)
    commands = find_commands(root)
    candidates: list[Entry] = []
    for adapter in langs:
        own = {f: m for f, m in modules.items() if m.language == adapter.language}
        adapter.link(root, own)
        candidates += adapter.entries(root, own, commands)
    functions = {fid: fn for m in modules.values() for fid, fn in m.functions.items()}
    classes = {cid: c for m in modules.values() for cid, c in m.classes.items()}
    return Project(root, modules, functions, classes, merge_entries(candidates, functions))
