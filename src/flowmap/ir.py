"""Language-neutral flow model.

Each language adapter turns source files into these objects. Everything after
that (resolution results aside) is shared: flow diagrams, data trails, exports.

A function body is a list of *items* in execution order. Only calls, control
structures and the statements that move data are kept; everything else is
dropped at extraction time.

Variable names in ``uses``/``defs``:
- plain local names (``prices``),
- ``self.<field>`` for instance fields,
- ``$1``, ``$2``... temporaries carrying the result of a nested call into the
  expression that consumes it.

Expression sources (``Arg.expr``, ``Assign.value``, ``If.test``...) are full
source text in which every nested call is replaced by its temporary, written
``__t1`` for ``$1``, so an expression can be evaluated once its calls have run.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Union

# How a variable's type can be inferred later:
#   ("ann", "Store")  - from an annotation
#   ("call", "Store") - from being assigned the result of calling ``Store``
Hint = tuple[str, str]


@dataclass
class Param:
    name: str
    annotation: str | None = None
    kind: str = "normal"  # normal | vararg | kwarg
    default: str | None = None  # source of the default value


@dataclass
class Arg:
    text: str
    uses: list[str]
    keyword: str | None = None
    star: str = ""  # "", "*" or "**"
    expr: str = ""  # full source, nested calls replaced by their temporaries


@dataclass
class Call:
    callee: str  # "fetch", "self.store.save", "$1.save"; "" when not a plain name chain
    args: list[Arg]
    receiver_uses: list[str]
    defs: list[str]  # variables receiving the result
    line: int
    col: int
    text: str
    awaited: bool = False
    # Filled in by the language linker.
    kind: str = "unresolved"  # project | class | external | builtin | unresolved
    target: str | None = None  # function id (project) or class id (class)
    confidence: str = ""  # exact | inferred | guess
    external: str | None = None  # dotted name of an outside callable
    io: tuple[str, str] | None = None  # (channel, direction), e.g. ("file", "in")
    receiver: str = ""  # source of the object a method is called on
    method: str = ""  # the method's name, for ``receiver.method(...)`` calls


@dataclass
class Assign:
    defs: list[str]
    uses: list[str]
    line: int
    target: str = ""  # source of the assignment target(s)
    value: str = ""  # source of the value


@dataclass
class If:
    cond: str
    uses: list[str]
    then: list[Item]
    orelse: list[Item]
    line: int
    col: int = 0
    test: str = ""  # full source of the condition


@dataclass
class Loop:
    kind: str  # for | while | comprehension
    header: str
    defs: list[str]
    uses: list[str]
    body: list[Item]
    orelse: list[Item]
    line: int
    col: int = 0
    target: str = ""  # source of the loop variable(s)
    iter: str = ""  # source of what a for loop runs over
    test: str = ""  # source of a while loop's condition
    head: int = 0  # leading body items that compute a while loop's condition


@dataclass
class Handler:
    types: str
    name: str | None
    body: list[Item]
    line: int
    col: int = 0


@dataclass
class Try:
    body: list[Item]
    handlers: list[Handler]
    orelse: list[Item]
    final: list[Item]
    line: int
    col: int = 0


@dataclass
class Case:
    pattern: str
    body: list[Item]
    line: int
    col: int = 0


@dataclass
class Match:
    subject: str
    uses: list[str]
    cases: list[Case]
    line: int
    col: int = 0
    subject_expr: str = ""


@dataclass
class Return:
    value: str
    uses: list[str]
    line: int
    kind: str = "return"  # return | yield | yield from
    col: int = 0
    expr: str = ""  # full source of the value


@dataclass
class Raise:
    exc: str
    uses: list[str]
    line: int
    col: int = 0
    expr: str = ""  # full source of what is raised


@dataclass
class Break:
    line: int
    col: int = 0


@dataclass
class Continue:
    line: int
    col: int = 0


Item = Union[Call, Assign, If, Loop, Try, Match, Return, Raise, Break, Continue]


@dataclass
class Import:
    module: str  # as written; relative imports keep their leading dots
    name: str | None  # imported symbol for ``from`` imports, "*" for star imports
    alias: str | None
    line: int


@dataclass
class Function:
    id: str  # "<file>::<qualname>"
    name: str
    qualname: str
    file: str
    line: int
    end_line: int
    params: list[Param]
    returns: str | None
    doc: str | None
    decorators: list[str]
    body: list[Item]
    cls: str | None = None  # class id for methods
    is_async: bool = False
    imports: list[Import] = field(default_factory=list)
    hints: dict[str, list[Hint]] = field(default_factory=dict)
    language: str = "python"
    receiver: str | None = None  # implicit receiver; Python uses its first explicit param
    is_constructor: bool = False
    generated_temps: set[str] = field(default_factory=set)


@dataclass
class ClassDef:
    id: str
    name: str
    qualname: str
    file: str
    line: int
    bases: list[str]
    methods: dict[str, str]  # method name -> function id
    fields: dict[str, list[Hint]]
    doc: str | None
    defaults: dict[str, str] = field(default_factory=dict)  # class-level name -> source of its value


@dataclass
class Module:
    file: str
    language: str
    doc: str | None
    imports: list[Import]
    functions: dict[str, Function]
    classes: dict[str, ClassDef]
    has_main_guard: bool
    globals: dict[str, list[Hint]] = field(default_factory=dict)
    constants: dict[str, str] = field(default_factory=dict)  # module-level name -> source of its value
    guard: tuple[int, int] | None = None  # first and last line of ``if __name__ == "__main__":``
    metadata: dict = field(default_factory=dict)
    diagnostics: list[dict] = field(default_factory=list)


def is_generated(fn: Function, name: str) -> bool:
    """Generated value identity; Python retains its legacy reserved spelling."""
    return name in fn.generated_temps or (fn.language == "python" and name.startswith("$"))


def call_results(fn: Function) -> dict[int, list[str]]:
    """Visible results of calls, including an immediately assigned generated value.

    Some adapters lower ``value = call()`` to a generated call result followed
    by Assign. Keep that IR intact while exposing its user binding to diagrams
    and trails. Look only within each block, never across branches.
    """
    results: dict[int, list[str]] = {}

    def block(items: list[Item]) -> None:
        for index, item in enumerate(items):
            if isinstance(item, Call):
                results[id(item)] = item.defs
                following = items[index + 1] if index + 1 < len(items) else None
                if (fn.language != "python" and isinstance(following, Assign)
                        and following.uses and set(following.uses) <= set(item.defs)
                        and all(is_generated(fn, name) for name in following.uses)
                        and following.defs and all(not is_generated(fn, name) for name in following.defs)):
                    results[id(item)] = following.defs
            for children in child_blocks(item):
                block(children)

    block(fn.body)
    return results


def self_name(fn: Function) -> str | None:
    """The name a method calls its own object by (``self``), if it has one."""
    if fn.language != "python":
        return fn.receiver
    if fn.cls and fn.params and "staticmethod" not in fn.decorators:
        return fn.params[0].name
    return None


def constructor(fn: Function) -> bool:
    """Constructor metadata, with compatibility for existing Python IR."""
    return fn.is_constructor or (fn.language == "python" and fn.name == "__init__")


def callable_params(fn: Function, call: Call | None = None) -> list[Param]:
    """Actual parameters, preserving Python plain-name unbound method calls.

    An implicit receiver is separate from the parameter list. Python's first
    explicit receiver is omitted at START and for bound calls/constructors.
    """
    skip = fn.language == "python" and self_name(fn) is not None and (
        call is None or constructor(fn) or "." in call.callee
    )
    return fn.params[1:] if skip else fn.params


def child_blocks(item: Item) -> list[list[Item]]:
    """The nested item lists of a control structure."""
    if isinstance(item, If):
        return [item.then, item.orelse]
    if isinstance(item, Loop):
        return [item.body, item.orelse]
    if isinstance(item, Try):
        return [item.body, *(h.body for h in item.handlers), item.orelse, item.final]
    if isinstance(item, Match):
        return [c.body for c in item.cases]
    return []


def walk(items: list[Item]) -> Iterator[Item]:
    """Every item, depth first, in source order."""
    for item in items:
        yield item
        for block in child_blocks(item):
            yield from walk(block)
