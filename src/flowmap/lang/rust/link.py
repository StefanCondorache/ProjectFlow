"""Conservative Rust crate, path and method resolution.

Crates come from Cargo.toml manifests and Cargo's layout conventions; each
file's module path follows the ``mod`` declarations from its crate root.
Paths resolve through each module's own namespace (items, ``use`` imports,
single-provider globs), ``crate``/``self``/``super`` and workspace crates by
name; ``std``/``core``/``alloc`` and unknown crates are external. Method
calls resolve only on a known concrete receiver type: inherent methods, then
a unique trait impl, then a unique project trait default. Trait objects,
generics, smart-pointer wrappers and unknown receivers stay unresolved.
"""
from __future__ import annotations

import posixpath
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from flowmap.discover import discover_files
from flowmap.inventory import read_text
from flowmap.ir import Call, Module, walk

from .syntax import deref

STD_ROOTS = {'std', 'core', 'alloc'}
PRELUDE = {'Some': 'Option', 'None': 'Option', 'Ok': 'Result', 'Err': 'Result', 'Box': 'Box',
           'Vec': 'Vec', 'String': 'String', 'Option': 'Option', 'Result': 'Result', 'ToString': 'ToString',
           'Default': 'Default', 'Clone': 'Clone', 'Iterator': 'Iterator', 'From': 'From', 'Into': 'Into',
           'drop': 'drop'}
# Types whose own methods shadow the wrapped value's through Deref.
WRAPPERS = {'Box', 'Rc', 'Arc', 'RefCell', 'Cell', 'Mutex', 'RwLock', 'Ref', 'RefMut', 'MutexGuard',
            'RwLockReadGuard', 'RwLockWriteGuard', 'Cow', 'Pin', 'ManuallyDrop', 'Weak'}
# std trait functions whose result is Self (often derived, so no project body exists).
SELF_RETURNING = {('assoc', 'default', 0), ('assoc', 'from', 1), ('method', 'clone', 0)}
PRIMITIVES = {'i8', 'i16', 'i32', 'i64', 'i128', 'isize', 'u8', 'u16', 'u32', 'u64', 'u128', 'usize',
              'f32', 'f64', 'bool', 'char', 'str', '()'}


@dataclass(frozen=True)
class Ref:
    kind: str  # project | external
    key: str  # type id (struct/enum/union/trait), or a path for external


@dataclass
class Crate:
    id: str
    name: str
    root: str
    kind: str  # lib | bin | example | test | bench | build
    dir: str
    bin_name: str = ''
    modules: dict[str, str] = field(default_factory=dict)  # file -> module path


def find_crates(root: Path, modules: dict[str, Module]) -> list[Crate]:
    """Crate roots from Cargo.toml manifests and Cargo's conventional layout."""
    crates: list[Crate] = []
    covered: set[str] = set()

    def add(name: str, root: str, kind: str, directory: str, bin_name: str = '') -> None:
        if root in modules and root not in covered:
            covered.add(root)
            crates.append(Crate(f'c{len(crates)}', name.replace('-', '_'), root, kind, directory, bin_name))

    for manifest in sorted(discover_files(root, extensions=(), patterns=('Cargo.toml',))):
        source = read_text(root, manifest)
        try:
            data = tomllib.loads(source or '')
        except tomllib.TOMLDecodeError:
            continue
        directory = posixpath.dirname(manifest)
        package = data.get('package', {}) if isinstance(data.get('package'), dict) else {}
        name = str(package.get('name') or posixpath.basename(directory) or 'crate')
        at = lambda path: posixpath.normpath(posixpath.join(directory, path))  # noqa: E731
        lib = data.get('lib') if isinstance(data.get('lib'), dict) else {}
        add(str(lib.get('name') or name), at(str(lib.get('path') or 'src/lib.rs')), 'lib', directory)
        for target in data.get('bin', []) if isinstance(data.get('bin'), list) else []:
            if isinstance(target, dict) and target.get('path'):
                add(name, at(str(target['path'])), 'bin', directory, str(target.get('name') or name))
        add(name, at('src/main.rs'), 'bin', directory, name)
        for kind, folder in (('bin', 'src/bin'), ('example', 'examples'), ('test', 'tests'), ('bench', 'benches')):
            prefix = at(folder) + '/'
            for file in sorted(modules):
                if file.startswith(prefix):
                    rest = file[len(prefix):]
                    if '/' not in rest:
                        add(name, file, kind, directory, rest[:-3])
                    elif rest.count('/') == 1 and rest.endswith('/main.rs'):
                        add(name, file, kind, directory, rest.split('/')[0])
        add(name, at('build.rs'), 'build', directory)
    # Without a manifest, conventional roots still form crates.
    for file in sorted(modules):
        if posixpath.basename(file) in {'main.rs', 'lib.rs'} and file not in covered:
            directory = posixpath.dirname(file)
            owner = posixpath.dirname(directory) if posixpath.basename(directory) == 'src' else directory
            kind = 'lib' if file.endswith('lib.rs') else 'bin'
            if not any(file.startswith(c.dir + '/') for c in crates if c.dir) or kind == 'lib':
                add(posixpath.basename(owner) or 'crate', file, kind, owner, posixpath.basename(owner) or 'main')
    return crates


