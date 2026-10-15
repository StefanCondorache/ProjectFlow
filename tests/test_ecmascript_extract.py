"""Real parser fixtures for ECMAScript extraction, before any linking."""
import json

import pytest

from flowmap.ir import Assign, Call, If, Loop, Raise, Return, Try, walk
from flowmap.lang.ecmascript.extract import extract_module


def module(source, file='main.ts'):
    return extract_module(file, source.encode())


def function(mod, qualname):
    return next(fn for fn in mod.functions.values() if fn.qualname == qualname)


def calls(items):
    return [item for item in walk(items) if isinstance(item, Call)]


def metadata(mod):
    return mod.metadata['ecmascript']


def test_nested_calls_keep_evaluation_order_and_temporaries():
    fn = function(module('function run(value: number) { return finish(load(value), other()); }'), 'run')
    found = calls(fn.body)
    assert [call.callee for call in found] == ['load', 'other', 'finish']
    assert found[2].args[0].uses == found[0].defs
    assert found[2].args[1].uses == found[1].defs
    assert found[2].args[0].expr == '__t1'
    assert fn.body[-1].uses == found[2].defs


def test_signatures_documentation_spans_and_real_method_arguments():
    mod = module('/** Store docs */\nclass Store {\n constructor(first: string) { this.value=first; }\n save(first: string, extra=1, ...rest: number[]) { return first; }\n static build(first: number): Store { return new Store(first); }\n}')
    cls = next(iter(mod.classes.values()))
    assert cls.doc == 'Store docs'
    ctor = mod.functions[cls.methods['constructor']]
    assert ctor.receiver == 'this' and ctor.is_constructor
    assert [(p.name, p.annotation) for p in ctor.params] == [('first', 'string')]
    save = mod.functions[cls.methods['save']]
    assert [(p.name, p.kind) for p in save.params] == [('first', 'normal'), ('extra', 'normal'), ('rest', 'vararg')]
    assert save.params[1].default == '1'
    assert save.language == 'typescript'
    assert (save.line, save.end_line) == (4, 4)
    build = mod.functions[cls.methods['build']]
    assert build.returns == 'Store'
    assert metadata(mod)['functions'][build.id]['static'] is True
    found = calls(build.body)[0]
    assert metadata(mod)['calls'][f'{build.id}:{found.line}:{found.col}']['construct'] is True


def test_bound_arrows_named_expressions_and_callbacks_have_separate_bodies():
    mod = module('function run(x) { const local = (v: number) => consume(v); const named = function internal(v) { return internal(v); }; send(x, () => hidden()); }')
    run = function(mod, 'run')
    assert [c.callee for c in calls(run.body)] == ['send']
    arrow = function(mod, 'run.<locals>.local')
    assert [c.callee for c in calls(arrow.body)] == ['consume']
    assert isinstance(arrow.body[-1], Return)
    named = function(mod, 'run.<locals>.named')
    assert [c.callee for c in calls(named.body)] == ['internal']
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'internal')
    assert binding['scope'] == metadata(mod)['functions'][named.id]['scope']
    callback = next(fn for fn in mod.functions.values() if '<callback@' in fn.name)
    assert [c.callee for c in calls(callback.body)] == ['hidden']


def test_duplicate_names_have_distinct_identifiers_and_lexical_scopes():
    mod = module('function same() {} function same() {} function outer(same) { if (same) { const same = () => inside(); same(); } same(); }')
    same = [fn for fn in mod.functions.values() if fn.qualname == 'same']
    assert len(same) == 2 and same[0].id != same[1].id
    assert all(fn.id.startswith('main.ts::same@') for fn in same)
    bindings = [b for b in metadata(mod)['bindings'] if b['name'] == 'same']
    assert {b['kind'] for b in bindings} >= {'function', 'parameter', 'const'}
    outer = function(mod, 'outer')
    keys = [f'{outer.id}:{c.line}:{c.col}' for c in calls(outer.body)]
    assert metadata(mod)['calls'][keys[0]]['scope'] != metadata(mod)['calls'][keys[1]]['scope']


