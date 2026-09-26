"""Evaluate Python expressions for the simulation, without running any code.

Only literals, operators, comparisons, subscripts, f-strings, comprehensions
over known values and the fields of project objects are computed, plus a short
list of pure builtins and ``str``/``dict``/``list`` methods that the
simulation calls through ``call_builtin`` and ``call_method``. Everything else
becomes an ``Unknown`` that reads like the code it came from. Results that
would be huge are never built.

Expression sources come from the IR, where nested calls were replaced by their
temporaries: ``__t1`` is looked up as ``$1``.
"""

from __future__ import annotations

import ast
import base64
import builtins
import collections
import copy
import datetime
import decimal
import fractions
import functools
import json
import math
import operator
import posixpath
import re
import statistics
import textwrap
from collections.abc import Callable
from pathlib import PurePath, PurePosixPath

from flowmap.values import MISSING, Handle, Obj, Parser, Unknown

NOT_PURE = object()  # returned for calls the simulation must not evaluate
MAX_LEN = 200_000  # longest string or list the simulation will build
MAX_LOOP = 200_000

# In-memory types whose attributes and methods have no effect outside the value.
SAFE_TYPES = (
    datetime.date,
    datetime.time,
    datetime.timedelta,
    decimal.Decimal,
    fractions.Fraction,
    PurePath,
    collections.Counter,
    collections.OrderedDict,
    collections.defaultdict,
    collections.deque,
    re.Match,
)
BUILTIN_TYPES = {t.__name__: t for t in (int, float, str, bool, list, dict, tuple, set, frozenset, bytes)}
_STAND_INS = (Obj, Handle, Parser)


class EvalError(Exception):
    """What the program itself would raise here: a real error on real values.
    ``bases`` names the classes the error's class derives from."""

    def __init__(self, type_name: str, message: str, bases: tuple[str, ...] = ()):
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name
        self.message = message
        self.bases = bases


def lineage_of(kind: type) -> tuple[str, ...]:
    return tuple(k.__name__ for k in kind.__mro__[1:] if k is not object)


def _raised(err: Exception) -> EvalError:
    message = repr(err.args[0]) if isinstance(err, KeyError) and err.args else str(err)
    return EvalError(type(err).__name__, message, lineage_of(type(err)))

Lookup = Callable[[str], object]

_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.BitAnd: operator.and_,
    ast.BitOr: operator.or_,
    ast.BitXor: operator.xor,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}

PURE_BUILTINS = {
    "len", "str", "int", "float", "bool", "abs", "round", "min", "max", "sum", "sorted", "list",
    "tuple", "dict", "set", "repr", "any", "all", "reversed", "enumerate", "zip", "range", "isinstance",
}
# Builtins that only need the shape of their argument, not every element.
_SHAPE_ONLY = {"len", "list", "tuple", "dict", "set", "reversed", "enumerate", "zip"}
STR_METHODS = {
    "upper", "lower", "strip", "lstrip", "rstrip", "split", "rsplit", "join", "replace", "startswith",
    "endswith", "title", "capitalize", "casefold", "zfill", "center", "ljust", "rjust", "count", "find",
    "format", "splitlines", "isdigit", "isalpha", "isalnum", "isspace", "removeprefix", "removesuffix",
}
READ_METHODS = {
    dict: {"get", "keys", "values", "items", "copy"},
    list: {"copy", "index", "count"},
    tuple: {"index", "count"},
    set: {"copy", "union", "intersection", "difference"},
}
MUTATING_METHODS = {
    list: {"append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse"},
    dict: {"update", "setdefault", "pop", "clear", "popitem"},
    set: {"add", "update", "discard", "remove", "clear"},
}


_CLOCK = {"now", "today", "utcnow"}  # the time of the simulation is not the time of a run


class _Opaque(Exception):
    """This expression cannot be known without running code."""


