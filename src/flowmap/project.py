"""Load a project folder: discover its files, extract them, resolve calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flowmap.inventory import MAX_TEXT_BYTES, Inventory, build_inventory, contained_path
from flowmap.entries import Entry, find_commands, merge_entries
from flowmap.ir import ClassDef, Function, Module
from flowmap.lang import Adapter, adapters


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

    def function_language(self, function: str) -> str:
        """Language semantics of a function, independent of its file suffix."""
        return self.functions[function].language

    @property
    def flow_languages(self) -> list[str]:
        return sorted({module.language for module in self.modules.values()})

    @property
    def simulation_languages(self) -> list[str]:
        supported = {adapter.language for adapter in adapters() if adapter.simulation}
        return sorted(supported & set(self.flow_languages))

    def supports_simulation(self, function: str) -> bool:
        language = self.function_language(function)
        return any(adapter.language == language and adapter.simulation for adapter in adapters())

    def require_simulation(self, function: str) -> None:
        if not self.supports_simulation(function):
            raise ValueError(f"simulation is not supported for {self.function_language(function)} functions")


def load_project(root: str | Path) -> Project:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"not a project folder: {root}")
    inventory = build_inventory(root)
    langs = adapters()
    by_ext = {ext.lower(): adapter for adapter in langs for ext in adapter.extensions}
    modules: dict[str, Module] = {}
    for rel, item in inventory.files.items():
        adapter = by_ext.get(Path(rel).suffix.lower())
        if adapter is None:
            continue
        try:
            target = contained_path(root, rel)
            if adapter.family == "ecmascript":
                with target.open("rb") as stream:
                    source = stream.read(MAX_TEXT_BYTES + 1)
                if len(source) > MAX_TEXT_BYTES:
                    continue
            else:
                source = target.read_bytes()
        except (OSError, ValueError):
            continue
        try:
            modules[rel] = adapter.extract(rel, source)
        except RecursionError:
            continue  # too deeply nested to analyze; the file keeps its structural view
        item.analysis = "semantic"
    commands = find_commands(root)
    candidates: list[Entry] = []
    resolvers: dict[str, object] = {}
    families: dict[str, list[Adapter]] = {}
    for adapter in langs:
        families.setdefault(adapter.family or adapter.language, []).append(adapter)
    for family in families.values():
        languages = {adapter.language for adapter in family}
        own = {f: m for f, m in modules.items() if m.language in languages}
        resolver = family[0].link(root, own)
        for adapter in family:
            resolvers[adapter.language] = resolver
            language_modules = {f: m for f, m in own.items() if m.language == adapter.language}
            candidates += adapter.entries(root, language_modules, commands)
    functions = {fid: fn for m in modules.values() for fid, fn in m.functions.items()}
    classes = {cid: c for m in modules.values() for cid, c in m.classes.items()}
    project = Project(root, modules, functions, classes, merge_entries(candidates, functions), resolvers, inventory)
    from flowmap.overview import extract_relationships

    project.relationships = extract_relationships(project)
    return project
