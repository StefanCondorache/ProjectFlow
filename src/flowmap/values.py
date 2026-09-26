"""Values carried by the simulation.

Known values are plain Python values. Stand-ins cover the rest:

- ``Obj``: an instance of a project class; its fields fill in as its code
  sets them,
- ``Unknown``: anything that depends on the outside world (an argument, a
  file, a library call). It remembers where it came from, plus any keys or
  attributes the code sets on it afterwards,
- ``Handle`` and ``Parser``: a file the code opened and an argparse parser it
  built, so real files can be read and real command lines parsed.

``encode`` turns a value into JSON for the viewer and ``diff`` says what
changed between two moments, as dotted paths (``cfg.mode``, ``rows.2``).
"""

from __future__ import annotations

import builtins
import datetime
import json
import math
from collections import deque
from decimal import Decimal
from fractions import Fraction
from pathlib import PurePath

MISSING = object()  # a name with no value in scope
MAX_ITEMS = 50
_TYPE_NAMES = {"PurePosixPath": "path", "PurePath": "path", "PosixPath": "path", "timedelta": "duration", "Decimal": "decimal", "Fraction": "fraction"}
MAX_TEXT = 300


class Unknown:
    __slots__ = ("origin", "known")

    def __init__(self, origin: str, known: dict | None = None):
        self.origin = origin
        self.known: dict = dict(known) if known else {}

    def __repr__(self) -> str:
        return f"Unknown({self.origin!r})"


class Obj:
    """An instance of a project class (or an exception). ``bases`` names what
    its class derives from, nearest first, for ``except`` and ``isinstance``."""

    __slots__ = ("cls", "fields", "bases")

    def __init__(self, cls: str, fields: dict | None = None, bases: tuple[str, ...] = ()):
        self.cls = cls
        self.fields: dict = dict(fields) if fields else {}
        self.bases = tuple(bases)

    def __repr__(self) -> str:
        return f"Obj({self.cls}, {self.fields!r})"


class Handle:
    """A file the code opened. ``path`` is the real file inside the project
    (None when the name points outside it); nothing is ever written to it."""

    __slots__ = ("name", "path", "mode")

    def __init__(self, name: str, path, mode: str = "r"):
        self.name = name
        self.path = path
        self.mode = mode

    @property
    def writing(self) -> bool:
        return any(flag in self.mode for flag in "wax+")

    def __repr__(self) -> str:
        return f"Handle({self.name!r}, {self.mode!r})"


class Parser:
    """An ``argparse.ArgumentParser`` as the code builds it."""

    __slots__ = ("options", "arguments", "defaults")

    def __init__(self, options: dict):
        self.options = options
        self.arguments: list[tuple[list, dict]] = []
        self.defaults: dict = {}

    def __repr__(self) -> str:
        return f"Parser({len(self.arguments)} arguments)"


def summary(value) -> str:
    """A few words for a value, for frame notes."""
    if isinstance(value, Unknown):
        return f"‹{value.origin}›"
    if isinstance(value, Obj):
        return f"{value.cls} object"
    if isinstance(value, Handle):
        return f"file {value.name}"
    if isinstance(value, Parser):
        return "argument parser"
    if isinstance(value, type):
        return value.__name__
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    if isinstance(value, (list, tuple, set, deque)):
        return f"[{len(value)} items]"
    text = str(value) if isinstance(value, PurePath) else repr(value)
    return text if len(text) <= 40 else text[:39] + "…"


def exception(name: str, message="", bases: tuple[str, ...] | None = None, **fields) -> Obj:
    if bases is None:
        found = getattr(builtins, name, None)
        if isinstance(found, type) and issubclass(found, BaseException):
            bases = tuple(k.__name__ for k in found.__mro__[1:] if k is not object)
        else:
            bases = ("Exception", "BaseException")
    return Obj(name, {"message": message, **fields}, bases)


