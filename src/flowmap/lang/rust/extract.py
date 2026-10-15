"""Rust items to shared IR plus versioned ``metadata['rust']``.

Scopes are module paths relative to the file (``self``, ``self::inner`` for
inline modules); the linker prefixes them with the file's crate module path.
Methods take the implicit receiver ``self`` (not a parameter). Closures and
nested ``fn`` items are separate functions that do not run where they appear.
"""
from __future__ import annotations

from tree_sitter import Node

from flowmap.ir import ClassDef, Function, Import, Module, Param

from .syntax import TYPE_ITEMS, attributes, col, descendants, doc_comment, erase, line, named, parser, path_text, text


def extract_module(relpath: str, source: bytes) -> Module:
    return _Extractor(relpath, source, parser().parse(source).root_node).run()


def join(base: str, path: str) -> str:
    return f'{base}::{path}' if base and path else base or path


class _Extractor:
    def __init__(self, file: str, source: bytes, root: Node):
        self.file, self.source, self.root = file, source, root
        self.meta: dict = {'version': 1, 'mods': [], 'uses': [], 'items': {}, 'types': {},
                           'impls': [], 'functions': {}, 'calls': {}}
        self.module = Module(file, 'rust', None, [], {}, {}, False, metadata={'rust': self.meta})
        self.identifiers = {text(n) for n in descendants(root) if n.type == 'identifier'}

    def diagnostic(self, code: str, node: Node, message: str) -> None:
        self.module.diagnostics.append({'code': code, 'message': message,
                                        'line': line(node), 'col': col(node),
                                        'end_line': node.end_point[0] + 1,
                                        'end_col': node.end_point[1], 'severity': 'warning'})

    def run(self) -> Module:
        for node in descendants(self.root):
            if node.type == 'ERROR' or node.is_missing:
                self.diagnostic('parse-error', node, 'Parser recovery leaves this source only partially analyzed.')
        self.items(self.root, 'self', '')
        return self.module

    # ── items ──────────────────────────────────────────────────────────────

    def items(self, container: Node, scope: str, prefix: str) -> None:
        for child in named(container):
            self.item(child, scope, prefix)

    def item(self, node: Node, scope: str, prefix: str, local_to: str | None = None) -> str | None:
        kind = node.type
        if kind == 'function_item':
            return self.function(node, scope, prefix, local_to=local_to)
        if kind == 'mod_item':
            name = text(node.child_by_field_name('name'))
            body = node.child_by_field_name('body')
            path_attr = next((text(a.child_by_field_name('value'))[1:-1] for a in attributes(node)
                              if text(next(iter(named(a)), None)) == 'path' and a.child_by_field_name('value') is not None), None)
            self.meta['mods'].append({'name': name, 'inline': body is not None, 'path_attr': path_attr,
                                      'line': line(node), 'scope': scope})
            self.meta['items'][f'{scope}::{name}'] = {'kind': 'mod'}
            if body is not None:
                self.items(body, f'{scope}::{name}', f'{prefix}{name}.')
        elif kind == 'use_declaration':
            self.use(node.child_by_field_name('argument'), scope, '', line(node))
        elif kind == 'extern_crate_declaration':
            name = text(node.child_by_field_name('name'))
            alias = text(node.child_by_field_name('alias')) or name
            self.meta['uses'].append({'scope': scope, 'path': name, 'alias': alias, 'glob': False, 'line': line(node)})
        elif kind in TYPE_ITEMS:
            self.type_item(node, scope, prefix)
        elif kind == 'impl_item':
            self.impl(node, scope, prefix)
        elif kind in {'const_item', 'static_item'}:
            self.meta['items'][f"{scope}::{text(node.child_by_field_name('name'))}"] = {
                'kind': 'const', 'type': erase(node.child_by_field_name('type'))}
        elif kind == 'type_item':
            self.meta['items'][f"{scope}::{text(node.child_by_field_name('name'))}"] = {
                'kind': 'alias', 'type': erase(node.child_by_field_name('type'))}
        return None

    def use(self, node: Node | None, scope: str, base: str, at: int) -> None:
        if node is None:
            return
        kind = node.type
        if kind == 'scoped_use_list':
            prefix = join(base, path_text(node.child_by_field_name('path')))
            self.use(node.child_by_field_name('list'), scope, prefix, at)
        elif kind == 'use_list':
            for child in named(node):
                self.use(child, scope, base, at)
        elif kind == 'use_as_clause':
            path = join(base, path_text(node.child_by_field_name('path')))
            alias = text(node.child_by_field_name('alias'))
            self.add_use(scope, path, alias, False, at)
        elif kind == 'use_wildcard':
            inner = next(iter(named(node)), None)
            self.add_use(scope, join(base, path_text(inner)), None, True, at)
        else:
            path = path_text(node)
            if not path:
                return
            if path == 'self' and base:  # use a::b::{self} imports b
                self.add_use(scope, base, base.rsplit('::', 1)[-1], False, at)
            else:
                full = join(base, path)
                self.add_use(scope, full, full.rsplit('::', 1)[-1], False, at)

    def add_use(self, scope: str, path: str, alias: str | None, glob: bool, at: int) -> None:
        self.meta['uses'].append({'scope': scope, 'path': path, 'alias': alias, 'glob': glob, 'line': at})
        self.module.imports.append(Import(path, '*' if glob else None, alias, at))

    def type_item(self, node: Node, scope: str, prefix: str) -> str:
        name = text(node.child_by_field_name('name'))
        kind = TYPE_ITEMS[node.type]
        qual = f'{prefix}{name}'
        cid = f'{self.file}::{qual}@{line(node)}:{col(node)}'
        fields: dict[str, str] = {}
        body = node.child_by_field_name('body')
        if body is not None and body.type == 'field_declaration_list':
            for field in named(body):
                if field.type == 'field_declaration':
                    fields[text(field.child_by_field_name('name'))] = erase(field.child_by_field_name('type')) or ''
        elif body is not None and body.type == 'ordered_field_declaration_list':
            types = [c for c in named(body) if c.type not in {'visibility_modifier', 'attribute_item'}]
            fields = {str(i): erase(t) or '' for i, t in enumerate(types)}
        params = node.child_by_field_name('type_parameters')
        info = {'name': name, 'kind': kind, 'scope': scope, 'fields': fields,
                'type_params': [text(p.child_by_field_name('name') or p) for p in named(params) if p.type == 'type_parameter'] if params else []}
        self.meta['types'][cid] = info
        self.meta['items'][f'{scope}::{name}'] = {'kind': kind, 'id': cid}
        self.module.classes[cid] = ClassDef(cid, name, qual, self.file, line(node), [], {},
                                            {k: [('ann', v)] for k, v in fields.items() if v}, doc_comment(node))
        if kind == 'trait':
            info['methods'] = {}
            for child in named(body):
                if child.type == 'function_signature_item':
                    info['methods'][text(child.child_by_field_name('name'))] = None
                elif child.type == 'function_item':
                    fid = self.function(child, scope, f'{qual}.', trait=name)
                    info['methods'][self.module.functions[fid].name] = fid
        return cid

    def impl(self, node: Node, scope: str, prefix: str) -> None:
        type_text = erase(node.child_by_field_name('type')) or ''
        trait = erase(node.child_by_field_name('trait'))
        params = node.child_by_field_name('type_parameters')
        type_params = [text(p.child_by_field_name('name') or p) for p in named(params) if p.type == 'type_parameter'] if params else []
        record = {'type': type_text, 'trait': trait, 'scope': scope, 'methods': {}, 'line': line(node),
                  'type_params': type_params}
        self.meta['impls'].append(record)
        owner = type_text.rsplit('::', 1)[-1]
        for child in named(node.child_by_field_name('body')):
            if child.type == 'function_item':
                fid = self.function(child, scope, f'{prefix}{owner}.', self_type=type_text, trait=trait,
                                    impl_params=type_params)
                record['methods'][self.module.functions[fid].name] = fid

    # ── functions ──────────────────────────────────────────────────────────

    def params(self, node: Node | None) -> tuple[list[Param], list[str | None], bool]:
        params, types, has_self = [], [], False
        for child in named(node):
            if child.type == 'self_parameter':
                has_self = True
            elif child.type == 'parameter':
                pattern = child.child_by_field_name('pattern')
                type_text = erase(child.child_by_field_name('type'))
                params.append(Param(text(pattern).removeprefix('mut ').strip(), type_text))
                types.append(type_text)
            elif child.type == 'variadic_parameter':
                params.append(Param('...', None, kind='vararg'))
                types.append(None)
        return params, types, has_self

    def function(self, node: Node, scope: str, prefix: str, *, self_type: str | None = None,
                 trait: str | None = None, impl_params: list[str] | None = None,
                 local_to: str | None = None) -> str:
        name = text(node.child_by_field_name('name'))
        qual = f'{prefix}{name}'
        fid = f'{self.file}::{qual}@{line(node)}:{col(node)}'
        params, types, has_self = self.params(node.child_by_field_name('parameters'))
        returns = erase(node.child_by_field_name('return_type'))
        in_impl = self_type is not None or trait is not None
        fn = Function(fid, name, qual, self.file, line(node), node.end_point[0] + 1, params, returns,
                      doc_comment(node), [], [], language='rust', receiver='self' if has_self else None,
                      is_async=any(c.type == 'function_modifiers' and 'async' in text(c) for c in node.children))
        self.module.functions[fid] = fn
        type_params = node.child_by_field_name('type_parameters')
        self.meta['functions'][fid] = {
            'kind': 'method' if in_impl and has_self else 'assoc' if in_impl else 'fn',
            'scope': scope, 'self_type': self_type, 'trait': trait, 'has_self': has_self,
            'param_types': types, 'returns': returns,
            'locals': {p.name: t for p, t in zip(params, types) if p.name and t},
            'type_params': (impl_params or []) + ([text(p.child_by_field_name('name') or p) for p in named(type_params)
                                                  if p.type == 'type_parameter'] if type_params else []),
            'local_to': local_to, 'nested': {},
            'test': any(text(next(iter(named(a)), None)) == 'test' for a in attributes(node)),
        }
        if not in_impl and local_to is None:
            self.meta['items'][f'{scope}::{name}'] = {'kind': 'fn', 'id': fid}
        self.lower(fn, node.child_by_field_name('body'))
        return fid

    def lower(self, fn: Function, body: Node | None) -> None:
        from .body import Body

        info = self.meta['functions'][fn.id]
        fn.body = Body(self, fn, dict(info['locals'])).function_body(body) if body is not None else []
