"""Walk a flow diagram with the data it carries, step by step.

Nothing of the project is run and nothing is written. The walk follows the
diagram the viewer shows, opened steps included, in execution order, and keeps
the variables of every function it is inside:

- what the code computes from known values is computed as Python would
  (arithmetic, pure builtins and library functions, what goes into dicts,
  lists and objects), errors included,
- what only a real run would know is an ``Unknown`` named after where it came
  from (``json.load(fh)``), unless it is given: ``inputs`` for the variables
  where the walk starts, ``env`` for environment variables (a name missing
  from it is unknown, ``None`` means not set), ``argv`` for the command line,
  parsed by the code's own argparse set-up, and ``provided`` for the result of
  any step. Files inside the project are really read (hidden ones never);
  writes, prints and log lines are only recorded, as frame ``outputs``.

Each stop is a ``Frame``: the node reached, the arrow travelled, the variables
at that moment, what just changed, what was written and the error being
raised, if any.

- Closed steps run silently: their result and what they do to the objects
  they get come back, without frames. ``auto_open`` opens every step the data
  enters instead, so the walk shows the inside of all of them.
- A decision whose condition is known takes its branch; an unknown one stops
  the walk and asks (``choices`` holds the answers so far). Silent runs take
  both branches and keep what they agree on.
- Loops run every round; the first ``VISIBLE_ROUNDS`` are shown. A ``while``
  whose condition is unknown asks before each round.
- Exceptions propagate like in Python, through ``try``/``except``/``finally``,
  handlers matched by class (the project's own classes included).
- ``start_at`` begins the walk at any node: what comes before runs unseen,
  taking the branches that lead there, then ``inputs`` are applied.
  ``expects`` says which variables exist at a node, with their types and a
  JSON template for each.
"""

from __future__ import annotations

import ast
import copy
import posixpath
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from flowmap.flow import build_flow, display_name, item_key
from flowmap.ir import Assign, Break, Call, Continue, Function, Handler, If, Item, Loop, Match, Raise, Return, Try, self_name, walk
from flowmap.lang.python.evaluate import (
    BUILTIN_TYPES,
    EvalError,
    evaluate,
    is_known,
    hashable,
    parse_expression,
)
from flowmap.given import Given, needs
from flowmap.project import Project
from flowmap.values import MISSING, Handle, Obj, Parser, Unknown, diff, encode, exception, is_exception, summary
from flowmap.world import MAX_OUTPUTS, Outside

MAX_FRAMES = 4000
VISIBLE_ROUNDS = 25  # rounds of a loop that are shown; the rest run unseen
MAX_ROUNDS = 200_000
SILENT_DEPTH = 12  # closed steps followed inside closed steps
SILENT_BUDGET = 50_000  # statements one closed step may run
AUTO_OPEN_DEPTH = 6  # how deep auto_open nests boxes
MAX_OPENED = 60  # steps auto_open opens in one walk
TIME_LIMIT = 20.0  # seconds for one walk


@dataclass
class Frame:
    node: str
    edge: list[str] | None  # [from, to] travelled to get here
    hop: bool  # True when no drawn arrow joins the two (into or out of a box, round a loop)
    note: str
    stack: list[dict]  # the functions the walk is inside, innermost last, with their variables
    changes: dict[str, list[str]]
    outputs: list[dict] = field(default_factory=list)  # {"target", "how", "data"}: written, printed, logged
    error: dict | None = None  # {"type", "message"} of the exception at this node


@dataclass
class Simulation:
    frames: list[Frame] = field(default_factory=list)
    status: str = "done"  # done | choose | raised | limit | unreached
    choice: dict | None = None
    error: dict | None = None  # the exception that ended the run
    expanded: list[str] = field(default_factory=list)  # opened steps, auto_open's included


def simulate(
    project: Project,
    root: str,
    expanded: set[str] | frozenset[str] = frozenset(),
    start_label: str | None = None,
    choices: dict[str, str] | None = None,
    *,
    inputs: dict | None = None,
    env: dict[str, str | None] | None = None,
    argv: list[str] | None = None,
    provided: dict[str, object] | None = None,
    start_at: str | None = None,
    auto_open: bool = False,
) -> Simulation:
    runner = _Runner(project, root, expanded, start_label, choices or {}, inputs or {}, env, argv, provided or {}, start_at, auto_open)
    runner.run()
    return runner.sim


def expects(project: Project, root: str, expanded: set[str] | frozenset[str] = frozenset(), at: str = "s") -> dict:
    """What there is at node ``at`` for a walk started there: the variables in
    scope (value so far, type, JSON template), plus the environment variables
    and command line the code reads."""
    runner = _Runner(project, root, expanded, None, {}, {}, None, None, {}, at, False, probe=True)
    runner.run()
    found = runner.probed or {"reached": False, "vars": {}}
    found.update(needs(project, root))
    return found


# ── walking ────────────────────────────────────────────────────────────────


class _Stop(Exception):
    """The walk ends here: a question to ask, or a limit."""


class _Arrived(Exception):
    """``expects``: the walk reached its node."""


class _Returned(Exception):
    def __init__(self, value):
        self.value = value


class _Thrown(Exception):
    """The program raises ``exc``, first at ``where`` (file, line)."""

    def __init__(self, exc: Obj, where: tuple[str, int]):
        self.exc = exc
        self.where = where
        self.shown = False  # a frame already shows it


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


class _OutOfBudget(Exception):
    pass


_FLOW = (_Thrown, _Returned, _Break, _Continue)  # what finally blocks run for


@dataclass
class _Scope:
    fn: Function
    prefix: str
    vars: dict
    path: frozenset[str]
    quiet: bool = False
    handling: list = field(default_factory=list)  # exceptions being handled, for a bare ``raise``
    yielded: list | None = None  # a generator's values
    maybe: list = field(default_factory=list)  # values it may have returned already (unseen branches)


def _truth(value) -> bool | None:
    if isinstance(value, Unknown):
        return None
    if isinstance(value, (Obj, Handle, Parser)):
        return True
    try:
        return bool(value)
    except Exception:
        return None