def link_rust(root: Path, modules: dict[str, Module]) -> 'RustLinker':
    linker = RustLinker(root, modules)
    linker.link()
    return linker


class RustLinker:
    def __init__(self, root: Path, modules: dict[str, Module]):
        self.root, self.modules = root, modules
        self.meta = {file: module.metadata.get('rust', {}) for file, module in modules.items()}
        self.functions = {fid: fn for module in modules.values() for fid, fn in module.functions.items()}
        self.fmeta = {fid: info for meta in self.meta.values() for fid, info in meta.get('functions', {}).items()}
        self.crates = find_crates(root, modules)
        self.file_module: dict[str, str] = {}  # file -> absolute module path ('c0::store')
        self.mod_edges: list[tuple[str, str, int]] = []  # (file, child file, line)
        for crate in self.crates:
            self.walk_modules(crate)
        for file, module in modules.items():
            if file not in self.file_module:
                self.file_module[file] = f'?{file}'
                module.diagnostics.append({'code': 'unreachable-module', 'line': 1, 'col': 0, 'end_line': 1, 'end_col': 0,
                                           'severity': 'warning', 'message': 'No crate root declares this module; its paths resolve only within the file.'})
        self.lib_crates: dict[str, str] = {}
        for crate in self.crates:
            if crate.kind == 'lib':
                self.lib_crates.setdefault(crate.name, crate.id)
        # Absolute item index: 'c0::store::Store' -> {'kind', 'id', 'file', ...}
        self.items: dict[str, dict] = {}
        self.uses: dict[str, list[dict]] = {}
        self.impls: list[dict] = []
        self.types: dict[str, dict] = {}
        for file, meta in self.meta.items():
            base = self.file_module[file]
            for key, item in meta.get('items', {}).items():
                path = self.absolute(base, key)
                entry = {**item, 'file': file}
                if item['kind'] == 'mod':
                    entry['module'] = path
                self.items.setdefault(path, entry)
            for use in meta.get('uses', []):
                self.uses.setdefault(self.absolute(base, use['scope']), []).append({**use, 'file': file})
            for impl in meta.get('impls', []):
                self.impls.append({**impl, 'file': file, 'module': self.absolute(base, impl['scope'])})
            for cid, info in meta.get('types', {}).items():
                self.types[cid] = {**info, 'file': file, 'module': self.absolute(base, info['scope'])}
        self.results: dict[str, Ref | None] = {}
        self._impl_types: dict[int, Ref | None] = {}

    # ── crates and modules ────────────────────────────────────────────────

    def walk_modules(self, crate: Crate) -> None:
        queue = [(crate.root, crate.id)]
        while queue:
            file, path = queue.pop(0)
            if file in self.file_module:
                continue
            self.file_module[file] = path
            crate.modules[file] = path
            directory = posixpath.dirname(file)
            stem = posixpath.basename(file)[:-3]
            own_dir = directory if file == crate.root or stem == 'mod' else posixpath.join(directory, stem)
            for decl in self.meta.get(file, {}).get('mods', []):
                if decl['inline']:
                    continue
                inline = decl['scope'].split('::')[1:]  # mods declared inside inline modules
                base = posixpath.join(own_dir, *inline) if inline else own_dir
                if decl['path_attr']:
                    candidates = [posixpath.normpath(posixpath.join(directory if not inline else base, decl['path_attr']))]
                else:
                    candidates = [posixpath.join(base, f"{decl['name']}.rs"), posixpath.join(base, decl['name'], 'mod.rs')]
                child = next((c for c in candidates if c in self.modules), None)
                if child is not None:
                    self.mod_edges.append((file, child, decl['line']))
                    queue.append((child, '::'.join([path, *inline, decl['name']])))

    def module_path(self, file: str) -> str:
        """Readable module path, e.g. 'shop_core::store::ledger'."""
        path = self.file_module.get(file, '')
        crate = next((c for c in self.crates if path == c.id or path.startswith(c.id + '::')), None)
        return path.replace(crate.id, crate.name, 1) if crate else path

    @staticmethod
    def absolute(base: str, relative: str) -> str:
        return base + relative[4:] if relative.startswith('self') else f'{base}::{relative}'

    def crate_of(self, module: str) -> str:
        return module.split('::', 1)[0]

    # ── paths ──────────────────────────────────────────────────────────────

    def resolve_path(self, path: str, module: str, fid: str | None = None, seen: frozenset = frozenset()) -> Ref | dict | None:
        """An item dict (project), Ref('external', path) or None for a path used in ``module``."""
        segments = [s for s in path.split('::') if s]
        if not segments:
            return None
        if path.startswith('::'):
            return self.from_crate_name(segments, module, seen)
        head, rest = segments[0], segments[1:]
        if head == 'crate':
            return self.walk(self.crate_of(module), rest, seen)
        if head in {'self', 'super'}:
            current = self.enclosing_module(module)
            while segments and segments[0] in {'self', 'super'}:
                if segments.pop(0) == 'super':
                    if '::' not in current:
                        return None
                    current = current.rsplit('::', 1)[0]
            return self.walk(current, segments, seen)
        if fid is not None and not rest:
            nested = self.nested(fid, head)
            if nested:
                return {'kind': 'fn', 'id': nested}
        found = self.lookup(module, head, seen)
        if found is not None:
            return self.descend(found, rest, seen)
        return self.from_crate_name(segments, module, seen)

    def from_crate_name(self, segments: list[str], module: str, seen: frozenset) -> Ref | dict | None:
        head, rest = segments[0], segments[1:]
        if head in self.lib_crates:
            return self.walk(self.lib_crates[head], rest, seen)
        if head in STD_ROOTS:
            return Ref('external', '::'.join(segments))
        if head in PRELUDE:
            return Ref('external', '::'.join([PRELUDE[head], *rest]) if head[:1].isupper() and PRELUDE[head] != head else '::'.join(segments))
        if module.startswith('?'):
            return None  # an unreachable file cannot see other crates' layout
        if head[:1].islower() and rest:
            return Ref('external', '::'.join(segments))  # a dependency crate
        return None

    def nested(self, fid: str, name: str) -> str | None:
        while fid is not None and fid in self.fmeta:
            info = self.fmeta[fid]
            if name in info.get('nested', {}):
                return info['nested'][name]
            fid = info.get('local_to')
        return None

    def lookup(self, module: str, name: str, seen: frozenset) -> Ref | dict | None:
        """A name in a module's namespace: its items, explicit imports, then single-provider globs."""
        key = ('lookup', module, name)
        if key in seen:
            return None
        seen = seen | {key}
        item = self.items.get(f'{module}::{name}')
        if item is not None:
            return item
        uses = self.uses.get(module, [])
        explicit = [u for u in uses if not u['glob'] and u['alias'] == name]
        if explicit:
            targets = {self.ref_key(self.resolve_path(u['path'], module, seen=seen)) for u in explicit}
            if len(targets) != 1:
                return None
            return self.resolve_path(explicit[0]['path'], module, seen=seen)
        provided = []
        for use in (u for u in uses if u['glob']):
            target = self.resolve_path(use['path'], module, seen=seen) if use['path'] else None
            if isinstance(target, Ref):
                provided.append(Ref('external', f"{target.key}::{name}"))  # a library glob may provide anything
            elif isinstance(target, dict) and target.get('kind') == 'mod':
                found = self.lookup(target['module'], name, seen)
                if found is not None:
                    provided.append(found)
        if len({self.ref_key(p) for p in provided}) == 1:
            return provided[0]
        if len(provided) > 1:
            return None
        if module.rsplit('::', 1)[-1].startswith('<'):
            return self.lookup(module.rsplit('::', 1)[0], name, seen)  # a function body's scope
        return None

    @staticmethod
    def enclosing_module(module: str) -> str:
        """The module a (function-body) scope belongs to."""
        while module.rsplit('::', 1)[-1].startswith('<') and '::' in module:
            module = module.rsplit('::', 1)[0]
        return module

    @staticmethod
    def ref_key(target) -> str | None:
        if isinstance(target, Ref):
            return f'ext:{target.key}'
        if isinstance(target, dict):
            return target.get('id') or target.get('module') or str(sorted(target.items()))
        return None

    def walk(self, module: str, segments: list[str], seen: frozenset) -> Ref | dict | None:
        current: Ref | dict | None = {'kind': 'mod', 'module': module}
        return self.descend(current, segments, seen)

    def descend(self, current: Ref | dict | None, segments: list[str], seen: frozenset) -> Ref | dict | None:
        for index, segment in enumerate(segments):
            if current is None:
                return None
            if isinstance(current, Ref):
                return Ref('external', '::'.join([current.key, *segments[index:]]))
            if current.get('kind') == 'mod':
                current = self.lookup(current['module'], segment, seen)
            elif current.get('kind') in {'struct', 'enum', 'union', 'trait'} and index == len(segments) - 1:
                return {'kind': 'assoc', 'type': current['id'], 'name': segment}
            else:
                return None
        return current

    # ── types ──────────────────────────────────────────────────────────────

    def type_ref(self, type_text: str | None, module: str, fid: str | None = None,
                 self_type: str | None = None) -> Ref | None:
        type_text = deref(type_text)
        if not type_text or type_text.startswith(('dyn ', 'impl ', '[', '(', '*', 'fn')):
            return None
        if type_text == 'Self':
            if self_type is None or self_type == 'Self':
                return None
            impl_module = module
            return self.type_ref(self_type, impl_module, fid)
        if fid is not None and type_text in self.fmeta.get(fid, {}).get('type_params', []):
            return None
        last = type_text.rsplit('::', 1)[-1]
        if last in WRAPPERS:
            return None
        if type_text in PRIMITIVES:
            return Ref('external', type_text)
        target = self.resolve_path(type_text, module, fid)
        if isinstance(target, dict) and target.get('kind') in {'struct', 'enum', 'union', 'trait'}:
            return Ref('project', target['id'])
        if isinstance(target, dict) and target.get('kind') == 'alias':
            return self.type_ref(target.get('type'), self.item_module(target), fid)
        if isinstance(target, Ref):
            return target
        return None

    def item_module(self, item: dict) -> str:
        return self.file_module.get(item.get('file', ''), '')

    def impl_type(self, impl: dict) -> Ref | None:
        key = id(impl)
        if key not in self._impl_types:
            if impl['type'] in impl.get('type_params', []):
                self._impl_types[key] = None  # blanket impl over a type parameter
            else:
                self._impl_types[key] = self.type_ref(impl['type'], impl['module'])
        return self._impl_types[key]

    def method(self, ref: Ref, name: str) -> str | None:
        """Inherent method first, then a unique trait impl method or project trait default."""
        if ref.kind != 'project' or self.types.get(ref.key, {}).get('kind') == 'trait':
            return None
        impls = [impl for impl in self.impls if self.impl_type(impl) == ref]
        inherent = [impl['methods'][name] for impl in impls if impl['trait'] is None and name in impl['methods']]
        if inherent:
            return inherent[0] if len(inherent) == 1 else None
        found = []
        for impl in (i for i in impls if i['trait'] is not None):
            if name in impl['methods']:
                found.append(impl['methods'][name])
                continue
            trait = self.type_ref(impl['trait'], impl['module'])
            if trait is not None and trait.kind == 'project':
                methods = self.types.get(trait.key, {}).get('methods', {})
                if name in methods:
                    found.append(methods[name])  # None for a required method: ambiguous below
            elif trait is None or trait.kind == 'external':
                found.append(None)  # a library trait may provide this method
        if len(found) == 1 and found[0] is not None:
            return found[0]
        return None

    def returns(self, fid: str) -> Ref | None:
        info = self.fmeta[fid]
        fn = self.functions[fid]
        module = self.absolute(self.file_module.get(fn.file, ''), info['scope'])
        return self.type_ref(info.get('returns'), module, fid, info.get('self_type'))

    def field_type(self, ref: Ref | None, field_name: str) -> Ref | None:
        if ref is None or ref.kind != 'project':
            return None
        info = self.types.get(ref.key, {})
        return self.type_ref(info.get('fields', {}).get(field_name), info.get('module', ''), None)

    # ── calls ──────────────────────────────────────────────────────────────

    def scope(self, fid: str) -> str:
        """The innermost scope a function's paths resolve in (its own or an enclosing body's)."""
        info, owner = self.fmeta[fid], fid
        while owner is not None and owner in self.fmeta:
            local = self.fmeta[owner].get('local_scope')
            if local:
                return self.absolute(self.file_module.get(self.functions[fid].file, ''), local)
            owner = self.fmeta[owner].get('local_to')
        return self.absolute(self.file_module.get(self.functions[fid].file, ''), info['scope'])

    def self_ref(self, fid: str) -> Ref | None:
        info = self.fmeta[fid]
        return self.type_ref(info.get('self_type'), self.scope(fid), fid) if info.get('self_type') else None

    def receiver_type(self, fid: str, record: dict) -> Ref | None:
        module, info = self.scope(fid), self.fmeta[fid]
        kind = record['receiver']
        if kind == 'self':
            return self.self_ref(fid)
        if kind == 'call':
            return self.results.get(record['receiver_call'])
        if kind == 'field':
            root, *fields = record['receiver_text'].split('.')
            ref = self.self_ref(fid) if root == 'self' else (
                self.type_ref(record['receiver_type'], module, fid, info.get('self_type')))
            for name in fields:
                ref = self.field_type(ref, name)
            return ref
        if record['receiver_type']:
            return self.type_ref(record['receiver_type'], module, fid, info.get('self_type'))
        if kind == 'name' and record.get('receiver_call'):
            return self.results.get(record['receiver_call'])
        return None

    def resolve(self, fid: str, call: Call, record: dict) -> Ref | None:
        module = self.scope(fid)
        kind = record['kind']
        if kind == 'method':
            ref = self.receiver_type(fid, record)
            if ref is None:
                return None
            if ref.kind == 'external':
                self.external(call, f"{ref.key}::{record['name']}")
                return None
            target = self.method(ref, record['name'])
            if target is None:
                return ref if ('method', record['name'], record['arity']) in SELF_RETURNING else None
            self.project(call, target)
            return self.returns(target)
        if kind != 'path':
            return None
        path = record['path']
        if path.startswith('Self::'):
            ref = self.self_ref(fid)
            target = self.method(ref, path.split('::', 1)[1]) if ref is not None and path.count('::') == 1 else None
            if target is not None:
                self.project(call, target)
                return self.returns(target)
            return None
        found = self.resolve_path(path, module, fid)
        if isinstance(found, Ref):
            self.external(call, found.key)
            return None
        if not isinstance(found, dict):
            return None
        if found.get('kind') == 'fn':
            self.project(call, found['id'])
            return self.returns(found['id'])
        if found.get('kind') == 'assoc':
            owner = Ref('project', found['type'])
            target = self.method(owner, found['name'])
            if target is not None:
                self.project(call, target)
                return self.returns(target)
            return owner if ('assoc', found['name'], record['arity']) in SELF_RETURNING else None
        if found.get('kind') == 'struct' and record['arity'] >= 0:  # tuple-struct constructor
            call.kind, call.target, call.confidence = 'class', found['id'], 'exact'
            return Ref('project', found['id'])
        return None

    def project(self, call: Call, fid: str) -> None:
        call.kind, call.target, call.confidence = 'project', fid, 'exact'

    def external(self, call: Call, name: str) -> None:
        call.kind, call.external, call.confidence = 'external', name, 'exact'
        call.io = io_for(name)

    def link(self) -> None:
        for fn in self.functions.values():
            calls = self.meta[fn.file].get('calls', {})
            for call in (item for item in walk(fn.body) if isinstance(item, Call)):
                key = f'{fn.id}:{call.line}:{call.col}'
                record = calls.get(key)
                if record is None or record['kind'] == 'macro':
                    continue
                call.kind, call.target, call.confidence, call.external, call.io = 'unresolved', None, '', None, None
                self.results[key] = self.resolve(fn.id, call, record)

    # ── map evidence ───────────────────────────────────────────────────────

    def dependencies(self, file: str) -> list[tuple[str, str, int]]:
        found = [(f'file:{child}', 'verified', at) for parent, child, at in self.mod_edges if parent == file]
        module = self.file_module.get(file, '')
        for use in self.meta.get(file, {}).get('uses', []):
            scope = self.absolute(module, use['scope'])
            target = self.resolve_path(use['path'], scope) if use['path'] else None
            if isinstance(target, Ref):
                found.append((f"external:{target.key.split('::')[0]}", 'inferred', use['line']))
            elif isinstance(target, dict):
                target_file = target.get('file') or self.module_file(target.get('module'))
                if target_file and target_file != file:
                    found.append((f'file:{target_file}', 'verified', use['line']))
            elif use['path'] and not use['path'].startswith(('crate', 'self', 'super')):
                head = use['path'].split('::')[0]
                if head[:1].islower() and head not in self.lib_crates:
                    found.append((f'external:{head}', 'inferred', use['line']))
        return list(dict.fromkeys(found))

    def module_file(self, module: str | None) -> str | None:
        return next((f for f, m in self.file_module.items() if m == module), None) if module else None


def io_for(name: str) -> tuple[str, str] | None:
    """Small catalog keyed by resolved std/known-crate paths."""
    if name in {'std::fs::read', 'std::fs::read_to_string', 'std::fs::File::open', 'std::fs::read_dir',
                'std::io::stdin', 'std::fs::metadata'}:
        return ('console', 'in') if name == 'std::io::stdin' else ('file', 'in')
    if name in {'std::fs::write', 'std::fs::File::create', 'std::fs::create_dir_all', 'std::fs::remove_file'}:
        return 'file', 'out'
    if name in {'std::env::var', 'std::env::var_os', 'std::env::vars', 'std::env::args', 'std::env::args_os'}:
        return 'env', 'in'
    if name in {'std::net::TcpStream::connect', 'reqwest::get', 'reqwest::blocking::get'}:
        return 'net', 'in'
    return None