@pytest.mark.parametrize('expr,then,other', [('flag ? left() : right()', ['left'], ['right']), ('flag && right()', ['right'], []), ('flag || right()', [], ['right']), ('flag ?? right()', ['right'], [])])
def test_conditional_expression_calls_are_branches(expr, then, other):
    fn = function(module(f'function run(flag) {{ return {expr}; }}'), 'run')
    assert not any(isinstance(item, Call) for item in fn.body)
    branch = next(item for item in fn.body if isinstance(item, If))
    assert [c.callee for c in calls(branch.then)] == then
    assert [c.callee for c in calls(branch.orelse)] == other
    result = fn.body[-1]
    assert isinstance(result, Return) and len(result.uses) == 1
    assert any(isinstance(item, Assign) and item.defs == result.uses for item in branch.then + branch.orelse)
    if '??' in expr:
        assert 'null' in branch.test


@pytest.mark.parametrize('expr', ['obj?.method(side())', 'obj.method?.(side())', 'obj?.method(side()).next(tail())'])
def test_optional_chains_guard_argument_calls_and_following_chain(expr):
    fn = function(module(f'function run(obj) {{ return {expr}; }}'), 'run')
    assert not any(isinstance(item, Call) for item in fn.body)
    branch = next(item for item in fn.body if isinstance(item, If))
    assert [c.callee for c in calls(branch.then)][0] == 'side'
    assert 'null' in branch.test


def test_receiver_and_arguments_evaluate_before_outer_call():
    fn = function(module('function run(value) { return build().save(load(value)); }'), 'run')
    found = calls(fn.body)
    assert [c.callee for c in found] == ['build', 'load', '$1.save']
    assert found[-1].receiver_uses == ['$1']
    assert found[-1].args[0].uses == ['$2']


def test_control_statements_throws_and_try_blocks():
    fn = function(module('function run(rows) { if (check()) { yes(); } else { no(); } for (const row of rows) { use(row); } while (ready()) { tick(); } try { fail(); } catch (error) { report(error); } finally { close(); } throw problem(); }'), 'run')
    assert [type(item) for item in fn.body] == [Call, If, Loop, Loop, Try, Call, Raise]
    loops = [item for item in fn.body if isinstance(item, Loop)]
    assert loops[0].defs == ['row'] and loops[0].uses == ['rows']
    assert [c.callee for c in calls(loops[1].body)] == ['ready', 'tick']
    assert loops[1].head == 1
    attempt = next(item for item in fn.body if isinstance(item, Try))
    assert attempt.handlers[0].name == 'error'
    assert [c.callee for c in calls(attempt.final)] == ['close']


def test_classic_for_condition_and_increment_stay_inside_loop():
    fn = function(module('function run() { for (let i=start(); test(i); advance(i)) { work(i); } }'), 'run')
    assert [c.callee for c in calls(fn.body[:1])] == ['start']
    loop = next(item for item in fn.body if isinstance(item, Loop))
    assert [c.callee for c in calls(loop.body)] == ['test', 'work', 'advance']
    assert loop.head == 1


def test_imports_named_default_namespace_type_only_and_exports():
    mod = module("import type {Shape} from './types'; import start, {work as go} from './work.js'; import * as ns from './ns'; import './side'; export {go as launch}; export {thing as named} from './re'; export * from './all'; export default function(x) { return go(x); }")
    assert [(i.module, i.name, i.alias) for i in mod.imports] == [('./types', 'Shape', 'Shape'), ('./work.js', 'default', 'start'), ('./work.js', 'work', 'go'), ('./ns', '*', 'ns'), ('./side', None, None), ('./re', 'thing', None), ('./all', '*', None)]
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'Shape')
    assert binding['type_only'] is True
    exports = {e['name']: e for e in metadata(mod)['exports']}
    assert exports['launch']['local'] == 'go'
    assert exports['named']['module'] == './re' and exports['named']['imported'] == 'thing'
    assert exports['default']['target'] in mod.functions
    assert exports['*']['module'] == './all'


