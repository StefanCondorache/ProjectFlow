"""Python source -> flowmap IR, using tree-sitter."""

from __future__ import annotations

import re

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from flowmap.ir import (
    Arg,
    Assign,
    Call,
    Case,
    ClassDef,
    Function,
    Handler,
    Hint,
    If,
    Import,
    Item,
    Loop,
    Match,
    Module,
    Param,
    Raise,
    Return,
    Try,
    walk,
)

_PARSER = Parser(Language(tree_sitter_python.language()))

_IMPORTS = ("import_statement", "import_from_statement")
_DEFINITIONS = ("function_definition", "class_definition", "decorated_definition")
_COMPREHENSIONS = ("list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression")
_MAX_TEXT = 100


def extract_module(relpath: str, source: bytes) -> Module:
    tree = _PARSER.parse(source)
    return _ModuleExtractor(relpath, tree.root_node).run()


# ── small helpers ──────────────────────────────────────────────────────────


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _short(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _line(node: Node) -> int:
    return node.start_point[0] + 1


def _col(node: Node) -> int:
    return node.start_point[1]


def _named(node: Node) -> list[Node]:
    return [c for c in node.named_children if c.type != "comment"]


def _dedupe(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _dotted(node: Node) -> str | None:
    """``a.b.c`` for pure name chains, else None."""
    if node.type == "identifier":
        return _text(node)
    if node.type == "attribute":
        base = _dotted(node.child_by_field_name("object"))
        attr = node.child_by_field_name("attribute")
        if base is not None and attr is not None:
            return f"{base}.{_text(attr)}"
    return None


def _string_content(node: Node) -> str:
    return "".join(_text(c) for c in node.named_children if c.type == "string_content")


def _docstring(block: Node | None) -> str | None:
    if block is None:
        return None
    for stmt in _named(block):
        if stmt.type == "expression_statement":
            parts = _named(stmt)
            if parts and parts[0].type == "string":
                for line in _string_content(parts[0]).splitlines():
                    if line.strip():
                        return line.strip()
        return None
    return None


def _annotation(node: Node | None) -> str | None:
    if node is None:
        return None
    inner = _named(node)[0] if node.type == "type" and _named(node) else node
    if inner.type == "string":
        return _short(_string_content(inner))
    return _short(_text(inner))


def _params(node: Node | None) -> list[Param]:
    out: list[Param] = []
    if node is None:
        return out
    for p in _named(node):
        kind = p.type
        if kind == "identifier":
            out.append(Param(_text(p)))
        elif kind == "typed_parameter":
            inner = _named(p)[0]
            ann = _annotation(p.child_by_field_name("type"))
            if inner.type == "list_splat_pattern":
                out.append(Param(_splat_name(inner), ann, "vararg"))
            elif inner.type == "dictionary_splat_pattern":
                out.append(Param(_splat_name(inner), ann, "kwarg"))
            else:
                out.append(Param(_text(inner), ann))
        elif kind == "default_parameter":
            out.append(Param(_text(p.child_by_field_name("name"))))
        elif kind == "typed_default_parameter":
            out.append(Param(_text(p.child_by_field_name("name")), _annotation(p.child_by_field_name("type"))))
        elif kind == "list_splat_pattern":
            out.append(Param(_splat_name(p), None, "vararg"))
        elif kind == "dictionary_splat_pattern":
            out.append(Param(_splat_name(p), None, "kwarg"))
    return out


def _splat_name(node: Node) -> str:
    names = [c for c in node.named_children if c.type == "identifier"]
    return _text(names[0]) if names else _text(node).lstrip("*")


def _is_main_guard(node: Node) -> bool:
    cond = node.child_by_field_name("condition")
    if cond is None or cond.type != "comparison_operator":
        return False
    parts = [_text(c) for c in _named(cond)]
    ops = [c.type for c in cond.children if not c.is_named]
    return (
        len(parts) == 2
        and ops == ["=="]
        and "__name__" in parts
        and any(p.strip("'\"") == "__main__" for p in parts if p != "__name__")
    )


def _parse_imports(node: Node) -> list[Import]:
    line = _line(node)
    if node.type == "import_statement":
        out = []
        for ch in node.children_by_field_name("name"):
            if ch.type == "aliased_import":
                out.append(Import(_text(ch.child_by_field_name("name")), None, _text(ch.child_by_field_name("alias")), line))
            else:
                out.append(Import(_text(ch), None, None, line))
        return out
    module = re.sub(r"\s+", "", _text(node.child_by_field_name("module_name")))
    if any(c.type == "wildcard_import" for c in node.named_children):
        return [Import(module, "*", None, line)]
    out = []
    for ch in node.children_by_field_name("name"):
        if ch.type == "aliased_import":
            out.append(Import(module, _text(ch.child_by_field_name("name")), _text(ch.child_by_field_name("alias")), line))
        else:
            out.append(Import(module, _text(ch), None, line))
    return out


def _unwrap_decorated(node: Node) -> tuple[Node, list[str]]:
    if node.type != "decorated_definition":
        return node, []
    decorators = [_short(_text(_named(d)[0])) for d in node.named_children if d.type == "decorator" and _named(d)]
    return node.child_by_field_name("definition"), decorators


def _exception_types(node: Node) -> str:
    if node.type in ("tuple", "parenthesized_expression"):
        return ", ".join(_text(c) for c in _named(node))
    return _text(node)


# ── modules, classes and functions ─────────────────────────────────────────


class _ModuleExtractor:
    def __init__(self, relpath: str, root: Node):
        self.file = relpath
        self.root = root
        self.imports: list[Import] = []
        self.functions: dict[str, Function] = {}
        self.classes: dict[str, ClassDef] = {}
        self.has_main_guard = False

    def run(self) -> Module:
        doc = _docstring(self.root)
        statements = _named(self.root)
        if doc is not None:
            statements = statements[1:]
        executable: list[Node] = []
        for stmt in statements:
            if stmt.type in _IMPORTS:
                self.imports.extend(_parse_imports(stmt))
            elif stmt.type in _DEFINITIONS:
                self.definition(stmt, scope="")
            else:
                if stmt.type == "if_statement" and _is_main_guard(stmt):
                    self.has_main_guard = True
                if stmt.type in ("if_statement", "try_statement", "with_statement"):
                    self._scan_nested(stmt)
                executable.append(stmt)

        body = _Body(self, qualname="<main>", params=[], cls_id=None, self_name=None, module_level=True)
        items = body.statements(executable)
        module_globals = {k: v for k, v in body.hints.items() if not k.startswith("$")}
        if self.has_main_guard or self.file.endswith("__main__.py") or any(isinstance(i, Call) for i in walk(items)):
            fid = f"{self.file}::<main>"
            self.functions[fid] = Function(
                id=fid,
                name="<main>",
                qualname="<main>",
                file=self.file,
                line=1,
                end_line=self.root.end_point[0] + 1,
                params=[],
                returns=None,
                doc=doc,
                decorators=[],
                body=items,
                hints=body.hints,
            )
        return Module(
            file=self.file,
            language="python",
            doc=doc,
            imports=self.imports,
            functions=self.functions,
            classes=self.classes,
            has_main_guard=self.has_main_guard,
            globals=module_globals,
        )

    def _scan_nested(self, node: Node) -> None:
        """Imports and definitions inside module-level if/try/with blocks."""
        for child in node.named_children:
            if child.type == "block":
                for stmt in _named(child):
                    if stmt.type in _IMPORTS:
                        self.imports.extend(_parse_imports(stmt))
                    elif stmt.type in _DEFINITIONS:
                        self.definition(stmt, scope="")
                    elif stmt.type in ("if_statement", "try_statement", "with_statement"):
                        self._scan_nested(stmt)
            elif child.type in ("elif_clause", "else_clause", "except_clause", "except_group_clause", "finally_clause"):
                self._scan_nested(child)

    def definition(self, node: Node, scope: str, cls_id: str | None = None) -> tuple[str, str] | None:
        inner, decorators = _unwrap_decorated(node)
        if inner is None:
            return None
        if inner.type == "function_definition":
            return "function", self.function(inner, decorators, scope, cls_id)
        if inner.type == "class_definition":
            return "class", self.klass(inner, scope)
        return None

    def function(self, node: Node, decorators: list[str], scope: str, cls_id: str | None) -> str:
        name = _text(node.child_by_field_name("name"))
        qualname = f"{scope}.{name}" if scope else name
        fid = f"{self.file}::{qualname}"
        params = _params(node.child_by_field_name("parameters"))
        self_name = None
        if cls_id is not None and params and "staticmethod" not in decorators:
            self_name = params[0].name
        block = node.child_by_field_name("body")
        body = _Body(self, qualname=qualname, params=params, cls_id=cls_id, self_name=self_name)
        items = body.block(block)
        self.functions[fid] = Function(
            id=fid,
            name=name,
            qualname=qualname,
            file=self.file,
            line=_line(node),
            end_line=node.end_point[0] + 1,
            params=params,
            returns=_annotation(node.child_by_field_name("return_type")),
            doc=_docstring(block),
            decorators=decorators,
            body=items,
            cls=cls_id,
            is_async=any(c.type == "async" for c in node.children),
            imports=body.imports,
            hints=body.hints,
        )
        return fid

    def klass(self, node: Node, scope: str) -> str:
        name = _text(node.child_by_field_name("name"))
        qualname = f"{scope}.{name}" if scope else name
        cid = f"{self.file}::{qualname}"
        supers = node.child_by_field_name("superclasses")
        bases = [_text(b) for b in _named(supers) if b.type != "keyword_argument"] if supers is not None else []
        block = node.child_by_field_name("body")
        cls = ClassDef(cid, name, qualname, self.file, _line(node), bases, {}, {}, _docstring(block))
        self.classes[cid] = cls
        if block is None:
            return cid
        for stmt in _named(block):
            if stmt.type in _DEFINITIONS:
                made = self.definition(stmt, scope=qualname, cls_id=cid)
                if made and made[0] == "function":
                    cls.methods[made[1].rsplit(".", 1)[-1]] = made[1]
            elif stmt.type == "expression_statement":
                for part in _named(stmt):
                    self._class_field(cls, part)
        return cid

    @staticmethod
    def _class_field(cls: ClassDef, node: Node) -> None:
        if node.type != "assignment":
            return
        left = node.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        hints = cls.fields.setdefault(_text(left), [])
        ann = _annotation(node.child_by_field_name("type"))
        if ann:
            hints.append(("ann", ann))
        right = node.child_by_field_name("right")
        if right is not None and right.type == "call":
            callee = _dotted(right.child_by_field_name("function"))
            if callee:
                hints.append(("call", callee))
        if not hints:
            del cls.fields[_text(left)]


# ── function bodies ────────────────────────────────────────────────────────


class _Body:
    """Turns the statements of one function (or of a module) into items."""

    def __init__(
        self,
        mod: _ModuleExtractor,
        qualname: str,
        params: list[Param],
        cls_id: str | None,
        self_name: str | None,
        module_level: bool = False,
    ):
        self.mod = mod
        self.qualname = qualname
        self.cls_id = cls_id
        self.self_name = self_name
        self.module_level = module_level
        self.temps = 0
        self.hints: dict[str, list[Hint]] = {}
        self.imports: list[Import] = []
        for p in params:
            if p.annotation:
                self.hint(p.name, ("ann", p.annotation))

    # bookkeeping

    def temp(self) -> str:
        self.temps += 1
        return f"${self.temps}"

    def hint(self, name: str, hint: Hint) -> None:
        known = self.hints.setdefault(name, [])
        if hint not in known:
            known.append(hint)
        if self.cls_id and self.self_name and name.startswith(self.self_name + "."):
            field_name = name[len(self.self_name) + 1 :]
            if "." not in field_name:
                fields = self.mod.classes[self.cls_id].fields.setdefault(field_name, [])
                if hint not in fields:
                    fields.append(hint)

    # statements

    def block(self, node: Node | None) -> list[Item]:
        if node is None:
            return []
        stmts = _named(node)
        if stmts and _docstring(node) is not None:
            stmts = stmts[1:]
        return self.statements(stmts)

    def statements(self, nodes: list[Node]) -> list[Item]:
        items: list[Item] = []
        for stmt in nodes:
            items.extend(self.statement(stmt))
        return items

    def statement(self, node: Node) -> list[Item]:
        kind = node.type
        handler = getattr(self, f"_st_{kind}", None)
        if handler is not None:
            return handler(node)
        if kind in _IMPORTS:
            if not self.module_level:
                self.imports.extend(_parse_imports(node))
            return []
        if kind in _DEFINITIONS:
            if not self.module_level:
                self.mod.definition(node, scope=f"{self.qualname}.<locals>")
            return []
        if kind in ("assert_statement", "delete_statement"):
            items: list[Item] = []
            for child in _named(node):
                items += self.expr(child)[0]
            return items
        return []

    def _st_expression_statement(self, node: Node) -> list[Item]:
        items: list[Item] = []
        for child in _named(node):
            if child.type == "assignment":
                items += self.assignment(child)
            elif child.type == "augmented_assignment":
                items += self.augmented(child)
            elif child.type == "yield":
                items += self.yield_(child)
            else:
                items += self.expr(child, defs=[])[0]
        return items

    def _st_if_statement(self, node: Node) -> list[Item]:
        if self.module_level and _is_main_guard(node):
            return self.block(node.child_by_field_name("consequence"))
        cond = node.child_by_field_name("condition")
        items, uses = self.expr(cond)
        then = self.block(node.child_by_field_name("consequence"))
        orelse = self._alternatives(node.children_by_field_name("alternative"))
        return items + [If(_short(_text(cond)), uses, then, orelse, _line(node), _col(node))]

    def _alternatives(self, alts: list[Node]) -> list[Item]:
        if not alts:
            return []
        first, rest = alts[0], alts[1:]
        if first.type == "else_clause":
            return self.block(first.child_by_field_name("body"))
        cond = first.child_by_field_name("condition")
        items, uses = self.expr(cond)
        then = self.block(first.child_by_field_name("consequence"))
        return items + [If(_short(_text(cond)), uses, then, self._alternatives(rest), _line(first), _col(first))]

    def _st_for_statement(self, node: Node) -> list[Item]:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        items, uses = self.expr(right)
        defs = self.targets(left)[0]
        body = self.block(node.child_by_field_name("body"))
        alt = node.child_by_field_name("alternative")
        orelse = self.block(alt.child_by_field_name("body")) if alt is not None else []
        header = _short(f"for {_text(left)} in {_text(right)}")
        return items + [Loop("for", header, defs, uses, body, orelse, _line(node), _col(node))]

    def _st_while_statement(self, node: Node) -> list[Item]:
        cond = node.child_by_field_name("condition")
        items, uses = self.expr(cond)
        body = items + self.block(node.child_by_field_name("body"))
        alt = node.child_by_field_name("alternative")
        orelse = self.block(alt.child_by_field_name("body")) if alt is not None else []
        return [Loop("while", _short(f"while {_text(cond)}"), [], uses, body, orelse, _line(node), _col(node))]

    def _st_try_statement(self, node: Node) -> list[Item]:
        body = self.block(node.child_by_field_name("body"))
        handlers: list[Handler] = []
        orelse: list[Item] = []
        final: list[Item] = []
        for child in node.named_children:
            if child.type in ("except_clause", "except_group_clause"):
                handlers.append(self._handler(child))
            elif child.type == "else_clause":
                orelse = self.block(child.child_by_field_name("body"))
            elif child.type == "finally_clause":
                final = self.block(next((c for c in child.named_children if c.type == "block"), None))
        return [Try(body, handlers, orelse, final, _line(node), _col(node))]

    def _handler(self, node: Node) -> Handler:
        value = node.child_by_field_name("value")
        if value is None:
            value = next((c for c in _named(node) if c.type != "block"), None)
        types, name = "", None
        if value is not None:
            if value.type == "as_pattern":
                exc = _named(value)[0]
                types = _exception_types(exc)
                alias = value.child_by_field_name("alias")
                name = _text(alias) or None
            else:
                types = _exception_types(value)
        block = next((c for c in node.named_children if c.type == "block"), None)
        return Handler(types, name, self.block(block), _line(node), _col(node))

    def _st_with_statement(self, node: Node) -> list[Item]:
        items: list[Item] = []
        clause = next((c for c in node.named_children if c.type == "with_clause"), None)
        for with_item in _named(clause) if clause is not None else []:
            value = with_item.child_by_field_name("value")
            if value is None:
                continue
            if value.type == "as_pattern":
                expr_node = _named(value)[0]
                alias = value.child_by_field_name("alias")
                target = _named(alias)[0] if alias is not None and alias.type == "as_pattern_target" and _named(alias) else alias
                defs = self.targets(target)[0] if target is not None else []
                if self._is_call(expr_node):
                    items += self.expr(expr_node, defs=defs)[0]
                else:
                    more, uses = self.expr(expr_node)
                    items += more + [Assign(defs, uses, _line(with_item))]
            else:
                items += self.expr(value, defs=[])[0]
        return items + self.block(node.child_by_field_name("body"))

    def _st_match_statement(self, node: Node) -> list[Item]:
        subjects = node.children_by_field_name("subject")
        items: list[Item] = []
        uses: list[str] = []
        for subject in subjects:
            more, used = self.expr(subject)
            items += more
            uses += used
        cases: list[Case] = []
        block = node.child_by_field_name("body")
        for clause in _named(block) if block is not None else []:
            if clause.type != "case_clause":
                continue
            pattern = ", ".join(_text(p) for p in clause.named_children if p.type == "case_pattern")
            guard_items: list[Item] = []
            guard = clause.child_by_field_name("guard")
            if guard is not None and _named(guard):
                cond = _named(guard)[0]
                pattern = f"{pattern} if {_text(cond)}"
                guard_items = self.expr(cond)[0]
            body = guard_items + self.block(clause.child_by_field_name("consequence"))
            cases.append(Case(_short(pattern), body, _line(clause), _col(clause)))
        subject_text = ", ".join(_text(s) for s in subjects)
        return items + [Match(_short(subject_text), _dedupe(uses), cases, _line(node), _col(node))]

    def _st_return_statement(self, node: Node) -> list[Item]:
        values = _named(node)
        if not values:
            return [Return("", [], _line(node), col=_col(node))]
        items, uses = self.expr(values[0])
        return items + [Return(_short(_text(values[0])), uses, _line(node), col=_col(node))]

    def _st_raise_statement(self, node: Node) -> list[Item]:
        cause = node.child_by_field_name("cause")
        values = [c for c in _named(node) if cause is None or c.id != cause.id]
        if not values:
            return [Raise("", [], _line(node), _col(node))]
        items, uses = self.expr(values[0])
        return items + [Raise(_short(_text(values[0])), uses, _line(node), _col(node))]

    def yield_(self, node: Node) -> list[Item]:
        values = _named(node)
        if not values:
            return [Return("", [], _line(node), kind="yield", col=_col(node))]
        items, uses = self.expr(values[0])
        return items + [Return(_short(_text(values[0])), uses, _line(node), kind="yield", col=_col(node))]

    # assignments

    def assignment(self, node: Node) -> list[Item]:
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        defs, target_uses, simple = self.targets(left)
        ann = _annotation(node.child_by_field_name("type"))
        if ann:
            for name in defs:
                self.hint(name, ("ann", ann))
        if right is None:
            return []
        if right.type == "assignment":  # a = b = value
            items = self.assignment(right)
            inner = self.targets(right.child_by_field_name("left"))[0]
            return items + [Assign(defs, _dedupe(inner + target_uses), _line(node))]
        if simple and self._is_call(right):
            return self.expr(right, defs=defs)[0]
        items, uses = self.expr(right)
        if right.type == "identifier":
            for hint in self.hints.get(_text(right), []):
                for name in defs:
                    self.hint(name, hint)
        return items + [Assign(defs, _dedupe(uses + target_uses), _line(node))]

    def augmented(self, node: Node) -> list[Item]:
        defs, target_uses, _ = self.targets(node.child_by_field_name("left"))
        items, uses = self.expr(node.child_by_field_name("right"))
        return items + [Assign(defs, _dedupe(defs + target_uses + uses), _line(node))]

    def targets(self, node: Node | None) -> tuple[list[str], list[str], bool]:
        """Names written by an assignment target, extra names it reads, and
        whether every target is a plain name or ``self.<field>``."""
        if node is None:
            return [], [], False
        kind = node.type
        if kind == "identifier":
            return [_text(node)], [], True
        if kind == "attribute":
            obj = node.child_by_field_name("object")
            attr = _text(node.child_by_field_name("attribute"))
            if obj is not None and obj.type == "identifier" and _text(obj) == self.self_name:
                return [f"{self.self_name}.{attr}"], [], True
            root = self._root_name(node)
            uses = self.expr(obj)[1] if obj is not None else []
            return ([root] if root else []), uses, False
        if kind == "subscript":
            value = node.child_by_field_name("value")
            root = self._root_name(value) if value is not None else None
            uses: list[str] = []
            for child in _named(node):
                uses += self.expr(child)[1]
            return ([root] if root else []), uses, False
        if kind in ("pattern_list", "tuple_pattern", "list_pattern", "tuple", "list", "expression_list", "parenthesized_expression"):
            defs: list[str] = []
            uses = []
            simple = True
            for child in _named(node):
                d, u, s = self.targets(child)
                defs += d
                uses += u
                simple = simple and s
            return defs, uses, simple
        if kind in ("list_splat_pattern", "list_splat"):
            return self.targets(_named(node)[0]) if _named(node) else ([], [], False)
        return [], [], False

    def _root_name(self, node: Node) -> str | None:
        while node is not None and node.type in ("attribute", "subscript"):
            node = node.child_by_field_name("object" if node.type == "attribute" else "value")
        if node is not None and node.type == "identifier":
            name = _text(node)
            return name
        return None

    @staticmethod
    def _is_call(node: Node) -> bool:
        if node.type == "await":
            inner = _named(node)
            return bool(inner) and inner[0].type == "call"
        return node.type == "call"

    # expressions

    def expr(self, node: Node | None, defs: list[str] | None = None) -> tuple[list[Item], list[str]]:
        """Items for the calls inside an expression (in evaluation order) and
        the variables the expression's value depends on."""
        if node is None:
            return [], []
        kind = node.type
        if kind == "call":
            return self.call(node, defs)
        if kind == "await":
            inner = _named(node)
            if inner and inner[0].type == "call":
                return self.call(inner[0], defs, awaited=True)
            return self.expr(inner[0]) if inner else ([], [])
        if kind == "identifier":
            return [], [_text(node)]
        if kind == "attribute":
            return self.attribute(node)
        if kind in _COMPREHENSIONS:
            return self.comprehension(node)
        if kind in ("lambda", "comment"):
            return [], []
        if kind == "named_expression":
            name = _text(node.child_by_field_name("name"))
            items, uses = self.expr(node.child_by_field_name("value"))
            return items + [Assign([name], uses, _line(node))], [name]
        if kind == "keyword_argument":
            return self.expr(node.child_by_field_name("value"))
        items: list[Item] = []
        uses: list[str] = []
        for child in _named(node):
            more, used = self.expr(child)
            items += more
            uses += used
        return items, _dedupe(uses)

    def attribute(self, node: Node) -> tuple[list[Item], list[str]]:
        chain: list[str] = []
        cur = node
        while cur is not None and cur.type == "attribute":
            chain.append(_text(cur.child_by_field_name("attribute")))
            cur = cur.child_by_field_name("object")
        if cur is None:
            return [], []
        if cur.type == "identifier":
            root = _text(cur)
            if root == self.self_name and chain:
                return [], [f"{root}.{chain[-1]}"]
            return [], [root]
        return self.expr(cur)

    def call(self, node: Node, defs: list[str] | None, awaited: bool = False) -> tuple[list[Item], list[str]]:
        fn = node.child_by_field_name("function")
        items: list[Item] = []
        receiver_uses: list[str] = []
        callee = ""
        if fn is not None and fn.type == "identifier":
            callee = _text(fn)
        elif fn is not None and fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            attr = _text(fn.child_by_field_name("attribute"))
            dotted = _dotted(obj) if obj is not None else None
            if dotted is not None:
                callee = f"{dotted}.{attr}"
                receiver_uses = self.expr(obj)[1]
            elif obj is not None:
                more, used = self.expr(obj)
                items += more
                receiver_uses = used
                if self._is_call(obj) and more and isinstance(more[-1], Call) and more[-1].defs:
                    callee = f"{more[-1].defs[0]}.{attr}"
        elif fn is not None:
            more, used = self.expr(fn)
            items += more
            receiver_uses = used

        args: list[Arg] = []
        arg_node = node.child_by_field_name("arguments")
        if arg_node is not None and arg_node.type == "generator_expression":
            more, used = self.comprehension(arg_node)
            items += more
            args.append(Arg(_short(_text(arg_node)[1:-1]), used))
        elif arg_node is not None:
            for a in _named(arg_node):
                if a.type == "keyword_argument":
                    value = a.child_by_field_name("value")
                    more, used = self.expr(value)
                    args.append(Arg(_short(_text(value)), used, keyword=_text(a.child_by_field_name("name"))))
                elif a.type in ("list_splat", "dictionary_splat"):
                    inner = _named(a)[0] if _named(a) else None
                    more, used = self.expr(inner)
                    args.append(Arg(_short(_text(inner)), used, star="*" if a.type == "list_splat" else "**"))
                else:
                    more, used = self.expr(a)
                    args.append(Arg(_short(_text(a)), used))
                items += more

        out = [self.temp()] if defs is None else list(defs)
        if callee:
            for name in out:
                self.hint(name, ("call", callee))
        point = node.start_point
        items.append(
            Call(
                callee=callee,
                args=args,
                receiver_uses=receiver_uses,
                defs=out,
                line=point[0] + 1,
                col=point[1],
                text=_short(_text(node)),
                awaited=awaited,
            )
        )
        return items, out

    def comprehension(self, node: Node) -> tuple[list[Item], list[str]]:
        before: list[Item] = []
        inside: list[Item] = []
        header: list[str] = []
        loop_defs: list[str] = []
        loop_uses: list[str] = []
        first_for = True
        for clause in node.named_children:
            if clause.type == "for_in_clause":
                left = clause.child_by_field_name("left")
                right = clause.child_by_field_name("right")
                more, used = self.expr(right)
                if first_for:
                    before += more
                    first_for = False
                else:
                    inside += more
                loop_uses += used
                loop_defs += self.targets(left)[0]
                header.append(f"for {_text(left)} in {_text(right)}")
            elif clause.type == "if_clause" and _named(clause):
                cond = _named(clause)[0]
                inside += self.expr(cond)[0]
                header.append(f"if {_text(cond)}")
        body = node.child_by_field_name("body")
        more, body_uses = self.expr(body)
        inside += more
        loop = Loop("comprehension", _short(" ".join(header)), _dedupe(loop_defs), _dedupe(loop_uses), inside, [], _line(node), _col(node))
        value_uses = [u for u in body_uses if u not in loop_defs] + loop_uses
        return before + [loop], _dedupe(value_uses)
