"""Where a program starts.

Start points come from two places:

- commands the project declares for running itself: Dockerfiles, compose
  files, Procfiles and shell scripts (followed one script deep when a command
  runs another script). These are language-neutral; each language adapter
  decides which commands start *its* code.
- conventions of the language itself (``__main__`` guards, console scripts...),
  found by the adapters.

The same start point declared in several places becomes one entry that lists
every declaration, ranked by how "official" the best declaration is.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import yaml

from flowmap.discover import discover_files
from flowmap.ir import Call, Function, walk

DEPLOY_PATTERNS = (
    "Dockerfile",
    "Dockerfile.*",
    "*.Dockerfile",
    "*.dockerfile",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose.yml",
    "compose.yaml",
    "compose.*.yml",
    "compose.*.yaml",
    "Procfile",
    "*.sh",
)

# Lower is more official. Kinds with equal rank keep this order of preference.
KIND_ORDER = ("compose", "docker", "procfile", "pyproject", "shell", "package-main", "main-guard")
RANK = {"compose": 0, "docker": 0, "procfile": 0, "pyproject": 1, "shell": 2, "package-main": 3, "main-guard": 4}

_SHELLS = {"bash", "sh", "/bin/bash", "/bin/sh", "/usr/bin/env"}
_PREFIXES = {"exec", "nohup", "time", "env", "sudo"}
_RUNNERS = (("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("pdm", "run"), ("hatch", "run"))


@dataclass
class Source:
    kind: str
    text: str


@dataclass
class Entry:
    target: str  # function id where execution starts
    label: str
    kind: str  # kind of the most official declaration
    sources: list[Source]
    reach: int = 0  # project functions reachable from the start


@dataclass
class Command:
    argv: list[str]
    kind: str  # compose | docker | procfile | shell
    where: str
    base_dir: str = ""  # directory relative paths are resolved against


# ── commands ───────────────────────────────────────────────────────────────


def find_commands(root: Path) -> list[Command]:
    commands: list[Command] = []
    for rel in discover_files(root, extensions=(), patterns=DEPLOY_PATTERNS):
        try:
            text = (root / rel).read_text(errors="replace")
        except OSError:
            continue
        name = posixpath.basename(rel)
        if name.endswith(".sh"):
            commands += _shell(rel, text, via=None, kind="shell")
        elif "compose" in name:
            commands += _compose(rel, text)
        elif name == "Procfile":
            commands += _procfile(rel, text)
        else:
            commands += _dockerfile(rel, text)
    return _follow_scripts(root, commands)


def _argv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        return shlex.split(str(value))
    except ValueError:
        return []


def _dockerfile(rel: str, text: str) -> list[Command]:
    text = re.sub(r"\\\r?\n", " ", text)
    found: dict[str, tuple[list[str], int]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(ENTRYPOINT|CMD)\s+(.*)", line, re.IGNORECASE)
        if not m:
            continue
        value = m.group(2).strip()
        if value.startswith("["):
            try:
                argv = [str(v) for v in yaml.safe_load(value)]
            except (yaml.YAMLError, TypeError):
                continue
        else:
            argv = _argv(value)
        found[m.group(1).upper()] = (argv, lineno)
    entrypoint, cmd = found.get("ENTRYPOINT", ([], 0)), found.get("CMD", ([], 0))
    argv = entrypoint[0] + cmd[0]
    if not argv:
        return []
    line = cmd[1] or entrypoint[1]
    return [Command(argv, "docker", f"{rel}:{line}", posixpath.dirname(rel))]


def _compose(rel: str, text: str) -> list[Command]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    services = data.get("services") if isinstance(data, dict) else None
    commands = []
    for name, service in (services or {}).items():
        if not isinstance(service, dict):
            continue
        argv = _argv(service.get("entrypoint")) + _argv(service.get("command"))
        if not argv:
            continue
        if argv[0].startswith("-") or argv[0].endswith(".py"):
            argv = ["python", *argv]  # arguments for an image whose entrypoint is python
        commands.append(Command(argv, "compose", f"{rel}: service {name}", posixpath.dirname(rel)))
    return commands


def _procfile(rel: str, text: str) -> list[Command]:
    commands = []
    for line in text.splitlines():
        name, sep, command = line.partition(":")
        if sep and name.strip() and not name.lstrip().startswith("#"):
            argv = _argv(command)
            if argv:
                commands.append(Command(argv, "procfile", f"{rel}: {name.strip()}", posixpath.dirname(rel)))
    return commands


_ASSIGNMENT = re.compile(r"\s*(?:export\s+|readonly\s+|local\s+)?([A-Za-z_]\w*)=(.*)")
_VARIABLE = re.compile(r"\$(?:\{(\w+)(?::?-([^}]*))?\}|(\w+))")


def _is_assignment(line: str) -> bool:
    """``NAME=value`` on its own, as opposed to ``NAME=value command ...``."""
    m = _ASSIGNMENT.fullmatch(line)
    if not m:
        return False
    try:
        return len(shlex.split(m.group(2), comments=True)) <= 1
    except ValueError:
        return True


def _shell_variables(text: str) -> dict[str, str]:
    """Variables assigned a plain literal, e.g. ``MODULE="pkg.job"``."""
    variables = {}
    for line in text.splitlines():
        m = _ASSIGNMENT.fullmatch(line)
        if m and "$(" not in m.group(2) and "`" not in m.group(2):
            try:
                value = shlex.split(m.group(2), comments=True)
            except ValueError:
                continue
            if len(value) == 1:
                variables[m.group(1)] = value[0]
    return variables


def _shell(rel: str, text: str, via: str | None, kind: str) -> list[Command]:
    variables = _shell_variables(text)

    def expand(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        if name in variables:
            return variables[name]
        return m.group(2) if m.group(2) is not None else m.group(0)

    commands = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or _is_assignment(stripped):
            continue
        stripped = re.sub(r"\$\([^()]*\)", "X", stripped)  # command substitutions become a placeholder
        stripped = _VARIABLE.sub(expand, stripped)
        for part in re.split(r"&&|\|\||;|\||\(|\)", stripped):
            argv = _strip_prefixes(_argv(part.split(" #", 1)[0]))
            if argv:
                where = f"{via} → {rel}:{lineno}" if via else f"{rel}:{lineno}"
                commands.append(Command(argv, kind, where, posixpath.dirname(rel)))
    return commands


def _strip_prefixes(argv: list[str]) -> list[str]:
    changed = True
    while argv and changed:
        changed = False
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[0]) or argv[0] in _PREFIXES:
            argv, changed = argv[1:], True
        elif len(argv) >= 2 and (argv[0], argv[1]) in _RUNNERS:
            argv, changed = argv[2:], True
    return argv


def _script_of(argv: list[str]) -> str | None:
    if argv and argv[0].endswith(".sh"):
        return argv[0]
    if len(argv) >= 2 and argv[0] in _SHELLS and argv[1].endswith(".sh"):
        return argv[1]
    return None


def _follow_scripts(root: Path, commands: list[Command], depth: int = 0) -> list[Command]:
    out: list[Command] = []
    for command in commands:
        out.append(command)
        script = _script_of(command.argv)
        if script is None or depth >= 3 or command.kind == "shell":
            continue
        for candidate in (posixpath.join(command.base_dir, script), script.lstrip("./")):
            path = posixpath.normpath(candidate)
            if not path.startswith("..") and (root / path).is_file():
                text = (root / path).read_text(errors="replace")
                inner = _shell(path, text, via=command.where, kind=command.kind)
                out += _follow_scripts(root, inner, depth + 1)
                break
    return out


# ── merging and ranking ────────────────────────────────────────────────────


def _preference(kind: str) -> tuple[int, int]:
    return RANK.get(kind, 9), KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)


def call_graph(functions: dict[str, Function]) -> dict[str, list[str]]:
    return {
        fid: [c.target for c in walk(fn.body) if isinstance(c, Call) and c.kind == "project" and c.target]
        for fid, fn in functions.items()
    }


def reachable(graph: dict[str, list[str]], start: str) -> set[str]:
    seen: set[str] = set()
    queue = deque(graph.get(start, []))
    while queue:
        fid = queue.popleft()
        if fid in seen or fid == start:
            continue
        seen.add(fid)
        queue.extend(graph.get(fid, []))
    return seen


def merge_entries(candidates: list[Entry], functions: dict[str, Function]) -> list[Entry]:
    merged: dict[str, Entry] = {}
    for cand in candidates:
        entry = merged.get(cand.target)
        if entry is None:
            merged[cand.target] = Entry(cand.target, cand.label, cand.kind, list(cand.sources))
            continue
        for source in cand.sources:
            if source not in entry.sources:
                entry.sources.append(source)
        if _preference(cand.kind) < _preference(entry.kind):
            entry.kind, entry.label = cand.kind, cand.label
    graph = call_graph(functions)
    for entry in merged.values():
        entry.reach = len(reachable(graph, entry.target))
    return sorted(merged.values(), key=lambda e: (RANK.get(e.kind, 9), -e.reach, e.label))