def test_literal_commonjs_bindings_and_exports_respect_shadowing():
    mod = module("const worker = require('./worker'); const {go: launch} = require('./go'); function run() {} exports.run=run; module.exports={run, launch}; function hidden(require, exports) { const fake=require('./fake'); exports.fake=fake; }")
    imports = [b for b in metadata(mod)['bindings'] if b['kind'] == 'import']
    assert [(b['name'], b['module'], b['imported']) for b in imports] == [('worker', './worker', '*'), ('launch', './go', 'go')]
    assert not any(i.module == './fake' for i in mod.imports)
    exports = metadata(mod)['exports']
    assert any(e['name'] == 'run' and e['local'] == 'run' for e in exports)
    assert not any(e['name'] == 'fake' for e in exports)


def test_reassigned_binding_records_write_and_var_function_scope():
    mod = module('function run() { if (flag) { var fn = () => first(); let local=1; } fn=unknown; fn(); }')
    meta = metadata(mod)
    fn = function(mod, 'run')
    bound = next(b for b in meta['bindings'] if b['name'] == 'fn')
    assert bound['scope'] == meta['functions'][fn.id]['scope']
    assert bound['mutable'] is True and bound['writes']
    local = next(b for b in meta['bindings'] if b['name'] == 'local')
    assert local['scope'] != bound['scope']


@pytest.mark.parametrize('file', ['view.tsx', 'view.jsx', 'view.js', 'view.TSX'])
def test_jsx_only_evaluates_expression_containers(file):
    mod = module('const view = (x) => <Panel title={load(x)}>text<Inner />{render(x)}</Panel>;', file)
    fn = function(mod, 'view')
    assert [c.callee for c in calls(fn.body)] == ['load', 'render']
    assert mod.language == ('typescript' if file.lower().endswith('.tsx') else 'javascript')


def test_unicode_paths_source_positions_and_serializable_metadata():
    mod = module('const café = 1;\nfunction caféFn(x: string) { return save(x); }', 'src/școală.ts')
    fn = function(mod, 'caféFn')
    assert fn.file == 'src/școală.ts' and fn.line == 2
    assert fn.id.startswith('src/școală.ts::caféFn@2:')
    assert calls(fn.body)[0].col == 37  # IR columns are UTF-8 byte offsets.
    json.dumps(mod.metadata)


def test_malformed_source_and_unsupported_switch_are_visible_without_flattening():
    broken = module('function incomplete( {')
    assert any(d['code'] == 'parse-error' for d in broken.diagnostics)
    mod = module('function run(x) { switch(x) { case 1: first(); case 2: second(); } after(); }')
    fn = function(mod, 'run')
    assert [c.callee for c in calls(fn.body)] == ['after']
    assert any(d['code'] == 'unsupported-switch-fallthrough' for d in mod.diagnostics)


def test_arrow_inherits_this_but_nested_normal_function_does_not():
    mod = module('class Store { save(value) { const arrow=()=>this.use(value); function normal(){ return this.use(value); } return arrow(); } }')
    arrow = function(mod, 'Store.save.<locals>.arrow')
    normal = function(mod, 'Store.save.<locals>.normal')
    assert arrow.receiver == 'this' and normal.receiver is None


def test_module_main_is_stable_and_declarations_do_not_execute():
    mod = module('function declared() { hidden(); } const arrow=()=>alsoHidden(); kickoff();')
    fn = mod.functions['main.ts::<main>']
    assert [c.callee for c in calls(fn.body)] == ['kickoff']


def test_chained_calls_have_distinct_site_keys_and_keep_source_spans():
    mod = module('function run() { return build().save(); }')
    fn = function(mod, 'run')
    found = calls(fn.body)
    keys = [f'{fn.id}:{c.line}:{c.col}' for c in found]
    assert len(set(keys)) == 2
    assert all(key in metadata(mod)['calls'] for key in keys)
    assert metadata(mod)['calls'][keys[1]]['start_byte'] == 24


