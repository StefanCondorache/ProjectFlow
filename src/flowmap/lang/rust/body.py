"""Rust expressions lowered to shared IR items.

Rust is expression-oriented: blocks, ``if`` and ``match`` produce values. Calls
are emitted in evaluation order with nested results in generated temporaries;
an ``if``/``match``/block whose value is used assigns a temporary in each
branch. Each call gets a ``metadata['rust']['calls']`` record with the static
evidence the linker needs (path, receiver kind and declared type).
"""
from __future__ import annotations

from tree_sitter import Node

from flowmap.ir import Arg, Assign, Break, Call, Case, Continue, Function, If, Item, Loop, Match, Param, Raise, Return

from .syntax import col, descendants, erase, line, named, path_text, short, text, unique, unparen

LITERALS = {'integer_literal', 'float_literal', 'string_literal', 'raw_string_literal', 'char_literal',
            'boolean_literal', 'unit_expression', 'line_comment', 'block_comment'}
CONSOLE_MACROS = {'println', 'print', 'eprintln', 'eprint', 'dbg'}
PANIC_MACROS = {'panic', 'unreachable', 'todo', 'unimplemented'}
ITEMS = {'function_item', 'struct_item', 'enum_item', 'union_item', 'trait_item', 'impl_item', 'mod_item',
         'use_declaration', 'const_item', 'static_item', 'type_item', 'macro_definition', 'extern_crate_declaration'}
TYPES = {'type_identifier', 'primitive_type', 'generic_type', 'scoped_type_identifier', 'reference_type',
         'array_type', 'tuple_type', 'type_arguments', 'lifetime', 'dynamic_type', 'abstract_type'}


