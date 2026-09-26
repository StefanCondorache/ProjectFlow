"""Resolve imports and calls across the Python files of a project.

Resolution is static and deliberately modest: names, imports (absolute,
relative, re-exports, script-folder imports), ``self``/``super()``, class
construction, and simple type inference from annotations, constructor calls
and return types. Every resolved call carries a confidence:

- ``exact``: followed names and imports only,
- ``inferred``: needed a type from an annotation, assignment or return value,
- ``guess``: the receiver's name matches exactly one project class
  (``store.save()`` -> ``Store.save``).

Anything else stays ``unresolved`` instead of being made up.
"""

from __future__ import annotations

import builtins
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from flowmap.ir import Call, Function, Hint, Import, Module, Return, Try, walk
from flowmap.lang.python.io_catalog import classify

_BUILTINS = frozenset(dir(builtins))
_DOTTED = re.compile(r"[A-Za-z_][\w.]*")
_PROJECT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg")


@dataclass(frozen=True)
class Ref:
    """What a name or expression refers to.

    kind is one of:
      module     key = file of a project module (or package ``__init__.py``)
      namespace  key = project directory without ``__init__.py``
      func       key = function id
      class      key = class id
      instance   key = class id of the instance's class
      super      key = class id whose bases ``super()`` searches
      external   key = dotted name of an outside module or callable
      value      key = description of an outside value, e.g. ``requests.post()``
      builtin    key = builtin name
    """

    kind: str
    key: str


@dataclass(frozen=True)
class _Scope:
    file: str
    fn: Function | None


def link_python(root: Path, modules: dict[str, Module]) -> _Linker:
    """Resolve every call; the linker stays available to answer type questions
    later (``class_of``, ``lineage``)."""
    linker = _Linker(root, modules)
    linker.link()
    return linker


def _binds(imp: Import) -> str:
    if imp.alias:
        return imp.alias
    if imp.name:
        return imp.name
    return imp.module.split(".")[0]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _split_top(text: str, sep: str = ",") -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _strip_annotation(text: str) -> str:
    """``Optional[Store]``, ``Store | None``, ``"Store"`` -> ``Store``;
    ``dict[str, int]`` -> ``dict``. Real unions give ``""``."""
    t = text.strip().strip("'\"").strip()
    for _ in range(10):
        m = re.fullmatch(r"(?:typing\.|t\.)?(?:Optional|Annotated|Final|ClassVar)\[(.*)\]", t)
        if m:
            t = _split_top(m.group(1))[0].strip()
            continue
        m = re.fullmatch(r"(?:typing\.|t\.)?Union\[(.*)\]", t)
        if m:
            parts = [p.strip() for p in _split_top(m.group(1)) if p.strip() != "None"]
            if len(parts) != 1:
                return ""
            t = parts[0]
            continue
        parts = [p.strip() for p in _split_top(t, "|")]
        if len(parts) > 1:
            parts = [p for p in parts if p != "None"]
            if len(parts) != 1:
                return ""
            t = parts[0]
            continue
        return t.split("[", 1)[0].strip().strip("'\"")
    return ""


