"""Values carried by the simulation.

Known values are plain Python values. Two stand-ins cover the rest:

- ``Obj``: an instance of a project class; its fields fill in as its code
  sets them,
- ``Unknown``: anything that depends on the outside world (an argument, a
  file, a library call). It remembers where it came from, plus any keys or
  attributes the code sets on it afterwards.

``encode`` turns a value into JSON for the viewer and ``diff`` says what
changed between two moments, as dotted paths (``cfg.mode``, ``rows.2``).
"""

from __future__ import annotations

import json
import math

MISSING = object()  # a name with no value in scope
MAX_ITEMS = 50
MAX_TEXT = 300


class Unknown:
    __slots__ = ("origin", "known")

    def __init__(self, origin: str, known: dict | None = None):
        self.origin = origin
        self.known: dict = dict(known) if known else {}

    def __repr__(self) -> str:
        return f"Unknown({self.origin!r})"


class Obj:
    __slots__ = ("cls", "fields")

    def __init__(self, cls: str, fields: dict | None = None):
        self.cls = cls
        self.fields: dict = dict(fields) if fields else {}

    def __repr__(self) -> str:
        return f"Obj({self.cls}, {self.fields!r})"


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
