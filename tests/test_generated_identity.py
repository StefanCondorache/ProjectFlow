"""Real-source regressions for user bindings that resemble generated results."""
from dataclasses import asdict

import pytest

from flowmap.dataflow import trace
from flowmap.flow import build_flow, item_key
from flowmap.ir import Call, walk
from flowmap.journey import build_journey
from flowmap.project import load_project
from flowmap.server import _Handler


@pytest.fixture(params=['js', 'ts'])
def example(tmp_path, request):
    file = f'app.{request.param}'
    (tmp_path / file).write_text('''
function echo($value) { return $value; }
function run($input, $1) {
  const $value = echo($input);
  const $remote = external($value);
  return echo(echo($remote));
}
const $global = new Unknown();
''')
    project = load_project(tmp_path)
    fn = next(f for f in project.functions.values() if f.name == 'run')
    calls = [i for i in walk(fn.body) if isinstance(i, Call)]
    return project, fn, calls


def test_dollar_parameters_and_results_remain_visible(example):
    project, fn, calls = example
    graph = build_flow(project, fn.id)
    start_edge = next(e for e in graph.edges if e.source == 's')
    assert start_edge.label == '$input, $1'
    step = next(n for n in graph.nodes if n.id == item_key(calls[0]))
    assert step.detail['defs'] == ['$value']
    edge = next(e for e in graph.edges if e.source == step.id)
    assert (edge.label, edge.data) == ('$value', ['$value'])
    assert asdict(start_edge)['generated_data'] == []
    assert asdict(edge)['generated_data'] == []


def test_unknown_call_assigned_to_dollar_variable_stays_in_trail(example):
    project, fn, calls = example
    trail = trace(project, fn.id, 's', '$input')
    remote = item_key(calls[1])
    assert remote in trail.keep
    assert remote in trail.uses
    graph = build_flow(project, fn.id, trail=trail)
    assert next(n for n in graph.nodes if n.id == remote).kind == 'use'
    edge = next(e for e in graph.edges if e.source == remote)
    assert (edge.label, edge.data) == ('$remote', ['$remote'])
    assert asdict(edge)['generated_data'] == []


def test_journey_and_server_identify_user_and_generated_origins(example):
    project, fn, calls = example
    handler = object.__new__(_Handler)
    handler.project = project
    for at, name, expected, generated in [
        ('s', '$1', '$1', False),
        (item_key(calls[0]), '$value', '$value', False),
        (item_key(calls[2]), calls[2].defs[0], 'result', True),
        (item_key(calls[0]) + '/s', '$value', '$value', False),
    ]:
        trail = trace(project, fn.id, at, name)
        assert trail.found
        graph = build_journey(project, trail)
        assert next(n.label for n in graph.nodes if n.id == 'origin') == expected
        edge = next(e for e in graph.edges if e.source == 'origin')
        assert asdict(edge)['generated_data'] == ([name] if generated else [])
        _, info = handler._graph({'root': fn.id, 'at': at, 'var': name})
        assert info['generated'] is generated


def test_generated_result_keeps_result_label_and_identity(example):
    project, fn, calls = example
    trail = trace(project, fn.id, 's', '$input')
    graph = build_flow(project, fn.id, trail=trail)
    edge = next(e for e in graph.edges if e.source == item_key(calls[2]))
    assert edge.label == 'result'
    assert edge.data == calls[2].defs
    assert asdict(edge)['generated_data'] == calls[2].defs
    assert '$1' not in fn.generated_temps
    assert calls[2].defs[0] in fn.generated_temps
    # Expanded callee parameters belong to the callee, not the caller's temps.
    inner = next(e for e in graph.edges if e.source == item_key(calls[0]) + '/s')
    assert (inner.label, inner.data, inner.generated_data) == ('$value', ['$value'], [])


def test_dollar_module_global_hints_are_preserved(example):
    project, fn, _ = example
    assert project.modules[fn.file].globals['$global'] == [('construct', 'Unknown')]


def test_python_implicit_temporary_identity_is_compatible(tmp_path):
    (tmp_path / 'app.py').write_text('def echo(value):\n    return value\ndef run(value):\n    return echo(echo(value))\n')
    project = load_project(tmp_path)
    fn = project.functions['app.py::run']
    calls = [i for i in walk(fn.body) if isinstance(i, Call)]
    name = calls[0].defs[0]
    trail = trace(project, fn.id, item_key(calls[0]), name)
    assert next(n.label for n in build_journey(project, trail).nodes if n.id == 'origin') == 'result'
    edge = next(e for e in build_flow(project, fn.id, trail=trail).edges if e.source == item_key(calls[0]))
    assert (edge.label, edge.data) == ('result', [name])
    assert asdict(edge)['generated_data'] == [name]


def test_result_projection_stays_with_its_assignment_and_block(tmp_path):
    (tmp_path / 'app.js').write_text('''
function echo(value) { return value; }
function run($input, extra, obj, flag) {
  if (flag) { const $branch = echo($input); }
  const $outside = extra;
  const $combined = echo($input) + extra;
  obj.value = echo($input);
  const $final = echo(echo($input));
}
''')
    project = load_project(tmp_path)
    fn = next(f for f in project.functions.values() if f.name == 'run')
    calls = [i for i in walk(fn.body) if isinstance(i, Call)]
    graph = build_flow(project, fn.id)
    assert [next(n.detail['defs'] for n in graph.nodes if n.id == item_key(call))
            for call in calls] == [['$branch'], [], [], [], ['$final']]
    # Actual extraction results stay synthetic and are never rewritten by views.
    assert all(len(call.defs) == 1 and call.defs[0] in fn.generated_temps for call in calls)


def test_named_call_origin_survives_lowered_assignment(example):
    project, fn, calls = example
    trail = trace(project, fn.id, item_key(calls[0]), '$value')
    assert item_key(calls[1]) in trail.keep
    assert trail.contexts[''].returns
    graph = build_flow(project, fn.id, trail=trail)
    assert next(e.label for e in graph.edges if e.source == item_key(calls[0])) == '$value'


def test_generated_names_belong_to_the_function_that_created_them(tmp_path):
    (tmp_path / 'app.js').write_text('''
function echo(value) { return value; }
function outer($input) {
  const nested = () => echo(echo($input));
  class Local { run($value) { return echo(echo($value)); } }
  return $input;
}
''')
    project = load_project(tmp_path)
    outer = next(f for f in project.functions.values() if f.name == 'outer')
    nested = next(f for f in project.functions.values() if f.name == 'nested')
    method = next(f for f in project.functions.values() if f.name == 'run')
    assert not outer.generated_temps
    for fn, parameter in [(nested, '$input'), (method, '$value')]:
        calls = [i for i in walk(fn.body) if isinstance(i, Call)]
        trail = trace(project, fn.id, item_key(calls[0]), calls[0].defs[0])
        assert next(n.label for n in build_journey(project, trail).nodes if n.id == 'origin') == 'result'
        assert parameter not in fn.generated_temps
    assert nested.generated_temps is not method.generated_temps
