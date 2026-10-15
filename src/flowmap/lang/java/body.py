"""Java statements and expressions lowered to shared IR items.

Calls are emitted in evaluation order (receiver, then arguments left to
right); nested results flow through generated temporaries. Each call also gets
a ``metadata['java']['calls']`` record with the static evidence the linker
needs: receiver kind and declared type, arity and known argument types.
Constructs whose execution order the IR cannot represent soundly (labeled
jumps, switch fallthrough, classic for + continue) become diagnostics and are
left out instead of being shown as unconditional execution.
"""
from __future__ import annotations

from tree_sitter import Node

from flowmap.ir import Arg, Assign, Break, Call, Continue, Function, Handler, If, Item, Loop, Param, Raise, Return, Try

from .syntax import TYPES, col, erase, line, named, short, text, unique, unparen

LITERALS = {'decimal_integer_literal': 'int', 'hex_integer_literal': 'int', 'octal_integer_literal': 'int',
            'binary_integer_literal': 'int', 'decimal_floating_point_literal': 'double',
            'hex_floating_point_literal': 'double', 'string_literal': 'String', 'text_block': 'String',
            'character_literal': 'char', 'true': 'boolean', 'false': 'boolean', 'null_literal': 'null'}
JUMPS = {'break_statement', 'return_statement', 'throw_statement', 'yield_statement', 'continue_statement'}
# Nodes whose nested break/continue/yield belong to themselves, not an outer switch.
JUMP_SCOPES = {'for_statement', 'enhanced_for_statement', 'while_statement', 'do_statement',
               'switch_expression', 'lambda_expression', 'class_body'}