class Body:
    def __init__(self, module, fn: Function, locals_: dict[str, str | None]):
        self.module, self.fn = module, fn
        self.meta = module.meta
        self.info = module.meta['functions'][fn.id]
        self.locals = locals_  # name -> erased declared type (None when unknown)
        self.local_calls: dict[str, str] = {}  # local -> call key whose result initialized it
        self.temps = 0
        self.spans: list[tuple[int, int, str]] = []
        self.temp_calls: dict[str, str] = {}

    # ── helpers ────────────────────────────────────────────────────────────

    def temp(self) -> str:
        while True:
            self.temps += 1
            name = f'${self.temps}'
            if f'__t{self.temps}' not in self.module.identifiers:
                self.fn.generated_temps.add(name)
                return name

    def src(self, node: Node | None) -> str:
        if node is None:
            return ''
        start, end = node.start_byte, node.end_byte
        edits = sorted(((a, b, v) for a, b, v in self.spans if start <= a and b <= end), key=lambda v: (v[0], -v[1]))
        result, pos = [], start
        for a, b, value in edits:
            if a < pos:
                continue
            result.extend([self.module.source[pos:a].decode('utf-8', 'replace'), value])
            pos = b
        result.append(self.module.source[pos:end].decode('utf-8', 'replace'))
        return ''.join(result)

    def replace(self, node: Node, name: str) -> None:
        self.spans.append((node.start_byte, node.end_byte, f'__t{name[1:]}'))

    def scoped(self, run):
        saved, saved_calls = dict(self.locals), dict(self.local_calls)
        try:
            return run()
        finally:
            self.locals.clear()
            self.locals.update(saved)
            self.local_calls.clear()
            self.local_calls.update(saved_calls)

    def bind(self, pattern: Node | None, type_text: str | None = None, call: str | None = None) -> list[str]:
        """Declare the variables a pattern binds; a plain identifier keeps the declared type."""
        if pattern is None:
            return []
        if pattern.type == 'identifier':
            names = [text(pattern)]
        elif pattern.type == 'mut_pattern':
            return self.bind(next(iter(named(pattern)), None), type_text, call)
        else:
            # Destructuring: bound names are identifiers that are not a path or variant name.
            names = [text(n) for n in descendants(pattern)
                     if n.type in {'identifier', 'shorthand_field_identifier'}
                     and n.parent is not None and n.parent.type != 'scoped_identifier'
                     and n.parent.child_by_field_name('type') != n and not text(n)[:1].isupper()]
            type_text, call = None, None
        for name in names:
            self.locals[name] = type_text
            if call:
                self.local_calls[name] = call
            else:
                self.local_calls.pop(name, None)
        return unique(names)

    def opaque(self, node: Node, code: str, message: str) -> list[Item]:
        self.module.diagnostic(code, node, message)
        return []

    # ── blocks and statements ─────────────────────────────────────────────

    def function_body(self, block: Node) -> list[Item]:
        items, used, tail = self.block(block, want=True)
        if tail is not None:
            items.append(Return(short(text(tail)), used, line(tail), col=col(tail), expr=self.src_value(tail, used)))
        return items

    def src_value(self, node: Node, used: list[str]) -> str:
        return self.src(node) if not (len(used) == 1 and used[0] in self.fn.generated_temps and node.type in {
            'if_expression', 'match_expression', 'block'}) else f'__t{used[0][1:]}'

    def block(self, node: Node | None, want: bool = False) -> tuple[list[Item], list[str], Node | None]:
        """Statements of a block; returns (items, uses of the tail value, tail node)."""
        if node is None:
            return [], [], None

        def run():
            children = named(node)
            tail = children[-1] if children and children[-1].type not in {
                'expression_statement', 'let_declaration', 'empty_statement'} | ITEMS else None
            items: list[Item] = []
            for child in children:
                if child is tail:
                    continue
                items += self.statement(child)
            if tail is None:
                return items, [], None
            more, used = self.expr(tail, want=want)
            return items + more, used, tail

        return self.scoped(run)

    def statement(self, node: Node) -> list[Item]:
        kind = node.type
        if kind == 'expression_statement':
            return [item for child in named(node) for item in self.expr(child, want=False)[0]]
        if kind == 'let_declaration':
            return self.let(node)
        if kind == 'function_item':
            fid = self.module.function(node, self.info['scope'], f'{self.fn.qualname}.', local_to=self.fn.id)
            self.info['nested'][self.module.module.functions[fid].name] = fid
            return []
        if kind in ITEMS:
            # Items and `use` inside a body are visible in that block only (falling back to the module).
            if not self.info.get('local_scope'):
                self.info['local_scope'] = f"{self.info['scope']}::<{self.fn.name}@{self.fn.line}>"
            self.module.item(node, self.info['local_scope'], f'{self.fn.qualname}.')
            return []
        if kind in {'empty_statement', 'attribute_item', 'inner_attribute_item'}:
            return []
        return self.expr(node, want=False)[0]

    def let(self, node: Node) -> list[Item]:
        pattern = node.child_by_field_name('pattern')
        value = node.child_by_field_name('value')
        declared = erase(node.child_by_field_name('type'))
        if value is None:
            self.bind(pattern, declared)
            return []
        items, used = self.expr(value)
        core = unparen(value)
        while core is not None and core.type in {'try_expression', 'await_expression'}:
            core = next(iter(named(core)), None)
        call = self.temp_calls.get(used[0]) if len(used) == 1 and core is not None and core.type == 'call_expression' else None
        inferred = declared or (erase(core.child_by_field_name('name')) if core is not None and core.type == 'struct_expression' else None)
        alternative = node.child_by_field_name('alternative')
        if alternative is not None:  # let PATTERN = value else { diverge };
            otherwise, _, _ = self.block(next(iter(named(alternative)), alternative))
            items.append(If(short(f'not let {text(pattern)}'), used, otherwise, [], line(node), col(node),
                            f'not ({text(pattern)})'))
        names = self.bind(pattern, inferred, call)
        return items + [Assign(names, used, line(node), text(pattern), self.src(value))]

    # ── expressions ────────────────────────────────────────────────────────

    def expr(self, node: Node | None, want: bool = True) -> tuple[list[Item], list[str]]:
        node = unparen(node)
        if node is None:
            return [], []
        kind = node.type
        if kind in LITERALS or kind in TYPES:
            return [], []
        if kind == 'identifier':
            return [], [text(node)]
        if kind == 'self':
            return [], ['self']
        if kind in {'scoped_identifier', 'generic_function'}:
            return [], []  # a path value: constant, enum variant, function item
        if kind == 'field_expression':
            value = node.child_by_field_name('value')
            if value is not None and value.type == 'self':
                return [], [f"self.{text(node.child_by_field_name('field'))}"]
            return self.expr(value)
        if kind == 'call_expression':
            return self.call(node)
        if kind == 'macro_invocation':
            return self.macro(node)
        if kind == 'binary_expression' and text(node.child_by_field_name('operator')) in {'&&', '||'}:
            return self.short_circuit(node)
        if kind == 'if_expression':
            return self.if_(node, want)
        if kind == 'match_expression':
            return self.match(node, want)
        if kind in {'block', 'unsafe_block', 'async_block', 'const_block'}:
            inner = node if kind == 'block' else next((c for c in named(node) if c.type == 'block'), node)
            items, used, tail = self.block(inner, want=want)
            if want and tail is not None and used:
                return items, used
            return items, []
        if kind in {'loop_expression', 'while_expression', 'for_expression'}:
            return self.loop(node), []
        if kind == 'return_expression':
            value = next(iter(named(node)), None)
            items, used = self.expr(value)
            return items + [Return(short(text(value)), used, line(node), col=col(node), expr=self.src(value))], []
        if kind in {'break_expression', 'continue_expression'}:
            if any(c.type == 'label' for c in named(node)):
                return self.opaque(node, 'unsupported-control-flow', 'Labeled jumps are not expanded into flow.'), []
            value = next(iter(named(node)), None)
            items = self.expr(value)[0] if value is not None else []
            return items + [Break(line(node), col(node)) if kind == 'break_expression' else Continue(line(node), col(node))], []
        if kind == 'try_expression':
            return self.expr(next(iter(named(node)), None))  # `?` passes the value through
        if kind == 'await_expression':
            inner = next(iter(named(node)), None)
            items, used = self.expr(inner)
            if unparen(inner) is not None and unparen(inner).type == 'call_expression':
                for item in reversed(items):
                    if isinstance(item, Call):
                        item.awaited = True
                        break
            return items, used
        if kind == 'closure_expression':
            self.closure(node)
            return [], []
        if kind in {'assignment_expression', 'compound_assignment_expr'}:
            return self.assignment(node)
        if kind == 'let_condition':
            items, used = self.expr(node.child_by_field_name('value'))
            self.bind(node.child_by_field_name('pattern'))
            return items, used
        if kind == 'struct_expression':
            items, used = [], []
            for child in named(node.child_by_field_name('body')):
                target = child.child_by_field_name('value') if child.type == 'field_initializer' else child
                more, refs = self.expr(target)
                items += more
                used += refs
            return items, unique(used)
        items, used = [], []
        for child in named(node):
            more, refs = self.expr(child)
            items += more
            used += refs
        return items, unique(used)

    def receiver(self, obj: Node | None) -> dict:
        obj = unparen(obj)
        record = {'receiver': 'expr', 'receiver_text': text(obj), 'receiver_type': None, 'receiver_call': None}
        if obj is None:
            return record
        if obj.type == 'self':
            record['receiver'] = 'self'
        elif obj.type == 'identifier' and text(obj) in self.locals:
            name = text(obj)
            record.update(receiver='name', receiver_type=self.locals[name], receiver_call=self.local_calls.get(name))
        elif obj.type == 'field_expression':
            chain, root = [], obj
            while root is not None and root.type == 'field_expression':
                chain.insert(0, text(root.child_by_field_name('field')))
                root = unparen(root.child_by_field_name('value'))
            if root is not None and (root.type == 'self' or (root.type == 'identifier' and text(root) in self.locals)):
                base = 'self' if root.type == 'self' else text(root)
                record.update(receiver='field', receiver_text='.'.join([base, *chain]),
                              receiver_type=None if root.type == 'self' else self.locals[base])
        elif obj.type in {'call_expression', 'macro_invocation'}:
            record['receiver'] = 'call'
        elif obj.type == 'identifier' and text(obj)[:1].isupper():
            record['receiver_type'] = text(obj)  # a unit struct value
        elif obj.type == 'struct_expression':
            record['receiver_type'] = erase(obj.child_by_field_name('name'))
        elif obj.type == 'reference_expression':
            inner = self.receiver(obj.child_by_field_name('value'))
            if inner['receiver'] in {'name', 'self', 'field'}:
                return inner
        return record

    def call(self, node: Node) -> tuple[list[Item], list[str]]:
        function = node.child_by_field_name('function')
        record: dict = {'receiver': 'none', 'receiver_text': '', 'receiver_type': None, 'receiver_call': None}
        items: list[Item] = []
        receiver_uses: list[str] = []
        receiver_src, method = '', ''
        target = function.child_by_field_name('function') if function is not None and function.type == 'generic_function' else function
        if target is not None and target.type == 'field_expression':
            obj = target.child_by_field_name('value')
            name = text(target.child_by_field_name('field'))
            record.update(self.receiver(obj))
            items, receiver_uses = self.expr(obj)
            if record['receiver'] == 'call' and len(receiver_uses) == 1:
                record['receiver_call'] = self.temp_calls.get(receiver_uses[0])
            receiver_src, method = self.src(obj), name
            root = text(obj) if unparen(obj) is not None and unparen(obj).type in {'identifier', 'self'} else ''
            if record['receiver'] == 'field':
                root = record['receiver_text']
            callee = f'{root}.{name}' if root else (f'{receiver_uses[0]}.{name}' if len(receiver_uses) == 1 and receiver_uses[0] in self.fn.generated_temps else '')
            kind, anchor, path = 'method', target.child_by_field_name('field'), ''
        else:
            path = path_text(target)
            name = path.rsplit('::', 1)[-1] if path else ''
            kind = 'path' if path else 'expr'
            if kind == 'path' and '::' not in path and path in self.locals:
                kind = 'local'  # calling a closure or function value
            if kind == 'expr':
                items, receiver_uses = self.expr(target)
            callee = path
            anchor = target.child_by_field_name('name') if target is not None and target.type == 'scoped_identifier' else target
        args = []
        for arg in named(node.child_by_field_name('arguments')):
            more, used = self.expr(arg)
            items += more
            args.append(Arg(short(text(arg)), used, expr=self.src(arg)))
        out = self.temp()
        anchor = anchor or node
        call = Call(callee, args, receiver_uses, [out], line(anchor), col(anchor), short(text(node)),
                    receiver=receiver_src, method=method)
        key = f'{self.fn.id}:{call.line}:{call.col}'
        record.update({'kind': kind, 'path': path, 'name': name, 'arity': len(args), 'start_byte': node.start_byte})
        self.meta['calls'][key] = record
        self.temp_calls[out] = key
        self.replace(node, out)
        return items + [call], [out]

    def macro(self, node: Node) -> tuple[list[Item], list[str]]:
        name = path_text(node.child_by_field_name('macro')) or text(node.child_by_field_name('macro'))
        bare = name.rsplit('::', 1)[-1]
        tokens = next((c for c in named(node) if c.type == 'token_tree'), None)
        # Token trees are not expanded: their calls are not shown, their locals are data.
        used = unique([text(n) for n in descendants(tokens) if n.type == 'identifier' and text(n) in self.locals]) if tokens is not None else []
        if bare in PANIC_MACROS:
            return [Raise(short(text(node)), used, line(node), col(node), self.src(node))], []
        out = self.temp()
        call = Call(f'{bare}!', [Arg(short(text(tokens)), used, expr=text(tokens))] if used else [], [], [out],
                    line(node), col(node), short(text(node)))
        call.kind, call.external, call.confidence = 'external', f'{bare}!', 'exact'
        call.io = ('console', 'out') if bare in CONSOLE_MACROS else None
        key = f'{self.fn.id}:{call.line}:{call.col}'
        self.meta['calls'][key] = {'kind': 'macro', 'path': name, 'name': bare, 'arity': 0, 'receiver': 'none',
                                   'receiver_text': '', 'receiver_type': None, 'receiver_call': None,
                                   'start_byte': node.start_byte}
        self.temp_calls[out] = key
        self.replace(node, out)
        return [call], [out]

    def short_circuit(self, node: Node) -> tuple[list[Item], list[str]]:
        left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
        operator = text(node.child_by_field_name('operator'))
        items, used = self.expr(left)
        out = self.temp()
        more, right_uses = self.expr(right)
        evaluated = more + [Assign([out], right_uses, line(right), out, self.src(right))]
        retained = [Assign([out], used, line(left), out, self.src(left))]
        then, other = (retained, evaluated) if operator == '||' else (evaluated, retained)
        boundary = node.child_by_field_name('operator') or node
        items.append(If(short(self.src(left)), used, then, other, line(boundary), col(boundary), self.src(left)))
        self.replace(node, out)
        return items, [out]

    def branch(self, node: Node | None, out: str | None) -> list[Item]:
        """A branch body (block, else-if, or expression), assigning its value to ``out``."""
        if node is None:
            return []
        if node.type == 'else_clause':
            node = next(iter(named(node)), None)
            if node is None:
                return []
        if node.type == 'if_expression':
            items, used = self.if_(node, out is not None)
            return items + ([Assign([out], used, line(node), out, self.src(node))] if out and used else [])
        if node.type == 'block':
            items, used, tail = self.block(node, want=out is not None)
            if out and tail is not None:
                items.append(Assign([out], used, line(tail), out, self.src(tail)))
            return items
        items, used = self.scoped(lambda: self.expr(node, want=out is not None))
        return items + ([Assign([out], used, line(node), out, self.src(node))] if out else [])

    def if_(self, node: Node, want: bool) -> tuple[list[Item], list[str]]:
        condition = node.child_by_field_name('condition')
        out = self.temp() if want else None

        def run():
            items, used = self.expr(condition)
            then = self.branch(node.child_by_field_name('consequence'), out)
            return items, used, then

        items, used, then = self.scoped(run)  # if-let bindings live in the consequence only
        other = self.branch(node.child_by_field_name('alternative'), out)
        items.append(If(short(text(condition)), used, then, other, line(node), col(node), self.src(condition)))
        if out:
            self.replace(node, out)
        return items, [out] if out else []

    def match(self, node: Node, want: bool) -> tuple[list[Item], list[str]]:
        subject = node.child_by_field_name('value')
        items, used = self.expr(subject)
        out = self.temp() if want else None
        cases = []
        for arm in named(node.child_by_field_name('body')):
            if arm.type != 'match_arm':
                continue
            pattern = arm.child_by_field_name('pattern')

            def run(arm=arm, pattern=pattern):
                inner = next(iter(named(pattern)), None) if pattern is not None else None
                self.bind(inner)
                guard = pattern.child_by_field_name('condition') if pattern is not None else None
                guard_items = self.expr(guard)[0] if guard is not None else []
                return guard_items + self.branch(arm.child_by_field_name('value'), out)

            cases.append(Case(' '.join(text(pattern).split()), self.scoped(run), line(arm), col(arm)))
        items.append(Match(short(text(subject)), used, cases, line(node), col(node), self.src(subject)))
        if out:
            self.replace(node, out)
        return items, [out] if out else []

    def loop(self, node: Node) -> list[Item]:
        if any(c.type == 'label' for c in named(node)):
            return self.opaque(node, 'unsupported-control-flow', 'Labeled loops are not expanded into flow.')
        return self.scoped(lambda: self.loop_(node))

    def loop_(self, node: Node) -> list[Item]:
        kind = node.type
        body = node.child_by_field_name('body')
        header = short(text(node).split('{')[0])
        if kind == 'for_expression':
            pattern, value = node.child_by_field_name('pattern'), node.child_by_field_name('value')
            prefix, used = self.expr(value)
            names = self.bind(pattern)
            contents = self.block(body)[0]
            return prefix + [Loop('for', header, names, used, contents, [], line(node), col(node),
                                  target=text(pattern), iter=self.src(value))]
        if kind == 'loop_expression':
            return [Loop('while', header or 'loop', [], [], self.block(body)[0], [], line(node), col(node), test='true')]
        condition = node.child_by_field_name('condition')
        head, used = self.expr(condition)
        contents = self.block(body)[0]
        return [Loop('while', header, [], used, head + contents, [], line(node), col(node),
                     test=self.src(condition) or 'true', head=len(head))]

    def assignment(self, node: Node) -> tuple[list[Item], list[str]]:
        left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
        target = unparen(left)
        if target is not None and target.type == 'field_expression' and unparen(target.child_by_field_name('value')) is not None \
                and unparen(target.child_by_field_name('value')).type == 'self':
            defs = [f"self.{text(target.child_by_field_name('field'))}"]
            before, target_uses = [], []
        else:
            root = target
            while root is not None and root.type in {'field_expression', 'index_expression', 'reference_expression', 'unary_expression'}:
                root = unparen(named(root)[0]) if named(root) else None
            defs = [text(root)] if root is not None and root.type in {'identifier', 'self'} else []
            before, target_uses = self.expr(target) if target is not None and target.type != 'identifier' else ([], [])
        items, used = self.expr(right)
        if node.type == 'compound_assignment_expr':
            used = used + defs
        return before + items + [Assign(defs, unique(used + target_uses), line(node), text(left), self.src(right))], []

    def closure(self, node: Node) -> str:
        fid = f'{self.fn.file}::{self.fn.qualname}.<closure@{line(node)}:{col(node)}>@{line(node)}:{col(node)}'
        params: list[Param] = []
        types: list[str | None] = []
        for param in named(node.child_by_field_name('parameters')):
            if param.type == 'parameter':
                pattern = param.child_by_field_name('pattern')
                params.append(Param(text(pattern), erase(param.child_by_field_name('type'))))
                types.append(params[-1].annotation)
            else:
                params.append(Param(text(param)))
                types.append(None)
        fn = Function(fid, '<closure>', f'{self.fn.qualname}.<closure@{line(node)}:{col(node)}>', self.fn.file,
                      line(node), node.end_point[0] + 1, params, None, None, [], [], language='rust')
        self.module.module.functions[fid] = fn
        locals_ = dict(self.locals)
        locals_.update({p.name: t for p, t in zip(params, types)})
        self.meta['functions'][fid] = {**self.info, 'kind': 'closure', 'has_self': False, 'param_types': types,
                                       'returns': None, 'locals': locals_, 'outer_fid': self.fn.id, 'nested': {},
                                       'local_to': self.fn.id, 'test': False}
        inner = Body(self.module, fn, locals_)
        inner.local_calls = dict(self.local_calls)
        body = node.child_by_field_name('body')
        if body is not None and body.type == 'block':
            fn.body = inner.function_body(body)
        elif body is not None:
            items, used = inner.expr(body)
            fn.body = items + [Return(short(text(body)), used, line(body), col=col(body), expr=inner.src(body))]
        return fid
