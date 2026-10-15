"""Parser-backed lexical linking, including deliberate unresolved boundaries."""
from pathlib import Path

import pytest

from flowmap.ir import Call, walk
from flowmap.lang.ecmascript.extract import extract_module


def linked(files: dict[str, str], root: Path):
    modules = {file: extract_module(file, source.encode()) for file, source in files.items()}
    # Before the linker exists, extraction gives the real unresolved behavior.
    try:
        from flowmap.lang.ecmascript.link import link_ecmascript
    except ModuleNotFoundError as error:
        if error.name != 'flowmap.lang.ecmascript.link':
            raise
        return modules, None
    return modules, link_ecmascript(root, modules)


def fn(modules, name, file=None):
    found = [f for m in modules.values() for f in m.functions.values()
             if f.qualname == name and (file is None or f.file == file)]
    assert len(found) == 1
    return found[0]


def calls(function):
    return [item for item in walk(function.body) if isinstance(item, Call)]


def test_nearest_lexical_function_and_alias_resolve(tmp_path):
    modules, _ = linked({'app.js': '''
function save(value) { return value; }
function outer(value) {
  function save(value) { return value; }
  const persist = save;
  return persist(value);
}
function run(value) { return save(value); }
'''}, tmp_path)
    outer = calls(fn(modules, 'outer'))[0]
    run = calls(fn(modules, 'run'))[0]
    assert (outer.kind, outer.target, outer.confidence) == ('project', fn(modules, 'outer.<locals>.save').id, 'exact')
    assert run.target == fn(modules, 'save').id


@pytest.mark.parametrize('body', [
    'function run(save) { return save(1); }',
    'function run() { let save; return save(1); }',
    'function run() { const save = unknown; return save(1); }',
    'function run() { save(1); const save = (x) => x; }',
    'function run() { const alias = save; alias = unknown; return alias(1); }',
    'function run() { if (flag) { var save = (x) => x; } return save(1); }',
])
def test_shadowing_and_reassignment_block_outer_fallback(tmp_path, body):
    modules, _ = linked({'app.js': 'function save(x) { return x; }\n' + body}, tmp_path)
    call = calls(fn(modules, 'run'))[-1]
    assert (call.kind, call.target) == ('unresolved', None)


def test_duplicate_declarations_and_unrelated_global_names_are_unresolved(tmp_path):
    modules, _ = linked({
        'a.js': 'export function save(x) { return x; }',
        'b.js': 'function save(x) { return x; } function save(x) { return x; } function run() { return save(1); }',
        'c.js': 'export function run() { return save(1); }',
    }, tmp_path)
    assert calls(fn(modules, 'run', 'b.js'))[0].target is None
    assert calls(fn(modules, 'run', 'c.js'))[0].target is None


def test_named_default_and_namespace_imports_cross_js_ts(tmp_path):
    modules, _ = linked({
        'lib.ts': 'export function save(value: number) { return value; } export default function load(value: number) { return value; }',
        'main.js': "import load, { save as persist } from './lib.js'; import * as tools from './lib.js'; export function run(v) { load(v); persist(v); return tools.save(v); }",
    }, tmp_path)
    linked_calls = calls(fn(modules, 'run'))
    assert [(c.kind, c.target, c.confidence) for c in linked_calls] == [
        ('project', fn(modules, 'load').id, 'exact'),
        ('project', fn(modules, 'save').id, 'exact'),
        ('project', fn(modules, 'save').id, 'exact'),
    ]


def test_reexports_and_literal_commonjs_resolve(tmp_path):
    modules, _ = linked({
        'lib.js': 'function save(v) { return v; } exports.save = save; module.exports.default = save;',
        'barrel.ts': "export { save as persist } from './lib.js';",
        'app.ts': "import { persist } from './barrel'; const { save: again } = require('./lib'); const tools = require('./lib'); function run(v) { persist(v); again(v); return tools.save(v); }",
    }, tmp_path)
    assert [c.target for c in calls(fn(modules, 'run'))] == [fn(modules, 'save').id] * 3


