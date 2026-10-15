"""Rust launch entries: binary crate roots, examples and literal cargo commands.

Crates come from the same Cargo.toml/layout discovery the linker uses; cargo
is never run. Tests, benches and build scripts are not entries.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

from flowmap.entries import Command, Entry, Source
from flowmap.ir import Module

from .link import Crate, find_crates

_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*')
_VALUE_OPTIONS = {'-p', '--package', '--manifest-path', '--features', '-F', '--target', '--profile', '-j', '--jobs',
                  '--target-dir', '--config', '--color'}


def _main(module: Module) -> str | None:
    meta = module.metadata.get('rust', {})
    return meta.get('items', {}).get('self::main', {}).get('id')


def _label(crate: Crate) -> str:
    return f"cargo run --{'example' if crate.kind == 'example' else 'bin'} {crate.bin_name}"


def _launch(command: Command, runnable: list[tuple[Crate, str]]) -> str | None:
    args = list(command.argv)
    while args and (args[0] == 'exec' or posixpath.basename(args[0]) == 'env' or _ENV.fullmatch(args[0])):
        args.pop(0)
    if len(args) < 2 or posixpath.basename(args[0]) != 'cargo':
        return None
    args.pop(0)
    if args and args[0].startswith('+'):
        args.pop(0)  # +toolchain
    if not args or args.pop(0) not in {'run', 'r'}:
        return None
    wanted: tuple[str, str] | None = None
    package = None
    while args:
        token = args.pop(0)
        if token == '--':
            break
        name, equals, value = token.partition('=')
        if name in {'--bin', '--example'}:
            value = value if equals else (args.pop(0) if args else '')
            wanted = ('example' if name == '--example' else 'bin', value)
        elif name in {'-p', '--package'}:
            package = value if equals else (args.pop(0) if args else None)
        elif name in _VALUE_OPTIONS and not equals:
            if args:
                args.pop(0)
    if wanted is not None:
        kind, name = wanted
        found = [fid for crate, fid in runnable if crate.kind == kind and crate.bin_name == name
                 and (package is None or crate.name == package.replace('-', '_'))]
    else:
        # The default binary of the package in the command's directory.
        found = [fid for crate, fid in runnable if crate.kind == 'bin' and crate.root == posixpath.join(crate.dir, 'src/main.rs')
                 and crate.dir == command.base_dir.rstrip('/') and (package is None or crate.name == package.replace('-', '_'))]
    return found[0] if len(found) == 1 else None


def rust_entries(root: Path, modules: dict[str, Module], commands: list[Command]) -> list[Entry]:
    runnable = [(crate, fid) for crate in find_crates(root, modules) if crate.kind in {'bin', 'example'}
                for fid in [_main(modules[crate.root])] if fid]
    entries = []
    for command in commands:
        if command.ambiguous:
            continue
        target = _launch(command, runnable)
        if target:
            crate = next(c for c, fid in runnable if fid == target)
            entries.append(Entry(target, _label(crate), command.kind, [Source(command.kind, command.where)]))
    for crate, fid in runnable:
        kind = 'cargo-example' if crate.kind == 'example' else 'cargo-bin'
        entries.append(Entry(fid, _label(crate), kind, [Source(kind, f'{crate.root}: fn main')]))
    return entries
