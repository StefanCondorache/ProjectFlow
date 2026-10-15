"""ECMAScript statement/control extraction; expressions live in expressions.py."""
from __future__ import annotations

from tree_sitter import Node
from flowmap.ir import Assign, Break, Continue, Function, Handler, If, Item, Loop, Raise, Return, Try
from .syntax import (
    CLASSES, DECLARATIONS, FUNCTIONS, annotation, col, descendants, dotted,
    line, named, pattern_names, short, text, unparen,
)
from .expressions import Expressions
from .evidence import collect_opaque


class Body(Expressions):
    def __init__(self, module, fn: Function, scope: str):
        self.module, self.fn, self.scope = module, fn, scope
        self.temps = 0
        self.generated_temps = fn.generated_temps
        self.spans: list[tuple[int, int, str]] = []
        self.hints: dict[str, list[tuple[str, str]]] = {}
        self.cached: dict[int, list[str]] = {}
        self.optional_disabled: set[int] = set()

    def block(self, node: Node | None, new_scope: bool = True) -> list[Item]:
        if node is None:
            return []
        if node.type not in {'statement_block', 'program'}:
            previous = self.scope
            if new_scope:
                self.scope = self.module.new_scope('block', node, self.scope, self.fn.id)
            items = self.statement(node)
            self.scope = previous
            return items
        previous = self.scope
        if new_scope:
            self.scope = self.module.new_scope('block', node, self.scope, self.fn.id)
        items = [item for child in named(node) for item in self.statement(child)]
        self.scope = previous
        return items

    def statement(self, node: Node) -> list[Item]:
        kind = node.type
        if kind in DECLARATIONS:
            self.module.function(node, self, declaration=True)
            return []
        if kind in CLASSES:
            self.module.klass(node, self)
            return []
        if kind == 'import_statement':
            self.module.imports(node, self.scope)
            return []
        if kind == 'export_statement':
            return self.module.exports(node, self)
        if kind in {'function_signature', 'interface_declaration', 'type_alias_declaration', 'ambient_declaration', 'empty_statement', 'comment'}:
            return []
        if kind in {'switch_statement', 'with_statement', 'labeled_statement'}:
            self.module.diagnostic('unsupported-switch-fallthrough' if kind == 'switch_statement' else 'unsupported-control-flow', node,
                                   'This construct is opaque; its calls are not shown as unconditional execution.')
            collect_opaque(self.module, node, self)
            return []
        if kind == 'statement_block':
            return self.block(node)
        if kind in {'lexical_declaration', 'variable_declaration'}:
            declaration_kind = next((c.type for c in node.children if c.type in {'const', 'let', 'var'}), 'var')
            items = []
            for declarator in named(node):
                if declarator.type != 'variable_declarator':
                    continue
                pattern = declarator.child_by_field_name('name')
                value = declarator.child_by_field_name('value')
                self.module.check_pattern(pattern)
                ann = annotation(declarator.child_by_field_name('type'))
                bindings = [self.module.binding(self.scope, name, declaration_kind, declarator,
                                               annotation=ann, initializer=text(value),
                                               conditional=self.module.meta['scopes'][self.scope]['kind'] == 'block')
                            for name in pattern_names(pattern)]
                if ann:
                    for binding in bindings:
                        self.hints.setdefault(binding['name'], []).append(('ann', ann))
                if value is None:
                    # Uninitialized declarations still shadow outer symbols.
                    continue
                if value.type in FUNCTIONS:
                    fid = self.module.function(value, self, text(pattern) if pattern.type == 'identifier' else None)
                    if len(bindings) == 1 and pattern.type == 'identifier':
                        bindings[0]['target'] = fid
                    continue
                if value.type in CLASSES:
                    cid = self.module.klass(value, self, text(pattern) if pattern.type == 'identifier' else None)
                    if len(bindings) == 1:
                        bindings[0]['target'] = cid
                    continue
                if value.type == 'call_expression' and dotted(value.child_by_field_name('function')) == 'require':
                    self.module.commonjs.append(('require', self.scope, declarator, bindings))
                elif value.type == 'member_expression':
                    obj = value.child_by_field_name('object')
                    if obj is not None and obj.type == 'call_expression' and dotted(obj.child_by_field_name('function')) == 'require':
                        self.module.commonjs.append(('require-member', self.scope, declarator, bindings))
                definitions = self.refs(pattern_names(pattern))
                more, used = self.expr(value)
                items += more
                items.append(Assign(definitions, used, line(declarator), text(pattern), self.src(value)))
                if len(used) == 1 and used[0] in self.hints:
                    for definition in definitions:
                        self.hints.setdefault(definition, []).extend(self.hints[used[0]])
            return items
        if kind == 'expression_statement':
            return [item for child in named(node) for item in self.expr(child)[0]]
        if kind in {'return_statement', 'throw_statement'}:
            value = next(iter(named(node)), None)
            items, used = self.expr(value)
            if kind == 'return_statement':
                return items + [Return(short(text(value)), used, line(node), col=col(node), expr=self.src(value))]
            return items + [Raise(short(text(value)), used, line(node), col(node), self.src(value))]
        if kind == 'if_statement':
            condition = unparen(node.child_by_field_name('condition'))
            items, used = self.expr(condition)
            then = self.block(node.child_by_field_name('consequence'))
            alternative = node.child_by_field_name('alternative')
            if alternative is not None and alternative.type == 'else_clause':
                alternative = next(iter(named(alternative)), None)
            other = self.block(alternative)
            return items + [If(short(text(condition)), used, then, other, line(node), col(node), self.src(condition))]
        if kind in {'for_statement', 'for_in_statement', 'while_statement', 'do_statement'}:
            return self.loop(node)
        if kind == 'try_statement':
            contents = self.block(node.child_by_field_name('body'))
            handler = node.child_by_field_name('handler')
            handlers = []
            if handler is not None:
                previous = self.scope
                self.scope = self.module.new_scope('block', handler, self.scope, self.fn.id)
                param = handler.child_by_field_name('parameter')
                self.module.check_pattern(param)
                names = pattern_names(param)
                for name in names:
                    self.module.binding(self.scope, name, 'catch', param)
                handlers.append(Handler('', names[0] if len(names) == 1 else None,
                                        self.block(handler.child_by_field_name('body'), new_scope=False),
                                        line(handler), col(handler)))
                self.scope = previous
            finalizer = node.child_by_field_name('finalizer')
            final = self.block(finalizer.child_by_field_name('body')) if finalizer is not None else []
            return [Try(contents, handlers, [], final, line(node), col(node))]
        if kind == 'break_statement':
            return [Break(line(node), col(node))]
        if kind == 'continue_statement':
            return [Continue(line(node), col(node))]
        if kind == 'ERROR':
            return []
        self.module.diagnostic('unsupported-statement', node, f'{kind} is not expanded into execution flow.')
        collect_opaque(self.module, node, self)
        return []

    def loop(self, node: Node) -> list[Item]:
        previous = self.scope
        self.scope = self.module.new_scope('block', node, self.scope, self.fn.id)
        prefix: list[Item] = []
        kind = node.type
        contents = node.child_by_field_name('body')
        if kind == 'for_in_statement':
            left, right = node.child_by_field_name('left'), node.child_by_field_name('right')
            self.module.check_pattern(left)
            decl = next((c.type for c in node.children if c.type in {'const', 'let', 'var'}), None)
            names = pattern_names(left)
            if decl:
                for name in names:
                    self.module.binding(self.scope, name, decl, left, conditional=True)
            else:
                for name in names:
                    self.module.writes.append((self.scope, name, node))
                self.module.write_targets(self.scope, left)
            prefix, used = self.expr(right)
            body = self.block(contents)
            loop = Loop('for', short(text(node).split('{')[0]), self.refs(names), used, body, [],
                        line(node), col(node), target=text(left), iter=self.src(right))
        else:
            initializer = node.child_by_field_name('initializer')
            if initializer is not None:
                prefix = self.statement(initializer) if initializer.type in {'lexical_declaration', 'variable_declaration'} else self.expr(initializer)[0]
            condition = unparen(node.child_by_field_name('condition'))
            head, used = self.expr(condition)
            body = self.block(contents)
            increment = node.child_by_field_name('increment')
            if increment is not None:
                if contents is not None and any(n.type == 'continue_statement' for n in descendants(contents)):
                    self.module.diagnostic('unsupported-loop-continue', node, 'A classic for loop with continue has opaque increment ordering.')
                    collect_opaque(self.module, increment, self)
                    self.scope = previous
                    return prefix
                body += self.expr(increment)[0]
            loop = Loop('do while' if kind == 'do_statement' else 'while' if kind == 'while_statement' else 'for', short(text(node).split('{')[0]),
                        [], used, body + head if kind == 'do_statement' else head + body, [], line(node), col(node),
                        test=self.src(condition) or 'true', head=0 if kind == 'do_statement' else len(head))
        self.scope = previous
        return prefix + [loop]