def test_star_exports_reject_conflicts_and_cycles(tmp_path):
    modules, _ = linked({
        'left.js': 'export function save() { return 1; }',
        'right.js': 'export function save() { return 2; }',
        'barrel.js': "export * from './left'; export * from './right';",
        'cycle-a.js': "export * from './cycle-b';",
        'cycle-b.js': "export * from './cycle-a';",
        'app.js': "import {save} from './barrel'; import {missing} from './cycle-a'; function run() { save(); missing(); }",
    }, tmp_path)
    assert all(c.target is None for c in calls(fn(modules, 'run')))


def test_explicit_export_wins_over_conflicting_star_exports(tmp_path):
    modules, _ = linked({
        'left.js': 'export function save() { return 1; }',
        'right.js': 'export function save() { return 2; }',
        'barrel.js': "export * from './left'; export * from './right'; export {save} from './left';",
        'app.js': "import {save} from './barrel'; function run() { return save(); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'save', 'left.js').id


@pytest.mark.parametrize('source', [
    "import type {save} from './lib'; function run() { return save(1); }",
    "const require = replacement; const {save} = require('./lib'); function run() { return save(1); }",
    "function run(require) { const tools = require('./lib'); return tools.save(1); }",
])
def test_type_only_import_and_shadowed_require_are_unresolved(tmp_path, source):
    modules, _ = linked({'lib.ts': 'export function save(v: number) { return v; }', 'app.ts': source}, tmp_path)
    assert calls(fn(modules, 'run'))[-1].target is None


def test_module_resolution_rejects_ambiguous_extension_and_directory_candidates(tmp_path):
    modules, _ = linked({
        'lib.ts': 'export function save(v: number) { return v; }',
        'lib.js': 'export function save(v) { return v; }',
        'app.js': "import {save} from './lib'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_this_static_and_constructor_evidence_resolve_without_dropping_arguments(tmp_path):
    modules, _ = linked({'app.ts': '''
class Store {
  constructor(value: number) { this.save(value); }
  save(value: number) { return value; }
  static build(value: number) { return value; }
  run(value: number) { const later = () => this.save(value); return later(); }
}
function run(value: number, typed: Store) {
  const store = new Store(value);
  store.save(value);
  typed.save(value);
  Store.build(value);
  return new Store(value).save(value);
}
'''}, tmp_path)
    assert calls(fn(modules, 'Store.constructor'))[0].target == fn(modules, 'Store.save').id
    arrow = next(f for f in modules['app.ts'].functions.values() if f.name == 'later')
    assert calls(arrow)[0].target == fn(modules, 'Store.save').id
    run_calls = calls(fn(modules, 'run'))
    assert [c.target for c in run_calls] == [
        fn(modules, 'Store.constructor').id, fn(modules, 'Store.save').id,
        fn(modules, 'Store.save').id, fn(modules, 'Store.build').id,
        fn(modules, 'Store.constructor').id, fn(modules, 'Store.save').id,
    ]
    assert fn(modules, 'Store.save').params[0].name == 'value'


def test_receiver_modes_and_unknown_factories_stay_unresolved(tmp_path):
    modules, _ = linked({'app.ts': '''
class Store { save(v: number) { return v; } static build(v: number) { return v; } }
function factory() { return new Store(); }
function run(unknown) {
  Store.save(1);
  const store = new Store();
  store.build(1);
  const dynamic = factory();
  dynamic.save(1);
  unknown.save(1);
  Store();
}
'''}, tmp_path)
    run_calls = calls(fn(modules, 'run'))
    assert [run_calls[i].target for i in (0, 2, 4, 5, 6)] == [None] * 5
    assert run_calls[1].kind == 'class'
    assert run_calls[3].target == fn(modules, 'factory').id


def test_known_import_io_and_unshadowed_builtin_boundaries_only(tmp_path):
    modules, _ = linked({'app.js': '''
import {readFile as read} from 'node:fs/promises';
import * as fs from 'node:fs';
export function run(path) { console.log(path); fetch(path); read(path); fs.writeFile(path, 'x'); }
export function shadow(console, fetch) { console.log('x'); fetch('x'); }
'''}, tmp_path)
    known = calls(fn(modules, 'run'))
    assert [(c.external, c.io) for c in known] == [
        ('console.log', ('console', 'out')), ('fetch', ('net', 'in')),
        ('node:fs/promises.readFile', ('file', 'in')), ('node:fs.writeFile', ('file', 'out')),
    ]
    assert all(c.external is None and c.io is None for c in calls(fn(modules, 'shadow')))


def test_deferred_arrow_can_reference_outer_const_but_same_scope_tdz_cannot(tmp_path):
    modules, _ = linked({'app.js': '''
const run = (value) => helper(value);
const helper = (value) => value;
function outer(v) {
  const later = () => save(v);
  const save = (x) => x;
  return later();
}
function invalid(v) { save(v); const save = (x) => x; }
'''}, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'helper').id
    assert calls(fn(modules, 'outer.<locals>.later'))[0].target == fn(modules, 'outer.<locals>.save').id
    assert calls(fn(modules, 'invalid'))[0].target is None


def test_external_default_imports_and_require_keep_source_names(tmp_path):
    modules, _ = linked({'app.js': '''
import fs from 'node:fs';
const {readFile: read} = require('node:fs/promises');
function run(path) { fs.readFileSync(path); return read(path); }
'''}, tmp_path)
    assert [(c.external, c.io) for c in calls(fn(modules, 'run'))] == [
        ('node:fs.readFileSync', ('file', 'in')),
        ('node:fs/promises.readFile', ('file', 'in')),
    ]


def test_relative_index_resolution_and_missing_local_import_stay_contained(tmp_path):
    modules, linker = linked({
        'pkg/index.ts': 'export function save(v: number) { return v; }',
        'app.js': "import {save} from './pkg'; import {missing} from '../escape'; function run(v) { save(v); return missing(v); }",
    }, tmp_path)
    linked_calls = calls(fn(modules, 'run'))
    assert linked_calls[0].target == fn(modules, 'save').id
    assert linked_calls[1].kind == 'unresolved'
    assert linker.module_ref('app.js', '../escape') is None


def test_getter_and_detached_method_are_not_direct_callable_targets(tmp_path):
    modules, _ = linked({'app.ts': '''
class Store { get save() { return unknown; } method(v: number) { return v; } }
function run() { const store = new Store(); store.save(1); const method = store.method; method(1); }
'''}, tmp_path)
    run_calls = calls(fn(modules, 'run'))
    assert run_calls[1].target is None
    assert run_calls[2].target is None


def test_const_namespace_and_class_member_mutation_block_stale_targets(tmp_path):
    modules, _ = linked({
        'lib.js': 'export function save(v) { return v; }',
        'app.js': "import * as tools from './lib'; class Store { static save(v) { return v; } } function run(v) { tools.save = replacement; tools.save(v); Store.save = replacement; Store.save(v); }",
    }, tmp_path)
    assert all(c.target is None for c in calls(fn(modules, 'run')))


def test_aliased_new_instance_and_class_alias_use_constructor_evidence(tmp_path):
    modules, _ = linked({'app.ts': '''
class Store { save(v: number) { return v; } }
const Alias = Store;
function run(v: number) { const original = new Alias(); const same = original; return same.save(v); }
'''}, tmp_path)
    assert calls(fn(modules, 'run'))[-1].target == fn(modules, 'Store.save').id


def test_mutated_global_boundaries_do_not_receive_known_io_labels(tmp_path):
    modules, _ = linked({'app.js': '''
console.log = replacement;
fetch = replacement;
function run(path) { console.log(path); fetch(path); }
'''}, tmp_path)
    assert all(c.external is None and c.io is None for c in calls(fn(modules, 'run')))


def test_forward_var_function_initializer_is_not_treated_as_hoisted_callable(tmp_path):
    modules, _ = linked({'app.js': '''
function run(v) { save(v); var save = (x) => x; }
'''}, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_member_alias_uses_value_when_taken_even_if_export_is_reassigned_later(tmp_path):
    modules, _ = linked({'app.js': '''
function save(v) { return v; }
const alias = save;
save = replacement;
function run(v) { return alias(v); }
'''}, tmp_path)
    # Without execution-aware value capture, conservative rejection is safer.
    assert calls(fn(modules, 'run'))[0].target is None


def test_annotated_union_receiver_is_not_guessed(tmp_path):
    modules, _ = linked({'app.ts': '''
class Store { save(v: number) { return v; } }
class Other { save(v: number) { return v; } }
function run(value: Store | Other) { return value.save(1); }
'''}, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_top_level_initializer_temporal_dead_zone_is_not_a_deferred_body(tmp_path):
    modules, _ = linked({'app.js': 'save(1); const save = (value) => value;'}, tmp_path)
    assert calls(fn(modules, '<main>'))[0].target is None


def test_commonjs_exports_inside_deferred_functions_do_not_establish_module_exports(tmp_path):
    modules, _ = linked({
        'lib.js': 'function save(v) { return v; } function initialize() { exports.save = save; }',
        'app.js': "import {save} from './lib'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_class_annotation_import_can_justify_instance_method_but_not_runtime_call(tmp_path):
    modules, _ = linked({
        'lib.ts': 'export class Store { save(v: number) { return v; } }',
        'app.ts': "import type {Store} from './lib'; function run(store: Store) { Store(); return store.save(1); }",
    }, tmp_path)
    run_calls = calls(fn(modules, 'run'))
    assert run_calls[0].target is None
    assert run_calls[1].target == fn(modules, 'Store.save').id


def test_literal_constructor_alias_before_var_initializer_stays_unknown(tmp_path):
    modules, _ = linked({'app.js': '''
class Store { save(v) { return v; } }
function run(v) { const store = new Alias(); store.save(v); var Alias = Store; }
'''}, tmp_path)
    assert all(c.target is None for c in calls(fn(modules, 'run')))


def test_namespace_reexport_has_export_lookup_without_global_name_guessing(tmp_path):
    modules, _ = linked({
        'lib.ts': 'export function save(v: number) { return v; }',
        'barrel.js': "export * as tools from './lib';",
        'app.js': "import {tools} from './barrel'; function run(v) { return tools.save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'save').id


def test_explicit_missing_module_reexport_never_resolves_local_name(tmp_path):
    modules, _ = linked({
        'lib.js': "function save(v) { return v; } export {save} from './missing';",
        'app.js': "import {save} from './lib'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_escaped_reexport_source_blocks_local_symbol_fallback(tmp_path):
    modules, _ = linked({
        'lib.js': r"function save(v) { return v; } export {save} from './ta\x72get';",
        'app.js': "import {save} from './lib'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_unknown_star_source_blocks_other_star_candidates(tmp_path):
    modules, _ = linked({
        'lib.js': 'export function save(v) { return v; }',
        'barrel.js': r"export * from './lib'; export * from './unk\x6eown';",
        'app.js': "import {save} from './barrel'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_commonjs_replacement_removes_previous_named_exports(tmp_path):
    modules, _ = linked({
        'lib.js': 'function save(v) { return v; } exports.save = save; module.exports = replacement;',
        'app.js': "import {save} from './lib'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_default_arguments_use_parameter_environment_not_body_declarations(tmp_path):
    modules, _ = linked({'app.js': '''
function save() { return 1; }
function run(value = save()) { function save() { return 2; } return value; }
function missing(value = bodyOnly()) { function bodyOnly() { return 3; } return value; }
'''}, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'save').id
    assert calls(fn(modules, 'missing'))[0].target is None


def test_opaque_global_writes_block_builtin_fallback(tmp_path):
    modules, _ = linked({'app.js': '''
switch(flag) { case 1: fetch = replacement; }
function run(path) { return fetch(path); }
'''}, tmp_path)
    call = calls(fn(modules, 'run'))[0]
    assert call.external is None and call.io is None


def test_replaced_unbound_require_does_not_create_literal_import_targets(tmp_path):
    modules, _ = linked({
        'lib.js': 'exports.save = function save(v) { return v; };',
        'app.js': "require = replacement; const {save} = require('./lib'); function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


@pytest.mark.parametrize('initializer', ['new Store() && unknown', 'new Store().other', 'new Store() + unknown', 'new Store() ? unknown : other'])
def test_new_subexpression_does_not_make_entire_receiver_a_class_instance(tmp_path, initializer):
    modules, _ = linked({'app.js': f'class Store {{ save(v) {{ return v; }} }} function run(v) {{ const store = {initializer}; return store.save(v); }}'}, tmp_path)
    assert calls(fn(modules, 'run'))[-1].target is None


@pytest.mark.parametrize('right', ['export const save = unknown;', 'module.exports = replacement;',
                                   "export * from './opaque';"])
def test_star_branch_with_unresolved_export_blocks_other_candidates(tmp_path, right):
    modules, _ = linked({
        'left.js': 'export function save(v) { return v; }',
        'right.js': right,
        'opaque.js': 'module.exports = replacement;',
        'barrel.js': "export * from './left'; export * from './right';",
        'app.js': "import {save} from './barrel'; function run(v) { return save(v); }",
    }, tmp_path)
    call = calls(fn(modules, 'run'))[0]
    assert (call.kind, call.target) == ('unresolved', None)


def test_star_branch_without_the_name_still_resolves(tmp_path):
    modules, _ = linked({
        'left.js': 'export function save(v) { return v; }',
        'right.js': 'export function load(v) { return v; }',
        'barrel.js': "export * from './left'; export * from './right';",
        'app.js': "import {save} from './barrel'; function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'save').id


@pytest.mark.parametrize('source', [
    'class Store {\n  save(v) { return v; }\n  run(v) { this.save = replacement; return this.save(v); }\n}',
    'class Store {\n  static save(v) { return v; }\n  static run(v) { this.save = replacement; return this.save(v); }\n}',
    'class Store {\n  save(v) { return v; }\n  run(v) { const go = () => { this.save = replacement; }; go(); return this.save(v); }\n}',
    'class Store {\n  save(v) { return v; }\n  run(v) { this[key] = replacement; return this.save(v); }\n}',
    'class Store {\n  save(v) { return v; }\n  run(v) { return this.save(v); }\n}\nStore.prototype.save = replacement;',
])
def test_this_receiver_respects_method_replacement(tmp_path, source):
    modules, _ = linked({'app.js': source}, tmp_path)
    call = next(c for c in calls(fn(modules, 'Store.run')) if c.method == 'save')
    assert (call.kind, call.target) == ('unresolved', None)


def test_this_receiver_ignores_unrelated_field_writes(tmp_path):
    modules, _ = linked({'app.js': '''
class Store {
  constructor(items) { this.items = items; }
  save(v) { return v; }
  run(v) { this.count = 1; return this.save(v); }
}
'''}, tmp_path)
    call = next(c for c in calls(fn(modules, 'Store.run')) if c.method == 'save')
    assert (call.kind, call.target) == ('project', fn(modules, 'Store.save').id)


@pytest.mark.parametrize('lib', [
    'exports.save = save; var save = (v) => v;',
    'exports.save = save; const save = (v) => v;',
    'module.exports = { save }; let save = (v) => v;',
])
def test_commonjs_export_captures_value_at_assignment(tmp_path, lib):
    modules, _ = linked({'lib.js': lib,
                         'app.js': "const {save} = require('./lib'); function run(v) { return save(v); }"}, tmp_path)
    call = calls(fn(modules, 'run'))[0]
    assert (call.kind, call.target) == ('unresolved', None)


@pytest.mark.parametrize('lib', [
    'exports.save = save; function save(v) { return v; }',
    'const save = (v) => v; exports.save = save;',
])
def test_commonjs_export_of_initialized_value_resolves(tmp_path, lib):
    modules, _ = linked({'lib.js': lib,
                         'app.js': "const {save} = require('./lib'); function run(v) { return save(v); }"}, tmp_path)
    assert calls(fn(modules, 'run'))[0].target == fn(modules, 'save').id


@pytest.mark.parametrize('lib, resolves', [
    ('export default save; var save = (v) => v;', False),
    ('export default save; function save(v) { return v; }', True),
    ('export { save as default }; var save = (v) => v;', True),
])
def test_default_expression_export_is_a_snapshot(tmp_path, lib, resolves):
    modules, _ = linked({'lib.js': lib,
                         'app.js': "import save from './lib'; function run(v) { return save(v); }"}, tmp_path)
    call = calls(fn(modules, 'run'))[0]
    assert call.target == (fn(modules, 'save').id if resolves else None)


STORE = 'class Store {\n  save(v) { return v; }\n  static make(v) { return v; }\n  run(v) { return this.save(v); }\n}\n'


@pytest.mark.parametrize('write', [
    '[this.save] = [r];', '({save: this.save} = o);', 'for (this.save of xs) {}',
    'const self = this; self.save = r;',
])
def test_member_writes_in_patterns_and_aliases_block_this_methods(tmp_path, write):
    source = f'class Store {{\n  save(v) {{ return v; }}\n  run(v) {{ {write} return this.save(v); }}\n}}'
    modules, _ = linked({'app.js': source}, tmp_path)
    call = next(c for c in calls(fn(modules, 'Store.run')) if c.method == 'save')
    assert (call.kind, call.target) == ('unresolved', None)


@pytest.mark.parametrize('write, caller', [
    ('[Store.make] = [r];', 'function go(v) { return Store.make(v); }'),
    ('({save: Store.prototype.save} = o);', 'function go(v) { const s = new Store(); return s.save(v); }'),
    ('for (Store.prototype.save of xs) {}', 'function go(v) { const s = new Store(); return s.save(v); }'),
    ('const t = new Store(); [t.save] = [r];', 'function go(v) { const s = new Store(); return s.save(v); }'),
    ('const t = new Store(); t.save = r; t.run(1);', 'function go(v) { const s = new Store(); return s.run(v); }'),
])
def test_member_writes_anywhere_block_receiver_methods(tmp_path, write, caller):
    modules, _ = linked({'app.js': STORE + write + '\n' + caller}, tmp_path)
    targets = [c.target for f in (fn(modules, 'go'), fn(modules, 'Store.run')) for c in calls(f)
               if c.method in {'save', 'make'}]
    assert targets and all(t is None for t in targets)


def test_cross_file_prototype_patch_blocks_method_links(tmp_path):
    modules, _ = linked({
        'store.js': 'export ' + STORE,
        'patch.js': "import {Store} from './store'; Store.prototype.save = replacement;",
        'use.js': "import {Store} from './store'; function go(v) { const s = new Store(); return s.save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target is None
    assert calls(fn(modules, 'go'))[-1].target is None


@pytest.mark.parametrize('lib', ['function save(v) { return v; } exports.save = save;',
                                 'exports.save = function (v) { return v; };'])
@pytest.mark.parametrize('patch', [
    "require('./lib').save = other;",
    "const lib = require('./lib'); lib.save = other;",
])
def test_commonjs_export_patched_elsewhere_stays_unresolved(tmp_path, patch, lib):
    modules, _ = linked({
        'lib.js': lib,
        'patch.js': patch,
        'app.js': "const {save} = require('./lib'); function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


@pytest.mark.parametrize('lib', [
    'function save(v) { return v; } exports.save = save; const e = exports; e.save = other;',
    'exports.save = function (v) { return v; }; const e = module.exports; e.save = other;',
    'exports.save = function (v) { return v; }; this.save = other;',
])
def test_exports_alias_write_blocks_commonjs_export(tmp_path, lib):
    modules, _ = linked({
        'lib.js': lib,
        'app.js': "const {save} = require('./lib'); function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


def test_patched_global_blocks_builtin_io(tmp_path):
    modules, _ = linked({'app.js': 'globalThis.fetch = mock;\nfunction run(u) { return fetch(u); }'}, tmp_path)
    call = calls(fn(modules, 'run'))[0]
    assert (call.kind, call.io) == ('unresolved', None)


@pytest.mark.parametrize('sub', [
    'class Sub extends Store { save(v) { return 2; } }',
    'class Mid extends Store {}\nclass Sub extends Mid { save(v) { return 2; } }',
    'class Sub extends mixin(Store) { save(v) { return 2; } }',
])
def test_subclass_override_keeps_this_dispatch_unresolved(tmp_path, sub):
    modules, _ = linked({'app.js': STORE + sub}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target is None


def test_subclass_override_blocks_annotation_but_not_constructed_receivers(tmp_path):
    modules, _ = linked({'app.ts': STORE + '''class Sub extends Store { save(v: number) { return 2; } }
function typed(s: Store) { return s.save(1); }
function built() { const s = new Store(); return s.save(1); }
'''}, tmp_path)
    assert calls(fn(modules, 'typed'))[0].target is None
    assert calls(fn(modules, 'built'))[-1].target == fn(modules, 'Store.save').id


def test_unrelated_subclass_override_keeps_link(tmp_path):
    modules, _ = linked({'app.js': STORE + 'class Other {}\nclass Sub extends Other { save(v) { return 2; } }'}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target == fn(modules, 'Store.save').id


@pytest.mark.parametrize('write', ["t['save'] = r;", 't[`save`] = r;', "Store.prototype['save'] = r;"])
def test_literal_bracket_writes_block_receiver_methods(tmp_path, write):
    modules, _ = linked({'app.js': STORE + 'const t = new Store();\n' + write +
                         '\nfunction go(v) { const s = new Store(); return s.save(v); }'}, tmp_path)
    assert calls(fn(modules, 'go'))[-1].target is None


def test_literal_bracket_patch_blocks_commonjs_export(tmp_path):
    modules, _ = linked({
        'lib.js': 'exports.save = function (v) { return v; };',
        'patch.js': "require('./lib')['save'] = other;",
        'app.js': "const {save} = require('./lib'); function run(v) { return save(v); }",
    }, tmp_path)
    assert calls(fn(modules, 'run'))[0].target is None


@pytest.mark.parametrize('files', [
    {'app.js': STORE + 'class Sub extends Store { save = replacement; }'},
    {'app.js': STORE + 'class Sub extends Store { save; }'},
    {'app.js': STORE + "class Sub extends Store { ['save'](v) { return 2; } }"},
    {'app.js': STORE + 'const B = Store;\nclass Sub extends B { save(v) { return 2; } }'},
    {'app.js': 'export ' + STORE,
     'sub.js': "import {Store as Base} from './app'; class Sub extends Base { save(v) { return 2; } }"},
    {'app.js': 'export default ' + STORE,
     'sub.js': "import Base from './app'; class Sub extends Base { save(v) { return 2; } }"},
], ids=['field', 'bare-field', 'computed', 'local-alias', 'import-alias', 'default-alias'])
def test_subclass_overrides_through_fields_computed_names_and_aliases(tmp_path, files):
    modules, _ = linked(files, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target is None


def test_static_field_override_blocks_static_this(tmp_path):
    modules, _ = linked({'app.js': '''class Store {
  static save(v) { return v; }
  static run(v) { return this.save(v); }
}
class Sub extends Store { static save = r; }'''}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target is None


def test_subclass_of_external_base_with_same_name_keeps_link(tmp_path):
    modules, _ = linked({'app.js': STORE + "import {Store as Remote} from 'pkg';\nclass Sub extends Remote { save(v) { return 2; } }"}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target == fn(modules, 'Store.save').id


def test_builtin_global_base_is_not_a_project_subclass(tmp_path):
    modules, _ = linked({'app.js': STORE + 'class Failure extends Error { save(v) { return 2; } }'}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target == fn(modules, 'Store.save').id


def test_global_named_like_a_project_class_may_be_that_class(tmp_path):
    modules, _ = linked({'app.js': 'export ' + STORE,
                         'sub.js': 'class Sub extends Store { save(v) { return 2; } }'}, tmp_path)
    assert calls(fn(modules, 'Store.run'))[0].target is None
