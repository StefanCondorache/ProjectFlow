"""Real project-to-flow/trail/map behavior for the ECMAScript adapter family."""
from pathlib import Path

import pytest

from flowmap.dataflow import trace
from flowmap.flow import build_flow
from flowmap.ir import Call, walk
from flowmap.journey import build_journey
from flowmap.mermaid import to_mermaid
from flowmap.overview import build_overview
from flowmap.project import load_project
from flowmap.server import project_summary
from flowmap.simulate import expects, simulate


def write(root: Path, files: dict[str, str]):
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


def function(project, name):
    found = [fn for fn in project.functions.values() if fn.qualname == name]
    assert len(found) == 1
    return found[0]


def test_import_alias_resolves_across_js_and_ts_in_project_loading(tmp_path):
    write(tmp_path, {
        'lib.ts': 'export function save(value: number) { return value; }',
        'main.js': "import { save as persist } from './lib.js'; export function run(v) { return persist(v); }",
        'python.py': 'def unchanged(value):\n    return value\n',
    })
    project = load_project(tmp_path)
    assert set(project.modules) == {'lib.ts', 'main.js', 'python.py'}
    run, save = function(project, 'run'), function(project, 'save')
    call = next(c for c in walk(run.body) if isinstance(c, Call))
    assert (call.kind, call.target, call.confidence) == ('project', save.id, 'exact')
    assert project.resolvers['javascript'] is project.resolvers['typescript']
    assert project.inventory.files['main.js'].analysis == 'semantic'
    summary = project_summary(project)
    assert summary['capabilities'] == {
        'overview': True, 'flow_languages': ['javascript', 'python', 'typescript'],
        'trail_languages': ['javascript', 'python', 'typescript'], 'simulation_languages': ['python'],
    }
    assert summary['file_languages'] == {'lib.ts': 'typescript', 'main.js': 'javascript', 'python.py': 'python'}
    assert 'python.py::unchanged' in project.functions


def test_real_flow_expansion_trails_journeys_and_mermaid_preserve_method_argument(tmp_path):
    write(tmp_path, {
        'lib.ts': '''
export function clean(value: number) { return value; }
export class Store { save(value: number) { return clean(value); } }
''',
        'app.js': "import {Store} from './lib.js'; export function run(input) { const store = new Store(); return store.save(input); }",
    })
    project = load_project(tmp_path)
    run = function(project, 'run')
    assert run.language == 'javascript'
    graph = build_flow(project, run.id, depth=2)
    method_start = next(n for n in graph.nodes if n.kind == 'start' and n.detail.get('function') == function(project, 'Store.save').id)
    assert method_start.detail['params'] == ['value']
    assert any(n.kind == 'step' and n.detail.get('target') == function(project, 'clean').id for n in graph.nodes)
    trail = trace(project, run.id, 's', 'input')
    assert trail.found and not trail.truncated
    assert {context.function for context in trail.contexts.values() if context.active} == {
        run.id, function(project, 'Store.save').id, function(project, 'clean').id,
    }
    assert next(ctx for ctx in trail.contexts.values() if ctx.function == function(project, 'Store.save').id).params == ['value']
    assert next(ctx for ctx in trail.contexts.values() if ctx.function == function(project, 'clean').id).returns
    steps = build_flow(project, run.id, trail=trail)
    assert any(n.kind == 'group' and n.detail.get('target') == function(project, 'Store.save').id for n in steps.nodes)
    journey = build_journey(project, trail)
    assert {n.detail['target'] for n in journey.nodes if n.kind == 'step'} == {
        run.id, function(project, 'Store.save').id, function(project, 'clean').id,
    }
    assert 'Store.save' in to_mermaid(graph)
    assert 'clean' in to_mermaid(journey)


def test_ast_import_evidence_preserves_semantic_status_and_rejects_shadowed_require(tmp_path):
    write(tmp_path, {
        'lib.ts': 'export function save(value: number) { return value; }',
        'app.js': '''
import {save} from './lib.js';
const text = "import './fake.js'";
function shadow(require) { return require('./fake.js'); }
export function run(v) { return save(v); }
''',
        'fake.js': 'export function fake() { return 0; }',
    })
    project = load_project(tmp_path)
    graph = build_overview(project)
    edge = next(e for e in graph.edges if e.source == 'file:app.js')
    assert (edge.target, edge.confidence, edge.count) == ('file:lib.ts', 'verified', 1)
    assert edge.evidence == [{'file': 'app.js', 'line': 2, 'text': "import {save} from './lib.js';"}]
    assert not any(e.target == 'file:fake.js' for e in graph.edges)
    node = next(n for n in graph.nodes if n.id == 'file:app.js')
    assert node.detail['analysis'] == 'semantic'
    assert any(f['id'] == function(project, 'run').id for f in node.detail['functions'])


