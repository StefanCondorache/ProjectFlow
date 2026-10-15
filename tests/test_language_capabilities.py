"""Shared consumers must respect explicit and implicit receiver semantics."""
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from flowmap.dataflow import bind, trace
from flowmap.flow import build_flow, display_name
from flowmap.ir import Arg, Call, ClassDef, Function, Module, Param, Return, self_name
from flowmap.lang import Adapter
from flowmap.given import needs
from flowmap.project import Project, load_project
from flowmap.server import make_server, project_summary
from flowmap.simulate import expects, simulate


def function(name='Store.save', *, language='typescript', receiver='this', params=None, body=None, **kwargs):
    return Function(f'a.ts::{name}@1:0', name.rsplit('.', 1)[-1], name, 'a.ts', 1, 3,
                    params if params is not None else [Param('value')], None, None, [], body or [],
                    cls='a.ts::Store', language=language, receiver=receiver, **kwargs)


def project_of(tmp_path, *functions):
    module = Module('a.ts', 'typescript', None, [], {f.id: f for f in functions}, {}, False)
    return Project(tmp_path, {'a.ts': module}, dict(module.functions), {})


def test_implicit_receiver_keeps_first_real_argument(tmp_path):
    fn = function()
    call = Call('store.save', [Arg('order', ['order'])], ['store'], [], 2, 0, 'store.save(order)')
    assert self_name(fn) == 'this'
    assert bind(call, fn, {'order'}) == {'value'}
    assert bind(call, fn, {'store'}) == {'this'}
    start = build_flow(project_of(tmp_path, fn), fn.id).nodes[0]
    assert start.detail['params'] == ['value']
    assert start.detail['language'] == 'typescript'


def test_receiver_hot_method_is_opened_and_binds_this(tmp_path):
    method = function(params=[], body=[Return('this', ['this'], 3)])
    call = Call('store.save', [], ['store'], ['result'], 2, 0, 'store.save()', kind='project', target=method.id)
    caller = function('main', receiver=None, params=[Param('store')], body=[call])
    trail = trace(project_of(tmp_path, caller, method), caller.id, 's', 'store')
    assert trail.opened == {'c2.0'}
    assert trail.contexts['c2.0/'].params == ['this']
    assert trail.contexts['c2.0/'].returns
    assert trail.names['c2.0'] == ['result']


def test_constructor_metadata_carries_arguments_into_the_created_object(tmp_path):
    constructor = function('Store.constructor', is_constructor=True)
    call = Call('Store', [Arg('order', ['order'])], [], ['store'], 2, 0, 'new Store(order)', kind='project', target=constructor.id)
    caller = function('main', receiver=None, params=[Param('order')], body=[call])
    project = project_of(tmp_path, caller, constructor)
    project.classes['a.ts::Store'] = ClassDef('a.ts::Store', 'Store', 'Store', 'a.ts', 1, [], {}, {}, 'Keeps orders')
    trail = trace(project, caller.id, 's', 'order')
    assert trail.names['c2.0'] == ['store']
    assert trail.contexts['c2.0/'].params == ['value']
    assert display_name(project, constructor) == 'Store()'
    assert build_flow(project, caller.id).nodes[1].detail['doc'] == 'Keeps orders'


def test_python_explicit_receiver_static_and_unbound_parameters_are_preserved(tmp_path):
    (tmp_path / 'm.py').write_text('class Store:\n    def save(self, value):\n        return value\n    @staticmethod\n    def static(value):\n        return value\n')
    project = load_project(tmp_path)
    method = project.functions['m.py::Store.save']
    static = project.functions['m.py::Store.static']
    assert self_name(method) == 'self'
    assert self_name(static) is None
    assert build_flow(project, method.id).nodes[0].detail['params'] == ['value']
    assert build_flow(project, static.id).nodes[0].detail['params'] == ['value']
    bound = Call('store.save', [Arg('order', ['order'])], ['store'], [], 2, 0, 'store.save(order)')
    assert bind(bound, method, {'order'}) == {'value'}
    unbound = Call('save', [Arg('store', ['store']), Arg('order', ['order'])], [], [], 2, 0, 'save(store, order)')
    assert bind(unbound, method, {'store'}) == {'self'}
    assert bind(unbound, method, {'order'}) == {'value'}


@pytest.mark.parametrize('operation', [simulate, expects, needs])
def test_public_simulation_rejects_unsupported_language_before_evaluation(tmp_path, operation):
    # This Python-looking body must never be interpreted for a TypeScript function.
    fn = function('main', receiver=None, body=[Return('1 + 2', [], 2, expr='1 + 2')])
    with pytest.raises(ValueError, match='simulation.*typescript|typescript.*simulation'):
        operation(project_of(tmp_path, fn), fn.id)


