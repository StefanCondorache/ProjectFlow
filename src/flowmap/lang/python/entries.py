"""Python start points: commands that run python, console scripts declared in
packaging metadata, packages runnable with ``-m`` and ``__main__`` guards."""

from __future__ import annotations

import configparser
import posixpath
import re
import tomllib
from pathlib import Path

from flowmap.discover import discover_files
from flowmap.entries import Command, Entry, Source
from flowmap.ir import Module
from flowmap.lang.python.link import _Linker

_PYTHON = re.compile(r"(?:.*/)?python(?:\d+(?:\.\d+)*)?")
_FLAGS_WITH_VALUE = {"-W", "-X", "-Q"}
_SCRIPT_SPEC = re.compile(r"""['"]\s*([\w.-]+)\s*=\s*([\w.]+)\s*:\s*([\w.]+)""")


def is_test_file(file: str) -> bool:
    parts = file.split("/")
    name = parts[-1]
    return (
        any(p in ("tests", "test", "testing") for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def python_entries(root: Path, modules: dict[str, Module], commands: list[Command]) -> list[Entry]:
    linker = _Linker(root, modules)
    functions = linker.functions
    dotted_of = {file: dotted for dotted, file in reversed(list(linker.by_dotted.items()))}
    found: list[Entry] = []

    for command in commands:
        hit = _from_argv(linker, command)
        if hit is not None and hit[0] in functions:
            found.append(Entry(hit[0], hit[1], command.kind, [Source(command.kind, command.where)]))

    found += _console_scripts(root, linker)

    for file, module in sorted(modules.items()):
        fid = f"{file}::<main>"
        if fid not in module.functions or is_test_file(file):
            continue
        if posixpath.basename(file) == "__main__.py":
            package = dotted_of.get(posixpath.join(posixpath.dirname(file), "__init__.py"))
            label = f"python -m {package}" if package else f"python {file}"
            found.append(Entry(fid, label, "package-main", [Source("package-main", file)]))
        elif module.has_main_guard:
            found.append(Entry(fid, f"python {file}", "main-guard", [Source("main-guard", f'{file}: if __name__ == "__main__"')]))
    return found


def _from_argv(linker: _Linker, command: Command) -> tuple[str, str] | None:
    argv = command.argv
    start = next((i for i, tok in enumerate(argv) if _PYTHON.fullmatch(tok)), None)
    if start is None:
        return None
    rest = argv[start + 1 :]
    i = 0
    while i < len(rest) and rest[i].startswith("-") and rest[i] != "-m":
        if rest[i] == "-c":
            return None
        i += 2 if rest[i] in _FLAGS_WITH_VALUE else 1
    if i >= len(rest):
        return None
    anchor = posixpath.join(command.base_dir, "__command__.py")
    if rest[i] == "-m" and i + 1 < len(rest):
        module = rest[i + 1]
        ref = linker.module_ref(anchor, module)
        if ref is None or ref.kind not in ("module", "namespace"):
            return None
        if ref.kind == "namespace":
            file = f"{ref.key}/__main__.py"
        elif ref.key.endswith("__init__.py"):
            file = ref.key[: -len("__init__.py")] + "__main__.py"
        else:
            file = ref.key
        return f"{file}::<main>", f"python -m {module}"
    script = rest[i]
    if script.endswith(".py"):
        for candidate in _script_candidates(command.base_dir, script, linker.modules):
            return f"{candidate}::<main>", f"python {candidate}"
    return None


def _script_candidates(base_dir: str, script: str, modules: dict[str, Module]):
    tries = [posixpath.normpath(posixpath.join(base_dir, script)), posixpath.normpath(script)]
    if script.startswith("/"):  # absolute path inside a container, e.g. /app/scripts/run.py
        parts = script.strip("/").split("/")
        tries += ["/".join(parts[k:]) for k in range(1, len(parts))]
    for path in tries:
        if path in modules:
            yield path


def _console_scripts(root: Path, linker: _Linker) -> list[Entry]:
    specs: list[tuple[str, str, str, str]] = []  # (name, module, function, declared in)
    for rel in discover_files(root, extensions=(), patterns=("pyproject.toml", "setup.cfg", "setup.py")):
        try:
            text = (root / rel).read_text(errors="replace")
        except OSError:
            continue
        name = posixpath.basename(rel)
        if name == "pyproject.toml":
            specs += _pyproject_scripts(rel, text)
        elif name == "setup.cfg":
            specs += _setup_cfg_scripts(rel, text)
        else:
            specs += [(n, m, f, f"{rel}: entry_points") for n, m, f in _SCRIPT_SPEC.findall(text)]
    entries = []
    for script, module, function, where in specs:
        anchor = posixpath.join(posixpath.dirname(where.split(":")[0]), "__command__.py")
        ref = linker.module_ref(anchor, module)
        if ref is None or ref.kind != "module":
            continue
        fid = f"{ref.key}::{function.split('[')[0].strip()}"
        if fid in linker.functions:
            entries.append(Entry(fid, script, "pyproject", [Source("pyproject", where)]))
    return entries


def _pyproject_scripts(rel: str, text: str) -> list[tuple[str, str, str, str]]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    tables = {
        "[project.scripts]": data.get("project", {}).get("scripts", {}),
        "[project.gui-scripts]": data.get("project", {}).get("gui-scripts", {}),
        "[tool.poetry.scripts]": data.get("tool", {}).get("poetry", {}).get("scripts", {}),
    }
    out = []
    for table, scripts in tables.items():
        for name, spec in scripts.items():
            if isinstance(spec, dict):
                spec = spec.get("reference") or spec.get("callable") or ""
            module, _, function = str(spec).partition(":")
            if module and function:
                out.append((name, module.strip(), function.strip(), f"{rel}: {table} {name}"))
    return out


def _setup_cfg_scripts(rel: str, text: str) -> list[tuple[str, str, str, str]]:
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
        raw = parser.get("options.entry_points", "console_scripts", fallback="")
    except configparser.Error:
        return []
    out = []
    for line in raw.splitlines():
        name, _, spec = line.partition("=")
        module, _, function = spec.partition(":")
        if name.strip() and module.strip() and function.strip():
            out.append((name.strip(), module.strip(), function.strip(), f"{rel}: console_scripts {name.strip()}"))
    return out