class _Linker:
    def __init__(self, root: Path, modules: dict[str, Module]):
        self.root = root
        self.modules = modules
        self.functions = {fid: f for m in modules.values() for fid, f in m.functions.items()}
        self.classes = {cid: c for m in modules.values() for cid, c in m.classes.items()}
        self.dirs: set[str] = set()
        for file in modules:
            d = posixpath.dirname(file)
            while d and d not in self.dirs:
                self.dirs.add(d)
                d = posixpath.dirname(d)
        self.roots = self._roots()
        self.by_dotted = self._index_modules()
        self._returns: dict[str, Ref | None] = {}
        self._locals: dict[str, set[str]] = {}
        self._mro: dict[str, list[str]] = {}
        self._inferred = False

    def link(self) -> None:
        for fn in self.functions.values():
            for item in walk(fn.body):
                if isinstance(item, Call):
                    self.resolve_call(fn, item)
                    item.io = classify(item)

    # ── module index ───────────────────────────────────────────────────────

    def _is_package(self, d: str) -> bool:
        return bool(d) and f"{d}/__init__.py" in self.modules

    def _package_root(self, file: str) -> str:
        d = posixpath.dirname(file)
        while self._is_package(d):
            d = posixpath.dirname(d)
        return d

    def _roots(self) -> list[str]:
        roots = [""]
        for d in sorted(self.dirs):
            if posixpath.basename(d) == "src" or any((self.root / d / m).is_file() for m in _PROJECT_MARKERS):
                roots.append(d)
                if f"{d}/src" in self.dirs:
                    roots.append(f"{d}/src")
        return list(dict.fromkeys(roots))

    def _index_modules(self) -> dict[str, str]:
        index: dict[str, str] = {}
        files = sorted(self.modules)
        for file in files:
            self._register(index, self._package_root(file), file)
        for root in self.roots:
            for file in files:
                if not root or file.startswith(root + "/"):
                    self._register(index, root, file)
        return index

    @staticmethod
    def _register(index: dict[str, str], root: str, file: str) -> None:
        rel = file[len(root) + 1 :] if root else file
        parts = rel[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(p.isidentifier() for p in parts):
            index.setdefault(".".join(parts), file)

    def _path_ref(self, path: str) -> Ref | None:
        if f"{path}.py" in self.modules:
            return Ref("module", f"{path}.py")
        if f"{path}/__init__.py" in self.modules:
            return Ref("module", f"{path}/__init__.py")
        if path in self.dirs:
            return Ref("namespace", path)
        return None

    def module_ref(self, from_file: str, spec: str) -> Ref | None:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            rest = spec[level:]
            base = posixpath.dirname(from_file)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            path = posixpath.join(base, rest.replace(".", "/")) if rest else base
            return self._path_ref(path) if path else None

        def via_index() -> Ref | None:
            if spec in self.by_dotted:
                return Ref("module", self.by_dotted[spec])
            for root in self.roots:
                path = posixpath.join(root, spec.replace(".", "/")) if root else spec.replace(".", "/")
                if path in self.dirs:
                    return Ref("namespace", path)
            return None

        def via_own_dir() -> Ref | None:
            own = posixpath.dirname(from_file)
            path = posixpath.join(own, spec.replace(".", "/")) if own else spec.replace(".", "/")
            return self._path_ref(path)

        in_package = self._is_package(posixpath.dirname(from_file))
        for attempt in (via_index, via_own_dir) if in_package else (via_own_dir, via_index):
            found = attempt()
            if found is not None:
                return found
        return Ref("external", spec)

    # ── names ──────────────────────────────────────────────────────────────

    def import_ref(self, from_file: str, imp: Import, seen: frozenset) -> Ref | None:
        if imp.name is None:
            return self.module_ref(from_file, imp.module if imp.alias else imp.module.split(".")[0])
        if imp.name == "*":
            return None
        mod = self.module_ref(from_file, imp.module)
        return self.member(mod, imp.name, seen) if mod is not None else None

    def module_symbol(self, file: str, name: str, seen: frozenset = frozenset()) -> Ref | None:
        if (file, name) in seen or file not in self.modules:
            return None
        seen = seen | {(file, name)}
        key = f"{file}::{name}"
        if key in self.functions and self.functions[key].cls is None:
            return Ref("func", key)
        if key in self.classes:
            return Ref("class", key)
        module = self.modules[file]
        for imp in module.imports:
            if imp.name != "*" and _binds(imp) == name:
                found = self.import_ref(file, imp, seen)
                if found is not None:
                    return found
        for imp in module.imports:
            if imp.name == "*":
                mod = self.module_ref(file, imp.module)
                if mod is not None and mod.kind == "module":
                    found = self.module_symbol(mod.key, name, seen)
                    if found is not None:
                        return found
        if name in module.globals:
            return self.hints_ref(_Scope(file, None), module.globals[name], 1)
        return None

    def member(self, ref: Ref, attr: str, seen: frozenset) -> Ref | None:
        if ref.kind == "module":
            found = self.module_symbol(ref.key, attr, seen)
            if found is None and ref.key.endswith("__init__.py"):
                found = self._path_ref(posixpath.join(posixpath.dirname(ref.key), attr))
            return found
        if ref.kind == "namespace":
            return self._path_ref(f"{ref.key}/{attr}")
        if ref.kind == "external":
            return Ref("external", f"{ref.key}.{attr}")
        return self.attr_ref(ref, attr, 1)

    def _locals_of(self, fn: Function) -> set[str]:
        names = self._locals.get(fn.id)
        if names is None:
            names = {p.name for p in fn.params}
            for item in walk(fn.body):
                names.update(getattr(item, "defs", ()))
                if isinstance(item, Try):
                    names.update(h.name for h in item.handlers if h.name)
            self._locals[fn.id] = names
        return names

    def _enclosing(self, fn: Function) -> Function | None:
        if ".<locals>." not in fn.qualname:
            return None
        return self.functions.get(f"{fn.file}::{fn.qualname.rsplit('.<locals>.', 1)[0]}")

    @staticmethod
    def _self_name(fn: Function) -> str | None:
        if fn.cls and fn.params and "staticmethod" not in fn.decorators:
            return fn.params[0].name
        return None

    def name_ref(self, scope: _Scope, name: str, depth: int) -> Ref | None:
        fn = scope.fn
        if fn is not None:
            if name == self._self_name(fn):
                return Ref("class" if "classmethod" in fn.decorators else "instance", fn.cls)
            if name in self._locals_of(fn):
                hints = fn.hints.get(name)
                found = self.hints_ref(scope, hints, depth) if hints else None
                if found is not None:
                    self._inferred = True
                return found
            nested = f"{fn.file}::{fn.qualname}.<locals>.{name}"
            if nested in self.functions:
                return Ref("func", nested)
            if nested in self.classes:
                return Ref("class", nested)
            for imp in fn.imports:
                if imp.name != "*" and _binds(imp) == name:
                    found = self.import_ref(fn.file, imp, frozenset())
                    if found is not None:
                        return found
            parent = self._enclosing(fn)
            if parent is not None:
                return self.name_ref(_Scope(scope.file, parent), name, depth)
        found = self.module_symbol(scope.file, name)
        if found is not None:
            return found
        if name in _BUILTINS:
            return Ref("builtin", name)
        return None

    def expr_ref(self, scope: _Scope, dotted: str, depth: int) -> Ref | None:
        if depth > 8:
            return None
        parts = dotted.split(".")
        ref = self.name_ref(scope, parts[0], depth)
        for attr in parts[1:]:
            if ref is None:
                return None
            ref = self.attr_ref(ref, attr, depth)
        return ref

    # ── types ──────────────────────────────────────────────────────────────

    def hints_ref(self, scope: _Scope, hints: list[Hint], depth: int) -> Ref | None:
        if depth > 8:
            return None
        for kind, text in hints:
            if kind == "ann":
                found = self.annotation_ref(scope, text, depth + 1)
            else:
                found = self.result_ref(scope, self.expr_ref(scope, text, depth + 1), depth + 1)
            if found is not None:
                return found
        return None

    def annotation_ref(self, scope: _Scope, text: str, depth: int) -> Ref | None:
        name = _strip_annotation(text)
        if not name or not _DOTTED.fullmatch(name):
            return None
        ref = self.expr_ref(scope, name, depth + 1)
        if ref is None:
            return None
        if ref.kind == "class":
            return Ref("instance", ref.key)
        if ref.kind in ("external", "builtin"):
            return Ref("value", ref.key)
        return None

    def result_ref(self, scope: _Scope, callee: Ref | None, depth: int) -> Ref | None:
        """What calling ``callee`` produces."""
        if callee is None or depth > 8:
            return None
        if callee.kind == "func":
            self._inferred = True
            return self.return_ref(callee.key, depth + 1)
        if callee.kind == "class":
            return Ref("instance", callee.key)
        if callee.kind in ("external", "value"):
            return Ref("value", f"{callee.key}()")
        if callee.kind == "builtin":
            if callee.key == "super" and scope.fn is not None and scope.fn.cls:
                return Ref("super", scope.fn.cls)
            return Ref("value", f"{callee.key}()")
        if callee.kind == "instance":
            call_method = self.lookup_method(callee.key, "__call__")
            return self.return_ref(call_method, depth + 1) if call_method else None
        return None

    def return_ref(self, fid: str, depth: int) -> Ref | None:
        if fid in self._returns:
            return self._returns[fid]
        self._returns[fid] = None  # guards recursion
        fn = self.functions[fid]
        scope = _Scope(fn.file, fn)
        found = None
        if fn.returns:
            found = self.annotation_ref(scope, fn.returns, depth + 1)
        elif fn.name != "__init__":
            refs = [
                self.name_ref(scope, item.uses[0], depth + 1) if len(item.uses) == 1 else None
                for item in walk(fn.body)
                if isinstance(item, Return) and item.kind == "return" and item.value
            ]
            if refs and refs[0] is not None and all(r == refs[0] for r in refs):
                found = refs[0]
        self._returns[fid] = found
        return found

    def attr_ref(self, ref: Ref, attr: str, depth: int) -> Ref | None:
        kind = ref.kind
        if kind in ("module", "namespace"):
            return self.member(ref, attr, frozenset())
        if kind in ("external", "value"):
            return Ref(kind, f"{ref.key}.{attr}")
        if kind == "builtin":
            return Ref("value", f"{ref.key}.{attr}")
        if kind == "class":
            method = self.lookup_method(ref.key, attr)
            if method:
                return Ref("func", method)
            nested = f"{ref.key}.{attr}"
            return Ref("class", nested) if nested in self.classes else None
        if kind == "instance":
            field = self.field_ref(ref.key, attr, depth)
            if field is not None:
                self._inferred = True
                return field
            method = self.lookup_method(ref.key, attr)
            if method and "property" in self.functions[method].decorators:
                self._inferred = True
                return self.return_ref(method, depth + 1)
            if method:
                return Ref("func", method)
            base = self.external_base(ref.key)
            return Ref("value", f"{base}.{attr}") if base else None
        if kind == "super":
            method = self.lookup_method(ref.key, attr, skip_self=True)
            return Ref("func", method) if method else None
        return None

    def field_ref(self, cid: str, attr: str, depth: int) -> Ref | None:
        for c in self.mro(cid):
            hints = self.classes[c].fields.get(attr)
            if not hints:
                continue
            scope = _Scope(self.classes[c].file, None)
            for kind, text in hints:
                if kind == "ann":
                    found = self.annotation_ref(scope, text, depth + 1)
                elif text.startswith("self."):
                    found = self._self_call_result(c, text[len("self.") :], depth + 1)
                else:
                    found = self.result_ref(scope, self.expr_ref(scope, text, depth + 1), depth + 1)
                if found is not None:
                    return found
        return None

    def _self_call_result(self, cid: str, dotted: str, depth: int) -> Ref | None:
        parts = dotted.split(".")
        ref: Ref | None = Ref("instance", cid)
        for attr in parts[:-1]:
            ref = self.attr_ref(ref, attr, depth) if ref is not None else None
        target = self.callable_member(ref, parts[-1]) if ref is not None else None
        return self.result_ref(_Scope(self.classes[cid].file, None), target, depth + 1)

    # ── classes ────────────────────────────────────────────────────────────

    def bases(self, cid: str) -> list[Ref]:
        cls = self.classes[cid]
        scope = _Scope(cls.file, None)
        refs = []
        for text in cls.bases:
            name = text.split("[", 1)[0].strip()
            if _DOTTED.fullmatch(name):
                ref = self.expr_ref(scope, name, 1)
                if ref is not None:
                    refs.append(ref)
        return refs

    def mro(self, cid: str) -> list[str]:
        cached = self._mro.get(cid)
        if cached is not None:
            return cached
        self._mro[cid] = [cid]  # guards cycles
        order = [cid]
        for base in self.bases(cid):
            if base.kind == "class":
                for c in self.mro(base.key):
                    if c not in order:
                        order.append(c)
        self._mro[cid] = order
        return order

    def lookup_method(self, cid: str, name: str, skip_self: bool = False) -> str | None:
        for c in self.mro(cid)[1 if skip_self else 0 :]:
            method = self.classes[c].methods.get(name)
            if method:
                return method
        return None

    def class_of(self, file: str, annotation: str) -> str | None:
        """The project class an annotation written in ``file`` stands for."""
        ref = self.annotation_ref(_Scope(file, None), annotation, 0)
        return ref.key if ref is not None and ref.kind == "instance" else None

    def lineage(self, cid: str) -> list[str]:
        """Names of a class and of everything it derives from: its project
        classes in MRO order, then outside bases with their own ancestors
        when they are builtins (so ``except LookupError`` can match)."""
        names = [self.classes[c].name for c in self.mro(cid)]
        for c in self.mro(cid):
            for base in self.bases(c):
                if base.kind not in ("builtin", "external"):
                    continue
                outside = getattr(builtins, base.key, None) if base.kind == "builtin" else None
                if isinstance(outside, type):
                    found = [k.__name__ for k in outside.__mro__ if k is not object]
                else:
                    found = [base.key.rsplit(".", 1)[-1]]
                    if found[0].endswith(("Error", "Exception", "Warning")):
                        found += ["Exception", "BaseException"]
                names += [n for n in found if n not in names]
        return names

    def external_base(self, cid: str) -> str | None:
        for c in self.mro(cid):
            for base in self.bases(c):
                if base.kind == "external":
                    return base.key
        return None

    # ── calls ──────────────────────────────────────────────────────────────

    def callable_member(self, recv: Ref, name: str) -> Ref | None:
        kind = recv.kind
        if kind in ("module", "namespace"):
            return self.member(recv, name, frozenset())
        if kind in ("external", "value"):
            return Ref(kind, f"{recv.key}.{name}")
        if kind == "builtin":
            return Ref("external", f"{recv.key}.{name}")
        if kind in ("class", "instance", "super"):
            method = self.lookup_method(recv.key, name, skip_self=kind == "super")
            if method:
                return Ref("func", method)
            nested = f"{recv.key}.{name}"
            if kind == "class" and nested in self.classes:
                return Ref("class", nested)
            base = self.external_base(recv.key)
            return Ref("external", f"{base}.{name}") if base else None
        return None

    def resolve_call(self, fn: Function, call: Call) -> None:
        if not call.callee:
            return
        self._inferred = False
        scope = _Scope(fn.file, fn)
        parts = call.callee.split(".")
        if len(parts) == 1:
            target = self.name_ref(scope, parts[0], 0)
        else:
            recv = self.expr_ref(scope, ".".join(parts[:-1]), 0)
            if recv is None:
                self._guess(call, parts[-2], parts[-1])
                return
            target = self.callable_member(recv, parts[-1])
        self._apply(call, target, "inferred" if self._inferred else "exact")

    def _apply(self, call: Call, target: Ref | None, confidence: str) -> None:
        if target is None:
            return
        kind = target.kind
        if kind == "func":
            call.kind, call.target = "project", target.key
        elif kind == "class":
            init = self.lookup_method(target.key, "__init__")
            if init:
                call.kind, call.target = "project", init
            else:
                call.kind, call.target = "class", target.key
        elif kind in ("external", "value"):
            call.kind, call.external = "external", target.key
        elif kind == "builtin":
            call.kind, call.external = "builtin", target.key
        elif kind == "instance":
            method = self.lookup_method(target.key, "__call__")
            if not method:
                return
            call.kind, call.target = "project", method
        else:
            return
        call.confidence = confidence

    def _guess(self, call: Call, receiver: str, method: str) -> None:
        name = receiver.lstrip("_")
        if not name or name.startswith("$"):
            return
        hits = set()
        for cid, cls in self.classes.items():
            if _snake(cls.name) == name or cls.name.lower() == name:
                found = self.lookup_method(cid, method)
                if found:
                    hits.add(found)
        if len(hits) == 1:
            call.kind, call.target, call.confidence = "project", hits.pop(), "guess"