def is_exception(value) -> bool:
    return isinstance(value, Obj) and "BaseException" in value.bases


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def _key(key) -> str:
    return key if isinstance(key, str) else repr(key)


def encode(value, depth: int = 0) -> dict:
    """JSON-ready form: {"t": "val" | "dict" | "list" | "obj" | "?", "v": ...}."""
    if depth > 12:
        return {"t": "?", "from": "…"}
    if isinstance(value, Unknown):
        out: dict = {"t": "?", "from": _clip(value.origin)}
        if value.known:
            out["v"] = {_key(k): encode(v, depth + 1) for k, v in value.known.items()}
        return out
    if isinstance(value, Obj):
        return {"t": "obj", "cls": value.cls, "v": {_key(k): encode(v, depth + 1) for k, v in value.fields.items()}}
    if isinstance(value, dict):
        items = list(value.items())
        out = {"t": "dict", "v": {_key(k): encode(v, depth + 1) for k, v in items[:MAX_ITEMS]}}
        if len(items) > MAX_ITEMS:
            out["more"] = len(items) - MAX_ITEMS
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else list(value)
        out = {"t": "list", "v": [encode(v, depth + 1) for v in seq[:MAX_ITEMS]]}
        if isinstance(value, tuple):
            out["kind"] = "tuple"
        elif isinstance(value, (set, frozenset)):
            out["kind"] = "set"
        if len(seq) > MAX_ITEMS:
            out["more"] = len(seq) - MAX_ITEMS
        return out
    if isinstance(value, Handle):
        return {"t": "ref", "v": f"file {value.name} ({'writing' if value.writing else 'reading'})"}
    if isinstance(value, Parser):
        return {"t": "ref", "v": f"argument parser, {len(value.arguments)} options"}
    if isinstance(value, type):
        return {"t": "ref", "v": value.__name__}
    if isinstance(value, deque):
        return encode(list(value), depth)
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta, Decimal, Fraction, PurePath)):
        return {"t": "val", "v": str(value), "type": _TYPE_NAMES.get(type(value).__name__, type(value).__name__)}
    if value is None or isinstance(value, bool):
        return {"t": "val", "v": value}
    if isinstance(value, int):
        return {"t": "val", "v": value if abs(value) < 2**53 else str(value)}
    if isinstance(value, float):
        return {"t": "val", "v": value if math.isfinite(value) else repr(value)}
    if isinstance(value, str):
        return {"t": "val", "v": _clip(value)}
    return {"t": "?", "from": type(value).__name__}


def _flatten(encoded: dict, path: str, out: dict[str, str]) -> None:
    kind = encoded["t"]
    children = encoded.get("v") if kind in ("dict", "obj", "list", "?") else None
    if isinstance(children, (dict, list)):
        out[path] = f"{kind}:{encoded.get('cls') or encoded.get('from') or ''}"
        pairs = children.items() if isinstance(children, dict) else enumerate(children)
        for key, child in pairs:
            _flatten(child, f"{path}.{key}", out)
    else:
        out[path] = json.dumps(encoded, sort_keys=True)


def _shallowest(paths: list[str]) -> list[str]:
    chosen = set(paths)
    return sorted(p for p in paths if not any(p.startswith(q + ".") for q in chosen if q != p))


def diff(before: dict[str, dict], after: dict[str, dict]) -> dict[str, list[str]]:
    """What changed between two sets of encoded variables."""
    old: dict[str, str] = {}
    new: dict[str, str] = {}
    for name, value in before.items():
        _flatten(value, name, old)
    for name, value in after.items():
        _flatten(value, name, new)
    changed = _shallowest([p for p in new if p in old and new[p] != old[p]])
    under_changed = lambda p: any(p.startswith(c + ".") for c in changed)  # noqa: E731
    added = [p for p in _shallowest([p for p in new if p not in old]) if not under_changed(p)]
    removed = [p for p in _shallowest([p for p in old if p not in new]) if not under_changed(p)]
    return {"added": added, "changed": changed, "removed": removed}
