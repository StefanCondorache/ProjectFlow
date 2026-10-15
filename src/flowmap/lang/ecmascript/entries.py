"""Contained, literal launch declarations for JavaScript and TypeScript.

No shell is executed or emulated. Runtime launchers determine which source
extensions are runnable; missing build outputs never map back to source files.
"""
from __future__ import annotations

import json
import posixpath
import re
import shlex
from pathlib import Path

from flowmap.discover import discover_files
from flowmap.entries import Command, Entry, Source
from flowmap.inventory import contained_path, read_text
from flowmap.ir import Module

_NODE_SUFFIXES = {'.js', '.mjs', '.cjs'}
_SOURCE_SUFFIXES = _NODE_SUFFIXES | {'.jsx', '.ts', '.tsx', '.mts', '.cts'}
_TS_LAUNCHERS = {'tsx', 'ts-node', 'ts-node-esm'}
# Unknown flags are rejected: their next token might be an option value, not
# the script. Eval, preload hooks and shell wrappers are deliberately excluded.
_FLAGS = {'node': {'--enable-source-maps', '--trace-warnings', '--no-warnings', '--inspect', '--inspect-brk'},
          'tsx': {'--no-cache'},
          'ts-node': {'--esm', '--transpile-only', '--transpileOnly', '-T', '--files', '--prefer-ts-exts'},
          'ts-node-esm': {'--transpile-only', '--transpileOnly', '-T', '--files', '--prefer-ts-exts'}}
_VALUE_FLAGS = {'node': set(), 'tsx': {'--tsconfig'},
                'ts-node': {'--project', '-P'}, 'ts-node-esm': {'--project', '-P'}}
_SHELL_SYNTAX = re.compile(r'[\x00\r\n$`;&|<>*?(){}\[\]~]')
_ENV_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*', re.DOTALL)


def _literal_argv(text: str) -> list[str]:
    if _SHELL_SYNTAX.search(text):
        return []
    try:
        return shlex.split(text, comments=True)
    except ValueError:
        return []


def _launcher(argv: list[str]) -> tuple[str, str] | None:
    """Return the literal launcher and script, never searching inside argv."""
    if not argv or any(_SHELL_SYNTAX.search(arg) for arg in argv):
        return None
    args = list(argv)
    while args and (args[0] == 'exec' or posixpath.basename(args[0]) == 'env' or _ENV_ASSIGNMENT.fullmatch(args[0])):
        args.pop(0)
    if not args:
        return None
    launcher = posixpath.basename(args.pop(0))
    if launcher not in _FLAGS:
        return None
    while args and args[0].startswith('-'):
        option = args.pop(0)
        if option == '--':
            break
        name, equals, value = option.partition('=')
        if name in _VALUE_FLAGS[launcher]:
            if equals:
                if not value:
                    return None
            elif args and not args[0].startswith('-'):
                args.pop(0)
            else:
                return None
        elif name in _FLAGS[launcher]:
            if equals and name not in {'--inspect', '--inspect-brk'}:
                return None
        else:
            return None
    if not args or args[0].startswith('-'):
        return None
    return launcher, args[0]


def _target(root: Path, modules: dict[str, Module], base: str, script: str,
            suffixes: set[str]) -> str | None:
    if not script or posixpath.isabs(script) or posixpath.isabs(base):
        return None
    file = posixpath.normpath(posixpath.join(base, script))
    if Path(file).suffix.lower() not in suffixes:
        return None
    module = modules.get(file)
    if module is None or module.language not in {'javascript', 'typescript'}:
        return None
    main = module.functions.get(f'{file}::<main>')
    if main is None or not main.body:
        return None
    try:
        contained_path(root, file)
    except ValueError:
        return None
    return file


def _command_entry(root: Path, modules: dict[str, Module], command: Command) -> Entry | None:
    if command.ambiguous:
        return None
    launch = _launcher(command.argv)
    if launch is None:
        return None
    launcher, script = launch
    file = _target(root, modules, command.base_dir, script,
                   _SOURCE_SUFFIXES if launcher in _TS_LAUNCHERS else _NODE_SUFFIXES)
    if file is None:
        return None
    return Entry(f'{file}::<main>', f'{launcher} {file}', command.kind,
                 [Source(command.kind, command.where)])


def _is_test_file(file: str) -> bool:
    parts = file.lower().split('/')
    return (any(part in {'test', 'tests', '__tests__', 'testing', 'fixture', 'fixtures', '__fixtures__'} for part in parts[:-1])
            or bool(re.search(r'(?:^test_|[._](?:test|spec)\.)', parts[-1])))


def _shebang(root: Path, file: str) -> str | None:
    text = read_text(root, file)
    if text is None or not text.startswith('#!'):
        return None
    # A shebang names a launcher; the file being inspected is the script.
    argv = _literal_argv(text.splitlines()[0][2:].strip())
    if len(argv) >= 2 and posixpath.basename(argv[0]) == 'env' and argv[1] == '-S':
        argv.pop(1)
    launch = _launcher([*argv, file])
    return launch[0] if launch is not None and launch[1] == file else None


def ecmascript_entries(root: Path, modules: dict[str, Module], commands: list[Command]) -> list[Entry]:
    """Return candidates; the project merges/ranks duplicate family targets."""
    found = []
    for command in commands:
        entry = _command_entry(root, modules, command)
        if entry is not None:
            found.append(entry)

    for rel in discover_files(root, extensions=(), patterns=('package.json',)):
        text = read_text(root, rel)
        if text is None:
            continue
        try:
            package = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if not isinstance(package, dict):
            continue
        base = posixpath.dirname(rel)
        scripts = package.get('scripts')
        if isinstance(scripts, dict):
            for name, script in scripts.items():
                if not isinstance(script, str):
                    continue
                entry = _command_entry(root, modules, Command(_literal_argv(script), 'package-script', f'{rel}: scripts.{name}', base))
                if entry is not None:
                    entry.label = f'{name} ({rel})'
                    found.append(entry)
        bins = package.get('bin')
        if isinstance(bins, str):
            name = package.get('name')
            bins = {name if isinstance(name, str) and name else posixpath.basename(base) or 'bin': bins}
        declarations = [(name, path, 'package-bin') for name, path in bins.items()] if isinstance(bins, dict) else []
        declarations.append(('main', package.get('main'), 'package-main'))
        for name, script, kind in declarations:
            if not isinstance(script, str) or _SHELL_SYNTAX.search(script):
                continue
            file = _target(root, modules, base, script, _NODE_SUFFIXES)
            if file is None:
                continue
            label = name if kind == 'package-bin' else f'node {file}'
            found.append(Entry(f'{file}::<main>', label, kind, [Source(kind, f'{rel}: {"bin." + name if kind == "package-bin" else "main"}')]))

    for file, module in sorted(modules.items()):
        if _is_test_file(file) or not module.functions.get(f'{file}::<main>') or not module.functions[f'{file}::<main>'].body:
            continue
        launcher = _shebang(root, file)
        if launcher is None:
            continue
        entry = _command_entry(root, modules, Command([launcher, file], 'module-script', f'{file}: shebang'))
        if entry is not None:
            found.append(entry)
    return found