def is_known(value) -> bool:
    """Fully known: no Unknown anywhere inside."""
    if isinstance(value, Unknown):
        return False
    if isinstance(value, dict):
        return all(is_known(k) and is_known(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(is_known(v) for v in value)
    if isinstance(value, Obj):
        return all(is_known(v) for v in value.fields.values())
    return True


def _size_ok(value) -> bool:
    if isinstance(value, (str, bytes, list, tuple, set, dict)):
        return len(value) <= MAX_LEN
    if isinstance(value, int) and not isinstance(value, bool):
        return value.bit_length() <= 256
    return True


@functools.lru_cache(maxsize=4096)
def parse_expression(source: str) -> ast.expr | None:
    """Parsed once, evaluated many times (a loop runs the same code each round).
    Callers must not change the tree."""
    try:
        return ast.parse(source, mode="eval").body
    except SyntaxError:
        return None


def evaluate(source: str, lookup: Lookup) -> object:
    source = source.strip()
    if not source:
        return Unknown("?")
    tree = parse_expression(source)
    if tree is None:
        return Unknown(source)
    return _Evaluator(lookup).value(tree)


def render(source: str, lookup: Lookup) -> str | None:
    """An f-string as far as it can be known, for messages: the parts only a
    run would know read like the code, in ‹›. None when not an f-string."""
    tree = parse_expression(source.strip()) if source else None
    if not isinstance(tree, ast.JoinedStr):
        return None
    evaluator = _Evaluator(lookup)
    parts = []
    for part in tree.values:
        if isinstance(part, ast.Constant):
            parts.append(str(part.value))
            continue
        try:
            value = evaluator.known(part.value)
            if not is_known(value):
                raise _Opaque
            if part.conversion == ord("r"):
                value = repr(value)
            spec = evaluator.known(part.format_spec) if part.format_spec is not None else ""
            parts.append(format(value, spec))
        except Exception:  # only for display: whatever fails reads like the code
            parts.append(f"‹{evaluator.origin(part.value)}›")
    return "".join(parts)


class _Evaluator:
    def __init__(self, lookup: Lookup):
        self.lookup = lookup

    # readable origins for unknown results

    def origin(self, node: ast.AST) -> str:
        evaluator = self

        class Readable(ast.NodeTransformer):
            def visit_Name(self, name: ast.Name) -> ast.AST:
                if name.id.startswith("__t") and name.id[3:].isdigit():
                    value = evaluator.lookup(f"${name.id[3:]}")
                    text = value.origin if isinstance(value, Unknown) else repr(value) if value is not MISSING else "…"
                    return ast.Name(id=text, ctx=ast.Load())
                return name

        try:
            return ast.unparse(Readable().visit(copy.deepcopy(node)))
        except Exception:  # pragma: no cover - unparse of odd trees
            return "…"

    def value(self, node: ast.AST) -> object:
        try:
            result = self.eval(node)
        except (_Opaque, RecursionError):
            return Unknown(self.origin(node))
        except (ArithmeticError, TypeError, ValueError, KeyError, IndexError, AttributeError) as err:
            raise _raised(err) from None  # real values, real error
        if not _size_ok(result):
            return Unknown(self.origin(node))
        return result

    # the evaluator proper: raises _Opaque when a part is not knowable

    def known(self, node: ast.AST) -> object:
        value = self.eval(node)
        if isinstance(value, (Unknown, *_STAND_INS)):
            raise _Opaque  # an operator on a project object runs its own code: not modelled
        return value

    def eval(self, node: ast.AST) -> object:
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:
            raise _Opaque
        result = method(node)
        if not _size_ok(result):
            raise _Opaque
        return result

    def _Constant(self, node: ast.Constant):
        return node.value

    def _Name(self, node: ast.Name):
        name = f"${node.id[3:]}" if node.id.startswith("__t") and node.id[3:].isdigit() else node.id
        value = self.lookup(name)
        if value is MISSING:
            return BUILTIN_TYPES.get(node.id, Unknown(node.id))
        return value

    def _List(self, node: ast.List):
        return self._sequence(node.elts)

    def _Tuple(self, node: ast.Tuple):
        return tuple(self._sequence(node.elts))

    def _Set(self, node: ast.Set):
        return set(self._sequence(node.elts))

    def _sequence(self, elts: list[ast.expr]) -> list:
        out: list = []
        for elt in elts:
            if isinstance(elt, ast.Starred):
                spread = self.known(elt.value)
                out.extend(spread)
            else:
                out.append(self.value(elt))
            if len(out) > MAX_LEN:
                raise _Opaque
        return out

    def _Dict(self, node: ast.Dict):
        out: dict = {}
        for key, val in zip(node.keys, node.values):
            if key is None:  # {**other}
                spread = self.known(val)
                if not isinstance(spread, dict):
                    raise _Opaque
                out.update(spread)
            else:
                out[self.known(key)] = self.value(val)
        return out

    def _BinOp(self, node: ast.BinOp):
        left, right = self.known(node.left), self.known(node.right)
        op = _BINARY.get(type(node.op))
        if op is None:
            raise _Opaque
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > 64:
            raise _Opaque
        if isinstance(node.op, ast.Mult):
            for seq, times in ((left, right), (right, left)):
                if isinstance(seq, (str, list, tuple)) and isinstance(times, int) and len(seq) * times > MAX_LEN:
                    raise _Opaque
        if isinstance(node.op, ast.LShift) and isinstance(right, int) and right > 256:
            raise _Opaque
        return op(left, right)

    def _UnaryOp(self, node: ast.UnaryOp):
        value = self.known(node.operand)
        if isinstance(node.op, ast.Not):
            return not value
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return +value
        if isinstance(node.op, ast.Invert):
            return ~value
        raise _Opaque

    def _BoolOp(self, node: ast.BoolOp):
        is_and = isinstance(node.op, ast.And)
        value = None
        for part in node.values:
            value = self.known(part)
            if bool(value) != is_and:  # and: stop at the first falsy; or: at the first truthy
                return value
        return value

    def _Compare(self, node: ast.Compare):
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.eval(comparator)
            if isinstance(op, (ast.Is, ast.IsNot)) and (isinstance(left, Obj) or isinstance(right, Obj)):
                result = (left is right) if isinstance(op, ast.Is) else (left is not right)
            else:
                if isinstance(left, (Unknown, *_STAND_INS)) or isinstance(right, (Unknown, *_STAND_INS)):
                    raise _Opaque
                result = _COMPARE[type(op)](left, right)
            if not result:
                return False
            left = right
        return True

    def _IfExp(self, node: ast.IfExp):
        return self.eval(node.body) if self.known(node.test) else self.eval(node.orelse)

    def _Subscript(self, node: ast.Subscript):
        container = self.eval(node.value)
        if isinstance(node.slice, ast.Slice):
            parts = [self.known(p) if p is not None else None for p in (node.slice.lower, node.slice.upper, node.slice.step)]
            if isinstance(container, (list, tuple, str)):
                return container[slice(*parts)]
            raise _Opaque
        key = self.known(node.slice)
        if isinstance(container, Unknown):
            if hashable(key) and key in container.known:
                return container.known[key]
            raise _Opaque
        if isinstance(container, (dict, list, tuple, str)):
            return container[key]
        raise _Opaque

    def _Attribute(self, node: ast.Attribute):
        dotted = _dotted(node)
        if dotted is not None:  # the caller may know a whole name, like os.environ
            whole = self.lookup(dotted)
            if whole is not MISSING:
                return whole
        base = self.eval(node.value)
        if isinstance(base, Obj) and node.attr in base.fields:
            return base.fields[node.attr]
        if isinstance(base, Unknown) and node.attr in base.known:
            return base.known[node.attr]
        if isinstance(base, SAFE_TYPES) and not node.attr.startswith("_"):
            value = getattr(base, node.attr)
            if not callable(value):
                return value
        raise _Opaque

    def _JoinedStr(self, node: ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(str(part.value))
                continue
            value = self.known(part.value)
            if not is_known(value):
                raise _Opaque
            if part.conversion == ord("r"):
                value = repr(value)
            elif part.conversion == ord("s"):
                value = str(value)
            elif part.conversion == ord("a"):
                value = ascii(value)
            spec = self.known(part.format_spec) if part.format_spec is not None else ""
            parts.append(format(value, spec))
        text = "".join(parts)
        if len(text) > MAX_LEN:
            raise _Opaque
        return text

    def _comprehension(self, node, build):
        if len(node.generators) != 1 or any(
            isinstance(n, ast.Name) and n.id.startswith("__t") for n in ast.walk(node) if n is not node.generators[0].iter
        ):
            raise _Opaque  # nested loops, or a call per item
        generator = node.generators[0]
        items = self.known(generator.iter)
        if isinstance(items, dict):
            items = list(items)
        if not isinstance(items, (list, tuple, set, str)) or len(items) > MAX_LOOP:
            raise _Opaque
        outer = self.lookup
        results = []
        for item in items:
            bound = _bind_target(generator.target, item)

            def lookup(name, bound=bound):
                return bound[name] if name in bound else outer(name)

            inner = _Evaluator(lookup)
            if all(inner.known(cond) for cond in generator.ifs):
                results.append(build(inner))
        return results

    def _ListComp(self, node: ast.ListComp):
        return self._comprehension(node, lambda inner: inner.value(node.elt))

    def _SetComp(self, node: ast.SetComp):
        return set(self._comprehension(node, lambda inner: inner.known(node.elt)))

    def _GeneratorExp(self, node: ast.GeneratorExp):
        return self._comprehension(node, lambda inner: inner.value(node.elt))

    def _DictComp(self, node: ast.DictComp):
        return dict(self._comprehension(node, lambda inner: (inner.known(node.key), inner.value(node.value))))


def hashable(value) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _dotted(node: ast.expr) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _bind_target(target: ast.expr, value) -> dict:
    if isinstance(target, ast.Name):
        return {target.id: value}
    if isinstance(target, (ast.Tuple, ast.List)):
        if isinstance(value, (Unknown, *_STAND_INS)):
            raise _Opaque
        values = list(value)
        if len(values) != len(target.elts):
            raise _Opaque
        bound: dict = {}
        for elt, item in zip(target.elts, values):
            bound.update(_bind_target(elt, item))
        return bound
    raise _Opaque


# ── calls the simulation may evaluate ──────────────────────────────────────


def _plain(values) -> bool:
    return all(is_known(v) and not isinstance(v, _STAND_INS) for v in values)


def call_builtin(name: str, args: list, kwargs: dict) -> object:
    """Result of a pure builtin, an Unknown when its inputs are not known, or
    NOT_PURE when ``name`` is not one the simulation evaluates. A real error on
    real inputs raises EvalError."""
    if name not in PURE_BUILTINS:
        return NOT_PURE
    values = [*args, *kwargs.values()]
    shape_only = name in _SHAPE_ONLY
    if any(isinstance(v, (Unknown, *_STAND_INS)) for v in values) or (not shape_only and not _plain(values)):
        return Unknown(f"{name}(…)")
    if name == "range":
        try:
            span = range(*args)
        except (TypeError, ValueError) as err:
            raise _raised(err) from None
        return list(span) if len(span) <= MAX_LOOP else Unknown(f"range({len(span)} numbers)")
    try:
        result = getattr(builtins, name)(*args, **kwargs)
    except (ArithmeticError, TypeError, ValueError, KeyError, IndexError) as err:
        if not _plain(values):
            return Unknown(f"{name}(…)")  # maybe caused by what is not known
        raise _raised(err) from None
    if name in ("reversed", "enumerate", "zip"):
        result = list(result)
    return result if _size_ok(result) else Unknown(f"{name}(…)")


def call_method(receiver, method: str, args: list, kwargs: dict) -> object:
    """Result of a method on a known in-memory value (mutating ones change
    ``receiver`` in place), or NOT_PURE."""
    values = [*args, *kwargs.values()]
    allowed = False
    if isinstance(receiver, str):
        allowed = method in STR_METHODS
    elif isinstance(receiver, SAFE_TYPES):
        # PurePath lacks the disk methods of Path (read_text...): those are not evaluated here
        allowed = not method.startswith("_") and method not in _CLOCK and callable(getattr(receiver, method, None))
    else:
        for kind, methods in (*READ_METHODS.items(), *MUTATING_METHODS.items()):
            if isinstance(receiver, kind) and method in methods:
                allowed = True
    if not allowed:
        return NOT_PURE
    reads_only = isinstance(receiver, str) or any(isinstance(receiver, k) and method in m for k, m in READ_METHODS.items())
    if reads_only and not _plain(values):
        return Unknown(f"{method}(…)")
    try:
        result = getattr(receiver, method)(*args, **kwargs)
    except (ArithmeticError, TypeError, ValueError, KeyError, IndexError, AttributeError) as err:
        if not _plain([receiver, *values]):
            return Unknown(f"{method}(…)")  # maybe caused by what is not known
        raise _raised(err) from None
    if method in ("keys", "values", "items") and not isinstance(receiver, str):
        result = list(result)
    return result if _size_ok(result) and _size_ok(receiver) else Unknown(f"{method}(…)")


def _regex(function):
    def guarded(pattern, string, *rest, **kwargs):
        if len(str(pattern)) > 500 or len(str(string)) > 20_000:
            raise TypeError("too large to evaluate")
        return function(pattern, string, *rest, **kwargs)

    return guarded


LIBRARY: dict[str, Callable] = {
    "json.loads": json.loads,
    "json.dumps": json.dumps,
    **{f"math.{n}": getattr(math, n) for n in dir(math) if not n.startswith("_") and callable(getattr(math, n))},
    **{f"os.path.{n}": getattr(posixpath, n) for n in ("join", "basename", "dirname", "splitext", "split", "normpath", "isabs")},
    **{f"statistics.{n}": getattr(statistics, n) for n in ("mean", "fmean", "median", "mode", "stdev", "pstdev", "variance", "pvariance")},
    "datetime.date": datetime.date,
    "datetime.datetime": datetime.datetime,
    "datetime.time": datetime.time,
    "datetime.timedelta": datetime.timedelta,
    "datetime.datetime.strptime": datetime.datetime.strptime,
    "datetime.datetime.fromisoformat": datetime.datetime.fromisoformat,
    "datetime.date.fromisoformat": datetime.date.fromisoformat,
    "decimal.Decimal": decimal.Decimal,
    "fractions.Fraction": fractions.Fraction,
    "collections.Counter": collections.Counter,
    "collections.OrderedDict": collections.OrderedDict,
    "collections.deque": collections.deque,
    "copy.copy": copy.copy,
    "copy.deepcopy": copy.deepcopy,
    "textwrap.dedent": textwrap.dedent,
    "textwrap.shorten": textwrap.shorten,
    "base64.b64encode": base64.b64encode,
    "base64.b64decode": base64.b64decode,
    "pathlib.Path": PurePosixPath,
    "pathlib.PurePath": PurePosixPath,
    "pathlib.PurePosixPath": PurePosixPath,
    **{f"re.{n}": _regex(getattr(re, n)) for n in ("match", "search", "fullmatch", "findall", "split", "sub")},
}


def call_library(name: str, args: list, kwargs: dict) -> object:
    """Result of a pure library function on known inputs, or NOT_PURE."""
    function = LIBRARY.get(name)
    if function is None:
        return NOT_PURE
    if not _plain([*args, *kwargs.values()]):
        return Unknown(f"{name}(…)")
    try:
        result = function(*args, **kwargs)
    except (ArithmeticError, TypeError, ValueError, KeyError, IndexError, AttributeError, decimal.InvalidOperation) as err:
        raise _raised(err) from None
    return result if _size_ok(result) else Unknown(f"{name}(…)")
