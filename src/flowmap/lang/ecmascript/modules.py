"""Contained, source-only ECMAScript module resolution.

Only modules already supplied by project loading are considered. No package
code, tsconfig plugins, filesystem probing or resolution hooks are executed.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath

from flowmap.ir import Module

EXTENSIONS = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs')
EMITTED = {'.js': ('.ts', '.tsx'), '.jsx': ('.tsx',), '.mjs': ('.mts',), '.cjs': ('.cts',)}


@dataclass(frozen=True)
class ModuleRef:
    kind: str  # module | external
    key: str


def resolve_module(modules: dict[str, Module], file: str, spec: str) -> ModuleRef | None:
    """Resolve a literal module specifier without choosing ambiguous sources."""
    if not spec or '\\' in spec or '?' in spec or '#' in spec:
        return None
    if not spec.startswith('.'):
        return None if spec.startswith('/') else ModuleRef('external', spec)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(file), spec))
    if path == '..' or path.startswith('../') or path.startswith('/'):
        return None
    if path in modules:
        return ModuleRef('module', path)
    suffix = PurePosixPath(path).suffix.lower()
    if suffix:
        candidates = [path[:-len(suffix)] + ext for ext in EMITTED.get(suffix, ())]
    else:
        candidates = [path + ext for ext in EXTENSIONS]
        candidates += [posixpath.join(path, 'index' + ext) for ext in EXTENSIONS]
    found = [candidate for candidate in candidates if candidate in modules]
    return ModuleRef('module', found[0]) if len(found) == 1 else None