def test_project_capabilities_come_from_registered_adapters_and_parsed_modules(tmp_path, monkeypatch):
    from flowmap.lang.python import ADAPTER
    fake = Adapter('typescript', ('.ts',), lambda file, source: Module(file, 'typescript', None, [], {}, {}, False),
                   lambda root, modules: None, lambda root, modules, commands: [])
    monkeypatch.setattr('flowmap.project.adapters', lambda: [ADAPTER, fake])
    (tmp_path / 'a.ts').write_text('export {}')
    (tmp_path / 'm.py').write_text('def main(): pass\n')
    project = load_project(tmp_path)
    summary = project_summary(project)
    assert summary['file_languages'] == {'a.ts': 'typescript', 'm.py': 'python'}
    assert summary['capabilities'] == {'overview': True, 'flow_languages': ['python', 'typescript'],
                                       'trail_languages': ['python', 'typescript'], 'simulation_languages': ['python']}
    assert project.function_language('m.py::main') == 'python'
    assert project.supports_simulation('m.py::main')
    assert not fake.simulation
    assert fake.family == ''


def test_family_linker_receives_cross_language_modules_once(tmp_path, monkeypatch):
    # The linker annotates real modules only when both sibling languages are present.
    def link(root, modules):
        assert set(modules) == {'a.js', 'b.ts'}
        assert all('linked' not in module.metadata for module in modules.values())
        for module in modules.values():
            module.metadata['linked'] = True
        return modules
    adapters = [Adapter(language, (ext,), lambda file, source, language=language: Module(file, language, None, [], {}, {}, False),
                        link, lambda root, modules, commands: [], family='ecmascript')
                for language, ext in [('javascript', '.js'), ('typescript', '.ts')]]
    monkeypatch.setattr('flowmap.project.adapters', lambda: adapters)
    (tmp_path / 'a.js').write_text('')
    (tmp_path / 'b.ts').write_text('')
    project = load_project(tmp_path)
    assert all(module.metadata['linked'] for module in project.modules.values())
    assert project.resolvers['javascript'] is project.resolvers['typescript']


@pytest.fixture
def non_python_api(tmp_path):
    fn = function('main', receiver=None)
    project = project_of(tmp_path, fn)
    (tmp_path / 'a.ts').write_text('export function main() {}')
    project.inventory = load_project(tmp_path).inventory
    server = make_server(project, web_dir=tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', fn.id
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('endpoint,method', [('/api/simulate', 'GET'), ('/api/simulate', 'POST'), ('/api/expects', 'GET')])
def test_api_rejects_unsupported_simulation(non_python_api, endpoint, method):
    base, root = non_python_api
    request = Request(base + endpoint + ('?' + urlencode({'root': root}) if method == 'GET' else ''),
                      data=json.dumps({'root': root}).encode() if method == 'POST' else None,
                      headers={'Content-Type': 'application/json'}, method=method)
    with pytest.raises(HTTPError) as error:
        urlopen(request)
    assert error.value.code == 400
    assert 'typescript' in json.load(error.value)['error']


@pytest.mark.parametrize('operation', [simulate, expects])
def test_python_root_cannot_evaluate_a_non_python_project_callee(tmp_path, operation):
    callee = function('convert', receiver=None, params=[], body=[Return('1 + 2', [], 2, expr='1 + 2')])
    call = Call('convert', [], [], ['result'], 2, 0, 'convert()', kind='project', target=callee.id)
    caller = Function('m.py::main', 'main', 'main', 'm.py', 1, 3, [], None, None, [], [call])
    project = project_of(tmp_path, callee)
    project.functions[caller.id] = caller
    project.modules['m.py'] = Module('m.py', 'python', None, [], {caller.id: caller}, {}, False)
    options = {'at': 'e'} if operation is expects else {}
    with pytest.raises(ValueError, match='simulation.*typescript|typescript.*simulation'):
        operation(project, caller.id, **options)


def test_simulation_capability_honors_adapter_declaration(tmp_path, monkeypatch):
    from dataclasses import replace
    from flowmap.lang.python import ADAPTER
    (tmp_path / 'm.py').write_text('def main(): return 1\n')
    monkeypatch.setattr('flowmap.project.adapters', lambda: [replace(ADAPTER, simulation=False)])
    project = load_project(tmp_path)
    assert project_summary(project)['capabilities']['simulation_languages'] == []
    assert not project.supports_simulation('m.py::main')
    with pytest.raises(ValueError, match='simulation.*python'):
        simulate(project, 'm.py::main')
