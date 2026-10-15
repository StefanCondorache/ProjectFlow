"""Conservative lexical/cross-module JavaScript and TypeScript call linking.

A missing, shadowed, mutated or ambiguous binding never falls back to a
project-wide name guess. Receiver targets require a class/constructor/type
annotation or the lexical ``this`` of a method. Unknown code stays unknown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from flowmap.ir import Call, Function, Module, walk
from .modules import ModuleRef, resolve_module

IDENTIFIER = r'[A-Za-z_$][\w$]*'
DOTTED = re.compile(rf'{IDENTIFIER}(?:\.{IDENTIFIER})*\Z')
NEW = re.compile(rf'new\s+({IDENTIFIER}(?:\.{IDENTIFIER})*)\s*\(')


GLOBAL_OBJECTS = {'globalThis', 'window', 'global', 'self'}


@dataclass(frozen=True)
class Ref:
    kind: str  # function | class | instance | module | external
    key: str
    open: bool = False  # the runtime class may be a subclass (this, annotations)


def link_ecmascript(root: Path, modules: dict[str, Module]) -> 'EcmaLinker':
    linker = EcmaLinker(root, modules)
    linker.link()
    return linker


class EcmaLinker:
    def __init__(self, root: Path, modules: dict[str, Module]):
        self.root = root
        self.modules = modules
        self.functions = {fid: fn for module in modules.values() for fid, fn in module.functions.items()}
        self.classes = {cid: cls for module in modules.values() for cid, cls in module.classes.items()}
        self.metadata = {file: module.metadata.get('ecmascript', {}) for file, module in modules.items()}
        self.bindings: dict[tuple[str, str, str], list[dict]] = {}
        self.builtin_writes: dict[str, set[str]] = {}
        for file, meta in self.metadata.items():
            for binding in meta.get('bindings', []):
                self.bindings.setdefault((file, binding['scope'], binding['name']), []).append(binding)
            # Opaque control flow contributes write evidence without invented
            # execution steps. Unknown global replacement suppresses fallback.
            self.builtin_writes[file] = {
                record['name'] for record in meta.get('unbound_writes', [])
            }
        # Property names assigned anywhere (s.save = ..., [this.save] = ...,
        # Store.prototype.save = ..., require('./lib').save = ...) may replace a
        # method or CommonJS export of that name; such links stay unresolved.
        # Literal module-level CommonJS export assignments are export records.
        self.subclass_methods: dict[str, set[str] | None] = {}
        self.parents: dict[str, set[str] | None] | None = None
        self.class_names = {cls.name for cls in self.classes.values()}
        self.replaced_members: set[str] = set()
        self.global_writes: set[str] = set()
        for file, meta in self.metadata.items():
            for write in meta.get('member_writes', []):
                obj, member = write['object'], write['member']
                root = obj.split('.')[0]
                unbound = bool(root) and not self.lookup(file, write['scope'], root)
                if unbound and (obj in {'exports', 'module.exports'} or (obj, member) == ('module', 'exports')) \
                        and self.scope_owner(file, write['scope']) == f'{file}::<main>':
                    continue
                if unbound and obj in GLOBAL_OBJECTS and member:
                    self.global_writes.add(member)
                if member:
                    self.replaced_members.add(member)

    def module_ref(self, file: str, spec: str) -> ModuleRef | None:
        return resolve_module(self.modules, file, spec)

    def lookup(self, file: str, scope: str | None, name: str) -> list[dict]:
        scopes = self.metadata[file].get('scopes', {})
        while scope is not None:
            found = self.bindings.get((file, scope, name), [])
            if found:
                return found
            scope = scopes.get(scope, {}).get('parent')
        return []

    def scope_owner(self, file: str, scope: str | None) -> str | None:
        scopes = self.metadata[file].get('scopes', {})
        while scope is not None:
            record = scopes.get(scope, {})
            if record.get('kind') in {'function', 'module'}:
                return f'{file}::<main>' if record['kind'] == 'module' else record.get('owner')
            scope = record.get('parent')
        return None

    def visible(self, file: str, binding: dict, position: int | None,
                execution_owner: str | None) -> bool:
        value_hoisted = binding.get('hoisted') and binding['kind'] != 'var' and not (
            binding['kind'] == 'import' and binding.get('initializer')
        )
        if position is None or value_hoisted or position >= binding.get('initialized_byte', binding['start_byte']):
            return True
        # A closure may run after the surrounding scope initializes a const.
        # A read in the initializer's own execution scope remains a TDZ read.
        return execution_owner is not None and self.scope_owner(file, binding['scope']) != execution_owner

    def target_ref(self, target: str | None) -> Ref | None:
        if target in self.functions:
            return Ref('function', target)
        if target in self.classes:
            return Ref('class', target)
        return None

    def construct_name(self, file: str, binding: dict) -> str | None:
        initializer = binding.get('initializer') or ''
        match = NEW.match(initializer)
        if match is None:
            return None
        end = binding.get('initialized_byte')
        start = end - len(initializer.encode('utf-8')) if end is not None else None
        # A new subexpression inside a larger initializer does not justify the
        # resulting variable's class. Require the exact AST constructor span.
        sites = self.metadata[file].get('calls', {}).values()
        return match[1] if any(site.get('construct') and site['start_byte'] == start and site['end_byte'] == end for site in sites) else None

    def import_ref(self, file: str, spec: str | None, imported: str | None,
                   seen: frozenset, allow_type: bool = False) -> Ref | None:
        if spec is None:
            return None
        module = self.module_ref(file, spec)
        if module is None:
            return None
        if module.kind == 'external':
            name = module.key if imported in (None, '*', 'default') else f'{module.key}.{imported}'
            return Ref('external', name)
        if imported == '*':
            return Ref('module', module.key)
        return self.export_ref(module.key, imported or 'default', seen, allow_type)

    def export_ref(self, file: str, name: str, seen: frozenset = frozenset(),
                   allow_type: bool = False) -> Ref | None:
        return self.export_state(file, name, seen, allow_type)[1]

    def export_state(self, file: str, name: str, seen: frozenset,
                     allow_type: bool) -> tuple[bool, Ref | None]:
        """(may export name, unique target). (True, None) is unresolved, not absent."""
        key = ('export', file, name, allow_type)
        if key in seen:
            return False, None  # a cyclic star branch contributes nothing
        seen = seen | {key}
        exports = [record for record in self.metadata[file].get('exports', [])
                   if allow_type or not record.get('type_only')]
        explicit = [record for record in exports if record['name'] == name]
        if explicit:
            # Duplicate explicit declarations do not establish one binding.
            if len(explicit) != 1:
                return True, None
            record = explicit[0]
            if record.get('unknown_source') or record.get('unknown_export') or self.metadata[file]['scopes'].get(record['scope'], {}).get('kind') != 'module':
                return True, None
            if record.get('commonjs') and name in self.replaced_members:
                return True, None  # the exports object can be patched through any reference
            if record.get('module'):
                return True, self.import_ref(file, record['module'], record.get('imported'), seen, allow_type)
            if record.get('target'):
                return True, self.target_ref(record['target'])
            local = record.get('local')
            found = self.lookup(file, record['scope'], local) if local else []
            if len(found) != 1:
                return True, None
            # CommonJS assignments and default expressions capture the value
            # when they run; named ES exports stay live bindings.
            snapshot = record.get('commonjs') or record.get('snapshot')
            position = record.get('start_byte') if snapshot else None
            owner = self.scope_owner(file, record['scope']) if snapshot else None
            return True, self.binding_ref(file, found[0], position, seen, allow_type, owner)
        if name == 'default':
            return False, None  # export * never reexports default
        exported, candidates = False, set()
        for record in exports:
            if record['name'] != '*':
                continue
            if record.get('unknown_source') or record.get('unknown_export') or self.metadata[file]['scopes'].get(record['scope'], {}).get('kind') != 'module':
                return True, None
            if not record.get('module'):
                continue
            module = self.module_ref(file, record['module'])
            if module is None:
                return True, None  # an unknown star source could conflict
            if module.kind == 'external':
                exported = True
                candidates.add(Ref('external', f'{module.key}.{name}'))
                continue
            branch, ref = self.export_state(module.key, name, seen, allow_type)
            if branch and ref is None:
                return True, None  # an unresolved branch could conflict
            exported = exported or branch
            if ref is not None:
                candidates.add(ref)
        return exported, next(iter(candidates)) if len(candidates) == 1 else None

    def binding_ref(self, file: str, binding: dict, position: int | None,
                    seen: frozenset, allow_type: bool = False,
                    execution_owner: str | None = None) -> Ref | None:
        key = ('binding', file, binding['scope'], binding['name'])
        if key in seen or binding.get('writes') or (binding.get('type_only') and not allow_type):
            return None
        if binding.get('conditional') and binding['kind'] == 'var':
            return None
        if not self.visible(file, binding, position, execution_owner):
            return None
        seen = seen | {key}
        if binding['kind'] == 'import':
            if 'require' in self.builtin_writes[file] and re.match(r'\s*require\b', binding.get('initializer') or ''):
                return None
            return self.import_ref(file, binding.get('module'), binding.get('imported'), seen, allow_type)
        target = self.target_ref(binding.get('target'))
        if target:
            return target
        initializer = (binding.get('initializer') or '').strip()
        if DOTTED.fullmatch(initializer):
            return self.expression_ref(file, binding['scope'], initializer,
                                       binding.get('start_byte'), seen, allow_type,
                                       self.scope_owner(file, binding['scope']), detached=True)
        construct = self.construct_name(file, binding)
        if construct:
            target = self.expression_ref(file, binding['scope'], construct,
                                         binding.get('start_byte'), seen,
                                         execution_owner=self.scope_owner(file, binding['scope']))
            if target and target.kind == 'class':
                return Ref('instance', target.key)
        return None

    def expression_ref(self, file: str, scope: str, expression: str,
                       position: int | None, seen: frozenset = frozenset(),
                       allow_type: bool = False, execution_owner: str | None = None,
                       detached: bool = False) -> Ref | None:
        if not DOTTED.fullmatch(expression):
            return None
        root, *members = expression.split('.')
        found = self.lookup(file, scope, root)
        if found:
            ref = self.binding_ref(file, found[0], position, seen, allow_type, execution_owner) if len(found) == 1 else None
        elif root in {'console', 'fetch', 'require'} and root not in self.builtin_writes[file] \
                and root not in self.global_writes:
            ref = Ref('external', root)
        else:
            ref = None
        for member in members:
            if detached and ref and ref.kind == 'instance':
                return None
            ref = self.member_ref(ref, member, seen, allow_type) if ref else None
        return ref

    def member_ref(self, ref: Ref, member: str, seen: frozenset = frozenset(),
                   allow_type: bool = False) -> Ref | None:
        if ref.kind == 'module':
            return self.export_ref(ref.key, member, seen, allow_type)
        if ref.kind == 'external':
            return Ref('external', f'{ref.key}.{member}')
        if ref.kind in {'class', 'instance'}:
            if member in self.replaced_members or (ref.open and self.overridden(ref.key, member)):
                return None
            cls = self.classes[ref.key]
            # Metadata preserves both declarations even if the methods map
            # cannot represent a duplicate/accessor pair.
            methods = [fn for fn in self.functions.values()
                       if fn.cls == cls.id and fn.name == member]
            if len(methods) != 1:
                return None
            fn = methods[0]
            info = self.metadata[fn.file]['functions'][fn.id]
            if info.get('accessor') or bool(info.get('static')) != (ref.kind == 'class'):
                return None
            return Ref('function', fn.id)
        return None

    def instance_ref(self, file: str, scope: str, expression: str,
                     position: int | None, fn: Function) -> Ref | None:
        if not re.fullmatch(IDENTIFIER, expression):
            return None
        found = self.lookup(file, scope, expression)
        if len(found) != 1:
            return None
        binding = found[0]
        if binding.get('writes') or binding.get('conditional') and binding['kind'] == 'var':
            return None
        if not self.visible(file, binding, position, fn.id):
            return None
        annotation = (binding.get('annotation') or '').strip()
        construct = self.construct_name(file, binding)
        type_expression = annotation if DOTTED.fullmatch(annotation) else construct
        if not type_expression:
            return None
        target = self.expression_ref(file, binding['scope'], type_expression,
                                     position, allow_type=bool(annotation), execution_owner=fn.id)
        return Ref('instance', target.key, open=bool(annotation)) if target and target.kind == 'class' else None

    def overridden(self, cid: str, member: str) -> bool:
        """Whether a loaded subclass may define member (method or field).
        A subclass whose parent cannot be resolved may extend any class."""
        if cid not in self.subclass_methods:
            self.subclass_methods[cid] = self.subclass_members(cid)
        members = self.subclass_methods[cid]
        return members is None or member in members

    def subclass_members(self, cid: str) -> set[str] | None:
        """Own member names of every loaded subclass; None if one is computed."""
        if self.parents is None:
            self.parents = {cls.id: self.parent_classes(cls) for cls in self.classes.values()}
        subclasses: set[str] = set()
        changed = True
        while changed:
            changed = False
            for cls in self.classes.values():
                if cls.id == cid or cls.id in subclasses:
                    continue
                parents = self.parents[cls.id]
                if parents is None or parents & (subclasses | {cid}):
                    subclasses.add(cls.id)
                    changed = True
        members: set[str] = set()
        for sub in subclasses:
            own = self.metadata[self.classes[sub].file].get('class_members', {}).get(sub)
            if own is None or own['computed']:
                return None
            members.update(own['names'])
        return members

    def parent_classes(self, cls) -> set[str] | None:
        """Project classes a class extends, resolved through bindings and
        imports; None when a superclass expression cannot be resolved."""
        if not cls.bases:
            return set()
        meta = self.metadata[cls.file]
        scope = next((b['scope'] for b in meta.get('bindings', [])
                      if b['kind'] == 'class' and b.get('target') == cls.id), None)
        if scope is None:
            scope = next((meta['functions'][fn.id]['declaration_scope'] for fn in self.functions.values()
                          if fn.cls == cls.id), None)
        if scope is None:
            return None
        parents = set()
        for base in cls.bases:
            ref = self.expression_ref(cls.file, scope, base, None) if DOTTED.fullmatch(base) else None
            if ref is None:
                root = base.split('.')[0]
                # An unbound global (Error, HTMLElement) is not a project class
                # unless a project class could have been published under it.
                if DOTTED.fullmatch(base) and not self.lookup(cls.file, scope, root) \
                        and root not in self.global_writes and root not in self.class_names:
                    continue
                return None
            if ref.kind == 'class':
                parents.add(ref.key)
        return parents

    def this_owner(self, file: str, fid: str | None) -> Function | None:
        """The method whose ``this`` a function (or arrow inside it) sees."""
        info = self.metadata[file]['functions'].get(fid, {})
        owner = self.metadata[file]['scopes'].get(info.get('this_scope'), {}).get('owner')
        owner_fn = self.functions.get(owner)
        return owner_fn if owner_fn and owner_fn.cls else None

    def this_ref(self, file: str, fid: str, member: str) -> Ref | None:
        owner_fn = self.this_owner(file, fid)
        if owner_fn is None:
            return None
        # Any write to the class binding (e.g. Store.prototype.x = ...) may
        # replace a method, as it does for an explicit Store receiver.
        if any(binding.get('writes') for binding in self.metadata[file].get('bindings', [])
               if binding['kind'] == 'class' and binding.get('target') == owner_fn.cls):
            return None
        static = bool(self.metadata[file]['functions'][owner_fn.id].get('static'))
        # this.member = ... (or an unnamed this[...] write) on the same receiver.
        if any(write['name'] == 'this' and write.get('member') in {member, None}
               and self.this_receiver(file, write['scope']) == (owner_fn.cls, static)
               for write in self.metadata[file].get('unbound_writes', [])):
            return None
        return Ref('class' if static else 'instance', owner_fn.cls, open=True)

    def this_receiver(self, file: str, scope: str) -> tuple[str, bool] | None:
        owner_fn = self.this_owner(file, self.scope_owner(file, scope))
        if owner_fn is None:
            return None
        return owner_fn.cls, bool(self.metadata[file]['functions'][owner_fn.id].get('static'))

    def call_ref(self, fn: Function, call: Call, info: dict, temporaries: dict[str, Ref]) -> Ref | None:
        file, scope, position = fn.file, info['scope'], info['start_byte']
        # Bodies run after their declaration; module lexical initializers may
        # precede invocation even when a function's source appears before them.
        # Same-function references still respect the temporal dead zone.
        receiver = call.callee.rsplit('.', 1)[0] if call.method and '.' in call.callee else call.receiver
        if receiver == 'this' and call.method:
            base = self.this_ref(file, fn.id, call.method)
            return self.member_ref(base, call.method) if base else None
        if receiver and call.method:
            base = temporaries.get(receiver)
            if base is None:
                base = self.instance_ref(file, scope, receiver, position, fn)
            if base is None:
                base = self.expression_ref(file, scope, receiver, position, execution_owner=fn.id)
            return self.member_ref(base, call.method) if base else None
        return self.expression_ref(file, scope, call.callee, position, execution_owner=fn.id)

    def link(self) -> None:
        for fn in self.functions.values():
            meta = self.metadata[fn.file]
            temporaries: dict[str, Ref] = {}
            for call in (item for item in walk(fn.body) if isinstance(item, Call)):
                info = meta.get('calls', {}).get(f'{fn.id}:{call.line}:{call.col}')
                if not info:
                    continue
                ref = self.call_ref(fn, call, info, temporaries)
                call.kind, call.target, call.confidence, call.external, call.io = 'unresolved', None, '', None, None
                if ref is None:
                    continue
                if info.get('construct'):
                    if ref.kind != 'class':
                        continue
                    cls = self.classes[ref.key]
                    constructors = [f for f in self.functions.values() if f.cls == cls.id and f.is_constructor]
                    if len(constructors) > 1:
                        continue
                    if constructors:
                        call.kind, call.target = 'project', constructors[0].id
                    else:
                        call.kind, call.target = 'class', cls.id
                    call.confidence = 'exact'
                    for name in call.defs:
                        temporaries[name] = Ref('instance', cls.id)
                elif ref.kind == 'function':
                    call.kind, call.target, call.confidence = 'project', ref.key, 'exact'
                elif ref.kind == 'external':
                    call.kind, call.external, call.confidence = 'external', ref.key, 'exact'
                    call.io = io_for(ref.key)


def io_for(name: str) -> tuple[str, str] | None:
    """Small catalog keyed only by justified global/import resolution."""
    if name in {'console.log', 'console.info', 'console.warn', 'console.error', 'console.debug'}:
        return 'console', 'out'
    if name == 'fetch':
        return 'net', 'in'
    module, separator, method = name.rpartition('.')
    if separator and module.removeprefix('node:') in {'fs', 'fs/promises'}:
        if method in {'readFile', 'readFileSync', 'createReadStream', 'readdir', 'readdirSync'}:
            return 'file', 'in'
        if method in {'writeFile', 'writeFileSync', 'appendFile', 'appendFileSync', 'createWriteStream'}:
            return 'file', 'out'
    if separator and module.removeprefix('node:') in {'http', 'https'} and method in {'get', 'request'}:
        return 'net', 'in'
    return None
