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
    kind: str = "return"  # return | yield
    col: int = 0
    expr: str = ""  # full source of the value


@dataclass
class Raise:
    exc: str
    uses: list[str]
    line: int
    col: int = 0


Item = Union[Call, Assign, If, Loop, Try, Match, Return, Raise]


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