def test_multiple_optional_segments_keep_each_guard():
    fn = function(module('function run(obj) { return obj?.method(side())?.next(tail()); }'), 'run')
    first = next(item for item in fn.body if isinstance(item, If))
    assert [c.callee for c in first.then if isinstance(c, Call)] == ['side', 'obj.method']
    second = next(item for item in first.then if isinstance(item, If))
    assert [c.callee for c in calls(second.then)] == ['tail', '$3.next']
    assert not calls(first.orelse)


def test_optional_method_callee_receiver_evaluates_once():
    fn = function(module('function run() { return build().method?.(side()); }'), 'run')
    assert [c.callee for c in calls(fn.body)] == ['build', 'side', '$1.method']
    assert isinstance(fn.body[0], Call) and isinstance(fn.body[1], If)


def test_do_while_preserves_body_before_condition_calls():
    mod = module('function run() { do { tick(); } while(check()); }')
    fn = function(mod, 'run')
    loop = fn.body[0]
    assert isinstance(loop, Loop) and loop.kind == 'do while'
    assert [c.callee for c in calls(loop.body)] == ['tick', 'check']
    assert loop.head == 0


def test_default_parameter_calls_are_conditional_and_inside_function():
    mod = module('const run = (first=load()) => save(first);')
    assert not calls(mod.functions['main.ts::<main>'].body)
    fn = function(mod, 'run')
    assert isinstance(fn.body[0], If)
    assert fn.body[0].test == 'first === undefined'
    assert [c.callee for c in calls(fn.body[0].then)] == ['load']


def test_shadowed_block_variables_have_distinct_dataflow_keys():
    fn = function(module('function run(value) { if (flag) { let value = other; use(value); } use(value); }'), 'run')
    found = calls(fn.body)
    assert found[0].args[0].uses != found[1].args[0].uses
    assert found[1].args[0].uses == ['value']


def test_named_default_declaration_is_bound_in_module_scope():
    mod = module('export default function named(x) { return named(x); } named(1);')
    fn = function(mod, 'named')
    assert any(b['name'] == 'named' and b['target'] == fn.id and b['scope'] == metadata(mod)['functions']['main.ts::<main>']['scope'] for b in metadata(mod)['bindings'])


def test_await_marks_outer_call_only_when_receiver_is_also_a_call():
    fn = function(module('async function run() { return await build().save(); }'), 'run')
    found = calls(fn.body)
    assert [c.awaited for c in found] == [False, True]


def test_constructor_assignment_records_type_evidence_for_linker():
    mod = module('class Store {} function run() { const store = new Store(); return store.save(); }')
    fn = function(mod, 'run')
    assert fn.hints['store'] == [('construct', 'Store')]
    assert ('call', 'Store') not in fn.hints['store']


def test_side_effect_require_and_literal_dynamic_import_retain_dependency_evidence():
    mod = module("require('./side'); import('./lazy'); function run(require) { require('./fake'); }")
    assert [(i.module, i.name) for i in mod.imports] == [('./side', None), ('./lazy', None)]
    assert any(d['code'] == 'dynamic-import' for d in mod.diagnostics)


def test_commonjs_property_alias_and_late_shadowing():
    mod = module("const launch = require('./worker').go; function other() { const hidden=require('./fake'); const require = local; }")
    launch = next(b for b in metadata(mod)['bindings'] if b['name'] == 'launch')
    assert launch['kind'] == 'import' and launch['module'] == './worker' and launch['imported'] == 'go'
    assert not any(i.module == './fake' for i in mod.imports)


def test_destructured_parameters_preserve_argument_positions_and_bind_leaves():
    fn = function(module('function run({first}, [second], third) { return save(first, second, third); }'), 'run')
    assert [p.name for p in fn.params] == ['{first}', '[second]', 'third']
    assigns = [i for i in fn.body if isinstance(i, Assign)]
    assert [(i.defs, i.uses) for i in assigns] == [(['first'], ['{first}']), (['second'], ['[second]'])]


def test_short_circuit_decisions_use_distinct_positions():
    fn = function(module('function run(a) { return a && middle() && last(); }'), 'run')
    branches = [i for i in walk(fn.body) if isinstance(i, If)]
    assert len(branches) == 2
    assert len({(i.line, i.col) for i in branches}) == 2


