"""Java declarations to shared IR plus versioned ``metadata['java']``.

Types, members and signatures come from the real Tree-sitter AST. Method and
constructor bodies are lowered by body.py; anonymous classes, local classes
and lambdas are separate functions that do not execute where they appear.
"""
from __future__ import annotations

import re

from tree_sitter import Node

from flowmap.ir import ClassDef, Function, Import, Module, Param

from .syntax import TYPE_KINDS, TYPES, col, descendants, erase, line, modifiers, named, parser, text


def extract_module(relpath: str, source: bytes) -> Module:
    return _Extractor(relpath, source, parser().parse(source).root_node).run()


class _Extractor:
    def __init__(self, file: str, source: bytes, root: Node):
        self.file, self.source, self.root = file, source, root
        self.meta: dict = {'version': 1, 'package': '', 'imports': [], 'types': {},
                           'functions': {}, 'calls': {}}
        self.module = Module(file, 'java', None, [], {}, {}, False, metadata={'java': self.meta})
        self.identifiers = {text(n) for n in descendants(root) if n.type == 'identifier'}
        self.initializers: dict[str, list[Node]] = {}  # class id -> instance initializers in order

    def diagnostic(self, code: str, node: Node, message: str) -> None:
        self.module.diagnostics.append({'code': code, 'message': message,
                                        'line': line(node), 'col': col(node),
                                        'end_line': node.end_point[0] + 1,
                                        'end_col': node.end_point[1], 'severity': 'warning'})

    def run(self) -> Module:
        for node in descendants(self.root):
            if node.type == 'ERROR' or node.is_missing:
                self.diagnostic('parse-error', node, 'Parser recovery leaves this source only partially analyzed.')
        for child in named(self.root):
            if child.type == 'package_declaration':
                self.meta['package'] = text(next((c for c in named(child) if c.type in {'scoped_identifier', 'identifier'}), None))
            elif child.type == 'import_declaration':
                self.import_(child)
            elif child.type in TYPES:
                self.type_decl(child, None)
        return self.module

    def import_(self, node: Node) -> None:
        name = text(next((c for c in named(node) if c.type in {'scoped_identifier', 'identifier'}), None))
        static = any(c.type == 'static' for c in node.children)
        wildcard = any(c.type == 'asterisk' for c in node.children)
        self.meta['imports'].append({'name': name, 'static': static, 'wildcard': wildcard, 'line': line(node)})
        self.module.imports.append(Import(name, '*' if wildcard else None, None, line(node)))

    def document(self, node: Node) -> str | None:
        previous = node.prev_sibling
        while previous is not None and previous.type == 'line_comment':
            previous = previous.prev_sibling
        if previous is None or previous.type != 'block_comment' or not text(previous).startswith('/**'):
            return None
        if self.source[previous.end_byte:node.start_byte].strip():
            return None
        raw = text(previous)[3:-2]
        value = '\n'.join(re.sub(r'^\s*\* ?', '', part).strip() for part in raw.splitlines()).strip()
        return value.split('\n\n')[0].strip() or None

    # ── types ──────────────────────────────────────────────────────────────

    def type_decl(self, node: Node, outer: str | None, *, scope_fid: str | None = None,
                  anonymous_name: str | None = None, base: str | None = None) -> str:
        outer_meta = self.meta['types'].get(outer) if outer else None
        name = anonymous_name or text(node.child_by_field_name('name'))
        qual = f'{self.module.classes[outer].qualname}.{name}' if outer else name
        if outer_meta:
            fqn = f"{outer_meta['fqn']}.{name}"
        else:
            fqn = f"{self.meta['package']}.{name}" if self.meta['package'] else name
        cid = f'{self.file}::{qual}@{line(node)}:{col(node)}'
        kind = 'class' if anonymous_name else TYPE_KINDS[node.type]
        superclass = erase(next(iter(named(node.child_by_field_name('superclass'))), None)) if node.child_by_field_name('superclass') else None
        if anonymous_name:
            superclass = base
        interfaces_node = node.child_by_field_name('interfaces')
        if interfaces_node is None and node.type == 'interface_declaration':
            interfaces_node = next((c for c in named(node) if c.type == 'extends_interfaces'), None)
        type_list = next((c for c in named(interfaces_node) if c.type == 'type_list'), None) if interfaces_node is not None else None
        interfaces = [erase(t) for t in named(type_list)] if type_list is not None else []
        params_node = node.child_by_field_name('type_parameters')
        type_params = [text(next(iter(named(p)), None)) for p in named(params_node)] if params_node is not None else []
        mods = [] if anonymous_name else modifiers(node)
        self.meta['types'][cid] = {
            'fqn': fqn, 'name': name, 'kind': kind, 'modifiers': mods,
            'superclass': superclass, 'interfaces': [i for i in interfaces if i],
            'fields': {}, 'outer': outer, 'local': scope_fid is not None and not anonymous_name,
            'anonymous': bool(anonymous_name), 'type_params': type_params, 'scope_fid': scope_fid,
        }
        bases = [b for b in [superclass, *interfaces] if b]
        cls = ClassDef(cid, name, qual, self.file, line(node), bases, {}, {}, None if anonymous_name else self.document(node))
        self.module.classes[cid] = cls
        if node.type == 'record_declaration':
            for component in named(node.child_by_field_name('parameters')):
                if component.type == 'formal_parameter':
                    self.field(cid, text(component.child_by_field_name('name')), erase(component.child_by_field_name('type')))
        body = node if anonymous_name else node.child_by_field_name('body')
        self.members(body, cid)
        return cid

    def field(self, cid: str, name: str, type_text: str | None) -> None:
        if not name:
            return
        if type_text:
            self.meta['types'][cid]['fields'][name] = type_text
            self.module.classes[cid].fields.setdefault(name, []).append(('ann', type_text))

    def members(self, body: Node | None, cid: str) -> None:
        overloaded: set[str] = set()
        interface = self.meta['types'][cid]['kind'] == 'interface'
        children = [c for part in named(body) for c in (named(part) if part.type == 'enum_body_declarations' else [part])]
        # Instance field initializers and initializer blocks run inside every
        # constructor (after super(...)); static ones run at class load.
        self.initializers[cid] = [c for c in children if not interface and (
            c.type == 'block' or (c.type == 'field_declaration' and 'static' not in modifiers(c)))]
        for child in children:
            if (child.type == 'static_initializer' or (child.type in {'field_declaration', 'constant_declaration'}
                                                       and (interface or 'static' in modifiers(child)))):
                self.field_initializers(child, cid)
        for child in named(body):
            kind = child.type
            if kind == 'enum_body_declarations':
                self.members(child, cid)
            elif kind in TYPES:
                self.type_decl(child, cid)
            elif kind in {'method_declaration', 'constructor_declaration', 'compact_constructor_declaration'}:
                fid = self.function(child, cid)
                name = self.module.functions[fid].name
                if kind == 'method_declaration':
                    cls = self.module.classes[cid]
                    if name in cls.methods or name in overloaded:
                        cls.methods.pop(name, None)  # overloads: linker selects by signature
                        overloaded.add(name)
                    else:
                        cls.methods[name] = fid
            elif kind in {'field_declaration', 'constant_declaration'}:
                type_text = erase(child.child_by_field_name('type'))
                for declarator in named(child):
                    if declarator.type == 'variable_declarator':
                        dims = declarator.child_by_field_name('dimensions')
                        suffix = '[]' * text(dims).count('[') if dims is not None else ''
                        self.field(cid, text(declarator.child_by_field_name('name')), (type_text or '') + suffix or None)
            elif kind == 'enum_constant':
                if child.child_by_field_name('body') is not None:
                    self.meta['types'][cid]['constant_bodies'] = True
                    self.diagnostic('unsupported-class-initialization', child, 'Enum constant bodies are not expanded into flow.')

        self.implicit_constructor(body, cid)

    def field_initializers(self, node: Node, cid: str) -> None:
        """Static initializers run at class load; they are not expanded."""
        if has_calls(node):
            self.diagnostic('unsupported-class-initialization', node,
                            'Static initializer calls are not expanded into flow.')

    def implicit_constructor(self, body: Node | None, cid: str) -> None:
        """The default constructor runs the instance initializers; synthesize it when they call."""
        from .body import Body

        info = self.meta['types'][cid]
        nodes = self.initializers.get(cid, [])
        if (body is None or info['kind'] not in {'class', 'enum'} or not any(has_calls(n) for n in nodes)
                or any(f['cls'] == cid and f['kind'] == 'constructor' for f in self.meta['functions'].values())):
            return
        cls = self.module.classes[cid]
        fid = f'{self.file}::{cls.qualname}.<init>@{line(body)}:{col(body)}'
        fn = Function(fid, cls.name, f'{cls.qualname}.<init>', self.file, line(body), body.end_point[0] + 1,
                      [], None, None, [], [], cls=cid, language='java', receiver='this', is_constructor=True)
        self.module.functions[fid] = fn
        self.meta['functions'][fid] = {'cls': cid, 'kind': 'constructor', 'modifiers': [], 'static': False,
                                       'abstract': False, 'param_types': [], 'varargs': False, 'returns': None,
                                       'locals': {}, 'type_params': [], 'implicit': True}
        captured = self.captured(cid)
        fn.body = Body(self, fn, cid, dict(captured), set(captured)).initializers(nodes)

    def captured(self, cid: str | None) -> dict:
        scope_fid = self.meta['types'][cid]['scope_fid'] if cid else None
        # Local and anonymous classes see the enclosing method's (effectively final) locals.
        return dict(self.meta['functions'][scope_fid]['locals']) if scope_fid in self.meta['functions'] else {}

    # ── functions ──────────────────────────────────────────────────────────

    def params(self, node: Node | None) -> tuple[list[Param], list[str], bool]:
        params, types, varargs = [], [], False
        for child in named(node):
            if child.type == 'formal_parameter':
                dims = child.child_by_field_name('dimensions')
                type_text = (erase(child.child_by_field_name('type')) or '') + ('[]' * text(dims).count('[') if dims is not None else '')
                params.append(Param(text(child.child_by_field_name('name')), type_text or None))
                types.append(type_text)
            elif child.type == 'spread_parameter':
                type_node = next((c for c in named(child) if c.type not in {'variable_declarator', 'modifiers'}), None)
                declarator = next((c for c in named(child) if c.type == 'variable_declarator'), None)
                type_text = (erase(type_node) or '') + '[]'
                params.append(Param(text(declarator.child_by_field_name('name')) if declarator else '', type_text, kind='vararg'))
                types.append(type_text)
                varargs = True
        return params, types, varargs

    def function(self, node: Node, cid: str) -> str:
        cls = self.module.classes[cid]
        kind = node.type
        constructor = kind in {'constructor_declaration', 'compact_constructor_declaration'}
        name = cls.name if constructor else text(node.child_by_field_name('name'))
        qual = f'{cls.qualname}.<init>' if constructor else f'{cls.qualname}.{name}'
        fid = f'{self.file}::{qual}@{line(node)}:{col(node)}'
        if kind == 'compact_constructor_declaration':
            record = self.meta['types'][cid]
            params = [Param(n, t) for n, t in record['fields'].items()]
            types, varargs = list(record['fields'].values()), False
        else:
            params, types, varargs = self.params(node.child_by_field_name('parameters'))
        mods = modifiers(node)
        body = node.child_by_field_name('body')
        returns = None if constructor else erase(node.child_by_field_name('type'))
        fn = Function(fid, name, qual, self.file, line(node), node.end_point[0] + 1, params, returns,
                      self.document(node), [], [], cls=cid, language='java', receiver='this',
                      is_constructor=constructor)
        self.module.functions[fid] = fn
        self.meta['functions'][fid] = {
            'cls': cid, 'kind': 'constructor' if constructor else 'method', 'modifiers': mods,
            'static': 'static' in mods, 'abstract': body is None and 'native' not in mods,
            'param_types': types, 'varargs': varargs, 'returns': returns,
            'locals': {p.name: t for p, t in zip(params, types) if p.name and t},
            'type_params': [text(next(iter(named(p)), None)) for p in named(node.child_by_field_name('type_parameters'))],
        }
        self.lower(fn, body)
        return fid

    def lower(self, fn: Function, body: Node | None) -> None:
        from .body import Body

        meta = self.meta['functions'][fn.id]
        captured = self.captured(fn.cls)
        locals_ = dict(captured)
        locals_.update(meta['locals'])
        meta['locals'] = locals_
        lowering = Body(self, fn, fn.cls, locals_, set(captured) - {p.name for p in fn.params})
        nodes = self.initializers.get(fn.cls, []) if fn.is_constructor else []
        statements = named(body)
        first = statements[0] if statements and statements[0].type == 'explicit_constructor_invocation' else None
        if body is None:
            fn.body = []
        elif not nodes or (first is not None and text(first.child_by_field_name('constructor')) == 'this'):
            fn.body = lowering.block(body)  # this(...) runs the initializers in the target
        else:
            fn.body = (lowering.statement(first) if first is not None else []) + lowering.initializers(nodes, captured) + \
                [item for statement in statements[1 if first is not None else 0:] for item in lowering.statement(statement)]


def has_calls(node: Node) -> bool:
    return any(n.type in {'method_invocation', 'object_creation_expression'} for n in descendants(node))
