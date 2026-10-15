"""Real registered JS/TS adapters through HTTP and CLI, without source execution."""
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from flowmap.cli import main
from flowmap.project import load_project
from flowmap.server import make_server


@pytest.fixture
def ecma_root(tmp_path):
    (tmp_path / 'package.json').write_text(json.dumps({'scripts': {'start': 'tsx main.ts', 'worker': 'node worker.js'}}))
    (tmp_path / 'main.ts').write_text("import {finish} from './helper.js';\nexport function run(value: string) { return finish(value); }\nrun('hello');\n")
    (tmp_path / 'helper.js').write_text('export function finish(value) { return value; }\n')
    (tmp_path / 'worker.js').write_text("import {finish} from './helper.js';\nexport function worker(value) { return finish(value); }\nworker('job');\n")
    return tmp_path


@pytest.fixture
def ecma_http(ecma_root):
    project = load_project(ecma_root)
    server = make_server(project, web_dir=ecma_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}', project
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(base, route, query=None, body=None):
    url = base + route + ('?' + urlencode(query) if query else '')
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read().decode()
        return response.status, json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw


def function_id(project, name):
    return next(f.id for f in project.functions.values() if f.name == name)


def test_project_summary_search_and_detected_entries(ecma_http):
    base, project = ecma_http
    status, data = request(base, '/api/project')
    assert status == 200
    assert data['file_languages'] == {'helper.js': 'javascript', 'main.ts': 'typescript', 'worker.js': 'javascript'}
    assert data['capabilities']['flow_languages'] == ['javascript', 'typescript']
    assert data['capabilities']['trail_languages'] == ['javascript', 'typescript']
    assert data['capabilities']['simulation_languages'] == []
    assert {entry['target'] for entry in data['entries']} == {'main.ts::<main>', 'worker.js::<main>'}
    assert next(e for e in project.entries if e.target == 'main.ts::<main>').reach == 2
    status, hits = request(base, '/api/search', {'q': 'run'})
    assert status == 200 and hits[0]['id'] == function_id(project, 'run')


def test_real_function_flow_trail_and_mermaid(ecma_http):
    base, project = ecma_http
    root = function_id(project, 'run')
    status, flow = request(base, '/api/flow', {'root': root, 'depth': 1})
    assert status == 200 and any('finish' in node['label'] for node in flow['nodes'])
    status, trail = request(base, '/api/trail', {'root': root, 'at': 's', 'var': 'value', 'view': 'steps'})
    assert status == 200 and trail['trail']['var'] == 'value'
    assert any('finish' in node['label'] for node in trail['nodes'])
    status, mermaid = request(base, '/api/mermaid', {'root': root, 'depth': 1})
    assert status == 200 and mermaid.startswith('flowchart LR') and 'finish' in mermaid


def test_map_has_source_backed_import_and_source_is_readable(ecma_http):
    base, _ = ecma_http
    status, overview = request(base, '/api/overview', {'path': 'main.ts'})
    assert status == 200
    dependency = next(edge for edge in overview['edges']
                      if edge['source'] == 'file:main.ts' and edge['target'] == 'file:helper.js')
    assert dependency['confidence'] == 'verified'
    assert dependency['evidence'] == [
        {'file': 'main.ts', 'line': 1, 'text': "import {finish} from './helper.js';"},
    ]
    status, source = request(base, '/api/source', {'file': 'main.ts', 'start': 2, 'end': 2})
    assert status == 200 and source['lines'] == ['export function run(value: string) { return finish(value); }']
    status, bounded = request(base, '/api/map-source', {'file': 'helper.js'})
    assert status == 200 and 'finish' in bounded['lines'][0]


@pytest.mark.parametrize('name,language', [('run', 'typescript'), ('worker', 'javascript')])
def test_js_ts_simulation_and_expects_are_rejected(ecma_http, name, language):
    base, project = ecma_http
    root = function_id(project, name)
    for route in ('/api/simulate', '/api/expects'):
        status, data = request(base, route, {'root': root})
        assert status == 400 and language in data['error'] and 'not supported' in data['error']
    status, data = request(base, '/api/simulate', body={'root': root, 'inputs': {'value': 'x'}})
    assert status == 400 and language in data['error'] and 'not supported' in data['error']


def test_cli_lists_detected_entries_and_exports_entry_and_direct_function(ecma_root, capsys):
    assert main([str(ecma_root), '--list']) == 0
    listing = capsys.readouterr().out
    assert 'start' in listing and 'worker' in listing and 'package-script' in listing
    assert main([str(ecma_root), '--mermaid', 'start', '--depth', '2']) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith('flowchart LR') and 'run' in rendered and 'finish' in rendered
    root = function_id(load_project(ecma_root), 'run')
    assert main([str(ecma_root), '--mermaid', root, '--depth', '1']) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith('flowchart LR') and 'finish' in rendered
