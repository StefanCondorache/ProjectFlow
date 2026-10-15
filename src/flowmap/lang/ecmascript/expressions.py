"""Ordered expression extraction and conditional/optional execution guards."""
from __future__ import annotations

from tree_sitter import Node
from flowmap.ir import Arg, Assign, Call, If, Item, Return, walk
from .syntax import (
    CLASSES, FUNCTIONS, col, dotted, line, literal_string, named, pattern_names,
    short, text, unique,
)


class Expressions:
    def refs(self, values: list[str], scope: str | None = None) -> list[str]:
        values = unique(values)
        self.module.references.append((values, scope or self.scope, self.fn.id))
        return values

    def temp(self) -> str:
        while True:
            self.temps += 1
            name = f'${self.temps}'
            if name not in self.module.identifiers and f'__t{self.temps}' not in self.module.identifiers:
                self.generated_temps.add(name)
                return name

    def src(self, node: Node | None) -> str:
        if node is None:
            return ''
        start, end = node.start_byte, node.end_byte
        edits = sorted(((a, b, v) for a, b, v in self.spans if start <= a and b <= end),
                       key=lambda v: (v[0], -v[1]))
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

    def expr(self, node: Node | None) -> tuple[list[Item], list[str]]:
        if node is None:
            return [], []
        if node.id in self.cached:
            return [], self.cached[node.id]
        kind = node.type
        if kind in FUNCTIONS:
            self.module.function(node, self)
            return [], []
        if kind in CLASSES:
            self.module.klass(node, self)
            return [], []
        if kind in {'number', 'string', 'true', 'false', 'null', 'undefined', 'regex', 'comment', 'type_annotation'}:
            return [], []
        if kind in {'identifier', 'shorthand_property_identifier', 'this', 'super'}:
            return [], self.refs([text(node)])
        guard = self.optional_guard(node)
        if guard is not None:
            return self.optional(node, guard)
        if kind in {'call_expression', 'new_expression'}:
            return self.call(node)
        if kind in {'assignment_expression', 'augmented_assignment_expression'}:
            return self.assignment(node)
        if kind == 'update_expression':
            target = node.child_by_field_name('argument')
            before, defs, used = self.targets(target)
            for name in pattern_names(target):
                self.module.writes.append((self.scope, name, node))
            self.module.write_targets(self.scope, target)
            return before + [Assign(defs, self.refs(defs + used), line(node), text(target), self.src(node))], defs
        if kind == 'ternary_expression':
            return self.conditional(node)
        if kind == 'binary_expression' and text(node.child_by_field_name('operator')) in {'&&', '||', '??'}:
            return self.short_circuit(node)
        if kind == 'await_expression':
            inner = next(iter(named(node)), None)
            items, used = self.expr(inner)
            for item in walk(items):
                if isinstance(item, Call) and inner is not None and self.module.meta['calls'][f'{self.fn.id}:{item.line}:{item.col}']['start_byte'] == inner.start_byte and self.module.meta['calls'][f'{self.fn.id}:{item.line}:{item.col}']['end_byte'] == inner.end_byte:
                    item.awaited = True
            if used and used[0] in self.generated_temps:
                self.replace(node, used[0])
            return items, used
        if kind == 'yield_expression':
            inner = next(iter(named(node)), None)
            items, used = self.expr(inner)
            return items + [Return(short(text(inner)), used, line(node), kind='yield', col=col(node), expr=self.src(inner))], used
        if kind == 'member_expression':
            obj = node.child_by_field_name('object')
            items, used = self.expr(obj)
            chain = dotted(node)
            if chain.startswith('this.'):
                used = self.refs(used + [chain])
            return items, used
        if kind == 'subscript_expression':
            items, used = self.expr(node.child_by_field_name('object'))
            more, index_used = self.expr(node.child_by_field_name('index'))
            return items + more, self.refs(used + index_used)
        if kind in {'jsx_element', 'jsx_self_closing_element', 'jsx_fragment'}:
            items, used = [], []
            for expression in self.jsx_expressions(node):
                more, refs = self.expr(expression)
                items += more
                used += refs
            return items, self.refs(used)
        if kind in {'jsx_text', 'jsx_opening_element', 'jsx_closing_element', 'jsx_attribute'}:
            return [], []
        if kind in {'as_expression', 'satisfies_expression', 'type_assertion', 'non_null_expression'}:
            expression = next((c for c in named(node) if c.type not in {'type_arguments', 'type_annotation'} and not c.type.endswith('_type') and c.type not in {'type_identifier', 'predefined_type'}), None)
            return self.expr(expression)
        if kind == 'pair':
            key = node.child_by_field_name('key')
            items, used = self.expr(key) if key is not None and key.type == 'computed_property_name' else ([], [])
            more, refs = self.expr(node.child_by_field_name('value'))
            return items + more, self.refs(used + refs)
        if kind == 'spread_element':
            return self.expr(next(iter(named(node)), None))
        if kind == 'ERROR':
            return [], []
        items, used = [], []
        for child in named(node):
            more, refs = self.expr(child)
            items += more
            used += refs
        return items, self.refs(used)

    def jsx_expressions(self, node: Node):
        for child in named(node):
            if child.type == 'jsx_expression':
                yield from named(child)
            elif child.type.startswith('jsx_'):
                yield from self.jsx_expressions(child)

    def call(self, node: Node) -> tuple[list[Item], list[str]]:
        fn = node.child_by_field_name('function') or node.child_by_field_name('constructor')
        callee = dotted(fn)
        items, receiver_uses, receiver, method = [], [], '', ''
        if fn is not None and fn.type == 'member_expression':
            obj, prop = fn.child_by_field_name('object'), fn.child_by_field_name('property')
            items, receiver_uses = ([], self.cached[fn.id]) if fn.id in self.cached else self.expr(obj)
            receiver = self.src(obj)
            method = text(prop)
            if not callee and len(receiver_uses) == 1 and receiver_uses[0] in self.generated_temps:
                callee = f'{receiver_uses[0]}.{method}'
        elif fn is not None and fn.type not in {'identifier', 'this', 'super'}:
            items, receiver_uses = self.expr(fn)
        args = []
        for arg in named(node.child_by_field_name('arguments')):
            star = '*' if arg.type == 'spread_element' else ''
            value = next(iter(named(arg)), None) if star else arg
            more, used = self.expr(value)
            items += more
            args.append(Arg(short(text(value)), used, star=star, expr=self.src(value)))
        out = self.refs([self.temp()])
        construct = node.type == 'new_expression'
        anchor = fn.child_by_field_name('property') if fn is not None and fn.type == 'member_expression' else node.child_by_field_name('arguments') if fn is not None and fn.type == 'call_expression' else node
        anchor = anchor or node
        call = Call(callee, args, receiver_uses, out, line(anchor), col(anchor), short(text(node)),
                    receiver=receiver, method=method)
        if callee in {'require', 'import'}:
            self.module.commonjs.append(('require-call' if callee == 'require' else 'dynamic-import-call', self.scope, node, None))
        if callee == 'import':
            self.module.diagnostic('dynamic-import', node, 'Dynamic imports are not static callable bindings.')
        self.module.meta['calls'][f'{self.fn.id}:{call.line}:{call.col}'] = {
            'scope': self.scope, 'start_byte': node.start_byte, 'end_byte': node.end_byte,
            'construct': construct, 'optional': bool(self.optional_disabled),
            'module': literal_string(named(node.child_by_field_name('arguments'))[0]) if callee in {'require', 'import'} and len(named(node.child_by_field_name('arguments'))) == 1 else None,
        }
        if callee:
            self.hints.setdefault(out[0], []).append(('construct' if construct else 'call', callee))
        self.replace(node, out[0])
        return items + [call], out

    def targets(self, node: Node | None) -> tuple[list[Item], list[str], list[str]]:
        if node is None:
            return [], [], []
        if node.type in {'identifier', 'object_pattern', 'array_pattern', 'rest_pattern'}:
            self.module.check_pattern(node)
            return [], self.refs(pattern_names(node)), []
        if node.type in {'member_expression', 'subscript_expression'}:
            obj = node.child_by_field_name('object')
            items, used = self.expr(obj)
            chain = dotted(node)
            if chain.startswith('this.'):
                definitions = [chain]
            else:
                root = dotted(obj).split('.')[0]
                definitions = [root] if root else []
            if node.type == 'subscript_expression':
                more, indexes = self.expr(node.child_by_field_name('index'))
                items += more
                used += indexes
            return items, self.refs(definitions), self.refs(used)
        self.module.diagnostic('unsupported-binding-pattern', node, 'Assignment target cannot be reliably decomposed.')
        return [], [], []

    def assignment(self, node: Node) -> tuple[list[Item], list[str]]:
        left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
        before, definitions, target_uses = self.targets(left)
        for name in pattern_names(left):
            self.module.writes.append((self.scope, name, node))
        chain = dotted(left)
        self.module.write_targets(self.scope, left)
        computed_export = left is not None and left.type == 'subscript_expression' and dotted(left.child_by_field_name('object')) in {'exports', 'module.exports', 'module'}
        if chain == 'module.exports' or chain.startswith('exports.') or chain.startswith('module.exports.') or computed_export:
            self.module.commonjs.append(('export', self.scope, node, None))
        operator = text(node.child_by_field_name('operator'))
        if node.type == 'augmented_assignment_expression' and operator in {'&&=', '||=', '??='}:
            items, used = self.expr(right)
            items.append(Assign(definitions, self.refs(used + target_uses), line(node), text(left), self.src(right)))
            test = f'{self.src(left)} == null' if operator == '??=' else self.src(left)
            then, other = ([], items) if operator == '||=' else (items, [])
            return before + [If(short(test), self.refs(definitions + target_uses), then, other, line(node), col(node), test)], definitions
        items, used = self.expr(right)
        if node.type == 'augmented_assignment_expression':
            used = self.refs(used + definitions)
        used = self.refs(used + target_uses)
        if len(definitions) == 1 and right is not None:
            hints = self.hints.get(used[0], []) if len(used) == 1 else []
            if hints:
                self.hints[definitions[0]] = list(hints)
        return before + items + [Assign(definitions, used, line(node), text(left), self.src(right))], definitions

    def conditional(self, node: Node) -> tuple[list[Item], list[str]]:
        condition = node.child_by_field_name('condition')
        then_node, other_node = node.child_by_field_name('consequence'), node.child_by_field_name('alternative')
        items, used = self.expr(condition)
        out = self.refs([self.temp()])
        then, then_uses = self.expr(then_node)
        other, other_uses = self.expr(other_node)
        then.append(Assign(out.copy(), then_uses, line(then_node), out[0], self.src(then_node)))
        other.append(Assign(out.copy(), other_uses, line(other_node), out[0], self.src(other_node)))
        items.append(If(short(text(condition)), used, then, other, line(node), col(node), self.src(condition)))
        self.replace(node, out[0])
        return items, out

    def short_circuit(self, node: Node) -> tuple[list[Item], list[str]]:
        left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
        operator = text(node.child_by_field_name('operator'))
        items, used = self.expr(left)
        out = self.refs([self.temp()])
        more, right_uses = self.expr(right)
        evaluated = more + [Assign(out.copy(), right_uses, line(right), out[0], self.src(right))]
        retained = [Assign(out.copy(), used, line(left), out[0], self.src(left))]
        test = f'{self.src(left)} == null' if operator == '??' else self.src(left)
        then, other = (retained, evaluated) if operator == '||' else (evaluated, retained)
        boundary = node.child_by_field_name('operator') or node
        items.append(If(short(test), used, then, other, line(boundary), col(boundary), test))
        self.replace(node, out[0])
        return items, out

    def optional_sites(self, node: Node):
        # Optional propagation follows the call/member chain, not argument
        # subexpressions; parenthesizing a chain stops its propagation.
        if node.type not in {'call_expression', 'member_expression', 'subscript_expression'}:
            return
        child = node.child_by_field_name('function') if node.type == 'call_expression' else node.child_by_field_name('object')
        if child is not None:
            yield from self.optional_sites(child)
        if any(c.type in {'optional_chain', '?.'} for c in node.children):
            yield node

    def optional_guard(self, node: Node) -> Node | None:
        return next((site for site in self.optional_sites(node) if site.id not in self.optional_disabled), None)

    def optional(self, node: Node, guard: Node) -> tuple[list[Item], list[str]]:
        guarded = guard.child_by_field_name('function') if guard.type == 'call_expression' else guard.child_by_field_name('object')
        prefix, used = self.expr(guarded)
        out = self.refs([self.temp()])
        old = set(self.optional_disabled)
        self.optional_disabled.add(guard.id)
        if guarded is not None:
            self.cached[guarded.id] = used
        then, result = self.expr(node)
        if guarded is not None:
            self.cached.pop(guarded.id, None)
        self.optional_disabled = old
        then.append(Assign(out.copy(), result, line(node), out[0], self.src(node)))
        other = [Assign(out.copy(), [], line(node), out[0], 'undefined')]
        test = f'{self.src(guarded)} != null'
        boundary = next(c for c in guard.children if c.type in {'optional_chain', '?.'})
        prefix.append(If(short(test), used, then, other, line(boundary), col(boundary), test))
        self.replace(node, out[0])
        return prefix, out