class Body:
    def __init__(self, module, fn: Function, cid: str | None, locals_: dict[str, str | None],
                 captured: set[str] | frozenset[str] = frozenset()):
        self.module, self.fn, self.cid = module, fn, cid
        self.meta = module.meta
        self.locals = locals_  # name -> erased declared type, None when ambiguous
        # Enclosing-method locals seen by a local/anonymous class; its own and
        # inherited fields shadow them, which only the linker can tell.
        self.captured = set(captured)
        self.temps = 0
        self.spans: list[tuple[int, int, str]] = []
        self.temp_calls: dict[str, str] = {}
        self.yield_target: str | None = None

    # ── helpers ────────────────────────────────────────────────────────────

    def temp(self) -> str:
        while True:
            self.temps += 1
            name = f'${self.temps}'
            if name not in self.module.identifiers and f'__t{self.temps}' not in self.module.identifiers:
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

    def declare(self, name: str, type_text: str | None) -> None:
        if not name:
            return
        if name in self.locals and self.locals[name] != type_text:
            self.locals[name] = None  # same name, different types in sibling blocks
        else:
            self.locals[name] = type_text

    def field_type(self, name: str) -> tuple[bool, str | None]:
        """Whether a lexically enclosing class declares the field, and its type
        when the current class itself declares it (an inherited field could
        otherwise shadow an outer one; the linker resolves those)."""
        cid = self.cid
        while cid is not None:
            fields = self.meta['types'][cid]['fields']
            if name in fields:
                return True, fields[name] if cid == self.cid else None
            cid = self.meta['types'][cid]['outer']
        return False, None

    def scoped(self, run):
        """Run with a nested local scope: declarations inside do not outlive it."""
        saved = dict(self.locals)
        try:
            return run()
        finally:
            self.locals.clear()
            self.locals.update(saved)

    def type_of(self, node: Node | None) -> str | None:
        node = unparen(node)
        if node is None:
            return None
        kind = node.type
        if kind in LITERALS:
            value = text(node)
            if kind.endswith('integer_literal') and value[-1:] in 'lL':
                return 'long'
            if kind.endswith('floating_point_literal') and value[-1:] in 'fF':
                return 'float'
            return LITERALS[kind]
        if kind == 'identifier':
            name = text(node)
            if name in self.locals:
                return None if name in self.captured else self.locals[name]
            return self.field_type(name)[1]
        if kind in {'object_creation_expression', 'cast_expression'}:
            return erase(node.child_by_field_name('type'))
        if kind == 'class_literal':
            return 'Class'
        if kind == 'field_access' and text(node.child_by_field_name('object')) == 'this':
            return self.field_type(text(node.child_by_field_name('field')))[1]
        return None

    def dotted(self, node: Node | None) -> str:
        node = unparen(node)
        if node is None:
            return ''
        if node.type in {'identifier', 'this', 'super'}:
            return text(node)
        if node.type == 'field_access':
            root = self.dotted(node.child_by_field_name('object'))
            return f"{root}.{text(node.child_by_field_name('field'))}" if root else ''
        return ''

    # ── statements ─────────────────────────────────────────────────────────

    def block(self, node: Node | None) -> list[Item]:
        if node is None:
            return []
        if node.type in {'block', 'constructor_body', 'switch_block_statement_group'}:
            return self.scoped(lambda: [item for child in named(node) for item in self.statement(child)])
        return self.scoped(lambda: self.statement(node))

    def statement(self, node: Node) -> list[Item]:
        kind = node.type
        if kind in {'block', 'constructor_body'}:
            return self.block(node)
        if kind == 'local_variable_declaration':
            return self.declaration(node)
        if kind == 'expression_statement':
            return [item for child in named(node) for item in self.expr(child)[0]]
        if kind == 'explicit_constructor_invocation':
            return self.call(node)[0]
        if kind == 'return_statement':
            value = next(iter(named(node)), None)
            items, used = self.expr(value)
            return items + [Return(short(text(value)), used, line(node), col=col(node), expr=self.src(value))]
        if kind == 'throw_statement':
            value = next(iter(named(node)), None)
            items, used = self.expr(value)
            return items + [Raise(short(text(value)), used, line(node), col(node), self.src(value))]
        if kind == 'yield_statement':
            value = next(iter(named(node)), None)
            items, used = self.expr(value)
            if self.yield_target is None:
                return items + [Return(short(text(value)), used, line(node), kind='yield', col=col(node), expr=self.src(value))]
            return items + [Assign([self.yield_target], used, line(node), self.yield_target, self.src(value))]
        if kind == 'if_statement':
            condition = unparen(node.child_by_field_name('condition'))
            items, used = self.expr(condition)
            then = self.block(node.child_by_field_name('consequence'))
            other = self.block(node.child_by_field_name('alternative'))
            return items + [If(short(text(condition)), used, then, other, line(node), col(node), self.src(condition))]
        if kind in {'for_statement', 'enhanced_for_statement', 'while_statement', 'do_statement'}:
            return self.loop(node)
        if kind in {'try_statement', 'try_with_resources_statement'}:
            return self.try_(node)
        if kind == 'switch_expression':
            return self.switch(node, False)[0]
        if kind == 'synchronized_statement':
            lock = next((c for c in named(node) if c.type == 'parenthesized_expression'), None)
            return self.expr(lock)[0] + self.block(node.child_by_field_name('body'))
        if kind in {'break_statement', 'continue_statement'}:
            if named(node):  # labeled jump
                return self.opaque(node, 'unsupported-control-flow', 'Labeled jumps are not expanded into flow.')
            return [Break(line(node), col(node))] if kind == 'break_statement' else [Continue(line(node), col(node))]
        if kind == 'labeled_statement':
            return self.opaque(node, 'unsupported-control-flow', 'Labeled statements are not expanded into flow.')
        if kind in TYPES:
            self.module.type_decl(node, self.cid, scope_fid=self.fn.id)
            return []
        if kind in {'assert_statement', 'local_class_declaration'} or kind == ';':
            return []
        if kind == 'ERROR':
            return []
        return self.opaque(node, 'unsupported-statement', f'{kind} is not expanded into execution flow.')

    def opaque(self, node: Node, code: str, message: str) -> list[Item]:
        self.module.diagnostic(code, node, message)
        return []

    def declaration(self, node: Node) -> list[Item]:
        declared = erase(node.child_by_field_name('type'))
        items: list[Item] = []
        for declarator in named(node):
            if declarator.type != 'variable_declarator':
                continue
            name = text(declarator.child_by_field_name('name'))
            value = declarator.child_by_field_name('value')
            dims = declarator.child_by_field_name('dimensions')
            type_text = None if declared == 'var' else (declared or '') + ('[]' * text(dims).count('[') if dims is not None else '')
            if declared == 'var':
                type_text = self.type_of(value) if value is not None and value.type in {'object_creation_expression', 'cast_expression'} | set(LITERALS) else None
            self.declare(name, type_text or None)
            if value is None:
                continue
            more, used = self.expr(value)
            items += more + [Assign([name], used, line(declarator), name, self.src(value))]
        return items

    def initializers(self, nodes: list[Node], scope: dict[str, str | None] | None = None) -> list[Item]:
        """Instance field initializers (as this.<field> assignments) and initializer blocks.

        They run inside constructors but cannot see constructor parameters:
        ``scope`` is the class-level view (captured enclosing locals only).
        """
        if scope is not None:
            saved = dict(self.locals)
            self.locals.clear()
            self.locals.update(scope)
            try:
                return self.initializers(nodes)
            finally:
                self.locals.clear()
                self.locals.update(saved)
        items: list[Item] = []
        for node in nodes:
            if node.type == 'block':
                items += self.block(node)
                continue
            for declarator in named(node):
                value = declarator.child_by_field_name('value') if declarator.type == 'variable_declarator' else None
                if value is None:
                    continue
                target = f"this.{text(declarator.child_by_field_name('name'))}"
                more, used = self.expr(value)
                items += more + [Assign([target], used, line(declarator), target, self.src(value))]
        return items

    def loop(self, node: Node) -> list[Item]:
        return self.scoped(lambda: self.loop_(node))

    def loop_(self, node: Node) -> list[Item]:
        kind = node.type
        body = node.child_by_field_name('body')
        if kind == 'enhanced_for_statement':
            name = text(node.child_by_field_name('name'))
            declared = erase(node.child_by_field_name('type'))
            self.declare(name, None if declared == 'var' else declared)
            value = node.child_by_field_name('value')
            prefix, used = self.expr(value)
            header = short(text(node)[:text(node).find(')') + 1])
            return prefix + [Loop('for', header, [name], used, self.block(body), [], line(node), col(node),
                                  target=name, iter=self.src(value))]
        condition = unparen(node.child_by_field_name('condition'))
        prefix: list[Item] = []
        if kind == 'for_statement':
            updates = [node.children[i] for i in range(node.child_count) if node.field_name_for_child(i) == 'update']
            if updates and body is not None and any(n.type == 'continue_statement' for n in self.own_jumps(body)):
                return self.opaque(node, 'unsupported-loop-continue', 'A classic for loop with continue has opaque update ordering.')
            for i in range(node.child_count):
                if node.field_name_for_child(i) == 'init':
                    init = node.children[i]
                    prefix += self.declaration(init) if init.type == 'local_variable_declaration' else self.expr(init)[0]
        else:
            updates = []
        head, used = self.expr(condition)
        contents = self.block(body)
        for update in updates:
            contents += self.expr(update)[0]
        header = short(text(node).split('{')[0])
        test = self.src(condition) or 'true'
        if kind == 'do_statement':
            return prefix + [Loop('do while', header, [], used, contents + head, [], line(node), col(node), test=test)]
        return prefix + [Loop('for' if kind == 'for_statement' else 'while', header, [], used, head + contents, [],
                              line(node), col(node), test=test, head=len(head))]

    def own_jumps(self, node: Node):
        """break/continue/yield statements that target this construct."""
        stack = list(named(node))
        while stack:
            current = stack.pop()
            if current.type in JUMPS:
                yield current
            if current.type not in JUMP_SCOPES:
                stack.extend(named(current))

    def try_(self, node: Node) -> list[Item]:
        return self.scoped(lambda: self.try__(node))

    def try__(self, node: Node) -> list[Item]:
        contents: list[Item] = []
        resources = node.child_by_field_name('resources')
        for resource in named(resources):
            if resource.type == 'resource' and resource.child_by_field_name('value') is not None:
                name = text(resource.child_by_field_name('name'))
                declared = erase(resource.child_by_field_name('type'))
                value = resource.child_by_field_name('value')
                self.declare(name, self.type_of(value) if declared == 'var' else declared)
                more, used = self.expr(value)
                contents += more + [Assign([name], used, line(resource), name, self.src(value))]
            else:
                contents += self.expr(resource)[0]
        contents += self.block(node.child_by_field_name('body'))
        handlers, final = [], []
        for child in named(node):
            if child.type == 'catch_clause':
                param = next((c for c in named(child) if c.type == 'catch_formal_parameter'), None)
                types = next((c for c in named(param) if c.type == 'catch_type'), None)
                type_names = [erase(t) or text(t) for t in named(types)]
                name = text(param.child_by_field_name('name')) if param is not None else None

                def handler(child=child, name=name, type_names=type_names):
                    if name:
                        self.declare(name, type_names[0] if len(type_names) == 1 else None)
                    return self.block(child.child_by_field_name('body'))

                handlers.append(Handler(' | '.join(type_names), name or None, self.scoped(handler), line(child), col(child)))
            elif child.type == 'finally_clause':
                final = self.block(next((c for c in named(child) if c.type == 'block'), None))
        return [Try(contents, handlers, [], final, line(node), col(node))]

    def switch(self, node: Node, want_value: bool) -> tuple[list[Item], list[str]]:
        subject = unparen(node.child_by_field_name('condition'))
        items, used = self.expr(subject)
        block = node.child_by_field_name('body')
        # (labels, contents, kind); `case 1: case 2: x();` stacks empty groups onto the next one.
        arms_nodes: list[tuple[list[Node], list[Node], str]] = []
        pending: list[Node] = []
        for arm in named(block):
            if arm.type not in {'switch_rule', 'switch_block_statement_group'}:
                continue
            labels = pending + [c for c in named(arm) if c.type == 'switch_label']
            contents = [c for c in named(arm) if c.type != 'switch_label']
            if arm.type == 'switch_block_statement_group' and not contents:
                pending = labels
                continue
            pending = []
            arms_nodes.append((labels, contents, arm.type))
        out = self.temp() if want_value else None
        # A switch-level break inside nested statements cannot be expressed
        # as IR (it would read as a loop break); groups must not fall through.
        for index, (labels, contents, arm_type) in enumerate(arms_nodes):
            jumps = [j for c in contents for j in ([c] if c.type in JUMPS else self.own_jumps(c))]
            trailing = contents[-1] if contents else None
            falls = arm_type == 'switch_block_statement_group' and index < len(arms_nodes) - 1 and (trailing is None or trailing.type not in JUMPS)
            nested_break = any(j.type == 'break_statement' and j is not trailing for j in jumps)
            if falls or nested_break or pending:
                self.module.diagnostic('unsupported-switch-fallthrough', node,
                                       'This switch is opaque; its calls are not shown as unconditional execution.')
                if out is not None:
                    self.replace(node, out)
                return items, [out] if out else []
        # A statement switch keeps the enclosing switch expression's yield target.
        previous = self.yield_target
        if want_value:
            self.yield_target = out
        arms: list[tuple[str | None, list[Item]]] = []
        for labels, contents, arm_type in arms_nodes:
            if contents and contents[-1].type == 'break_statement':
                contents = contents[:-1]
            body: list[Item] = []
            for content in contents:
                if arm_type == 'switch_rule' and content.type == 'expression_statement' and out is not None:
                    value = next(iter(named(content)), None)
                    more, refs = self.expr(value)
                    body += more + [Assign([out], refs, line(content), out, self.src(value))]
                else:
                    body += self.statement(content)
            values = [v for label in labels for v in named(label)]
            test = None if any(text(label).strip() == 'default' for label in labels) or not values else \
                ' || '.join(f'{self.src(subject)} == {self.src(v)}' for v in values)
            arms.append((test, body))
        self.yield_target = previous
        default = next((body for test, body in arms if test is None), [])
        chain: list[Item] = default
        for test, body in reversed([(t, b) for t, b in arms if t is not None]):
            chain = [If(short(test), used, body, chain, line(node), col(node), test)]
        if out is not None:
            self.replace(node, out)
        return items + chain, [out] if out else []

    # ── expressions ────────────────────────────────────────────────────────

    def expr(self, node: Node | None) -> tuple[list[Item], list[str]]:
        node = unparen(node)
        if node is None:
            return [], []
        kind = node.type
        if kind in LITERALS or kind in {'class_literal', 'line_comment', 'block_comment'}:
            return [], []
        if kind == 'identifier':
            return [], [text(node)]
        if kind in {'this', 'super'}:
            return [], ['this']
        if kind == 'field_access':
            obj = node.child_by_field_name('object')
            if obj is not None and obj.type == 'this':
                return [], [f"this.{text(node.child_by_field_name('field'))}"]
            return self.expr(obj)
        if kind in {'method_invocation', 'object_creation_expression'}:
            return self.call(node)
        if kind in {'assignment_expression', 'update_expression'}:
            return self.assignment(node)
        if kind == 'ternary_expression':
            return self.conditional(node)
        if kind == 'binary_expression' and text(node.child_by_field_name('operator')) in {'&&', '||'}:
            return self.short_circuit(node)
        if kind == 'instanceof_expression':
            name = node.child_by_field_name('name')
            if name is not None:
                self.declare(text(name), erase(node.child_by_field_name('right')))
            return self.expr(node.child_by_field_name('left'))
        if kind == 'cast_expression':
            return self.expr(node.child_by_field_name('value'))
        if kind == 'lambda_expression':
            self.lambda_(node)
            return [], []
        if kind == 'method_reference':
            first = next(iter(named(node)), None)
            return ([], [text(first)]) if first is not None and first.type == 'identifier' and text(first) in self.locals else ([], [])
        if kind == 'switch_expression':
            return self.switch(node, True)
        if kind in {'type_identifier', 'scoped_type_identifier', 'generic_type', 'array_type', 'type_arguments',
                    'dimensions', 'integral_type', 'floating_point_type', 'boolean_type'}:
            return [], []
        items, used = [], []
        for child in named(node):
            more, refs = self.expr(child)
            items += more
            used += refs
        return items, unique(used)

    def receiver(self, obj: Node | None) -> dict:
        obj = unparen(obj)
        if obj is None:
            return {'receiver': 'none', 'receiver_text': '', 'receiver_type': None}
        source = text(obj)
        if obj.type in {'this', 'super'}:
            return {'receiver': obj.type, 'receiver_text': source, 'receiver_type': None}
        if obj.type == 'identifier':
            if source in self.locals:
                kind = 'captured' if source in self.captured else 'name'
                return {'receiver': kind, 'receiver_text': source, 'receiver_type': self.locals[source]}
            found, type_text = self.field_type(source)
            if found or not source[:1].isupper():
                return {'receiver': 'field', 'receiver_text': source, 'receiver_type': type_text}
            return {'receiver': 'type', 'receiver_text': source, 'receiver_type': None}
        if obj.type == 'field_access':
            chain = self.dotted(obj)
            root = chain.split('.')[0]
            if chain.startswith('this.'):
                return {'receiver': 'field', 'receiver_text': chain[5:], 'receiver_type': self.type_of(obj)}
            if chain and root not in self.locals and not self.field_type(root)[0]:
                return {'receiver': 'type', 'receiver_text': chain, 'receiver_type': None}
        if obj.type in {'method_invocation', 'object_creation_expression'}:
            return {'receiver': 'call', 'receiver_text': source, 'receiver_type': None}
        return {'receiver': 'expr', 'receiver_text': source, 'receiver_type': self.type_of(obj)}

    def call(self, node: Node) -> tuple[list[Item], list[str]]:
        kind = node.type
        items: list[Item] = []
        receiver_uses: list[str] = []
        record: dict = {'receiver': 'none', 'receiver_text': '', 'receiver_type': None,
                        'receiver_call': None, 'type': None}
        receiver_src, method = '', ''
        if kind == 'method_invocation':
            obj = node.child_by_field_name('object')
            name = text(node.child_by_field_name('name'))
            record.update(self.receiver(obj))
            if obj is not None:
                items, receiver_uses = self.expr(obj)
                receiver_src, method = self.src(obj), name
                if record['receiver'] == 'call' and len(receiver_uses) == 1:
                    record['receiver_call'] = self.temp_calls.get(receiver_uses[0])
            chain = self.dotted(obj) if obj is not None else ''
            if obj is None:
                callee = name
            elif chain:
                callee = f'{chain}.{name}'
            elif len(receiver_uses) == 1 and receiver_uses[0] in self.fn.generated_temps:
                callee = f'{receiver_uses[0]}.{name}'
            else:
                callee = ''
            call_kind = 'method'
            anchor = node.child_by_field_name('name')
        elif kind == 'object_creation_expression':
            type_node = node.child_by_field_name('type')
            name = erase(type_node) or text(type_node)
            outer = node.child_by_field_name('object')  # outer.new Inner()
            if outer is not None:
                items, receiver_uses = self.expr(outer)
            callee, call_kind, anchor = name, 'new', type_node
            record['type'] = name
            body = next((c for c in named(node) if c.type == 'class_body'), None)
            if body is not None:
                record['anonymous'] = self.module.type_decl(
                    body, self.cid, scope_fid=self.fn.id, anonymous_name=f'<anonymous@{line(node)}:{col(node)}>', base=name)
        else:  # explicit_constructor_invocation
            target = node.child_by_field_name('constructor')
            name = text(target)
            callee, call_kind, anchor = name, 'super-ctor' if name == 'super' else 'this-ctor', node
            receiver_uses = ['this']
        args, arg_types, arg_calls = [], [], []
        for arg in named(node.child_by_field_name('arguments')):
            more, used = self.expr(arg)
            items += more
            args.append(Arg(short(text(arg)), used, expr=self.src(arg)))
            arg_types.append(self.type_of(arg))
            # A call result's type is known only after linking.
            is_call = unparen(arg) is not None and unparen(arg).type in {'method_invocation', 'object_creation_expression'}
            arg_calls.append(self.temp_calls.get(used[0]) if is_call and len(used) == 1 else None)
        out = self.temp()
        call = Call(callee, args, receiver_uses, [out], line(anchor), col(anchor), short(text(node)),
                    receiver=receiver_src, method=method)
        key = f'{self.fn.id}:{call.line}:{call.col}'
        record.update({'kind': call_kind, 'name': name, 'arity': len(args), 'arg_types': arg_types,
                       'arg_calls': arg_calls, 'start_byte': node.start_byte})
        self.meta['calls'][key] = record
        self.temp_calls[out] = key
        self.replace(node, out)
        return items + [call], [out]

    def targets(self, node: Node | None) -> tuple[list[Item], list[str], list[str]]:
        node = unparen(node)
        if node is None:
            return [], [], []
        if node.type == 'identifier':
            return [], [text(node)], []
        if node.type == 'field_access':
            obj = node.child_by_field_name('object')
            if obj is not None and obj.type == 'this':
                return [], [f"this.{text(node.child_by_field_name('field'))}"], []
            items, used = self.expr(obj)
            root = self.dotted(obj).split('.')[0]
            return items, [root] if root else [], used
        if node.type == 'array_access':
            items, used = self.expr(node.child_by_field_name('array'))
            more, index = self.expr(node.child_by_field_name('index'))
            root = self.dotted(node.child_by_field_name('array')).split('.')[0]
            return items + more, [root] if root else [], used + index
        return [], [], []

    def assignment(self, node: Node) -> tuple[list[Item], list[str]]:
        if node.type == 'update_expression':
            target = next(iter(named(node)), None)
            before, defs, used = self.targets(target)
            return before + [Assign(defs, unique(defs + used), line(node), text(target), self.src(node))], defs
        left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
        before, defs, target_uses = self.targets(left)
        items, used = self.expr(right)
        operator = text(node.child_by_field_name('operator'))
        if operator != '=':
            used = used + defs
        return before + items + [Assign(defs, unique(used + target_uses), line(node), text(left), self.src(right))], defs

    def conditional(self, node: Node) -> tuple[list[Item], list[str]]:
        condition = unparen(node.child_by_field_name('condition'))
        then_node, other_node = node.child_by_field_name('consequence'), node.child_by_field_name('alternative')
        items, used = self.expr(condition)
        out = self.temp()
        then, then_uses = self.expr(then_node)
        other, other_uses = self.expr(other_node)
        then.append(Assign([out], then_uses, line(then_node), out, self.src(then_node)))
        other.append(Assign([out], other_uses, line(other_node), out, self.src(other_node)))
        items.append(If(short(text(condition)), used, then, other, line(node), col(node), self.src(condition)))
        self.replace(node, out)
        return items, [out]

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

    def lambda_(self, node: Node) -> str:
        fid = f'{self.fn.file}::{self.fn.qualname}.<lambda@{line(node)}:{col(node)}>@{line(node)}:{col(node)}'
        params_node = node.child_by_field_name('parameters')
        params: list[Param] = []
        types: list[str | None] = []
        if params_node is not None and params_node.type == 'identifier':
            params, types = [Param(text(params_node))], [None]
        elif params_node is not None and params_node.type == 'inferred_parameters':
            params = [Param(text(p)) for p in named(params_node)]
            types = [None] * len(params)
        elif params_node is not None:
            params, typed, _ = self.module.params(params_node)
            types = list(typed)
        fn = Function(fid, '<lambda>', f'{self.fn.qualname}.<lambda@{line(node)}:{col(node)}>', self.fn.file,
                      line(node), node.end_point[0] + 1, params, None, None, [], [], cls=None,
                      language='java', receiver='this')
        self.module.module.functions[fid] = fn
        locals_ = dict(self.locals)
        locals_.update({p.name: t for p, t in zip(params, types)})
        self.meta['functions'][fid] = {'cls': self.cid, 'kind': 'lambda', 'modifiers': [], 'static': False,
                                       'abstract': False, 'param_types': types, 'varargs': False,
                                       'returns': None, 'locals': locals_, 'outer_fid': self.fn.id}
        inner = Body(self.module, fn, self.cid, locals_, self.captured - {p.name for p in params})
        body = node.child_by_field_name('body')
        if body is not None and body.type == 'block':
            fn.body = inner.block(body)
        else:
            items, used = inner.expr(body)
            fn.body = items + [Return(short(text(body)), used, line(body), col=col(body), expr=inner.src(body))] if body is not None else []
        return fid