def _error_info(thrown: _Thrown) -> dict:
    message = thrown.exc.fields.get("message", "")
    file, line = thrown.where
    return {"type": thrown.exc.cls, "message": message if isinstance(message, str) else summary(message), "file": file, "line": line}


def _agree(values: list, origin: str):
    """One value if they are all the same, else an Unknown."""
    first = encode(values[0])
    return values[0] if all(encode(v) == first for v in values[1:]) else Unknown(origin)


def _describe(info: dict) -> str:
    return info["type"] + (f": {info['message']}" if info["message"] else "")


def _catches(types: str, exc: Obj) -> bool:
    if not types.strip():
        return True
    names = {n.rsplit(".", 1)[-1] for n in re.findall(r"[A-Za-z_][\w.]*", types)}
    return bool(names & {exc.cls, *(exc.bases or ("Exception", "BaseException"))})


_TOUCHED: dict[int, tuple[list, frozenset[str]]] = {}


def _touched(items: list[Item]) -> frozenset[str]:
    """Variables a block may change: those it assigns, and those it hands to
    calls (a method may change its object, a function its arguments)."""
    cached = _TOUCHED.get(id(items))
    if cached is not None and cached[0] is items:
        return cached[1]
    names: set[str] = set()
    for item in walk(items):
        if isinstance(item, (Assign, Call, Loop)):
            names.update(item.defs)
        if isinstance(item, Call):
            names.update(item.receiver_uses)
            for arg in item.args:
                names.update(arg.uses)
        if isinstance(item, Try):
            names.update(h.name for h in item.handlers if h.name)
    found = frozenset(re.split(r"[.\[]", name, maxsplit=1)[0] for name in names)
    _TOUCHED[id(items)] = (items, found)
    return found


def _holds(items: list[Item], key: str) -> bool:
    """Whether ``key`` (a node key such as ``c12.4``) is somewhere in ``items``."""
    for item in walk(items):
        if isinstance(item, (Call, If, Match, Loop, Try, Raise)) and item_key(item) == key:
            return True
        if isinstance(item, Try) and any(item_key(h) == key for h in item.handlers):
            return True
    return False


def _module_name(file: str) -> str:
    name = file.removesuffix(".py").removesuffix("/__init__")
    return name.removeprefix("src/").replace("/", ".")


_SPECIAL_NAMES = {"os.environ", "sys.argv"}


