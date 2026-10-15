"""JavaScript/TypeScript syntax -> shared static-flow IR and lexical evidence.

No source is executed. Lexical/module resolution belongs to the linker; this
module records declarations, scopes, exports and call sites as serializable
versioned metadata, including bindings which intentionally have no target.
"""
from __future__ import annotations

import re

from tree_sitter import Node

from flowmap.ir import (
    Assign, ClassDef, Function, If, Import, Item, Module, Param, Return,
)
from .syntax import (
    CLASSES, DECLARATIONS, FUNCTIONS, annotation, col, descendants, dotted,
    line, literal_string, member_name, member_targets, named, parser_for,
    pattern_names, short, text,
)

from .body import Body as _Body
from .commonjs import export_event
from .evidence import collect_opaque


def extract_module(relpath: str, source: bytes) -> Module:
    parser, language = parser_for(relpath)
    return _Extractor(relpath, source, parser.parse(source).root_node, language).run()


class _Extractor:
    def __init__(self, file: str, source: bytes, root: Node, language: str):
        self.file, self.source, self.root, self.language = file, source, root, language
        self.meta = {'version': 1, 'scopes': {}, 'functions': {}, 'bindings': [],
                     'exports': [], 'calls': {}, 'unbound_writes': [], 'member_writes': [], 'class_members': {}, 'dependencies': [], 'commonjs_events': []}
        self.module = Module(file, language, None, [], {}, {}, False,
                             metadata={'ecmascript': self.meta})
        self.identifiers = {text(n) for n in descendants(root) if n.type in {'identifier', 'property_identifier', 'private_property_identifier', 'shorthand_property_identifier', 'shorthand_property_identifier_pattern'}}
        self.scope_index = 0
        self.function_nodes: dict[int, str] = {}
        self.class_nodes: dict[int, str] = {}
        self.references: list[tuple[list[str], str, str]] = []
        self.writes: list[tuple[str, str, Node]] = []
        self.commonjs: list[tuple[str, str, Node, list[dict] | None]] = []
        self.scope = self.new_scope('module', root, None, None)

    def new_scope(self, kind: str, node: Node, parent: str | None, owner: str | None) -> str:
        sid = f's{self.scope_index}'
        self.scope_index += 1
        self.meta['scopes'][sid] = {'parent': parent, 'kind': kind,
                                   'start_byte': node.start_byte, 'end_byte': node.end_byte,
                                   'owner': owner}
        return sid

    def diagnostic(self, code: str, node: Node, message: str) -> None:
        self.module.diagnostics.append({'code': code, 'message': message,
                                        'line': line(node), 'col': col(node),
                                        'end_line': node.end_point[0] + 1,
                                        'end_col': node.end_point[1], 'severity': 'warning'})

    def document(self, node: Node) -> str | None:
        previous = node.prev_named_sibling
        if previous is None and node.parent is not None and node.parent.type == 'export_statement':
            previous = node.parent.prev_named_sibling
        if previous is None or previous.type != 'comment' or not text(previous).startswith('/**'):
            return None
        end = node.parent.start_byte if node.parent is not None and node.parent.type == 'export_statement' else node.start_byte
        if self.source[previous.end_byte:end].strip():
            return None
        raw = text(previous)[3:-2]
        value = '\n'.join(re.sub(r'^\s*\* ?', '', part).strip() for part in raw.splitlines()).strip()
        return value or None

    def check_pattern(self, node: Node | None) -> None:
        if node is not None and node.type in {'object_pattern', 'array_pattern'} and any(n.type in {'assignment_pattern', 'object_assignment_pattern', 'computed_property_name'} for n in descendants(node)):
            self.diagnostic('unsupported-binding-pattern', node, 'Destructuring defaults/computed keys are not expanded; their conditional calls are omitted.')

    def binding(self, scope: str, name: str, kind: str, node: Node, **extras) -> dict:
        if kind == 'var':
            while self.meta['scopes'][scope]['kind'] not in {'module', 'function'}:
                scope = self.meta['scopes'][scope]['parent']
        record = {'scope': scope, 'name': name, 'kind': kind, 'target': None,
                  'module': None, 'imported': None, 'type_only': False,
                  'start_byte': node.start_byte, 'initialized_byte': node.end_byte,
                  'hoisted': kind in {'function', 'var', 'import', 'parameter'},
                  'mutable': kind in {'let', 'var', 'parameter', 'catch'}, 'writes': []}
        record.update(extras)
        self.meta['bindings'].append(record)
        return record

    def lookup(self, scope: str, name: str) -> list[dict]:
        while scope is not None:
            found = [b for b in self.meta['bindings'] if b['scope'] == scope and b['name'] == name]
            if found:
                return found
            scope = self.meta['scopes'][scope]['parent']
        return []

    def write_targets(self, scope: str, target: Node | None) -> None:
        """Record each member a target assigns: a write to its root binding
        plus the property name, which the linker treats as possibly replaced."""
        for node in member_targets(target):
            chain = dotted(node.child_by_field_name('object'))
            root = chain.split('.')[0]
            if root:
                self.writes.append((scope, root, node))
            self.meta['member_writes'].append({'scope': scope, 'start_byte': node.start_byte,
                                               'object': chain, 'member': member_name(node)})

    def export(self, name: str, scope: str, **extras) -> None:
        record = {'name': name, 'scope': scope, 'local': None, 'target': None,
                  'module': None, 'imported': None, 'type_only': False}
        record.update(extras)
        self.meta['exports'].append(record)

    def run(self) -> Module:
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node.type == 'ERROR' or node.is_missing:
                self.diagnostic('parse-error', node, 'Parser recovery leaves this source only partially analyzed.')
            stack.extend(reversed(node.children))
        fid = f'{self.file}::<main>'
        main = Function(fid, '<main>', '<main>', self.file, 1, self.root.end_point[0] + 1,
                        [], None, None, [], [], language=self.language)
        self.module.functions[fid] = main
        self.meta['functions'][fid] = {'scope': self.scope, 'declaration_scope': self.scope,
                                      'static': False, 'constructor': False, 'this_scope': None, 'parameter_scope': self.scope,
                                      'start_byte': 0, 'end_byte': self.root.end_byte}
        body = _Body(self, main, self.scope)
        main.body = body.block(self.root, new_scope=False)
        main.hints = body.hints
        self.module.globals = {name: hints for name, hints in body.hints.items() if name not in main.generated_temps}
        self.module.constants = {i.target: i.value for i in main.body if isinstance(i, Assign)
                                 and len(i.defs) == 1 and i.target == i.defs[0] and i.value}
        for scope, name, node in self.writes:
            bindings = self.lookup(scope, name)
            evidence = {'scope': scope, 'start_byte': node.start_byte}
            for binding in bindings:
                binding['writes'].append(evidence)
            if not bindings:
                if name == 'this':
                    # The replaced member, or None when it cannot be named.
                    # this.a.b = x can only replace this.a.
                    parts = dotted(node.child_by_field_name('object')).split('.')
                    evidence['member'] = member_name(node) if len(parts) == 1 else parts[1]
                self.meta['unbound_writes'].append(dict(evidence, name=name))
        self.finalize_commonjs()
        self.rename_shadowed_references()
        return self.module

    def rename_shadowed_references(self) -> None:
        # Distinct block bindings must not kill or inherit a parameter's trail.
        # Keep unshadowed variable names readable and preserve source expressions.
        for refs, scope, _owner in self.references:
            for i, value in enumerate(refs):
                root, separator, suffix = value.partition('.')
                found = self.lookup(scope, root)
                if len(found) != 1:
                    continue
                binding = found[0]
                parent = self.meta['scopes'][binding['scope']]['parent']
                ancestor = self.lookup(parent, root) if parent is not None else []
                if ancestor and self.meta['scopes'][binding['scope']]['kind'] == 'block':
                    refs[i] = f'{root}@{binding["start_byte"]}' + (separator + suffix if separator else '')

    def function(self, node: Node, body: _Body, name: str | None = None,
                 cls: str | None = None, declaration: bool = False,
                 static: bool = False) -> str:
        if node.id in self.function_nodes:
            return self.function_nodes[node.id]
        original = text(node.child_by_field_name('name'))
        name = name or original or f'<callback@{line(node)}:{col(node)}>'
        parent_name = body.fn.qualname if body.fn.name != '<main>' else ''
        if cls:
            qualname = f'{self.module.classes[cls].qualname}.{name}'
        elif parent_name:
            qualname = f'{parent_name}.<locals>.{name}'
        else:
            qualname = name
        fid = f'{self.file}::{qualname}@{line(node)}:{col(node)}'
        self.function_nodes[node.id] = fid
        arrow = node.type == 'arrow_function'
        receiver = 'this' if cls else body.fn.receiver if arrow else None
        constructor = cls is not None and name == 'constructor' and not static
        contents = node.child_by_field_name('body')
        param_node = node.child_by_field_name('parameters') or node.child_by_field_name('parameter')
        parameter_nodes = named(param_node) if param_node is not None and param_node.type == 'formal_parameters' else [param_node] if param_node is not None else []
        has_expressions = any(
            p.child_by_field_name('value') is not None or
            any(n.type in {'assignment_pattern', 'object_assignment_pattern', 'computed_property_name'}
                for n in descendants(p))
            for p in parameter_nodes
        )
        parameter_scope = self.new_scope('function', node, body.scope, fid)
        sid = self.new_scope('function', contents or node, parameter_scope, fid) if has_expressions else parameter_scope
        params: list[Param] = []
        initialization: list[tuple[Node, Node | None, str, list[str], bool]] = []
        for param in parameter_nodes:
            pattern = param.child_by_field_name('pattern') if param.type in {'required_parameter', 'optional_parameter'} else param
            if param.type == 'assignment_pattern':
                pattern = param.child_by_field_name('left')
            if pattern is None:
                continue
            if pattern.type == 'this':
                # TypeScript's this annotation does not occupy an actual argument.
                continue
            self.check_pattern(pattern)
            names = pattern_names(pattern)
            param_name = names[0] if len(names) == 1 and pattern.type in {'identifier', 'rest_pattern'} else text(pattern)
            kind = 'vararg' if pattern.type == 'rest_pattern' else 'normal'
            value = None
            if param.type in {'required_parameter', 'optional_parameter', 'assignment_pattern'}:
                value = param.child_by_field_name('value') or param.child_by_field_name('right')
            ann = annotation(param.child_by_field_name('type'))
            params.append(Param(param_name, ann, kind, text(value) or None))
            for pname in names:
                self.binding(parameter_scope, pname, 'parameter', pattern, annotation=ann,
                             hoisted=not has_expressions,
                             initialized_byte=(value or pattern).end_byte)
            initialization.append((pattern, value, param_name, names,
                                   pattern.type not in {'identifier', 'rest_pattern'}))
        fn = Function(fid, name, qualname, self.file, line(node), node.end_point[0] + 1,
                      params, annotation(node.child_by_field_name('return_type')),
                      self.document(node), [], [], cls=cls,
                      is_async=any(c.type == 'async' for c in node.children),
                      language=self.language, receiver=receiver, is_constructor=constructor)
        self.module.functions[fid] = fn
        self.meta['functions'][fid] = {'scope': sid, 'parameter_scope': parameter_scope, 'declaration_scope': body.scope,
                                      'static': static, 'constructor': constructor,
                                      'this_scope': sid if cls else self.meta['functions'][body.fn.id]['this_scope'] if arrow else None,
                                      'start_byte': node.start_byte, 'end_byte': node.end_byte}
        if declaration:
            self.binding(body.scope, original or name, 'function', node, target=fid)
        elif original and node.type in {'function_expression', 'generator_function'}:
            self.binding(parameter_scope, original, 'function', node, target=fid)
        fb = _Body(self, fn, parameter_scope)
        prefix: list[Item] = []
        # Process each argument slot before the next slot's default evaluates.
        for pattern, value, param_name, names, destructured in initialization:
            if value is not None:
                items, used = fb.expr(value)
                items.append(Assign(fb.refs([param_name]), used, line(pattern), text(pattern), fb.src(value)))
                prefix.append(If(f'{param_name} === undefined', fb.refs([param_name]), items, [], line(pattern), col(pattern), f'{param_name} === undefined'))
            if destructured and names:
                prefix.append(Assign(fb.refs(names), fb.refs([param_name]), line(pattern), text(pattern), param_name))
        fb.scope = sid
        if contents is not None and contents.type == 'statement_block':
            fn.body = prefix + fb.block(contents, new_scope=False)
        else:
            items, used = fb.expr(contents)
            fn.body = prefix + items + [Return(short(text(contents)), used, line(contents or node), col=col(contents or node), expr=fb.src(contents))]
        fn.hints = fb.hints
        for param in params:
            if param.annotation:
                fn.hints.setdefault(param.name, []).append(('ann', param.annotation))
        return fid

    def klass(self, node: Node, body: _Body, name: str | None = None) -> str:
        if node.id in self.class_nodes:
            return self.class_nodes[node.id]
        original = text(node.child_by_field_name('name'))
        name = name or original or f'<class@{line(node)}:{col(node)}>'
        parent = body.fn.qualname if body.fn.name != '<main>' else ''
        qualname = f'{parent}.<locals>.{name}' if parent else name
        cid = f'{self.file}::{qualname}@{line(node)}:{col(node)}'
        bases = []
        heritage = next((c for c in named(node) if c.type == 'class_heritage'), None)
        if heritage is not None:
            for child in named(heritage):
                value = child.child_by_field_name('value')
                if value is not None:
                    bases.append(text(value))
                    if not dotted(value):
                        self.diagnostic('unsupported-class-heritage', value, 'Executable/computed superclass expressions are opaque; their evaluation is omitted from flow.')
                        collect_opaque(self, value, body)
                elif child.type not in {'implements_clause', 'extends_clause'}:
                    bases.append(text(child))
                    if not dotted(child):
                        self.diagnostic('unsupported-class-heritage', child, 'Executable/computed superclass expressions are opaque; their evaluation is omitted from flow.')
                        collect_opaque(self, child, body)
        cls = ClassDef(cid, name, qualname, self.file, line(node), bases, {}, {}, self.document(node))
        self.module.classes[cid] = cls
        self.class_nodes[node.id] = cid
        if node.type == 'class_declaration':
            self.binding(body.scope, original or name, 'class', node, target=cid)
        sid = self.new_scope('class', node, body.scope, cid)
        if original and node.type != 'class_declaration':
            self.binding(sid, original, 'class', node, target=cid)
        cb = _Body(self, body.fn, sid)
        for decorator in (c for c in named(node) if c.type == 'decorator'):
            self.diagnostic('unsupported-decorator', decorator, 'Decorator execution is not expanded into flow.')
        ambiguous: set[str] = set()
        # Every own member name (methods, fields with or without a value) and
        # whether any name is computed: a subclass may override through either.
        own = self.meta['class_members'][cid] = {'names': [], 'computed': False}
        for child in named(node.child_by_field_name('body')):
            if child.type == 'decorator':
                self.diagnostic('unsupported-decorator', child, 'Decorator execution is not expanded into flow.')
            elif child.type == 'method_definition':
                method = child.child_by_field_name('name')
                if method is None or method.type not in {'property_identifier', 'private_property_identifier'}:
                    self.diagnostic('dynamic-dispatch', child, 'Computed method names are not statically linked.')
                    own['computed'] = True
                    continue
                mname = text(method)
                own['names'].append(mname)
                static = any(c.type == 'static' for c in child.children)
                fid = self.function(child, cb, mname, cid, static=static)
                self.meta['functions'][fid]['accessor'] = next((c.type for c in child.children if c.type in {'get', 'set'}), None)
                if mname in cls.methods or mname in ambiguous:
                    self.diagnostic('ambiguous-method', child, 'Multiple methods share this name; receiver-aware linking is required.')
                    cls.methods.pop(mname, None)
                    ambiguous.add(mname)
                else:
                    cls.methods[mname] = fid
            elif child.type in {'public_field_definition', 'field_definition'}:
                value = child.child_by_field_name('value')
                field_node = child.child_by_field_name('name') or child.child_by_field_name('property')
                field_name = text(field_node)
                if field_node is not None and field_node.type in {'property_identifier', 'private_property_identifier'}:
                    own['names'].append(field_name)
                else:
                    own['computed'] = True
                ann = annotation(child.child_by_field_name('type'))
                if ann:
                    cls.fields.setdefault(field_name, []).append(('ann', ann))
                if value is not None and value.type in FUNCTIONS:
                    fid = self.function(value, cb, field_name, cid,
                                        static=any(c.type == 'static' for c in child.children))
                    cls.methods[field_name] = fid
                elif value is not None:
                    cls.defaults[field_name] = text(value)
                    if any(n.type in {'call_expression', 'new_expression'} for n in descendants(value)):
                        self.diagnostic('unsupported-class-initialization', child, 'Executable class field initialization is not expanded into constructor flow.')
            elif child.type == 'class_static_block':
                self.diagnostic('unsupported-class-initialization', child, 'Static class initialization is opaque to this flow.')
        return cid

    def imports(self, node: Node, scope: str) -> None:
        source = literal_string(node.child_by_field_name('source'))
        if source is None:
            self.diagnostic('dynamic-import', node, 'This module specifier is not a supported literal.')
        clause = next((c for c in named(node) if c.type == 'import_clause'), None)
        type_only = any(c.type == 'type' for c in node.children)
        if clause is None:
            if source is not None:
                self.module.imports.append(Import(source, None, None, line(node)))
            return
        for child in named(clause):
            if child.type == 'identifier':
                specs = [('default', text(child), type_only, child)]
            elif child.type == 'namespace_import':
                alias = next((c for c in named(child) if c.type == 'identifier'), None)
                specs = [('*', text(alias), type_only, child)]
            elif child.type == 'named_imports':
                specs = []
                for spec in named(child):
                    imported = text(spec.child_by_field_name('name'))
                    alias = text(spec.child_by_field_name('alias')) or imported
                    specs.append((imported, alias, type_only or any(c.type == 'type' for c in spec.children), spec))
            else:
                continue
            for imported, alias, only_type, spec in specs:
                if source is not None:
                    self.module.imports.append(Import(source, imported, alias, line(node)))
                self.binding(scope, alias, 'import', spec, module=source,
                             imported=imported, type_only=only_type)

    def exports(self, node: Node, body: _Body) -> list[Item]:
        source_node = node.child_by_field_name('source')
        source = literal_string(source_node)
        unknown_source = source_node is not None and source is None
        if unknown_source:
            self.diagnostic('dynamic-import', node, 'This re-export has an unsupported module specifier; its target remains unknown.')
            self.meta['dependencies'].append({'scope': body.scope, 'module': None,
                                              'source_text': text(source_node),
                                              'unknown_source': True, 'line': line(node)})
        type_only = any(c.type == 'type' for c in node.children)
        default = any(c.type == 'default' for c in node.children)
        declaration = node.child_by_field_name('declaration')
        value = node.child_by_field_name('value')
        clause = next((c for c in named(node) if c.type == 'export_clause'), None)
        if declaration is not None:
            items = body.statement(declaration)
            names = [text(declaration.child_by_field_name('name'))] if declaration.type in DECLARATIONS | CLASSES else [name for d in named(declaration) if d.type == 'variable_declarator' for name in pattern_names(d.child_by_field_name('name'))]
            for name in names:
                self.export('default' if default else name, body.scope, local=name, type_only=type_only)
            return items
        if value is not None:
            if value.type in FUNCTIONS:
                fid = self.function(value, body, name=text(value.child_by_field_name('name')) or '<default>')
                self.export('default', body.scope, target=fid, type_only=type_only)
                return []
            if value.type in CLASSES:
                cid = self.klass(value, body, '<default>')
                self.export('default', body.scope, target=cid, type_only=type_only)
                return []
            # export default <expression> captures the value when it runs.
            self.export('default', body.scope, local=text(value) if value.type == 'identifier' else None,
                        type_only=type_only, snapshot=True, start_byte=node.start_byte)
            return body.expr(value)[0]
        if clause is not None:
            for spec in named(clause):
                local = text(spec.child_by_field_name('name'))
                exported = text(spec.child_by_field_name('alias')) or local
                only_type = type_only or any(c.type == 'type' for c in spec.children)
                self.export(exported, body.scope, local=None if source_node is not None else local,
                            module=source, imported=local if source_node is not None else None,
                            type_only=only_type, unknown_source=unknown_source,
                            source_text=text(source_node), source_present=source_node is not None)
                if source:
                    self.module.imports.append(Import(source, local, None, line(node)))
        elif source_node is not None:
            namespace = next((c for c in named(node) if c.type == 'namespace_export'), None)
            exported = text(named(namespace)[0]) if namespace is not None and named(namespace) else '*'
            self.export(exported, body.scope, module=source, imported='*', type_only=type_only,
                        unknown_source=unknown_source, source_text=text(source_node), source_present=True)
            if source is not None:
                self.module.imports.append(Import(source, '*', None, line(node)))
        return []

    def finalize_commonjs(self) -> None:
        state = {'detached': False, 'unknown': False, 'uncertain': False}
        mutated_require = any(w['name'] == 'require' for w in self.meta['unbound_writes'])
        for kind, scope, node, bindings in sorted(self.commonjs, key=lambda item: item[2].start_byte):
            if kind in {'require-call', 'dynamic-import-call'}:
                if kind == 'require-call' and (self.lookup(scope, 'require') or mutated_require):
                    continue
                args = named(node.child_by_field_name('arguments'))
                source = literal_string(args[0]) if len(args) == 1 else None
                if source is not None and not any(i.module == source and i.line == line(node) for i in self.module.imports):
                    self.module.imports.append(Import(source, None, None, line(node)))
                elif source is None and kind == 'require-call':
                    self.diagnostic('dynamic-import', node, 'Computed require specifiers are not statically resolved.')
            elif kind in {'require', 'require-member'}:
                if self.lookup(scope, 'require') or mutated_require:
                    continue
                value = node.child_by_field_name('value')
                member = value.child_by_field_name('property') if kind == 'require-member' else None
                if kind == 'require-member':
                    value = value.child_by_field_name('object')
                args = named(value.child_by_field_name('arguments'))
                source = literal_string(args[0]) if len(args) == 1 else None
                if source is None:
                    continue
                pattern = node.child_by_field_name('name')
                imported_names = {}
                if pattern.type == 'identifier':
                    imported_names[text(pattern)] = text(member) if member is not None and member.type == 'property_identifier' else '*'
                elif pattern.type == 'object_pattern':
                    for child in named(pattern):
                        if child.type == 'pair_pattern':
                            pname = text(child.child_by_field_name('key'))
                            value_names = pattern_names(child.child_by_field_name('value'))
                            if len(value_names) == 1:
                                imported_names[value_names[0]] = pname
                        elif child.type == 'shorthand_property_identifier_pattern':
                            imported_names[text(child)] = text(child)
                for binding in bindings or []:
                    if binding['name'] not in imported_names:
                        continue
                    imported = imported_names[binding['name']]
                    binding.update(kind='import', module=source, imported=imported)
                    self.module.imports.append(Import(source, imported, binding['name'], line(node)))
            else:
                export_event(self, scope, node, state)
