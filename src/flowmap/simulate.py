"""Walk a flow diagram with the data it carries, step by step, without running
any code.

The walk follows exactly the diagram the viewer shows, opened steps included,
in execution order. It keeps the variables of every function it is inside:
what can be known is computed (literals, arithmetic, pure builtins, what the
code puts into dicts, lists and objects) and what cannot is named after where
it came from (``json.load(fh)``). Each stop is a ``Frame``: the node reached,
the arrow travelled, the variables at that moment and what just changed.

- Closed steps still run, silently: their result and what they do to the
  objects they are given come back, without frames (within limits).
- A decision whose condition is known takes its branch. An unknown one stops
  the walk and asks; ``choices`` holds the answers so far. Inside silent runs
  both branches are followed and differing values become unknown.
- Loops run over up to three known items, or once over "an item of ...".
- Exceptions are not simulated: try blocks take their normal path, and a
  ``raise`` ends the walk.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field

from flowmap.flow import display_name, item_key
from flowmap.flow import build_flow as _build_flow
from flowmap.ir import Assign, Call, Function, If, Item, Loop, Match, Raise, Return, Try
from flowmap.lang.python.evaluate import NOT_PURE, call_builtin, call_method, evaluate
from flowmap.project import Project
from flowmap.values import MISSING, Obj, Unknown, diff, encode

MAX_FRAMES = 1500
MAX_ITERATIONS = 3
SILENT_DEPTH = 4
SILENT_BUDGET = 600  # items a silent run may execute


@dataclass
class Frame:
    node: str
    edge: list[str] | None  # [from, to] travelled to get here
    hop: bool  # True when no drawn arrow joins the two (into or out of a box, round a loop)
    note: str
    stack: list[dict]  # the functions the walk is inside, innermost last, with their variables
    changes: dict[str, list[str]]


@dataclass
class Simulation:
    frames: list[Frame] = field(default_factory=list)
    status: str = "done"  # done | choose | raised | limit
    choice: dict | None = None


class _Stop(Exception):
    pass


class _Returned(Exception):
    def __init__(self, value):
        self.value = value


class _Raised(Exception):
    pass


class _OutOfBudget(Exception):
    pass


@dataclass
class _Scope:
    fn: Function
    prefix: str
    vars: dict
    path: frozenset[str]
    quiet: bool = False


def simulate(
    project: Project,
    root: str,
    expanded: set[str] | frozenset[str] = frozenset(),
    start_label: str | None = None,
    choices: dict[str, str] | None = None,
) -> Simulation:
    graph = _build_flow(project, root, expanded=expanded, start_label=start_label)
    runner = _Runner(project, graph, choices or {})
    runner.run(project.functions[root], start_label)
    return runner.sim


def summary(value) -> str:
    """A few words for a value, for frame notes."""
    if isinstance(value, Unknown):
        return f"‹{value.origin}›"
    if isinstance(value, Obj):
        return f"{value.cls} object"
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    if isinstance(value, (list, tuple, set)):
        return f"[{len(value)} items]"
    text = repr(value)
    return text if len(text) <= 40 else text[:39] + "…"


def _self_name(fn: Function) -> str | None:
    if fn.cls and fn.params and "staticmethod" not in fn.decorators:
        return fn.params[0].name
    return None


class _Runner:
    def __init__(self, project: Project, graph, choices: dict[str, str]):
        self.project = project
        self.nodes = {n.id: n for n in graph.nodes}
        self.groups = {n.id for n in graph.nodes if n.kind == "group"}
        self.edges = {(e.source, e.target) for e in graph.edges}
        self.choices = choices
        self.sim = Simulation()
        self.stack: list[_Scope] = []
        self.last: str | None = None
        self.snapshots: dict[str, dict] = {}
        self.constants: dict[tuple[str, str], object] = {}
        self.budget = 0

    # ── frames ─────────────────────────────────────────────────────────────

    def run(self, fn: Function, start_label: str | None) -> None:
        scope = _Scope(fn, "", {}, frozenset({fn.id}))
        self.bind(fn, scope, [], {}, None)
        self.stack.append(scope)
        try:
            self.body(fn, scope, f"start: {start_label or display_name(self.project, fn)}")
        except _Stop:
            pass

    def emit(self, node: str, note: str) -> None:
        scope = self.stack[-1]
        if scope.quiet or node not in self.nodes:
            return
        edge = [self.last, node] if self.last is not None else None
        hop = edge is not None and (edge[0], edge[1]) not in self.edges
        encoded = {k: encode(v) for k, v in scope.vars.items() if not k.startswith("$")}
        changes = diff(self.snapshots.get(scope.prefix, {}), encoded)
        self.snapshots[scope.prefix] = encoded
        stack = [
            {
                "function": s.fn.id,
                "label": display_name(self.project, s.fn),
                "file": s.fn.file,
                "vars": encoded if s is scope else {k: encode(v) for k, v in s.vars.items() if not k.startswith("$")},
            }
            for s in self.stack
        ]
        self.sim.frames.append(Frame(node, edge, hop, note, stack, changes))
        self.last = node
        if len(self.sim.frames) >= MAX_FRAMES:
            self.sim.status = "limit"
            raise _Stop

    def stop(self, status: str, choice: dict | None = None) -> None:
        self.sim.status = status
        self.sim.choice = choice
        raise _Stop

    # ── functions ──────────────────────────────────────────────────────────

    def body(self, fn: Function, scope: _Scope, note: str) -> object:
        self.emit(f"{scope.prefix}s", note)
        try:
            self.block(fn.body, scope)
            value = None
        except _Returned as returned:
            value = returned.value
        label = display_name(self.project, fn)
        self.emit(f"{scope.prefix}e", f"{label} returns {summary(value)}" if value is not None else f"{label} ends")
        return value

    def bind(self, fn: Function, scope: _Scope, args: list, kwargs: dict, self_obj) -> None:
        params = list(fn.params)
        if self_obj is not None and params:
            scope.vars[params[0].name] = self_obj
            params = params[1:]
        positional = list(args)
        for param in params:
            if param.kind == "vararg":
                scope.vars[param.name] = tuple(positional)
                positional = []
            elif param.kind == "kwarg":
                scope.vars[param.name] = {k: v for k, v in kwargs.items() if k not in scope.vars}
            elif positional:
                scope.vars[param.name] = positional.pop(0)
            elif param.name in kwargs:
                scope.vars[param.name] = kwargs[param.name]
            elif param.default is not None:
                scope.vars[param.name] = evaluate(param.default, self.lookup(scope))
            else:
                scope.vars[param.name] = Unknown(f"argument {param.name}")

    def call_function(self, call: Call, fn: Function, nid: str, caller: _Scope, args, kwargs, receiver, quiet: bool):
        self_obj = None
        if _self_name(fn):
            if fn.name == "__init__":
                cls = self.project.classes.get(fn.cls or "")
                self_obj = Obj(cls.name if cls else "object")
            elif "." in call.callee:
                self_obj = receiver if receiver is not None else Unknown(call.receiver or "self")
        scope = _Scope(fn, f"{nid}/", {}, caller.path | {fn.id}, quiet)
        self.bind(fn, scope, args, kwargs, self_obj)
        self.stack.append(scope)
        try:
            if quiet:
                try:
                    self.block(fn.body, scope)
                    value = None
                except _Returned as returned:
                    value = returned.value
            else:
                label = display_name(self.project, fn)
                given = ", ".join(f"{k} = {summary(v)}" for k, v in list(scope.vars.items())[:4] if k != _self_name(fn))
                value = self.body(fn, scope, f"inside {label}" + (f": {given}" if given else ""))
        finally:
            self.stack.pop()
        return self_obj if fn.name == "__init__" else value

    # ── statements ─────────────────────────────────────────────────────────

    def block(self, items: list[Item], scope: _Scope) -> None:
        for item in items:
            if scope.quiet:
                self.budget += 1
                if self.budget > SILENT_BUDGET:
                    raise _OutOfBudget
            self.item(item, scope)

    def item(self, item: Item, scope: _Scope) -> None:
        if isinstance(item, Call):
            self.call(item, scope)
        elif isinstance(item, Assign):
            value = evaluate(item.value, self.lookup(scope)) if item.value else Unknown("?")
            self.assign(item.target, value, scope)
        elif isinstance(item, If):
            self.decide(item, scope)
        elif isinstance(item, Match):
            self.match(item, scope)
        elif isinstance(item, Loop):
            self.loop(item, scope)
        elif isinstance(item, Try):
            nid = f"{scope.prefix}{item_key(item)}"
            self.emit(nid, "try")
            self.block(item.body, scope)
            self.leave(nid, scope)
            self.block(item.orelse, scope)
            self.block(item.final, scope)
        elif isinstance(item, Return):
            if item.kind == "return":
                raise _Returned(evaluate(item.expr, self.lookup(scope)) if item.expr else None)
        elif isinstance(item, Raise):
            if scope.quiet:
                raise _Raised
            self.emit(f"{scope.prefix}{item_key(item)}", f"raises {item.exc or 'the error again'}")
            self.stop("raised")

    def leave(self, nid: str, scope: _Scope) -> None:
        """Carry on from a frame box: its outgoing arrow starts at the box."""
        if not scope.quiet and nid in self.nodes:
            self.last = nid

    def decide(self, item: If, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        test = evaluate(item.test, self.lookup(scope)) if item.test else Unknown(item.cond)
        if not isinstance(test, Unknown):
            branch, how = bool(test), ""
        elif not scope.quiet and nid in self.nodes:
            answer = self.choices.get(nid)
            if answer is None:
                self.emit(nid, f"{item.cond}? It depends on data only a run would have: choose a way")
                options = [{"label": "yes", "value": "yes"}, {"label": "no", "value": "no"}]
                self.stop("choose", {"node": nid, "question": item.cond, "options": options})
            branch, how = answer == "yes", " (chosen)"
        else:
            self.both_ways([item.then, item.orelse], item.cond, scope)
            return
        self.emit(nid, f"{item.cond} → {'yes' if branch else 'no'}{how}")
        self.block(item.then if branch else item.orelse, scope)

    def match(self, item: Match, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        subject = evaluate(item.subject_expr, self.lookup(scope)) if item.subject_expr else Unknown(item.subject)
        chosen = None
        if not isinstance(subject, Unknown):
            for index, case in enumerate(item.cases):
                pattern = case.pattern.split(" if ")[0].strip()
                if pattern == "_":
                    chosen = index
                    break
                try:
                    if ast.literal_eval(pattern) == subject and " if " not in case.pattern:
                        chosen = index
                        break
                except (ValueError, SyntaxError):
                    break  # not a plain literal: cannot tell
        if chosen is None:
            if scope.quiet or nid not in self.nodes:
                self.both_ways([c.body for c in item.cases], item.subject, scope)
                return
            answer = self.choices.get(nid)
            if answer is None:
                self.emit(nid, f"match {item.subject}: choose a case")
                options = [{"label": c.pattern, "value": str(i)} for i, c in enumerate(item.cases)]
                self.stop("choose", {"node": nid, "question": f"match {item.subject}", "options": options})
            chosen = int(answer) if answer.isdigit() and int(answer) < len(item.cases) else 0
        self.emit(nid, f"match {item.subject} → {item.cases[chosen].pattern}")
        self.block(item.cases[chosen].body, scope)

    def both_ways(self, branches: list[list[Item]], condition: str, scope: _Scope) -> None:
        """Run every branch on its own copy of the variables; keep what they agree on."""
        base = scope.vars
        results = []
        for branch in branches:
            scope.vars = copy.deepcopy(base)
            try:
                self.block(branch, scope)
            except (_Returned, _Raised):
                pass
            results.append(scope.vars)
        merged = {}
        for name in dict.fromkeys([n for r in results for n in r]):
            values = [r.get(name, MISSING) for r in results]
            codes = [encode(v) if v is not MISSING else None for v in values]
            if name in base and all(c == encode(base[name]) for c in codes):
                merged[name] = base[name]  # untouched: keep the very same object
            elif all(c == codes[0] for c in codes) and values[0] is not MISSING:
                merged[name] = values[0]
            else:
                merged[name] = Unknown(f"{name}, depending on {condition}")
        scope.vars = merged

    def loop(self, item: Loop, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        look = self.lookup(scope)
        note = item.header
        if item.kind == "while":
            test = evaluate(item.test, look) if item.test else Unknown(item.header)
            elements = [] if not isinstance(test, Unknown) and not test else [None]
        else:
            iterable = evaluate(item.iter, look) if item.iter else Unknown(item.header)
            if isinstance(iterable, dict):
                iterable = list(iterable)
            if isinstance(iterable, (list, tuple, str, set, frozenset)):
                everything = sorted(iterable, key=repr) if isinstance(iterable, (set, frozenset)) else list(iterable)
                elements = everything[:MAX_ITERATIONS]
                if len(everything) > MAX_ITERATIONS:
                    note += f" (first {MAX_ITERATIONS} of {len(everything)})"
            else:
                origin = iterable.origin if isinstance(iterable, Unknown) else item.iter
                elements = [Unknown(f"an item of {origin}")]
        self.emit(nid, note)
        for element in elements:
            if item.kind != "while":
                self.assign(item.target, element, scope)
            self.block(item.body, scope)
        self.leave(nid, scope)
        self.block(item.orelse, scope)

    # ── calls ──────────────────────────────────────────────────────────────

    def call(self, call: Call, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(call)}"
        look = self.lookup(scope)
        args: list = []
        kwargs: dict = {}
        for arg in call.args:
            value = evaluate(arg.expr, look) if arg.expr else Unknown(arg.text)
            if arg.star == "*":
                args.extend(value if isinstance(value, (list, tuple)) else [value])
            elif arg.star == "**":
                kwargs.update(value if isinstance(value, dict) else {})
            elif arg.keyword:
                kwargs[arg.keyword] = value
            else:
                args.append(value)
        receiver = evaluate(call.receiver, look) if call.receiver else None
        fn = self.project.functions.get(call.target or "") if call.kind == "project" else None
        label = display_name(self.project, fn) if fn is not None else call.text

        if fn is not None and nid in self.groups and not scope.quiet:
            self.emit(nid, f"into {label}")
            result = self.call_function(call, fn, nid, scope, args, kwargs, receiver, quiet=False)
            self.store_result(call, result, scope)
            self.emit(nid, f"back from {label}" + (f": {', '.join(self.named(call))} = {summary(result)}" if self.named(call) else ""))
            return

        if fn is not None:
            result = self.silently(call, fn, nid, scope, args, kwargs, receiver)
        elif call.kind == "class":
            result = self.construct(call, args, kwargs)
        else:
            result = self.outside(call, receiver, args, kwargs)
        self.store_result(call, result, scope)
        if nid in self.nodes:
            produced = self.named(call)
            if call.io:
                verb = {"in": "reads", "out": "writes", "inout": "uses"}.get(call.io[1], "uses")
                note = f"{verb} {call.io[0]}: {call.text}"
            else:
                note = label
            self.emit(nid, note + (f" → {', '.join(produced)} = {summary(result)}" if produced else ""))

    @staticmethod
    def named(call: Call) -> list[str]:
        return [d for d in call.defs if not d.startswith("$")]

    def silently(self, call: Call, fn: Function, nid: str, scope: _Scope, args, kwargs, receiver):
        """Run a closed step's function without frames, for its result and side effects."""
        depth = sum(1 for s in self.stack if s.quiet)
        if fn.id in scope.path or depth >= SILENT_DEPTH:
            return Obj(self.project.classes[fn.cls].name) if fn.name == "__init__" and fn.cls in self.project.classes else Unknown(call.text)
        outermost = depth == 0
        if outermost:
            self.budget = 0
        try:
            value = self.call_function(call, fn, nid, scope, args, kwargs, receiver, quiet=True)
        except (_Raised, _OutOfBudget, _Stop, RecursionError):
            return Unknown(call.text)
        if isinstance(value, Unknown) and fn.name != "__init__":
            return Unknown(call.text)  # say it in the caller's words
        return value

    def construct(self, call: Call, args: list, kwargs: dict) -> Obj:
        cls = self.project.classes.get(call.target or "")
        obj = Obj(cls.name if cls else call.callee)
        if cls is not None:  # dataclass-like: annotated class fields, in order
            names = [name for name, hints in cls.fields.items() if any(kind == "ann" for kind, _ in hints)]
            for name, value in zip(names, args):
                obj.fields[name] = value
            for name, value in kwargs.items():
                if name in names:
                    obj.fields[name] = value
        return obj

    def outside(self, call: Call, receiver, args: list, kwargs: dict):
        if call.io:
            return Unknown(call.text)
        result = NOT_PURE
        if call.kind == "builtin" and "." not in call.callee:
            result = call_builtin(call.callee, args, kwargs)
        elif receiver is not None and "." in call.callee:
            result = call_method(receiver, call.callee.rsplit(".", 1)[-1], args, kwargs)
        if result is NOT_PURE or isinstance(result, Unknown):
            return Unknown(call.text)  # in the words of the code
        return result

    # ── variables ──────────────────────────────────────────────────────────

    def lookup(self, scope: _Scope):
        module = self.project.modules.get(scope.fn.file)

        def find(name: str):
            if name in scope.vars:
                return scope.vars[name]
            if module is not None and name in module.constants:
                return self.constant(module, name)
            return MISSING

        return find

    def constant(self, module, name: str):
        key = (module.file, name)
        if key not in self.constants:
            self.constants[key] = Unknown(name)  # guards constants defined through each other
            self.constants[key] = evaluate(
                module.constants[name],
                lambda other: self.constant(module, other) if other in module.constants else MISSING,
            )
        return copy.deepcopy(self.constants[key])

    def store_result(self, call: Call, result, scope: _Scope) -> None:
        if len(call.defs) == 1:
            self.put(call.defs[0], result, scope)
        elif call.defs:
            parts = list(result) if isinstance(result, (list, tuple)) and len(result) == len(call.defs) else None
            for index, name in enumerate(call.defs):
                self.put(name, parts[index] if parts else Unknown(f"{summary(result)}[{index}]"), scope)

    def put(self, name: str, value, scope: _Scope) -> None:
        owner = _self_name(scope.fn)
        if owner and name.startswith(f"{owner}."):
            obj = scope.vars.get(owner)
            field_name = name[len(owner) + 1 :]
            if isinstance(obj, Obj):
                obj.fields[field_name] = value
            elif isinstance(obj, Unknown):
                obj.known[field_name] = value
            return
        scope.vars[name] = value

    def assign(self, target: str, value, scope: _Scope) -> None:
        try:
            node = ast.parse(target, mode="eval").body
        except SyntaxError:
            return
        self.bind_target(node, value, scope)

    def bind_target(self, node: ast.expr, value, scope: _Scope) -> None:
        if isinstance(node, ast.Name):
            scope.vars[node.id] = value
        elif isinstance(node, (ast.Tuple, ast.List)):
            parts = list(value) if isinstance(value, (list, tuple, str)) and len(value) == len(node.elts) else None
            for index, element in enumerate(node.elts):
                self.bind_target(element, parts[index] if parts else Unknown(f"{summary(value)}[{index}]"), scope)
        elif isinstance(node, ast.Attribute):
            base = evaluate(ast.unparse(node.value), self.lookup(scope))
            if isinstance(base, Obj):
                base.fields[node.attr] = value
            elif isinstance(base, Unknown):
                base.known[node.attr] = value
        elif isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
            look = self.lookup(scope)
            container = evaluate(ast.unparse(node.value), look)
            key = evaluate(ast.unparse(node.slice), look)
            if isinstance(key, Unknown):
                return
            if isinstance(container, dict):
                container[key] = value
            elif isinstance(container, list) and isinstance(key, int) and -len(container) <= key < len(container):
                container[key] = value
            elif isinstance(container, Unknown):
                container.known[key] = value
