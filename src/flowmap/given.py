"""Data handed to a walk, and what a walk will need.

JSON inputs become what the code expects: objects of the annotated project
classes, paths, dates, decimals. Each variable also gets an example of the
JSON it takes, built from its annotation, so the viewer can show what data is
supposed to be there.
"""

from __future__ import annotations

import ast
import copy
import datetime
import decimal
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from flowmap.flow import display_name
from flowmap.ir import Assign, Call, Function, If, Item, Loop, Match, Raise, Return, self_name, walk
from flowmap.lang.python.evaluate import is_known, parse_expression
from flowmap.project import Project
from flowmap.values import MISSING, Handle, Obj, Parser, encode

if TYPE_CHECKING:
    from flowmap.simulate import _Scope


class Given:
    """The part of the walk's runner that turns given data into values."""

    def annotation(self, fn: Function, name: str) -> str | None:
        for param in fn.params:
            if param.name == name:
                if param.annotation:
                    return param.annotation
                if param.name == self_name(fn) and fn.cls in self.project.classes:
                    return self.project.classes[fn.cls].name
                return None
        for kind, text in fn.hints.get(name, []):
            if kind == "ann":
                return text
        for kind, text in fn.hints.get(name, []):
            if kind == "call" and self.types and self.types.class_of(fn.file, text):
                return text
        return None

    def typed(self, value, annotation: str | None, file: str, depth: int = 0):
        """A JSON value as the code expects it: dicts become objects of the
        annotated project class, strings become paths or dates, and so on."""
        if value is None or not annotation or depth > 8:
            return value
        node = parse_expression(annotation.strip())
        return self._typed(value, node, file, depth) if node is not None else value

    def _typed(self, value, node: ast.expr, file: str, depth: int):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.typed(value, node.value, file, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            options = [node.left, node.right]
            return self._first_fit(value, options, file, depth)
        if isinstance(node, ast.Subscript):
            base = (ast.unparse(node.value)).rsplit(".", 1)[-1]
            params = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base in ("Optional", "Annotated", "Final"):
                return self._typed(value, params[0], file, depth)
            if base == "Union":
                return self._first_fit(value, params, file, depth)
            if base in ("list", "List", "Sequence", "Iterable", "set", "Set", "frozenset", "tuple", "Tuple") and isinstance(value, list):
                inner = params[0]
                items = [self._typed(v, inner, file, depth + 1) for v in value]
                return tuple(items) if base in ("tuple", "Tuple") else items
            if base in ("dict", "Dict", "Mapping", "MutableMapping") and isinstance(value, dict) and len(params) == 2:
                return {k: self._typed(v, params[1], file, depth + 1) for k, v in value.items()}
            return value
        name = ast.unparse(node)
        last = name.rsplit(".", 1)[-1]
        try:
            if last in ("Path", "PurePath", "PosixPath", "PurePosixPath") and isinstance(value, str):
                return PurePosixPath(value)
            if last == "datetime" and isinstance(value, str):
                return datetime.datetime.fromisoformat(value)
            if last == "date" and isinstance(value, str):
                return datetime.date.fromisoformat(value)
            if last == "Decimal" and isinstance(value, (str, int, float)):
                return decimal.Decimal(str(value))
        except (ValueError, decimal.InvalidOperation):
            return value
        if isinstance(value, dict) and self.types:
            cid = self.types.class_of(file, name)
            if cid:
                return self.instance_from(cid, value, depth)
        return value

    def _first_fit(self, value, options: list[ast.expr], file: str, depth: int):
        for option in options:
            if isinstance(option, ast.Constant) and option.value is None:
                continue
            converted = self._typed(value, option, file, depth)
            if converted is not value or not isinstance(value, (dict, list, str)):
                return converted
        return value

    def instance_from(self, cid: str, data: dict, depth: int) -> Obj:
        obj = self.new_object(cid)
        annotations = self.field_annotations(cid)
        for key, value in data.items():
            ann = annotations.get(key)
            obj.fields[key] = self.typed(value, ann[0], ann[1], depth + 1) if ann else value
        for name in annotations:
            if name not in obj.fields:
                default = self.class_default(cid, name)
                if default is not MISSING:
                    obj.fields[name] = default
        return obj

    def template(self, annotation: str | None, file: str, depth: int = 0):
        """An example of the JSON a variable of this type takes."""
        if not annotation or depth > 4:
            return None
        node = parse_expression(annotation.strip())
        return self._template(node, file, depth) if node is not None else None

    def _template(self, node: ast.expr, file: str, depth: int):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self.template(node.value, file, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            for option in (node.left, node.right):
                if not (isinstance(option, ast.Constant) and option.value is None):
                    return self._template(option, file, depth)
            return None
        if isinstance(node, ast.Subscript):
            base = ast.unparse(node.value).rsplit(".", 1)[-1]
            params = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base in ("Optional", "Annotated", "Final", "Union"):
                return self._template(params[0], file, depth)
            if base in ("list", "List", "Sequence", "Iterable", "set", "Set", "tuple", "Tuple", "frozenset"):
                inner = self._template(params[0], file, depth + 1)
                return [inner] if inner is not None else []
            if base in ("dict", "Dict", "Mapping", "MutableMapping"):
                return {}
            return None
        name = ast.unparse(node)
        simple = {
            "str": "", "int": 0, "float": 0.0, "bool": False, "list": [], "dict": {}, "tuple": [], "set": [],
            "Path": "", "PurePath": "", "date": "2026-01-01", "datetime": "2026-01-01T00:00:00", "Decimal": "0",
            "bytes": "", "Any": None,
        }
        last = name.rsplit(".", 1)[-1]
        if last in simple:
            return copy.deepcopy(simple[last])
        cid = self.types.class_of(file, name) if self.types else None
        if cid:
            out = {}
            for field_name, (ann, where) in self.field_annotations(cid).items():
                out[field_name] = self.template(ann, where, depth + 1)
            return out
        return None

    def describe(self, scope: _Scope) -> dict:
        fn = scope.fn
        variables = {}
        for name, value in scope.vars.items():
            if name.startswith("$"):
                continue
            annotation = self.annotation(fn, name)
            variables[name] = {
                "value": encode(value),
                "known": is_known(value) and not isinstance(value, (Obj, Handle, Parser)),
                "type": annotation,
                "template": self.template(annotation, fn.file),
            }
        return {
            "reached": True,
            "function": fn.id,
            "label": display_name(self.project, fn),
            "file": fn.file,
            "vars": variables,
        }


# ── what a walk will need ──────────────────────────────────────────────────

_ENV_NAME = re.compile(
    r"""(?:getenv|environ\.get|environ\.setdefault)\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']|environ\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]"""
)
_ARGV = re.compile(r"\bparse_(?:known_)?args\s*\(|\bsys\.argv\b")


def _sources(item: Item) -> list[str]:
    if isinstance(item, Call):
        return [item.text, *(a.expr for a in item.args)]
    if isinstance(item, Assign):
        return [item.value]
    if isinstance(item, If):
        return [item.test]
    if isinstance(item, Loop):
        return [item.iter, item.test]
    if isinstance(item, Match):
        return [item.subject_expr]
    if isinstance(item, (Return, Raise)):
        return [item.expr]
    return []


def needs(project: Project, root: str) -> dict:
    """Environment variables and command-line parsing in the code reachable from ``root``."""
    seen: set[str] = set()
    todo = [root]
    files: set[str] = set()
    env: dict[str, None] = {}
    argv = False
    while todo and len(seen) < 3000:
        fid = todo.pop()
        if fid in seen or fid not in project.functions:
            continue
        seen.add(fid)
        fn = project.functions[fid]
        files.add(fn.file)
        for item in walk(fn.body):
            for text in _sources(item):
                for found in _ENV_NAME.finditer(text or ""):
                    env[found.group(1) or found.group(2)] = None
                argv = argv or bool(_ARGV.search(text or ""))
            if isinstance(item, Call) and item.kind == "project" and item.target:
                todo.append(item.target)
        main = f"{fn.file}::<main>"
        if main not in seen and main in project.functions:
            todo.append(main)
    for file in files:
        for source in project.modules[file].constants.values():
            for found in _ENV_NAME.finditer(source):
                env[found.group(1) or found.group(2)] = None
    return {"env": list(env), "argv": argv}