class _Runner(Outside, Given):
    def __init__(
        self,
        project: Project,
        root: str,
        expanded,
        start_label: str | None,
        choices: dict[str, str],
        inputs: dict,
        env: dict | None,
        argv: list[str] | None,
        provided: dict,
        start_at: str | None,
        auto_open: bool,
        probe: bool = False,
    ):
        self.project = project
        self.root = project.functions[root]
        self.expanded = set(expanded)
        self.start_label = start_label
        self.choices = choices
        self.inputs = inputs
        self.env = env
        self.argv = [str(a) for a in argv] if argv is not None else None
        self.provided = provided
        self.start_at = start_at
        self.fast = start_at is not None  # running unseen towards start_at
        self.auto_open = auto_open
        self.probe = probe
        self.probed: dict | None = None
        self.types = project.resolvers.get("python")
        self.sim = Simulation()
        self.stack: list[_Scope] = []
        self.last: str | None = None
        self.snapshots: dict[str, dict] = {}
        self.module_values: dict[str, dict] = {}
        self.import_paths: dict[tuple[str, str], tuple[str, str] | None] = {}
        self.texts: dict[Path, str | None] = {}
        self.pending: list[dict] = []  # outputs waiting for the next frame
        self.recording = True
        self.budget = 0
        self.muted = 0
        self.opened = 0
        self.steps = 0
        self.rounds: list[int] = []  # rounds of the while loops the walk is in
        self.at = (self.root.file, self.root.line)  # the statement running, for error reports
        self.deadline = time.monotonic() + TIME_LIMIT
        self.refresh()

    def refresh(self) -> None:
        graph = build_flow(self.project, self.root.id, expanded=self.expanded, start_label=self.start_label)
        self.nodes = {n.id: n for n in graph.nodes}
        self.groups = {n.id for n in graph.nodes if n.kind == "group"}
        self.edges = {(e.source, e.target) for e in graph.edges}

    # ── the walk ───────────────────────────────────────────────────────────

    def run(self) -> None:
        fn = self.root
        scope = _Scope(fn, "", {}, frozenset({fn.id}))
        self.stack.append(scope)
        try:
            self.bind(fn, scope, [], {}, self.root_self(fn), given=None if self.fast else self.inputs)
            self.body(fn, scope, f"start: {self.start_label or display_name(self.project, fn)}")
            if self.fast:
                self.sim.status = "unreached"
        except (_Stop, _Arrived):
            pass
        except _Thrown as thrown:
            self.uncaught(thrown)
        except RecursionError:
            self.sim.status = "limit"
        if self.pending and self.sim.frames:
            self.sim.frames[-1].outputs.extend(self.pending[:MAX_OUTPUTS])
        self.sim.expanded = sorted(self.expanded)

    def uncaught(self, thrown: _Thrown) -> None:
        exc = thrown.exc
        info = _error_info(thrown)
        if self.fast:
            self.sim.status, self.sim.error = "unreached", info
            return
        code = exc.fields.get("code")
        if exc.cls == "SystemExit" and (code in (0, None) or isinstance(code, Unknown)):
            self.sim.status = "done"  # an exit code only a run would know is not an error either
            note = "the program exits" + (f" with code {summary(code)}" if code not in (0, None) else "")
        else:
            self.sim.status, self.sim.error = "raised", info
            note = f"{_describe(info)}: not caught, the program stops"
        if not thrown.shown and self.last is not None:
            self.emit(self.last, note, error=info if self.sim.status == "raised" else None, travel=False)

    def root_self(self, fn: Function):
        name = self_name(fn)
        if name is None:
            return None
        if name in self.inputs and not self.fast:
            return self.typed(self.inputs[name], fn.cls and self.project.classes[fn.cls].name, fn.file)
        return self.new_object(fn.cls)

    def fail(self, name: str, message="", bases: tuple[str, ...] | None = None, **fields) -> _Thrown:
        """The exception the program raises here."""
        return _Thrown(exception(name, message, bases, **fields), self.at)

    def finish(self, status: str) -> None:
        self.sim.status = status
        raise _Stop

    def emit(self, node: str, note: str, error: dict | None = None, travel: bool = True, force: bool = False) -> bool:
        if (self.muted and not force) or self.fast or not self.stack or self.stack[-1].quiet or node not in self.nodes:
            return False
        scope = self.stack[-1]
        edge = [self.last, node] if travel and self.last is not None else None
        hop = edge is not None and (edge[0], edge[1]) not in self.edges
        encoded = self.shown_vars(scope)
        changes = diff(self.snapshots.get(scope.prefix, {}), encoded)
        self.snapshots[scope.prefix] = encoded
        stack = [
            {
                "function": s.fn.id,
                "label": display_name(self.project, s.fn),
                "file": s.fn.file,
                "vars": encoded if s is scope else self.shown_vars(s),
            }
            for s in self.stack
            if not s.quiet
        ]
        outputs, self.pending = self.pending[:MAX_OUTPUTS], []
        self.sim.frames.append(Frame(node, edge, hop, note, stack, changes, outputs, error))
        self.last = node
        if len(self.sim.frames) >= MAX_FRAMES:
            self.finish("limit")
        return True

    @staticmethod
    def shown_vars(scope: _Scope) -> dict:
        return {k: encode(v) for k, v in scope.vars.items() if not k.startswith("$")}

    def ask(self, key: str, nid: str, question: str, options: list[tuple[str, str]], note: str) -> None:
        self.emit(nid, note, force=True)
        self.sim.status = "choose"
        self.sim.choice = {"node": key, "question": question, "options": [{"label": lb, "value": v} for lb, v in options]}
        raise _Stop

    def choice_key(self, nid: str) -> str:
        return nid + "".join(f"@{r}" for r in self.rounds)

    def leave(self, nid: str, scope: _Scope) -> None:
        """Carry on from a frame box: its outgoing arrow starts at the box."""
        if not scope.quiet and nid in self.nodes:
            self.last = nid

    # ── start anywhere ─────────────────────────────────────────────────────

    def target_key(self, scope: _Scope) -> str | None:
        """While running towards start_at: the key of the item in this scope
        that leads there."""
        if not self.fast or scope.quiet or not self.start_at.startswith(scope.prefix):
            return None
        return self.start_at[len(scope.prefix) :].split("/", 1)[0]

    def aim(self, scope: _Scope, blocks: list[list[Item]]) -> int | None:
        key = self.target_key(scope)
        if key is not None:
            for index, block in enumerate(blocks):
                if _holds(block, key):
                    return index
        return None

    def arrive(self, scope: _Scope) -> None:
        self.fast = False
        if self.probe:
            self.probed = self.describe(scope)
            raise _Arrived
        for name, value in self.inputs.items():
            scope.vars[name] = self.typed(value, self.annotation(scope.fn, name), scope.fn.file)
        self.last = None
        self.snapshots = {}
        self.pending = []

    # ── functions ──────────────────────────────────────────────────────────

    def body(self, fn: Function, scope: _Scope, note: str):
        if self.fast and self.start_at == f"{scope.prefix}s":
            self.arrive(scope)
        self.emit(f"{scope.prefix}s", note)
        try:
            self.block(fn.body, scope)
            value = None
        except _Returned as returned:
            value = returned.value
        label = display_name(self.project, fn)
        value = self.result(fn, scope, value)
        if scope.yielded is not None:
            self.emit(f"{scope.prefix}e", f"{label} yields {summary(value)}")
            return value
        self.emit(f"{scope.prefix}e", f"{label} returns {summary(value)}" if value is not None else f"{label} ends")
        return value

    def result(self, fn: Function, scope: _Scope, value):
        if scope.yielded is not None:
            return list(scope.yielded)
        if scope.maybe:
            return _agree([*scope.maybe, value], f"{display_name(self.project, fn)}(…)")
        return value

    def bind(self, fn: Function, scope: _Scope, args: list, kwargs: dict, self_obj, given: dict | None = None) -> None:
        params = list(fn.params)
        if self_obj is not None and params:
            scope.vars[params[0].name] = self_obj
            params = params[1:]
        positional = list(args)
        for param in params:
            if given is not None and param.name in given:
                scope.vars[param.name] = self.typed(given[param.name], param.annotation, fn.file)
            elif param.kind == "vararg":
                scope.vars[param.name] = tuple(positional)
                positional = []
            elif param.kind == "kwarg":
                scope.vars[param.name] = {k: v for k, v in kwargs.items() if k not in scope.vars}
            elif positional:
                scope.vars[param.name] = positional.pop(0)
            elif param.name in kwargs:
                scope.vars[param.name] = kwargs[param.name]
            elif param.default is not None:
                scope.vars[param.name] = self.value(param.default, scope)
            else:
                scope.vars[param.name] = Unknown(f"argument {param.name}")
        if any(isinstance(i, Return) and i.kind != "return" for i in walk(fn.body)):
            scope.yielded = []

    def call_function(self, call: Call, fn: Function, nid: str, caller: _Scope, args, kwargs, receiver, quiet: bool):
        self_obj = None
        constructing = fn.name == "__init__" and not call.callee.endswith("__init__")
        if self_name(fn):
            if constructing:
                self_obj = self.new_object(fn.cls)
            elif "." in call.callee:
                self_obj = receiver if receiver is not None else Unknown(call.receiver or "self")
        scope = _Scope(fn, f"{nid}/", {}, caller.path | {fn.id}, quiet)
        self.stack.append(scope)
        try:
            self.bind(fn, scope, args, kwargs, self_obj)
            if quiet:
                try:
                    self.block(fn.body, scope)
                    value = None
                except _Returned as returned:
                    value = returned.value
                except _Thrown:
                    if not scope.maybe:
                        raise
                    value = scope.maybe.pop()  # it may well have returned before failing
                value = self.result(fn, scope, value)
            else:
                label = display_name(self.project, fn)
                given = ", ".join(f"{k} = {summary(v)}" for k, v in list(scope.vars.items())[:4] if k != self_name(fn))
                value = self.body(fn, scope, f"inside {label}" + (f": {given}" if given else ""))
        finally:
            self.stack.pop()
        return self_obj if constructing else value

    def silently(self, call: Call, fn: Function, nid: str, scope: _Scope, args, kwargs, receiver):
        """Run a closed step's function without frames, for its result and side effects."""
        depth = sum(1 for s in self.stack if s.quiet)
        if fn.id in scope.path or depth >= SILENT_DEPTH:
            return self.new_object(fn.cls) if fn.name == "__init__" else Unknown(call.text)
        outermost = depth == 0
        if outermost:
            self.budget = 0
        try:
            value = self.call_function(call, fn, nid, scope, args, kwargs, receiver, quiet=True)
        except _OutOfBudget:
            if not outermost:
                raise
            return Unknown(call.text)
        except RecursionError:
            return Unknown(call.text)
        if isinstance(value, Unknown) and fn.name != "__init__":
            return Unknown(call.text)  # say it in the caller's words
        return value

    # ── statements ─────────────────────────────────────────────────────────

    def block(self, items: list[Item], scope: _Scope) -> None:
        for item in items:
            self.steps += 1
            if scope.quiet:
                self.budget += 1
                if self.budget > SILENT_BUDGET:
                    raise _OutOfBudget
            if not self.steps % 2000 and time.monotonic() > self.deadline:
                self.finish("limit")
            self.item(item, scope)

    def item(self, item: Item, scope: _Scope) -> None:
        self.at = (scope.fn.file, item.line)
        if self.fast and isinstance(item, (Call, If, Match, Loop, Try, Raise)) and self.start_at == f"{scope.prefix}{item_key(item)}":
            self.arrive(scope)
        if isinstance(item, Call):
            self.call(item, scope)
        elif isinstance(item, Assign):
            self.assign(item.target, self.value(item.value, scope) if item.value else Unknown("?"), scope)
        elif isinstance(item, If):
            self.decide(item, scope)
        elif isinstance(item, Match):
            self.match(item, scope)
        elif isinstance(item, Loop):
            self.loop(item, scope)
        elif isinstance(item, Try):
            self.attempt(item, scope)
        elif isinstance(item, Return):
            value = self.value(item.expr, scope) if item.expr else None
            if item.kind == "return":
                raise _Returned(value)
            if scope.yielded is not None and len(scope.yielded) < MAX_ROUNDS:
                if item.kind == "yield from":
                    items = self.elements(value)
                    scope.yielded.extend(items if items is not None else [Unknown(f"an item of {item.value}")])
                else:
                    scope.yielded.append(value)
        elif isinstance(item, Raise):
            self.throw(item, scope)
        elif isinstance(item, Break):
            raise _Break
        elif isinstance(item, Continue):
            raise _Continue

    def value(self, source: str, scope: _Scope):
        try:
            return evaluate(source, self.lookup(scope))
        except EvalError as err:
            raise self.fail(err.type_name, err.message, err.bases or None) from None

    def decide(self, item: If, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        branch = self.aim(scope, [item.then, item.orelse])
        how = ""
        if branch is None:
            truth = _truth(self.value(item.test, scope) if item.test else Unknown(item.cond))
            if truth is not None:
                branch = 0 if truth else 1
            elif self.fast or scope.quiet or nid not in self.nodes:
                self.both_ways([item.then, item.orelse], item.cond, scope)
                return
            else:
                key = self.choice_key(nid)
                answer = self.choices.get(key)
                if answer is None:
                    note = f"{item.cond}? It depends on data only a run would have: choose a way"
                    self.ask(key, nid, item.cond, [("yes", "yes"), ("no", "no")], note)
                branch, how = (0 if answer == "yes" else 1), " (chosen)"
        self.emit(nid, f"{item.cond} → {'yes' if branch == 0 else 'no'}{how}")
        self.block(item.then if branch == 0 else item.orelse, scope)

    def match(self, item: Match, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        chosen = self.aim(scope, [c.body for c in item.cases])
        how = ""
        if chosen is None:
            subject = self.value(item.subject_expr, scope) if item.subject_expr else Unknown(item.subject)
            if not isinstance(subject, Unknown):
                for index, case in enumerate(item.cases):
                    pattern = case.pattern.split(" if ")[0].strip()
                    if pattern == "_":
                        chosen = index
                        break
                    try:
                        literal = ast.literal_eval(pattern)
                    except (ValueError, SyntaxError):
                        break  # not a plain literal: cannot tell
                    if literal == subject and " if " not in case.pattern:
                        chosen = index
                        break
            if chosen is None:
                if self.fast or scope.quiet or nid not in self.nodes:
                    self.both_ways([c.body for c in item.cases], item.subject, scope)
                    return
                key = self.choice_key(nid)
                answer = self.choices.get(key)
                if answer is None:
                    options = [(c.pattern, str(i)) for i, c in enumerate(item.cases)]
                    self.ask(key, nid, f"match {item.subject}", options, f"match {item.subject}: choose a case")
                chosen = int(answer) if answer.isdigit() and int(answer) < len(item.cases) else 0
                how = " (chosen)"
        self.emit(nid, f"match {item.subject} → {item.cases[chosen].pattern}{how}")
        self.block(item.cases[chosen].body, scope)

    def both_ways(self, branches: list[list[Item]], condition: str, scope: _Scope) -> None:
        """Run every branch on its own copy of the variables (of those it can
        change: copying everything is slow with big data). A branch that
        returns, raises or leaves the loop stops there; the variables of the
        others are merged, keeping what they agree on."""
        base = scope.vars
        touched = set().union(*(_touched(branch) for branch in branches))
        results = []
        left = []
        for branch in branches:
            memo: dict = {}  # one per branch: variables sharing an object still share its copy
            scope.vars = {k: copy.deepcopy(v, memo) if k in touched else v for k, v in base.items()}
            try:
                self.block(branch, scope)
            except _FLOW as flow:
                left.append((flow, scope.vars))
                continue
            results.append(scope.vars)
        returned = [flow.value for flow, _ in left if isinstance(flow, _Returned)]
        if not results:  # every way out of here leaves
            scope.vars = self.merged(base, [state for _, state in left], condition)
            if returned:
                raise _Returned(_agree(returned, f"a result depending on {condition}"))
            raise left[0][0]
        scope.maybe.extend(returned)
        scope.vars = self.merged(base, results, condition)

    @staticmethod
    def merged(base: dict, results: list[dict], condition: str) -> dict:
        merged = {}
        for name in dict.fromkeys([n for r in results for n in r]):
            values = [r.get(name, MISSING) for r in results]
            if name in base and all(v is base[name] for v in values):
                merged[name] = base[name]  # not touched by any branch
                continue
            codes = [encode(v) if v is not MISSING else None for v in values]
            if name in base and all(c == encode(base[name]) for c in codes):
                merged[name] = base[name]  # untouched: keep the very same object
            elif all(c == codes[0] for c in codes) and values[0] is not MISSING:
                merged[name] = values[0]
            else:
                merged[name] = Unknown(f"{name}, depending on {condition}")
        return merged

    # loops

    def loop(self, item: Loop, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        if item.kind == "while":
            self.loop_while(item, nid, scope)
            return
        if item.kind == "comprehension":  # its variables live only inside it
            saved = {name: scope.vars.get(name, MISSING) for name in item.defs}
            try:
                self.loop_for(item, nid, scope)
            finally:
                for name, value in saved.items():
                    if value is MISSING:
                        scope.vars.pop(name, None)
                    else:
                        scope.vars[name] = value
            return
        self.loop_for(item, nid, scope)

    def loop_for(self, item: Loop, nid: str, scope: _Scope) -> None:
        iterable = self.value(item.iter, scope) if item.iter else Unknown(item.header)
        elements = self.elements(iterable)
        before = None
        if elements is None:
            origin = iterable.origin if isinstance(iterable, Unknown) else item.iter
            elements = [Unknown(f"an item of {origin}")]
            note = f"{item.header} (shown once, for an item of ‹{origin}›)"
            before = (self.snapshot(scope, item), f"after the loop over {item.iter or origin}")
        else:
            note = f"{item.header}: {len(elements)} round{'' if len(elements) == 1 else 's'}"
            if not elements and self.aim(scope, [item.body]) is not None:
                elements = [Unknown(f"an item of {item.iter}")]  # the walk starts inside: go in
        self.emit(nid, note)
        muted_here = False
        done = 0
        broke = False
        try:
            for element in elements:
                if done == VISIBLE_ROUNDS and not muted_here:
                    self.muted += 1
                    muted_here = True
                done += 1
                self.assign(item.target, element, scope)
                try:
                    self.block(item.body, scope)
                except _Continue:
                    continue
                except _Break:
                    broke = True
                    break
        finally:
            if muted_here:
                self.muted -= 1
        if muted_here:
            self.emit(nid, f"{item.header}: {done - VISIBLE_ROUNDS} more rounds ran unseen")
        if before is not None:
            self.blur(scope, *before)
        self.leave(nid, scope)
        if not broke:
            self.block(item.orelse, scope)

    @staticmethod
    def snapshot(scope: _Scope, loop: Loop) -> dict[str, dict | None]:
        """The variables a loop may change, as they are before it."""
        names = _touched(loop.body) | set(loop.defs)
        return {name: encode(scope.vars[name]) if name in scope.vars else None for name in names if not name.startswith("$")}

    def blur(self, scope: _Scope, before: dict[str, dict | None], when: str) -> None:
        """After a loop run once for items only a run would know, what it
        changed is not known either (a count of 1 would be a lie)."""
        for name in before:
            value = scope.vars.get(name, MISSING)
            if value is MISSING:
                continue
            old = before[name]
            if old is not None and encode(value) == old:
                continue
            if isinstance(value, Obj) and old is not None and old.get("t") == "obj":
                for key, field_value in list(value.fields.items()):
                    if encode(field_value) != old["v"].get(key):
                        value.fields[key] = Unknown(f"{name}.{key} {when}")
                continue
            scope.vars[name] = Unknown(f"{name} {when}")

    def elements(self, value) -> list | None:
        if isinstance(value, dict):
            items = list(value)
        elif isinstance(value, (list, tuple, str, deque)):
            items = list(value)
        elif isinstance(value, (set, frozenset)):
            items = sorted(value, key=repr)
        elif isinstance(value, Handle) and not value.writing:
            text = self.read(value)
            if not isinstance(text, str):
                return None
            items = text.splitlines(keepends=True)
        else:
            return None
        return items[:MAX_ROUNDS]

    def loop_while(self, item: Loop, nid: str, scope: _Scope) -> None:
        head, rest = item.body[: item.head], item.body[item.head :]
        condition = item.header.removeprefix("while ").strip()
        self.emit(nid, item.header)
        before = self.snapshot(scope, item)
        guessed = False
        done = 0
        broke = False
        muted_here = False
        self.rounds.append(0)
        try:
            while done < MAX_ROUNDS:
                self.rounds[-1] = done + 1
                self.block(head, scope)
                truth = _truth(self.value(item.test, scope) if item.test else Unknown(condition))
                if truth is None:
                    truth = self.another_round(item, nid, scope, done + 1, condition)
                    guessed = guessed or not self.asked(nid, scope)
                if not truth:
                    break
                if done == VISIBLE_ROUNDS and not muted_here:
                    self.muted += 1
                    muted_here = True
                done += 1
                try:
                    self.block(rest, scope)
                except _Continue:
                    continue
                except _Break:
                    broke = True
                    break
        finally:
            self.rounds.pop()
            if muted_here:
                self.muted -= 1
        if muted_here:
            self.emit(nid, f"{item.header}: {done - VISIBLE_ROUNDS} more rounds ran unseen")
        if guessed:
            self.blur(scope, before, f"after the loop while {condition}")
        self.leave(nid, scope)
        if not broke:
            self.block(item.orelse, scope)

    def asked(self, nid: str, scope: _Scope) -> bool:
        """Whether the rounds of this loop are the user's answers."""
        return not (self.fast or scope.quiet or nid not in self.nodes)

    def another_round(self, item: Loop, nid: str, scope: _Scope, round_: int, condition: str) -> bool:
        if self.target_key(scope) is not None:
            return round_ == 1 and self.aim(scope, [item.body]) is not None
        if self.fast or scope.quiet or nid not in self.nodes:
            return round_ == 1  # unseen: once through
        key = self.choice_key(nid)
        answer = self.choices.get(key)
        if answer is None:
            note = f"{condition}? It depends on data only a run would have: {'go in' if round_ == 1 else 'another round'}?"
            self.ask(key, nid, condition, [("yes", "yes"), ("no", "no")], note)
        return answer == "yes"

    # exceptions

    def attempt(self, item: Try, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        self.emit(nid, "try")
        forced = None
        key = self.target_key(scope)
        if key is not None:
            forced = next((h for h in item.handlers if item_key(h) == key or _holds(h.body, key)), None)
        pending = None
        try:
            try:
                self.block(item.body, scope)
            except _Thrown as thrown:
                handler = forced or next((h for h in item.handlers if _catches(h.types, thrown.exc)), None)
                if handler is None:
                    raise
                self.catch(nid, handler, thrown, scope)
            else:
                if forced is not None:  # the walk starts in a handler: pretend its error happened
                    name = re.findall(r"[A-Za-z_][\w.]*", forced.types)
                    self.catch(nid, forced, self.fail(name[0].rsplit(".", 1)[-1] if name else "Exception"), scope)
                else:
                    self.leave(nid, scope)
                    self.block(item.orelse, scope)
        except _FLOW as flow:
            pending = flow
        if item.final:
            self.block(item.final, scope)
        if pending is not None:
            raise pending

    def catch(self, try_nid: str, handler: Handler, thrown: _Thrown, scope: _Scope) -> None:
        hid = f"{scope.prefix}{item_key(handler)}"
        if self.fast and self.start_at == hid:
            self.arrive(scope)
        if hid in self.nodes and try_nid in self.nodes and not scope.quiet:
            self.last = try_nid  # along the red error arrow
        exc = thrown.exc
        info = _error_info(thrown)
        self.emit(hid, f"except {handler.types or 'anything'}: caught {_describe(info)}", error=info)
        if handler.name:
            scope.vars[handler.name] = exc
        scope.handling.append(exc)
        try:
            self.block(handler.body, scope)
        finally:
            scope.handling.pop()
            if handler.name:
                scope.vars.pop(handler.name, None)  # like Python: gone after the handler
        self.leave(hid, scope)

    def throw(self, item: Raise, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(item)}"
        if not item.expr and not item.exc:
            exc = scope.handling[-1] if scope.handling else exception("RuntimeError", "No active exception to reraise")
        else:
            value = self.value(item.expr, scope) if item.expr else Unknown(item.exc)
            exc = self.as_exception(value, item.exc, scope)
        thrown = _Thrown(exc, self.at)
        info = _error_info(thrown)
        thrown.shown = self.emit(nid, f"raises {_describe(info)}", error=info)
        raise thrown

    def as_exception(self, value, text: str, scope: _Scope) -> Obj:
        if isinstance(value, Obj):
            return value
        if isinstance(value, type) and issubclass(value, BaseException):
            return exception(value.__name__)
        name = text.split("(", 1)[0].strip()
        cid = self.types.class_of(scope.fn.file, name) if self.types and name else None
        if cid:
            return self.new_object(cid)
        return exception(name.rsplit(".", 1)[-1] or "Exception")

    # ── calls ──────────────────────────────────────────────────────────────

    def call(self, call: Call, scope: _Scope) -> None:
        nid = f"{scope.prefix}{item_key(call)}"
        args, kwargs = self.arguments(call, scope)
        receiver = self.value(call.receiver, scope) if call.receiver else None
        fn = self.project.functions.get(call.target or "") if call.kind == "project" else None
        label = display_name(self.project, fn) if fn is not None else call.text
        drawn = nid in self.nodes and not scope.quiet

        if nid in self.provided and not scope.quiet:
            result = self.typed(self.provided[nid], fn.returns if fn else None, fn.file if fn else scope.fn.file)
            self.store_result(call, result, scope)
            self.emit(nid, f"{label}: given {summary(result)}")
            return

        if fn is not None and self.should_open(nid, fn, scope):
            self.expanded.add(nid)
            self.opened += 1
            self.refresh()

        if fn is not None and nid in self.groups and not scope.quiet:
            self.emit(nid, f"into {label}")
            try:
                result = self.call_function(call, fn, nid, scope, args, kwargs, receiver, quiet=False)
            except _Thrown as thrown:
                info = _error_info(thrown)
                if self.emit(nid, f"{label} raises {_describe(info)}", error=info):
                    thrown.shown = True
                raise
            self.store_result(call, result, scope)
            produced = self.named(call)
            self.emit(nid, f"back from {label}" + (f": {', '.join(produced)} = {summary(result)}" if produced else ""))
            return

        try:
            if fn is not None:
                result = self.silently(call, fn, nid, scope, args, kwargs, receiver)
            elif call.kind == "class":
                result = self.construct(call, args, kwargs, scope)
            else:
                result = self.outside(call, receiver, args, kwargs, scope)
        except _Thrown as thrown:
            if drawn:
                info = _error_info(thrown)
                if self.emit(nid, f"{label} raises {_describe(info)}", error=info):
                    thrown.shown = True
            raise
        self.store_result(call, result, scope)
        if drawn:
            produced = self.named(call)
            if call.io:
                verb = {"in": "reads", "out": "writes", "inout": "uses"}.get(call.io[1], "uses")
                note = f"{verb} {call.io[0]}: {call.text}"
            else:
                note = label
            self.emit(nid, note + (f" → {', '.join(produced)} = {summary(result)}" if produced else ""))

    def should_open(self, nid: str, fn: Function, scope: _Scope) -> bool:
        node = self.nodes.get(nid)
        return (
            self.auto_open
            and not (self.fast or self.muted or scope.quiet)
            and node is not None
            and node.kind == "step"
            and not node.detail.get("recursive")
            and fn.id not in scope.path
            and nid.count("/") < AUTO_OPEN_DEPTH
            and self.opened < MAX_OPENED
        )

    @staticmethod
    def named(call: Call) -> list[str]:
        return [d for d in call.defs if not d.startswith("$")]

    def arguments(self, call: Call, scope: _Scope) -> tuple[list, dict]:
        args: list = []
        kwargs: dict = {}
        for arg in call.args:
            value = self.value(arg.expr, scope) if arg.expr else Unknown(arg.text)
            if arg.star == "*":
                args.extend(value if isinstance(value, (list, tuple)) else [value])
            elif arg.star == "**":
                kwargs.update(value if isinstance(value, dict) else {})
            elif arg.keyword:
                kwargs[arg.keyword] = value
            else:
                args.append(value)
        return args, kwargs

    # objects

    def new_object(self, cid: str | None) -> Obj:
        cls = self.project.classes.get(cid or "")
        if cls is None:
            return Obj("object")
        lineage = self.types.lineage(cls.id) if self.types else [cls.name]
        return Obj(cls.name, {}, tuple(lineage[1:]))

    def class_chain(self, cid: str) -> list[str]:
        """Project classes of ``cid``'s MRO, bases first (dataclass field order)."""
        order = self.types.mro(cid) if self.types else [cid]
        return [c for c in reversed(order) if c in self.project.classes]

    def field_annotations(self, cid: str) -> dict[str, tuple[str, str]]:
        found: dict[str, tuple[str, str]] = {}
        for c in self.class_chain(cid):
            cls = self.project.classes[c]
            for name, hints in cls.fields.items():
                ann = next((text for kind, text in hints if kind == "ann"), None)
                if ann and not ann.startswith("ClassVar"):
                    found[name] = (ann, cls.file)
        return found

    def class_default(self, cid: str, name: str):
        for c in reversed(self.class_chain(cid)):
            cls = self.project.classes[c]
            source = cls.defaults.get(name)
            if source is None:
                continue
            factory = re.fullmatch(r"(?:dataclasses\.)?field\((.*)\)", source.strip(), re.S)
            if factory:
                made = re.search(r"default_factory\s*=\s*(\w+)", factory.group(1))
                if made:
                    kind = BUILTIN_TYPES.get(made.group(1))
                    return kind() if kind else Unknown(f"{made.group(1)}()")
                given = re.search(r"default\s*=\s*(.+?)\s*(?:,\s*\w+\s*=|$)", factory.group(1), re.S)
                source = given.group(1) if given else ""
                if not source:
                    return MISSING
            probe = _Scope(_module_stand_in(cls.file), "", {}, frozenset())
            try:
                return evaluate(source, self.lookup(probe))
            except EvalError:
                return Unknown(source)
        return MISSING

    def construct(self, call: Call, args: list, kwargs: dict, scope: _Scope) -> Obj:
        """A project class without ``__init__``: dataclass-like fields."""
        obj = self.new_object(call.target)
        cid = call.target or ""
        if cid not in self.project.classes:
            obj.cls = call.callee.rsplit(".", 1)[-1] or obj.cls
            return obj
        if is_exception(obj):
            obj.fields["message"] = self.message(call, args, scope)
            return obj
        names = list(self.field_annotations(cid))
        for name, value in zip(names, args):
            obj.fields[name] = value
        for name, value in kwargs.items():
            if name in names:
                obj.fields[name] = value
        for name in names:
            if name not in obj.fields:
                default = self.class_default(cid, name)
                if default is not MISSING:
                    obj.fields[name] = default
        return obj

    # ── variables ──────────────────────────────────────────────────────────

    def lookup(self, scope: _Scope):
        fn = scope.fn

        def find(name: str):
            if name in scope.vars:
                return scope.vars[name]
            special = self.special(name, fn)
            if special is not MISSING:
                return special
            if fn.name != "<main>":
                values = self.globals_of(fn.file)
                if name in values:
                    return values[name]
            return self.imported(fn, name)

        return find

    def special(self, name: str, fn: Function):
        if name == "__name__":
            return "__main__" if fn.file == self.root.file and self.root.name == "<main>" else _module_name(fn.file)
        if name == "__file__":
            return str(self.project.root / fn.file)
        if name == "os.environ":
            return Unknown("os.environ", {k: v for k, v in (self.env or {}).items() if v is not None})
        if name == "sys.argv" and self.argv is not None:
            return [posixpath.basename(self.root.file), *self.argv]
        return MISSING

    def globals_of(self, file: str) -> dict:
        """A module's global values: its top-level code, run unseen (without
        its ``if __name__ == "__main__":`` block), or its constants."""
        found = self.module_values.get(file)
        if found is not None:
            return found
        self.module_values[file] = {}  # guards modules that import each other
        module = self.project.modules.get(file)
        if module is None:
            return {}
        main = module.functions.get(f"{file}::<main>")
        scope = _Scope(main or _module_stand_in(file), f"{file}#", {}, frozenset(), quiet=True)
        if main is not None:
            guard = module.guard
            items = [i for i in main.body if not (guard and guard[0] <= i.line <= guard[1])]
        else:
            items = [Assign([name], [], 0, name, source) for name, source in module.constants.items()]
        saved = (self.budget, self.recording, self.rounds)
        self.budget, self.recording, self.rounds = 0, False, []
        self.stack.append(scope)
        try:
            for item in items:
                try:
                    self.block([item], scope)
                except (*_FLOW, _OutOfBudget, RecursionError):
                    if isinstance(item, Assign):
                        continue
                    break
        finally:
            self.stack.pop()
            self.budget, self.recording, self.rounds = saved
        self.module_values[file] = scope.vars
        return scope.vars

    def imported(self, fn: Function, name: str):
        key = (fn.id, name)
        if key not in self.import_paths:
            self.import_paths[key] = self.import_path(fn, name)
        found = self.import_paths[key]
        if found is None:
            return MISSING
        file, attr = found
        if file == "":  # os.environ or sys.argv under another name
            return self.special(attr, fn)
        return self.globals_of(file).get(attr, MISSING)

    def import_path(self, fn: Function, name: str) -> tuple[str, str] | None:
        """Where an imported name's value lives: (module file, name in it)."""
        module = self.project.modules.get(fn.file)
        if self.types is None or module is None:
            return None
        for imp in [*fn.imports, *module.imports]:
            if imp.name and imp.name != "*":
                if (imp.alias or imp.name) != name:
                    continue
                dotted = f"{imp.module}.{imp.name}"
                if dotted in _SPECIAL_NAMES:
                    return ("", dotted)
                ref = self.types.module_ref(fn.file, imp.module)
                return (ref.key, imp.name) if ref is not None and ref.kind == "module" else None
            if imp.name is None:
                bound = imp.alias or imp.module.split(".")[0]
                prefix = imp.alias or imp.module
                if not name.startswith(f"{prefix}.") or name.split(".", 1)[0] != bound:
                    continue
                attr = name[len(prefix) + 1 :]
                if "." in attr:
                    continue
                if f"{imp.module}.{attr}" in _SPECIAL_NAMES:
                    return ("", f"{imp.module}.{attr}")
                ref = self.types.module_ref(fn.file, imp.module)
                return (ref.key, attr) if ref is not None and ref.kind == "module" else None
        return None

    def store_result(self, call: Call, result, scope: _Scope) -> None:
        if len(call.defs) == 1:
            self.put(call.defs[0], result, scope)
        elif call.defs:
            parts = list(result) if isinstance(result, (list, tuple)) and len(result) == len(call.defs) else None
            for index, name in enumerate(call.defs):
                self.put(name, parts[index] if parts else Unknown(f"{summary(result)}[{index}]"), scope)

    def put(self, name: str, value, scope: _Scope) -> None:
        owner = self_name(scope.fn)
        if owner and name.startswith(f"{owner}."):
            obj = scope.vars.get(owner)
            field_name = name[len(owner) + 1 :]
            if isinstance(obj, Obj):
                obj.fields[field_name] = value
            elif isinstance(obj, Unknown):
                obj.known[field_name] = value
            return
        if "[" in name or "." in name:
            self.assign(name, value, scope)
            return
        scope.vars[name] = value

    def assign(self, target: str, value, scope: _Scope) -> None:
        node = parse_expression(target)
        if node is not None:
            self.bind_target(node, value, scope)

    def bind_target(self, node: ast.expr, value, scope: _Scope) -> None:
        if isinstance(node, ast.Name):
            scope.vars[node.id] = value
        elif isinstance(node, (ast.Tuple, ast.List)):
            self.unpack(node.elts, value, scope)
        elif isinstance(node, ast.Starred):
            self.bind_target(node.value, value, scope)
        elif isinstance(node, ast.Attribute):
            base = self.value(ast.unparse(node.value), scope)
            if isinstance(base, Obj):
                base.fields[node.attr] = value
            elif isinstance(base, Unknown):
                base.known[node.attr] = value
        elif isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Slice):
            container = self.value(ast.unparse(node.value), scope)
            key = self.value(ast.unparse(node.slice), scope)
            if isinstance(key, Unknown):
                self.spoil(ast.unparse(node.value), scope, f"{ast.unparse(node)} = …")
                return
            if isinstance(container, dict):
                try:
                    container[key] = value
                except TypeError as err:
                    raise self.fail("TypeError", str(err)) from None
            elif isinstance(container, list) and isinstance(key, int):
                if not -len(container) <= key < len(container):
                    raise self.fail("IndexError", "list assignment index out of range")
                container[key] = value
            elif isinstance(container, Unknown) and hashable(key):
                container.known[key] = value

    def spoil(self, source: str, scope: _Scope, why: str) -> None:
        """Something inside the value ``source`` names changed at a place only
        a run would know: the variable (or the object field) holding it is
        not known any more."""
        node = parse_expression(source)
        chain = []
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            chain.append(node)
            node = node.value
        if not isinstance(node, ast.Name) or node.id not in scope.vars:
            return
        root = scope.vars[node.id]
        if isinstance(root, Obj) and chain and isinstance(chain[-1], ast.Attribute):
            if chain[-1].attr in root.fields and not isinstance(root.fields[chain[-1].attr], Unknown):
                root.fields[chain[-1].attr] = Unknown(f"{node.id}.{chain[-1].attr}, changed by {why}")
        elif isinstance(root, (dict, list, set)):
            scope.vars[node.id] = Unknown(f"{node.id}, changed by {why}")

    def unpack(self, targets: list[ast.expr], value, scope: _Scope) -> None:
        starred = [i for i, t in enumerate(targets) if isinstance(t, ast.Starred)]
        if isinstance(value, (list, tuple, str)) or (isinstance(value, dict) and is_known(value)):
            items = list(value)
            if starred:
                at = starred[0]
                after = len(targets) - at - 1
                if len(items) < len(targets) - 1:
                    raise self.fail("ValueError", f"not enough values to unpack (expected at least {len(targets) - 1}, got {len(items)})")
                items = [*items[:at], items[at : len(items) - after], *items[len(items) - after :]]
            elif len(items) != len(targets):
                few = len(items) < len(targets)
                message = (
                    f"not enough values to unpack (expected {len(targets)}, got {len(items)})"
                    if few
                    else f"too many values to unpack (expected {len(targets)})"
                )
                raise self.fail("ValueError", message)
            for target, item in zip(targets, items):
                self.bind_target(target, item, scope)
            return
        for index, target in enumerate(targets):
            self.bind_target(target, Unknown(f"{summary(value)}[{index}]"), scope)





def _module_stand_in(file: str) -> Function:
    """A function standing for a module's top level, to look names up in it."""
    return Function(f"{file}::<module>", "<module>", "<module>", file, 1, 1, [], None, None, [], [])