def test_partial_source_keeps_flow_links_and_displays_diagnostics(tmp_path):
    write(tmp_path, {'app.js': '''
function save(v) { return v; }
function run(v) { switch(v) { case 1: save(v); } return save(v); }
'''})
    project = load_project(tmp_path)
    node = next(n for n in build_overview(project).nodes if n.id == 'file:app.js')
    assert node.detail['analysis'] == 'partial'
    assert any(d['code'] == 'unsupported-switch-fallthrough' for d in node.detail['diagnostics'])
    assert any(f['id'] == function(project, 'run').id for f in node.detail['functions'])


@pytest.mark.parametrize('suffix', ['.JS', '.jsx', '.mjs', '.cjs', '.TS', '.tsx', '.mts', '.cts'])
def test_all_official_ecmascript_extensions_are_registered_case_insensitively(tmp_path, suffix):
    write(tmp_path, {'app' + suffix: 'export function run(v) { return v; }'})
    project = load_project(tmp_path)
    assert function(project, 'run').file == 'app' + suffix


def test_oversized_ecmascript_is_structural_without_regressing_large_python(tmp_path):
    write(tmp_path, {
        'large.ts': "import 'hidden';\n" + ' ' * 1_000_001,
        'large.py': 'def run():\n    pass\n#' + 'x' * 1_000_001,
    })
    project = load_project(tmp_path)
    assert 'large.ts' not in project.modules
    assert project.inventory.files['large.ts'].analysis == 'structural'
    assert project.inventory.files['large.py'].analysis == 'semantic'
    assert 'large.py::run' in project.functions
    assert not project.relationships


def test_public_simulation_rejects_ecmascript_before_python_evaluation(tmp_path):
    write(tmp_path, {'app.ts': 'export function run(value: number) { return value + 1; }'})
    project = load_project(tmp_path)
    root = function(project, 'run').id
    for operation in (simulate, expects):
        with pytest.raises(ValueError, match='simulation is not supported for typescript'):
            operation(project, root)


def extracted_project(root, files, with_resolver=True):
    """Exercise shared map consumers before adapter registration is enabled."""
    from flowmap.inventory import build_inventory
    from flowmap.lang.ecmascript.extract import extract_module
    from flowmap.lang.ecmascript.link import link_ecmascript
    from flowmap.overview import extract_relationships
    from flowmap.project import Project
    write(root, files)
    modules = {file: extract_module(file, source.encode()) for file, source in files.items()}
    inventory = build_inventory(root)
    for item in inventory.files.values():
        item.analysis = 'semantic'
    resolvers = {}
    if with_resolver:
        linker = link_ecmascript(root, modules)
        resolvers = {'javascript': linker, 'typescript': linker}
    project = Project(root, modules, {f.id: f for m in modules.values() for f in m.functions.values()},
                      {c.id: c for m in modules.values() for c in m.classes.values()},
                      resolvers=resolvers, inventory=inventory)
    project.relationships = extract_relationships(project)
    return project


def test_parsed_ecmascript_without_resolver_keeps_semantic_status(tmp_path):
    project = extracted_project(tmp_path, {
        'app.js': 'function run(require) { require("./fake.js"); }',
        'fake.js': 'function fake() {}',
    }, with_resolver=False)
    assert project.inventory.files['app.js'].analysis == 'semantic'
    assert project.relationships == []


def test_parsed_ecmascript_map_uses_ast_imports_and_surfaces_partial_diagnostics(tmp_path):
    project = extracted_project(tmp_path, {
        'lib.ts': 'export function save(v: number) { return v; }',
        'app.js': "import {save} from './lib.js'; function run(v) { switch(v) { case 1: save(v); } return save(v); }",
    })
    graph = build_overview(project)
    assert [(e.source, e.target, e.confidence) for e in graph.edges] == [('file:app.js', 'file:lib.ts', 'verified')]
    node = next(n for n in graph.nodes if n.id == 'file:app.js')
    assert node.detail['analysis'] == 'partial'
    assert any(d['code'] == 'unsupported-switch-fallthrough' for d in node.detail['diagnostics'])
    assert project.inventory.files['app.js'].analysis == 'partial'