@pytest.mark.parametrize('file,language', [('a.ts', 'typescript'), ('a.mts', 'typescript'), ('a.cts', 'typescript'), ('a.tsx', 'typescript'), ('a.js', 'javascript'), ('a.mjs', 'javascript'), ('a.cjs', 'javascript'), ('a.jsx', 'javascript')])
def test_supported_extensions_choose_real_language_grammar(file, language):
    mod = module('function run(value) { return save(value); }', file)
    assert mod.language == language and function(mod, 'run').language == language
    assert [c.callee for c in calls(function(mod, 'run').body)] == ['save']


def test_unbraced_branch_var_initializer_is_marked_conditional():
    mod = module('if (flag) var run = () => hidden(); run();')
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'run')
    assert binding['scope'] == metadata(mod)['functions']['main.ts::<main>']['scope']
    assert binding['conditional'] is True


def test_loop_reassignment_and_receiver_property_override_record_writes():
    mod = module('function run() {} for (run of values) { run(); } class Store {} const store=new Store(); store.save=other; store.save();')
    bindings = metadata(mod)['bindings']
    assert next(b for b in bindings if b['name'] == 'run')['writes']
    assert next(b for b in bindings if b['name'] == 'store')['writes']


def test_unsupported_destructuring_defaults_and_decorators_are_diagnosed():
    mod = module('function run({x=load()}) { return x; } class Store { @decorate() save() {} }')
    assert any(d['code'] == 'unsupported-binding-pattern' for d in mod.diagnostics)
    assert any(d['code'] == 'unsupported-decorator' for d in mod.diagnostics)
    assert not calls(function(mod, 'run').body)


def test_escaped_import_keeps_unknown_shadowing_binding():
    mod = module(r"import {fetch} from '\x2e/worker'; function run() { fetch(); }")
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'fetch')
    assert binding['kind'] == 'import' and binding['module'] is None
    assert any(d['code'] == 'dynamic-import' for d in mod.diagnostics)


def test_method_name_does_not_create_a_recursive_lexical_binding():
    mod = module('function save() {} class Store { save() { return save(); } }')
    method = function(mod, 'Store.save')
    scope = metadata(mod)['functions'][method.id]['scope']
    assert not any(b['scope'] == scope and b['name'] == 'save' for b in metadata(mod)['bindings'])


@pytest.mark.parametrize('source', [
    'function run($1) { work(); return use($1); }',
    'function run() { work(); function closure() { return use($1); } }',
    'function run(__t1) { return use(work(), __t1); }',
])
def test_synthetic_temporaries_do_not_collide_with_user_identifiers(source):
    mod = module(source)
    fn = function(mod, 'run')
    assert all('$1' not in c.defs for c in calls(fn.body))
    if fn.params:
        last = calls(fn.body)[-1]
        parameter = fn.params[0].name
        assert parameter in last.args[-1].uses
        assert all(parameter not in c.defs for c in calls(fn.body))


@pytest.mark.parametrize('opaque', [
    'switch(x) { case 1: var run=other; hidden(); }',
    'with(x) { var run=other; hidden(); }',
    'label: { var run=other; hidden(); }',
])
def test_opaque_statements_retain_function_scoped_shadowing(opaque):
    mod = module(f'function run() {{}} function wrapper(x) {{ {opaque} run(); }}')
    fn = function(mod, 'wrapper')
    scope = metadata(mod)['functions'][fn.id]['scope']
    shadow = next(b for b in metadata(mod)['bindings'] if b['name'] == 'run' and b['scope'] == scope)
    assert shadow['kind'] == 'var' and shadow['conditional'] is True
    assert shadow['target'] is None
    assert [c.callee for c in calls(fn.body)] == ['run']


def test_opaque_writes_invalidate_lexical_and_unbound_global_evidence():
    mod = module('function target() {} function run(x) { switch(x) { case 1: target=other; fetch=replacement; } target(); fetch(); }')
    target = next(b for b in metadata(mod)['bindings'] if b['name'] == 'target')
    assert target['writes']
    assert any(w['name'] == 'fetch' for w in metadata(mod)['unbound_writes'])


