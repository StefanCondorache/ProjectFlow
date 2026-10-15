"""Conservative Java type and method resolution.

Type names resolve in Java's order (type parameters, enclosing and inherited
member types, local types, same file, single-type imports, same package,
on-demand imports, java.lang). Calls resolve from static evidence only: the
declared type of the receiver selects the method by name, arity and known
argument types. Declared types are sound supertypes of the runtime class, so
dispatch is the only uncertainty: interface and abstract targets, and methods
a loaded subtype overrides, stay unresolved. Nothing falls back to a
project-wide method-name guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flowmap.ir import Call, Module, walk

JAVA_LANG = {'String', 'Object', 'Integer', 'Long', 'Double', 'Float', 'Short', 'Byte', 'Character',
             'Boolean', 'Number', 'Math', 'StrictMath', 'System', 'Thread', 'Runnable', 'StringBuilder',
             'StringBuffer', 'CharSequence', 'Iterable', 'Comparable', 'AutoCloseable', 'Enum', 'Record',
             'Class', 'Void', 'Exception', 'RuntimeException', 'Error', 'Throwable',
             'IllegalArgumentException', 'IllegalStateException', 'NullPointerException',
             'UnsupportedOperationException', 'IndexOutOfBoundsException', 'ArithmeticException',
             'InterruptedException', 'ClassCastException', 'NumberFormatException', 'Process',
             'ProcessBuilder', 'Runtime'}
PRIMITIVES = {'int', 'long', 'short', 'byte', 'char', 'float', 'double', 'boolean', 'void'}
WIDENING = {'byte': {'short', 'int', 'long', 'float', 'double'}, 'short': {'int', 'long', 'float', 'double'},
            'char': {'int', 'long', 'float', 'double'}, 'int': {'long', 'float', 'double'},
            'long': {'float', 'double'}, 'float': {'double'}}
BOXES = {'int': 'Integer', 'long': 'Long', 'double': 'Double', 'float': 'Float', 'short': 'Short',
         'byte': 'Byte', 'char': 'Character', 'boolean': 'Boolean'}
UNBOX = {box: primitive for primitive, box in BOXES.items()}
# Final java.lang types: their complete supertype sets are known.
_NUMBERS = {'Integer', 'Long', 'Double', 'Float', 'Short', 'Byte'}
LIBRARY_SUPERS = {**{n: {'Number', 'Comparable', 'java.io.Serializable', 'Serializable', 'Object'} for n in _NUMBERS},
                  'Character': {'Comparable', 'java.io.Serializable', 'Serializable', 'Object'},
                  'Boolean': {'Comparable', 'java.io.Serializable', 'Serializable', 'Object'},
                  'String': {'CharSequence', 'Comparable', 'java.io.Serializable', 'Serializable', 'Object'},
                  'Class': {'java.lang.reflect.Type', 'Type', 'java.io.Serializable', 'Serializable', 'Object'}}
FINAL_LIBRARY = set(LIBRARY_SUPERS)


@dataclass(frozen=True)
class TypeRef:
    kind: str  # project | external
    key: str  # class id, or qualified name for external types


def link_java(root: Path, modules: dict[str, Module]) -> 'JavaLinker':
    linker = JavaLinker(root, modules)
    linker.link()
    return linker


class JavaLinker:
    def __init__(self, root: Path, modules: dict[str, Module]):
        self.root, self.modules = root, modules
        self.meta = {file: module.metadata.get('java', {}) for file, module in modules.items()}
        self.types: dict[str, dict] = {}
        self.type_file: dict[str, str] = {}
        self.by_fqn: dict[str, str] = {}
        self.package_types: dict[str, dict[str, str]] = {}
        self.nested: dict[str, dict[str, str]] = {}
        self.local_types: dict[str, dict[str, str]] = {}
        self.functions = {fid: fn for module in modules.values() for fid, fn in module.functions.items()}
        self.fmeta: dict[str, dict] = {}
        self.methods: dict[str, dict[str, list[str]]] = {}
        self.ctors: dict[str, list[str]] = {}
        self.ambiguous: set[str] = set()  # FQNs declared more than once (e.g. several source roots)
        for file, meta in self.meta.items():
            package = meta.get('package', '')
            for cid, info in meta.get('types', {}).items():
                self.types[cid], self.type_file[cid] = info, file
                if info['anonymous']:
                    continue
                if info['scope_fid']:
                    self.local_types.setdefault(info['scope_fid'], {})[info['name']] = cid
                    continue
                if info['fqn'] in self.by_fqn:
                    self.ambiguous.add(info['fqn'])
                self.by_fqn.setdefault(info['fqn'], cid)
                if info['outer']:
                    self.nested.setdefault(info['outer'], {})[info['name']] = cid
                else:
                    self.package_types.setdefault(package, {}).setdefault(info['name'], cid)
            for fid, info in meta.get('functions', {}).items():
                self.fmeta[fid] = info
                if info['kind'] == 'constructor':
                    self.ctors.setdefault(info['cls'], []).append(fid)
                elif info['kind'] == 'method':
                    self.methods.setdefault(info['cls'], {}).setdefault(self.functions[fid].name, []).append(fid)
        self.packages = set(self.package_types)
        self._supers: dict[str, list[TypeRef]] = {}
        self._subtypes: dict[str, set[str]] | None = None
        self._signatures: dict[str, tuple[str, ...]] = {}
        self.results: dict[str, TypeRef | None] = {}

    # ── types ──────────────────────────────────────────────────────────────

    def file_of(self, cid: str) -> str:
        return self.type_file[cid]

    def enclosing(self, cid: str | None):
        while cid is not None:
            yield cid
            cid = self.types[cid]['outer']

    def scope_fids(self, fid: str | None):
        """A function and the functions whose locals and local types it can see."""
        while fid is not None and fid in self.fmeta:
            yield fid
            info = self.fmeta[fid]
            cls = info.get('cls')
            fid = info.get('outer_fid') or (self.types[cls]['scope_fid'] if info['kind'] != 'lambda' and cls else None)

    def type_ref(self, file: str, name: str | None, cid: str | None = None, fid: str | None = None) -> TypeRef | None:
        if not name or name.endswith('[]') or name in PRIMITIVES or name == 'var':
            return None
        if '.' in name:
            if name in self.ambiguous:
                return None
            if name in self.by_fqn:
                return TypeRef('project', self.by_fqn[name])
            first, rest = name.split('.', 1)
            head = self.type_ref(file, first, cid, fid)
            if head is None:
                package = name.rsplit('.', 1)[0]
                if any(p == package or p.startswith(package + '.') for p in self.packages):
                    return None  # a missing project type, not a library one
                return TypeRef('external', name) if first[:1].islower() else None
            for part in rest.split('.'):
                if head.kind == 'external':
                    return TypeRef('external', f'{head.key}.{rest}')
                found = self.member_type(head.key, part)
                if found is None:
                    return None
                head = TypeRef('project', found)
            return head
        # Type parameters shadow everything.
        for f in self.scope_fids(fid):
            if name in self.fmeta[f].get('type_params', []):
                return None
        context = cid
        for c in self.enclosing(context):
            if name in self.types[c]['type_params']:
                return None
        for f in self.scope_fids(fid):
            if name in self.local_types.get(f, {}):
                return TypeRef('project', self.local_types[f][name])
        for c in self.enclosing(context):
            if self.types[c]['name'] == name and not self.types[c]['anonymous']:
                return TypeRef('project', c)
            found = self.member_type(c, name)
            if found:
                return TypeRef('project', found)
        meta = self.meta.get(file, {})
        package = meta.get('package', '')
        for c, info in meta.get('types', {}).items():
            if info['outer'] is None and not info['scope_fid'] and not info['anonymous'] and info['name'] == name:
                return TypeRef('project', c)
        imports = meta.get('imports', [])
        for imp in imports:
            if not imp['static'] and not imp['wildcard'] and imp['name'].rsplit('.', 1)[-1] == name:
                if imp['name'] in self.ambiguous:
                    return None
                cid_ = self.by_fqn.get(imp['name'])
                return TypeRef('project', cid_) if cid_ else TypeRef('external', imp['name'])
        if name in self.package_types.get(package, {}):
            cid_ = self.package_types[package][name]
            return None if self.types[cid_]['fqn'] in self.ambiguous else TypeRef('project', cid_)
        project, outside = set(), []
        for imp in imports:
            if imp['static'] or not imp['wildcard']:
                continue
            target = imp['name']
            if target in self.packages:
                if name in self.package_types[target]:
                    project.add(self.package_types[target][name])
            elif target in self.by_fqn:
                found = self.member_type(self.by_fqn[target], name)
                if found:
                    project.add(found)
            else:
                outside.append(target)
        if len(project) == 1 and not outside:
            found = project.pop()
            return None if self.types[found]['fqn'] in self.ambiguous else TypeRef('project', found)
        if project:
            return None  # ambiguous, or a library wildcard might also declare it
        if name in JAVA_LANG and not outside:
            return TypeRef('external', f'java.lang.{name}')
        if outside:
            # A library type from one of the wildcards or java.lang: which one is unknown.
            return TypeRef('external', name)
        return None

    def member_type(self, cid: str, name: str) -> str | None:
        """A member type declared in cid or inherited from a project supertype."""
        seen = set()
        stack = [cid]
        while stack:
            c = stack.pop(0)
            if c in seen:
                continue
            seen.add(c)
            if name in self.nested.get(c, {}):
                return self.nested[c][name]
            stack += [ref.key for ref in self.supertypes(c) if ref.kind == 'project']
        return None

    def supertypes(self, cid: str) -> list[TypeRef]:
        if cid in self._supers:
            return self._supers[cid]
        self._supers[cid] = []  # cycle guard
        info, file = self.types[cid], self.type_file[cid]
        outer = info['outer']
        refs = []
        names = ([info['superclass']] if info['superclass'] else []) + info['interfaces']
        for name in names:
            # Heritage is resolved in the declaring scope (outer class, enclosing method).
            ref = self.type_ref(file, name, outer, info['scope_fid'])
            refs.append(ref or TypeRef('external', f'?{name}'))
        if not info['superclass'] and info['kind'] in {'class', 'enum', 'record'}:
            refs.insert(0, TypeRef('external', {'enum': 'java.lang.Enum', 'record': 'java.lang.Record'}.get(info['kind'], 'java.lang.Object')))
        self._supers[cid] = refs
        return refs

    def superclass(self, cid: str) -> TypeRef | None:
        info = self.types[cid]
        if info['kind'] == 'interface':
            return None
        refs = self.supertypes(cid)
        return refs[0] if refs else None

    def subtypes(self, cid: str) -> set[str]:
        if self._subtypes is None:
            direct: dict[str, set[str]] = {}
            for c in self.types:
                for ref in self.supertypes(c):
                    if ref.kind == 'project':
                        direct.setdefault(ref.key, set()).add(c)
            self._subtypes = {}
            for c in self.types:
                found, stack = set(), list(direct.get(c, ()))
                while stack:
                    s = stack.pop()
                    if s not in found:
                        found.add(s)
                        stack += direct.get(s, ())
                self._subtypes[c] = found
        return self._subtypes.get(cid, set())

    def is_subtype(self, file: str, sub: str | None, sup: str | None) -> bool:
        a, b = self.type_ref(file, sub), self.type_ref(file, sup)
        return bool(a and b and a.kind == b.kind == 'project' and (a.key == b.key or a.key in self.subtypes(b.key)))

    # ── methods ────────────────────────────────────────────────────────────

    def chain(self, cid: str) -> tuple[list[str], list[TypeRef]]:
        """Project types in member-lookup order (superclass chain first, then
        interfaces) and the unresolved/library supertypes met on the way."""
        classes, seen, c = [], set(), cid
        while c is not None and c not in seen:
            seen.add(c)
            classes.append(c)
            sup = self.superclass(c)
            c = sup.key if sup is not None and sup.kind == 'project' else None
        order, outside, queue = list(classes), [], []
        for c in classes:
            for ref in self.supertypes(c):
                if ref.kind != 'project':
                    outside.append(ref)
                elif ref.key not in seen:
                    queue.append(ref.key)
        while queue:
            c = queue.pop(0)
            if c in seen:
                continue
            seen.add(c)
            order.append(c)
            for ref in self.supertypes(c):
                (queue if ref.kind == 'project' else outside).append(ref.key if ref.kind == 'project' else ref)
        return order, outside

    def signature(self, fid: str) -> tuple[str, ...]:
        """Parameter types resolved to stable keys; type variables become '*'."""
        if fid not in self._signatures:
            fn, info = self.functions[fid], self.fmeta[fid]
            keys = []
            for t in info['param_types']:
                base = t[:-2] if t and t.endswith('[]') else t
                if self.type_variable(fid, base):
                    keys.append('*')
                    continue
                ref = self.type_ref(fn.file, base, info.get('cls'), fid)
                key = ref.key if ref is not None else (base or '').removeprefix('java.lang.')
                keys.append(key + ('[]' if t and t.endswith('[]') else ''))
            self._signatures[fid] = tuple(keys)
        return self._signatures[fid]

    def same_signature(self, a: str, b: str) -> bool:
        x, y = self.signature(a), self.signature(b)
        return len(x) == len(y) and all(p == q or '*' in (p, q) for p, q in zip(x, y))

    def type_variable(self, fid: str, name: str | None) -> bool:
        if not name:
            return False
        if any(name in self.fmeta[f].get('type_params', []) for f in self.scope_fids(fid)):
            return True
        return any(name in self.types[c]['type_params'] for c in self.enclosing(self.fmeta[fid].get('cls')))

    def candidates(self, cid: str, name: str) -> tuple[list[str], list[TypeRef]]:
        order, outside = self.chain(cid)
        found: list[str] = []
        for c in order:
            # A subtype's override hides the method it overrides; overloads in one class all count.
            inherited = list(found)
            for fid in self.methods.get(c, {}).get(name, []):
                if not any(self.same_signature(fid, other) for other in inherited):
                    found.append(fid)
        return found, outside

    def accepts(self, file: str, arg: str | None, param: str | None, loose: bool = True) -> bool | None:
        """Whether an argument of static type ``arg`` can be passed to ``param``.

        True/False when provable, None when it depends on library type
        relationships this analysis does not know. ``loose`` allows boxing
        and unboxing (Java's second overload phase).
        """
        if arg is None or param is None:
            return None
        a, p = arg.removeprefix('java.lang.'), param.removeprefix('java.lang.')
        if a == p:
            return True
        if a == 'null':
            return p not in PRIMITIVES
        if a in PRIMITIVES:
            if p in PRIMITIVES:
                return p in WIDENING.get(a, set())
            if not loose:
                return False
            box = BOXES.get(a)
            return p == 'Object' or p == box or p in LIBRARY_SUPERS.get(box, ())  # box types are final
        if p in PRIMITIVES:
            unboxed = UNBOX.get(a)
            if unboxed is not None:
                return loose and (unboxed == p or p in WIDENING.get(unboxed, set()))
            return False if a in FINAL_LIBRARY or self.project_type(file, a) else None
        if p == 'Object':
            return True
        if a.endswith('[]') or p.endswith('[]'):
            return None if a.endswith('[]') and p.endswith('[]') else (None if p in {'Cloneable', 'java.io.Serializable', 'Serializable'} else False)
        if a in FINAL_LIBRARY:
            return p in LIBRARY_SUPERS.get(a, ())
        if a == 'Object':
            return False  # p is not Object here
        source, target = self.type_ref(file, a), self.type_ref(file, p)
        if source is None or target is None:
            return None
        if source.kind == 'project' and target.kind == 'project':
            return source.key == target.key or source.key in self.subtypes(target.key)
        if source.kind == 'external' and target.kind == 'project':
            return False  # library classes cannot extend project classes
        if source.kind == 'external':
            return True if source.key == target.key else None
        # A project class passed as a library type: provable through its declared supertypes only.
        order, outside = self.chain(source.key)
        return True if any(ref.key == target.key for ref in outside) else None

    def project_type(self, file: str, name: str) -> bool:
        ref = self.type_ref(file, name)
        return ref is not None and ref.kind == 'project'

    def select(self, file: str, found: list[str], arity: int, arg_types: list[str | None]) -> str | None:
        """Java's overload phases: strict, then boxing, then variable arity.

        A phase that might apply (some candidate is possibly applicable) ends
        the search; within it the most specific candidate must be provable.
        """
        fixed = [fid for fid in found if len(self.fmeta[fid]['param_types']) == arity]
        variable = [fid for fid in found if self.fmeta[fid]['varargs'] and arity >= len(self.fmeta[fid]['param_types']) - 1]
        phases = [(fixed, False, False), (fixed, True, False), (variable, True, True)]
        possible = {fid for group, loose, expand in phases for fid in group
                    if self.applicable(fid, arg_types, loose, expand) is not False}
        if len(possible) == 1:
            return possible.pop()  # the source compiles, so the only possible candidate applies
        for group, loose, expand in phases:
            status = {fid: self.applicable(fid, arg_types, loose, expand) for fid in group}
            viable = [fid for fid in group if status[fid] is not False]
            if not viable:
                continue
            best = [a for a in viable if all(a == b or self.applicable(b, self.param_view(a, len(arg_types), expand), loose, expand) is True
                                             for b in viable)]
            return best[0] if len(best) == 1 and status[best[0]] is True else None
        return None

    def param_view(self, fid: str, arity: int, expand: bool) -> list[str | None]:
        params = list(self.fmeta[fid]['param_types'])
        if expand and params:
            last = params[-1][:-2] if params[-1].endswith('[]') else params[-1]
            params = params[:-1] + [last] * max(arity - len(params) + 1, 0)
        return params

    def applicable(self, fid: str, arg_types: list[str | None], loose: bool = True, expand: bool = False) -> bool | None:
        file = self.functions[fid].file
        params = self.param_view(fid, len(arg_types), expand)
        if len(params) != len(arg_types):
            return False
        result: bool | None = True
        for arg, param in zip(arg_types, params):
            base = param[:-2] if param and param.endswith('[]') else param
            verdict = None if self.type_variable(fid, base) else self.accepts(file, arg, param, loose)
            if verdict is False:
                return False
            if verdict is None:
                result = None
        return result

    def overridden(self, fid: str, static_type: str) -> bool:
        name = self.functions[fid].name
        return any(self.same_signature(fid, other)
                   for sub in self.subtypes(static_type)
                   for other in self.methods.get(sub, {}).get(name, []))

    def exact(self, fid: str, static_type: str) -> bool:
        info = self.fmeta[fid]
        if info['abstract']:
            return False
        if info['static'] or 'private' in info['modifiers'] or 'final' in info['modifiers']:
            return True
        declaring = self.types[info['cls']]
        # Enum constants with bodies are anonymous subclasses that may override.
        if 'final' in declaring['modifiers'] or declaring['kind'] == 'record' or (
                declaring['kind'] == 'enum' and not declaring.get('constant_bodies')):
            return True
        if self.types[static_type]['kind'] == 'interface' or declaring.get('constant_bodies'):
            return False
        return not self.overridden(fid, static_type)

    def returns(self, fid: str) -> TypeRef | None:
        fn = self.functions[fid]
        return self.type_ref(fn.file, self.fmeta[fid].get('returns'), self.fmeta[fid]['cls'], fid)

    def field_type(self, cid: str, name: str) -> TypeRef | None:
        for c in self.chain(cid)[0]:
            type_text = self.types[c]['fields'].get(name)
            if type_text:
                return self.type_ref(self.type_file[c], type_text, c)
        return None

    # ── calls ──────────────────────────────────────────────────────────────

    def context(self, fid: str) -> str | None:
        return self.fmeta[fid].get('cls')

    def receiver_type(self, fn, record: dict) -> TypeRef | None:
        fid, cid = fn.id, self.context(fn.id)
        kind = record['receiver']
        if kind == 'call':
            return self.results.get(record['receiver_call'])
        if kind == 'captured':
            # The local/anonymous class's own and inherited fields shadow the captured local.
            if cid is not None and any(record['receiver_text'] in self.types[c]['fields'] for c in self.chain(cid)[0]):
                return self.field_type(cid, record['receiver_text'])
            if cid is not None and any(ref.kind != 'project' and ref.key not in {'java.lang.Object', 'java.lang.Enum', 'java.lang.Record'}
                                       for ref in self.chain(cid)[1]):
                return None  # a library supertype might declare that field
            return self.type_ref(fn.file, record['receiver_type'], cid, fid)
        if kind in {'name', 'expr'} or (kind == 'field' and record['receiver_type']):
            return self.type_ref(fn.file, record['receiver_type'], cid, fid)
        if kind == 'field':
            for c in self.enclosing(cid):
                found = self.field_type(c, record['receiver_text'])
                if found:
                    return found
            return None
        if kind == 'type':
            parts = record['receiver_text'].split('.')
            if parts[0][:1].islower():
                # a.b.c rooted at an (inherited) field rather than a package
                head = next((found for c in self.enclosing(cid) if (found := self.field_type(c, parts[0]))), None)
                if head is not None:
                    for i, field in enumerate(parts[1:], 1):
                        if head.kind == 'external':
                            return TypeRef('external', f"{head.key}.{'.'.join(parts[i:])}")
                        head = self.field_type(head.key, field)
                        if head is None:
                            return None
                    return head
            for i in range(len(parts), 0, -1):
                head = self.type_ref(fn.file, '.'.join(parts[:i]), cid, fid)
                if head is None:
                    continue
                for field in parts[i:]:
                    if head.kind == 'external':
                        return TypeRef('external', f"{head.key}.{'.'.join(parts[parts.index(field):])}")
                    head = self.field_type(head.key, field)
                    if head is None:
                        return None
                return head
        return None

    def resolve(self, fn, call: Call, record: dict) -> TypeRef | None:
        """Fill call resolution; return the static type of the call's result."""
        file, cid, fid = fn.file, self.context(fn.id), fn.id
        kind, name = record['kind'], record['name']
        arity = record['arity']
        arg_types = [self.result_type(key) if t is None and key else self.canonical(file, t, cid, fid)
                     for t, key in zip(record['arg_types'], record.get('arg_calls') or [None] * arity)]
        if kind == 'new':
            if record.get('anonymous'):
                anonymous = record['anonymous']
                ctors = self.ctors.get(anonymous, [])
                call.kind, call.target, call.confidence = ('project', ctors[0], 'exact') if ctors else ('class', anonymous, 'exact')
                return TypeRef('project', anonymous)
            ref = self.type_ref(file, record['type'], cid, fid)
            if ref is None:
                return None
            if ref.kind == 'external':
                self.external(call, ref.key)
                return ref
            return self.construct(call, ref.key, arity, arg_types, file)
        if kind in {'super-ctor', 'this-ctor'}:
            if cid is None:
                return None
            ref = TypeRef('project', cid) if kind == 'this-ctor' else self.superclass(cid)
            if ref is not None and ref.kind == 'project':
                self.construct(call, ref.key, arity, arg_types, file)
            return None
        receiver = record['receiver']
        if receiver == 'none':
            # The innermost class with a member of that name wins (Java's comb rule).
            enclosing = list(self.enclosing(cid))
            for index, c in enumerate(enclosing):
                if self.candidates(c, name)[0]:
                    return self.method(call, c, name, arity, arg_types, file, virtual=True)
                library = self.library_superclass(c)
                if library is not None:
                    # An unknown or library superclass may declare it; so may an
                    # outer class or a static import, so only a lone source is named.
                    elsewhere = any(self.candidates(o, name)[0] for o in enclosing[index + 1:]) or \
                        any(imp['static'] and (imp['wildcard'] or imp['name'].rsplit('.', 1)[-1] == name)
                            for imp in self.meta[file].get('imports', []))
                    if not library.startswith('?') and not elsewhere:
                        self.external(call, f'{library}.{name}')
                    return None
            return self.static_import(call, fn, name, arity, arg_types)
        if receiver == 'this':
            return self.method(call, cid, name, arity, arg_types, file, virtual=True) if cid else None
        if receiver == 'super':
            sup = self.superclass(cid) if cid else None
            if sup is None:
                return None
            if sup.kind == 'external':
                self.external(call, f'{sup.key}.{name}')
                return None
            return self.method(call, sup.key, name, arity, arg_types, file, virtual=False)
        ref = self.receiver_type(fn, record)
        if ref is None:
            return None
        if ref.kind == 'external':
            self.external(call, f'{ref.key}.{name}')
            return None
        return self.method(call, ref.key, name, arity, arg_types, file, virtual=receiver != 'type')

    def canonical(self, file: str, type_text: str | None, cid: str | None, fid: str) -> str | None:
        """An argument type made independent of the caller's file (callees judge it in theirs)."""
        if type_text is None or type_text in PRIMITIVES or type_text == 'null':
            return type_text
        base, array = (type_text[:-2], '[]') if type_text.endswith('[]') else (type_text, '')
        if base.removeprefix('java.lang.') in FINAL_LIBRARY:
            return type_text
        ref = self.type_ref(file, base, cid, fid)
        if ref is None:
            return None
        return (self.types[ref.key]['fqn'] if ref.kind == 'project' else ref.key) + array

    def result_type(self, key: str) -> str | None:
        """Type text of a linked call's result, usable as argument evidence."""
        ref = self.results.get(key)
        if ref is None:
            return None
        return self.types[ref.key]['fqn'] if ref.kind == 'project' else ref.key

    def construct(self, call: Call, cid: str, arity: int, arg_types, file: str) -> TypeRef:
        ctors = self.ctors.get(cid, [])
        if not ctors:
            if arity == 0:
                call.kind, call.target, call.confidence = 'class', cid, 'exact'
            return TypeRef('project', cid)
        chosen = self.select(file, ctors, arity, arg_types)
        if chosen:
            call.kind, call.target, call.confidence = 'project', chosen, 'exact'
        return TypeRef('project', cid)

    def library_superclass(self, cid: str) -> str | None:
        """The first non-project superclass other than Object ('?Name' when unresolved)."""
        seen = set()
        while cid not in seen:
            seen.add(cid)
            sup = self.superclass(cid)
            if sup is None:
                return None
            if sup.kind == 'project':
                cid = sup.key
                continue
            return None if sup.key in {'java.lang.Object', 'java.lang.Enum', 'java.lang.Record'} else sup.key
        return None

    def method(self, call: Call, cid: str, name: str, arity: int, arg_types, file: str,
               virtual: bool) -> TypeRef | None:
        found, _ = self.candidates(cid, name)
        chosen = self.select(file, found, arity, arg_types) if found else None
        if chosen is not None and self.library_superclass(cid) is not None and not self.no_better_library_overload(chosen, arg_types):
            chosen = None  # a library (or unresolved) superclass may declare a more specific overload
        if chosen is None:
            library = self.library_superclass(cid)
            if not found and library and not library.startswith('?'):
                self.external(call, f'{library}.{name}')
            return None
        if not virtual or self.exact(chosen, cid):
            call.kind, call.target, call.confidence = 'project', chosen, 'exact'
        return self.returns(chosen)

    def no_better_library_overload(self, fid: str, arg_types: list[str | None]) -> bool:
        """A library overload cannot be more specific than ``fid`` for these arguments:
        each argument is a project type (libraries cannot name project types) or
        exactly the declared parameter type."""
        params = self.param_view(fid, len(arg_types), self.fmeta[fid]['varargs'] and len(arg_types) != len(self.fmeta[fid]['param_types']))
        for arg, param in zip(arg_types, params):
            if arg is None:
                return False
            if arg in self.by_fqn and arg not in self.ambiguous:
                continue
            if param is None or arg.removeprefix('java.lang.') != param.removeprefix('java.lang.'):
                return False
        return True

    def static_import(self, call: Call, fn, name: str, arity: int, arg_types) -> TypeRef | None:
        """Single static imports shadow on-demand ones; several matches stay unresolved."""
        imports = [imp for imp in self.meta[fn.file].get('imports', []) if imp['static']]
        singles = [imp for imp in imports if not imp['wildcard'] and imp['name'].rsplit('.', 1)[-1] == name]
        demands = [imp for imp in imports if imp['wildcard']]
        for group in (singles, demands):
            hits: list[TypeRef | None] = []
            for imp in group:
                owner = imp['name'] if imp['wildcard'] else imp['name'].rsplit('.', 1)[0]
                ref = self.type_ref(fn.file, owner)
                if ref is not None and ref.kind == 'project':
                    if any(self.fmeta[fid]['static'] for fid in self.candidates(ref.key, name)[0]):
                        hits.append(ref)
                else:
                    hits.append(ref)  # a library or unknown owner may declare it
            if not hits:
                continue
            if len(set(hits)) > 1 or hits[0] is None:
                return None
            ref = hits[0]
            if ref.kind == 'external':
                self.external(call, f'{ref.key}.{name}')
                return None
            return self.method(call, ref.key, name, arity, arg_types, fn.file, virtual=False)
        return None

    def external(self, call: Call, name: str) -> None:
        if name.startswith('?') or '.?' in name:
            return
        call.kind, call.external, call.confidence = 'external', name, 'exact'
        call.io = io_for(name)

    def link(self) -> None:
        for fn in self.functions.values():
            calls = self.meta[fn.file].get('calls', {})
            for call in (item for item in walk(fn.body) if isinstance(item, Call)):
                key = f'{fn.id}:{call.line}:{call.col}'
                record = calls.get(key)
                call.kind, call.target, call.confidence, call.external, call.io = 'unresolved', None, '', None, None
                if record is None:
                    continue
                self.results[key] = self.resolve(fn, call, record)

    # ── map evidence ───────────────────────────────────────────────────────

    def dependencies(self, file: str) -> list[tuple[str, str, int]]:
        meta = self.meta.get(file, {})
        found: list[tuple[str, str, int]] = []
        referenced = {self.type_file[c] for c in self.referenced(file)}
        for imp in meta.get('imports', []):
            name = imp['name']
            if imp['wildcard'] and not imp['static']:
                if name in self.packages:
                    for path in sorted(p for p in referenced if self.meta[p].get('package') == name and p != file):
                        found.append((f'file:{path}', 'verified', imp['line']))
                elif name in self.by_fqn:
                    found.append((f"file:{self.type_file[self.by_fqn[name]]}", 'verified', imp['line']))
                else:
                    found.append((f'external:{name}', 'inferred', imp['line']))
                continue
            owner = name.rsplit('.', 1)[0] if imp['static'] and not imp['wildcard'] else name
            cid = self.by_fqn.get(owner)
            if cid is not None:
                if self.type_file[cid] != file:
                    found.append((f'file:{self.type_file[cid]}', 'verified', imp['line']))
            elif not any(owner == p or owner.startswith(p + '.') for p in self.packages):
                package = owner.rsplit('.', 1)[0] if '.' in owner else owner
                found.append((f'external:{package}', 'inferred', imp['line']))
        return list(dict.fromkeys(found))

    def referenced(self, file: str) -> set[str]:
        """Project classes this file's resolved calls reach."""
        found = set()
        for fid, fn in self.functions.items():
            if fn.file != file:
                continue
            for call in walk(fn.body):
                if isinstance(call, Call) and call.target:
                    target = call.target
                    found.add(target if target in self.types else self.fmeta.get(target, {}).get('cls'))
        return {c for c in found if c}


def io_for(name: str) -> tuple[str, str] | None:
    """Small catalog keyed by the resolved library type and member."""
    owner, _, member = name.rpartition('.')
    if owner in {'java.lang.System.out', 'java.lang.System.err'} and member.startswith('print'):
        return 'console', 'out'
    if name == 'java.lang.System.getenv':
        return 'env', 'in'
    if owner == 'java.util.Scanner' and (member.startswith('next') or member.startswith('has')):
        return 'console', 'in'
    if owner == 'java.nio.file.Files':
        if member.startswith('read') or member in {'lines', 'newBufferedReader', 'newInputStream'}:
            return 'file', 'in'
        if member.startswith('write') or member in {'newBufferedWriter', 'newOutputStream'}:
            return 'file', 'out'
    if owner == 'java.net.http.HttpClient' and member.startswith('send'):
        return 'net', 'in'
    if name in {'java.io.FileReader', 'java.io.FileInputStream'}:
        return 'file', 'in'
    if name in {'java.io.FileWriter', 'java.io.FileOutputStream'}:
        return 'file', 'out'
    return None
