"""Follow one piece of data through the code: a static, "may flow" slice.

Starting where a variable gets its value (a parameter at START, or the output
of a step), the trace walks the flow in execution order and tracks which
variables carry the data. It opens every call the data enters, follows
returns back to the caller, and keeps only what touches the data:

- calls that receive it, produce it, or are called on it,
- I/O that reads or writes it,
- decisions that test it, loops over it, raises and returns that carry it.

Rules of thumb: an outside call hands the data on to its result
(``df2 = df.dropna()``); container methods such as ``out.append(row)`` put it
into their receiver; logging is not a use; assigning something else to a
variable ends the trail for that variable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flowmap.flow import item_key
from flowmap.ir import Assign, Call, Function, If, Item, Loop, Match, Raise, Return, Try
from flowmap.project import Project

MUTATORS = {
    "append", "appendleft", "extend", "extendleft", "add", "update", "insert", "setdefault",
    "put", "put_nowait", "push", "write", "writelines", "send",
}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
MAX_DEPTH = 12
MAX_CONTEXTS = 300


@dataclass
class Context:
    """One function the trace went into, reached through a chain of calls."""

    prefix: str  # node id prefix of the function's diagram ("" for the root)
    function: str
    parent: str | None  # prefix of the calling context
    params: list[str]  # parameters that received the data
    returns: bool = False  # hands the data back to its caller
    active: bool = False  # the data shows up in this function
    io: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (node id, code, [channel, direction])


@dataclass
class Trail:
    origin: str  # node id where the data starts (an opened step's nodes are prefixed)
    var: str
    keep: set[str] = field(default_factory=set)  # node ids to draw
    opened: set[str] = field(default_factory=set)  # steps to open
    uses: set[str] = field(default_factory=set)  # outside calls drawn as "use" nodes
    names: dict[str, list[str]] = field(default_factory=dict)  # node id -> variables carrying the data out of it
    found: bool = False
    truncated: bool = False
    contexts: dict[str, Context] = field(default_factory=dict)  # by prefix, in trace order


def trace(project: Project, root: str, at: str, var: str) -> Trail:
    """Trail of ``var`` starting at node ``at`` in the diagram of ``root``."""
    trail = Trail(origin=at, var=var)
    parts = at.split("/")
    path_steps = {"/".join(parts[: i + 1]) for i in range(len(parts) - 1)}
    tracer = _Tracer(project, trail, path_steps)
    tracer.context(project.functions[root], "", set(), frozenset({root}), 0)
    return trail


def _is_logging(call: Call) -> bool:
    if call.callee == "print" or (call.external or "").startswith("logging."):
        return True
    parts = call.callee.split(".")
    return len(parts) >= 2 and parts[-1] in LOG_METHODS and any(
        p.lower().endswith(("log", "logger", "logging")) for p in parts[:-1]
    )


def bind(call: Call, callee: Function, hot: set[str]) -> set[str]:
    """Parameters of ``callee`` that receive data from the ``hot`` variables."""
    params = callee.params
    skip_self = bool(callee.cls and params and "staticmethod" not in callee.decorators) and (
        callee.name == "__init__" or "." in call.callee
    )
    plist = params[1:] if skip_self else params
    positional = [p for p in plist if p.kind == "normal"]
    by_name = {p.name: p for p in plist}
    varargs = [p.name for p in plist if p.kind == "vararg"]
    kwargs = [p.name for p in plist if p.kind == "kwarg"]
    tainted: set[str] = set()
    index = 0
    for arg in call.args:
        carries = bool(set(arg.uses) & hot)
        if arg.star == "*":
            if carries:
                tainted |= {p.name for p in positional[index:]} | set(varargs)
            continue
        if arg.star == "**":
            if carries:
                tainted |= {p.name for p in positional} | set(kwargs)
            continue
        if arg.keyword:
            if carries:
                tainted |= {arg.keyword} if arg.keyword in by_name else set(kwargs)
            continue
        if index < len(positional):
            if carries:
                tainted.add(positional[index].name)
            index += 1
        elif carries:
            tainted |= set(varargs)
    return tainted


class _Tracer:
    def __init__(self, project: Project, trail: Trail, path_steps: set[str]):
        self.project = project
        self.trail = trail
        self.path_steps = path_steps
        self.contexts = 0
        self.stack: list[Context] = []

    def context(
        self, fn: Function, prefix: str, hot: set[str], path: frozenset[str], depth: int, parent: str | None = None
    ) -> bool:
        """Trace one function body; True when it returns the data."""
        self.contexts += 1
        hot = set(hot)
        start = f"{prefix}s"
        if start == self.trail.origin:
            hot.add(self.trail.var)
            self.trail.found = True
        self.trail.names[start] = sorted(hot)
        ctx = Context(prefix, fn.id, parent, sorted(hot), active=bool(hot))
        self.trail.contexts[prefix] = ctx
        self.stack.append(ctx)
        _, ctx.returns, _ = self.block(fn.body, hot, prefix, path, depth)
        self.stack.pop()
        return ctx.returns

    def block(self, items: list[Item], hot: set[str], prefix: str, path, depth) -> tuple[set[str], bool, bool]:
        """(hot variables after the block, whether the data is returned, whether anything was kept)."""
        returns = kept = False
        for item in items:
            hot, r, k = self.item(item, hot, prefix, path, depth)
            returns |= r
            kept |= k
        return hot, returns, kept

    def keep(self, prefix: str, item) -> None:
        self.trail.keep.add(f"{prefix}{item_key(item)}")

    def item(self, item: Item, hot: set[str], prefix: str, path, depth) -> tuple[set[str], bool, bool]:
        if isinstance(item, Call):
            hot, kept = self.call(item, hot, prefix, path, depth)
            return hot, False, kept

        if isinstance(item, Assign):
            if set(item.uses) & hot:
                self.stack[-1].active = True
                return hot | set(item.defs), False, False
            return hot - set(item.defs), False, False

        if isinstance(item, If):
            tested = bool(set(item.uses) & hot)
            then_hot, r1, k1 = self.block(item.then, set(hot), prefix, path, depth)
            else_hot, r2, k2 = self.block(item.orelse, set(hot), prefix, path, depth)
            kept = tested or k1 or k2
            if kept:
                self.keep(prefix, item)
            return then_hot | else_hot, r1 or r2, kept

        if isinstance(item, Match):
            tested = bool(set(item.uses) & hot)
            after, returns, kept = set(hot), False, tested
            for case in item.cases:
                case_hot, r, k = self.block(case.body, set(hot), prefix, path, depth)
                after |= case_hot
                returns |= r
                kept |= k
            if kept:
                self.keep(prefix, item)
            return after, returns, kept

        if isinstance(item, Loop):
            inside = hot | set(item.defs) if set(item.uses) & hot else hot - set(item.defs)
            body_hot, returns, kept = self.block(item.body, set(inside), prefix, path, depth)
            if not body_hot <= inside:  # data carried round the loop: one more pass
                body_hot, returns, kept = self.block(item.body, inside | body_hot, prefix, path, depth)
            after = hot | body_hot
            if item.orelse:
                after, r, k = self.block(item.orelse, after, prefix, path, depth)
                returns |= r
                kept |= k
            if kept:
                self.keep(prefix, item)
            return after, returns, kept

        if isinstance(item, Try):
            body_hot, returns, kept = self.block(item.body, set(hot), prefix, path, depth)
            after = set(body_hot)
            for handler in item.handlers:
                start = (hot | body_hot) - ({handler.name} if handler.name else set())
                handler_hot, r, k = self.block(handler.body, start, prefix, path, depth)
                if k:
                    self.keep(prefix, handler)
                after |= handler_hot
                returns |= r
                kept |= k
            for block in (item.orelse, item.final):
                if block:
                    after, r, k = self.block(block, after, prefix, path, depth)
                    returns |= r
                    kept |= k
            if kept:
                self.keep(prefix, item)
            return after, returns, kept

        if isinstance(item, Return):
            carried = item.kind == "return" and bool(set(item.uses) & hot)
            return hot, carried, carried

        if isinstance(item, Raise):
            if set(item.uses) & hot:
                self.keep(prefix, item)
                return hot, False, True
            return hot, False, False

        return hot, False, False

    def call(self, call: Call, hot: set[str], prefix: str, path, depth) -> tuple[set[str], bool]:
        nid = f"{prefix}{item_key(call)}"
        is_origin = nid == self.trail.origin
        on_path = nid in self.path_steps
        args_hot = any(set(a.uses) & hot for a in call.args)
        receiver_hot = bool(set(call.receiver_uses) & hot)
        out = kept = False

        if call.kind in ("project", "class"):
            fn = self.project.functions.get(call.target or "") if call.kind == "project" else None
            entered = False
            if fn is not None and (args_hot or on_path) and not is_origin:
                if fn.id not in path and depth < MAX_DEPTH and self.contexts < MAX_CONTEXTS:
                    self.trail.opened.add(nid)
                    out = self.context(fn, f"{nid}/", bind(call, fn, hot), path | {fn.id}, depth + 1, prefix)
                    entered = True
                elif fn.id not in path:
                    self.trail.truncated = True
            if not entered and args_hot:
                out = True  # could not look inside: assume the data comes back out
            constructs = call.kind == "class" or (fn is not None and fn.name == "__init__")
            out = out or receiver_hot or (constructs and args_hot)  # a new object holds what it was given
            kept = entered or args_hot or receiver_hot or on_path or is_origin
        elif not _is_logging(call):
            out = args_hot or receiver_hot
            named_result = any(not d.startswith("$") for d in call.defs)
            receiver = call.receiver_uses[0] if len(call.receiver_uses) == 1 else None
            method = call.callee.rsplit(".", 1)[-1]
            if args_hot and receiver and method in MUTATORS:
                hot = hot | {receiver}
                self.trail.uses.add(nid)
                kept = True
            if (args_hot or receiver_hot) and call.io:
                kept = True
            elif (args_hot or receiver_hot) and named_result:
                self.trail.uses.add(nid)
                kept = True
            if is_origin:
                kept = True
                if not call.io:
                    self.trail.uses.add(nid)

        hot = set(hot)
        for name in call.defs:
            if out:
                hot.add(name)
            else:
                hot.discard(name)
        if is_origin:
            hot.add(self.trail.var)
            self.trail.found = True
        if args_hot or receiver_hot or is_origin:
            self.stack[-1].active = True
        if kept:
            self.trail.keep.add(nid)
            self.trail.names[nid] = [d for d in call.defs if d in hot]
            if call.io and call.kind not in ("project", "class"):
                self.stack[-1].io.append((nid, call.text, list(call.io)))
        return hot, kept