def test_opaque_callback_declarations_do_not_hoist_into_outer_scope():
    mod = module('function target() {} function run(x) { switch(x) { case 1: const callback=()=>{var target=other;}; } target(); }')
    run = function(mod, 'run')
    assert not any(b['scope'] == metadata(mod)['functions'][run.id]['scope'] and b['name'] == 'target' for b in metadata(mod)['bindings'])
    callback = function(mod, 'run.<locals>.callback')
    assert any(b['scope'] == metadata(mod)['functions'][callback.id]['scope'] and b['name'] == 'target' for b in metadata(mod)['bindings'])


@pytest.mark.parametrize('clause,name,imported', [('{run}', 'run', 'run'), ('*', '*', '*'), ('* as ns', 'ns', '*')])
def test_escaped_reexports_preserve_unknown_source_instead_of_local_binding(clause, name, imported):
    mod = module('function run() {} export ' + clause + r" from '\x2e/worker';")
    exported = next(e for e in metadata(mod)['exports'] if e['name'] == name)
    assert exported['local'] is None and exported['target'] is None
    assert exported['module'] is None and exported['imported'] == imported
    assert exported['unknown_source'] is True
    assert any(d['code'] == 'dynamic-import' for d in mod.diagnostics)
    assert any(d['unknown_source'] for d in metadata(mod)['dependencies'])


@pytest.mark.parametrize('replacement', ['{}', 'dynamic', '{...dynamic}', '{run, ...dynamic}', '{[key]: run}', '{run: dynamic.member}'])
def test_commonjs_replacement_invalidates_stale_exports_and_records_event(replacement):
    mod = module('function run() {} exports.run=run; module.exports=' + replacement + ';')
    assert not any(e['name'] == 'run' and e['local'] == 'run' for e in metadata(mod)['exports'])
    events = metadata(mod)['commonjs_events']
    assert [e['kind'] for e in events] == ['set', 'replace']
    assert events[0]['start_byte'] < events[1]['start_byte']
    if replacement != '{}':
        assert any(e.get('unknown_export') for e in metadata(mod)['exports'])


def test_supported_commonjs_replacement_keeps_only_current_exports():
    mod = module('function first() {} function second() {} exports.first=first; module.exports={second}; module.exports.current=second; exports.detached=first;')
    exports = metadata(mod)['exports']
    assert {e['name'] for e in exports} == {'second', 'current'}
    assert all(e['commonjs'] for e in exports)


def test_default_parameter_environment_cannot_see_body_function_declarations():
    mod = module('function outer() {} function run(value=outer()) { function outer() { hidden(); } return outer(); }')
    fn = function(mod, 'run')
    info = metadata(mod)['functions'][fn.id]
    found = calls(fn.body)
    default_scope = metadata(mod)['calls'][f'{fn.id}:{found[0].line}:{found[0].col}']['scope']
    body_scope = metadata(mod)['calls'][f'{fn.id}:{found[1].line}:{found[1].col}']['scope']
    assert default_scope == info['parameter_scope'] and body_scope == info['scope']
    assert default_scope != body_scope
    assert metadata(mod)['scopes'][body_scope]['parent'] == default_scope
    body_outer = function(mod, 'run.<locals>.outer')
    assert next(b for b in metadata(mod)['bindings'] if b['target'] == body_outer.id)['scope'] == body_scope


def test_later_parameter_binding_retains_tdz_in_default_environment():
    mod = module('class Store {} function run(first=later.save(), later: Store) { return later.save(); }')
    fn = function(mod, 'run')
    info = metadata(mod)['functions'][fn.id]
    later = next(b for b in metadata(mod)['bindings'] if b['scope'] == info['parameter_scope'] and b['name'] == 'later')
    default_call = calls(fn.body)[0]
    call_meta = metadata(mod)['calls'][f'{fn.id}:{default_call.line}:{default_call.col}']
    assert later['hoisted'] is False and call_meta['start_byte'] < later['initialized_byte']


def test_destructured_parameter_initializes_before_later_default_call():
    fn = function(module('function run({value}, result=use(value)) { return result; }'), 'run')
    assert isinstance(fn.body[0], Assign) and fn.body[0].defs == ['value']
    assert isinstance(fn.body[1], If)
    assert calls(fn.body[1].then)[0].args[0].uses == ['value']


@pytest.mark.parametrize('source', [
    'function base() {} class Store extends base() {}',
    'function base() {} const Store=class extends base() {};',
    'function base() {} export default class extends base() {}',
])
def test_dynamic_class_heritage_is_explicitly_diagnosed(source):
    mod = module(source)
    assert any(d['code'] == 'unsupported-class-heritage' for d in mod.diagnostics)
    assert not calls(mod.functions['main.ts::<main>'].body)


@pytest.mark.parametrize('statement', [
    '({value=load()}=input);',
    'for(const {value=load()} of input) { use(value); }',
    'try { work(); } catch({value=load()}) { use(value); }',
    '({[key()]: value}=input);',
    'for(const {[key()]: value} of input) { use(value); }',
    'try { work(); } catch({[key()]: value}) { use(value); }',
])
def test_unsupported_destructuring_is_diagnosed_at_each_execution_boundary(statement):
    mod = module('function run(input) {' + statement + '}')
    fn = function(mod, 'run')
    assert any(d['code'] == 'unsupported-binding-pattern' for d in mod.diagnostics)
    assert not any(c.callee in {'load', 'key'} for c in calls(fn.body))
    assert any(b['name'] == 'value' for b in metadata(mod)['bindings']) or any('value' in i.defs for i in walk(fn.body) if isinstance(i, Assign))


def test_mutated_unbound_require_does_not_create_fake_module_bindings_or_dependencies():
    mod = module("require=replacement; const {save}=require('./worker');")
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'save')
    assert binding['kind'] != 'import' and binding['module'] is None
    assert not any(i.module == './worker' for i in mod.imports)


def test_opaque_class_heritage_preserves_mutation_evidence():
    mod = module('function target() {} class Store extends (target=replacement) {} target();')
    binding = next(b for b in metadata(mod)['bindings'] if b['name'] == 'target')
    assert binding['writes']
    assert any(d['code'] == 'unsupported-class-heritage' for d in mod.diagnostics)


def test_computed_commonjs_property_write_invalidates_known_exports():
    mod = module('function run() {} exports.run=run; exports[key]=replacement;')
    assert not any(e['name'] == 'run' and e.get('local') == 'run' for e in metadata(mod)['exports'])
    assert any(e.get('unknown_export') for e in metadata(mod)['exports'])


@pytest.mark.parametrize('target,kind,name,root', [
    ('module.exports', 'replace', None, 'module'),
    ('exports.run', 'set', 'run', 'exports'),
])
@pytest.mark.parametrize('update', ['{}++', '{}--', '++{}', '--{}'])
def test_opaque_commonjs_updates_retain_unknown_export_evidence(target, kind, name, root, update):
    source = ('function run() {} exports.run=run; '
              'class Store extends (hidden(), ' + update.format(target) + ') {}')
    mod = module(source, 'main.js')
    meta = metadata(mod)
    event = meta['commonjs_events'][-1]
    assert event['kind'] == kind and event['name'] == name
    assert event['unknown_export'] is True
    assert meta['exports'] and all(e['unknown_export'] for e in meta['exports'])
    assert all(e['local'] is None and e['target'] is None for e in meta['exports'])
    assert any(w['name'] == root for w in meta['unbound_writes'])
    assert any(d['code'] == 'unsupported-class-heritage' for d in mod.diagnostics)
    assert not calls(mod.functions['main.js::<main>'].body)


def test_three_same_named_methods_stay_ambiguous():
    module = extract_module('a.js', b'class Store { save() {} save() {} save() {} }')
    cls = next(iter(module.classes.values()))
    assert 'save' not in cls.methods
