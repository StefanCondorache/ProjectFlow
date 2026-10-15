"""Registered Rust adapter through HTTP and CLI, without running cargo."""
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from flowmap.cli import main
from flowmap.project import load_project
from flowmap.server import make_server

FILES = {
    'Cargo.toml': '[package]\nname = "shop"\n',
    'src/main.rs': 'mod repo;\n\nuse repo::Repo;\n\nfn main() { run(2); }\n\nfn run(count: i32) { Repo::new().save(count); }\n',
    'src/repo.rs': 'pub struct Repo;\n\nimpl Repo {\n    pub fn new() -> Self { Repo }\n    pub fn save(&self, value: i32) { self.store(value); }\n    fn store(&self, value: i32) {}\n}\n',
}


@pytest.fixture
def rust_root(tmp_path):
    for path, source in FILES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(source)
    return tmp_path


@pytest.fixture
def rust_http(rust_root):
    project = load_project(rust_root)
    server = make_server(project, web_dir=rust_root, port=0)
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


def function_id(project, qualname):
    return next(f.id for f in project.functions.values() if f.qualname == qualname)


def test_project_entries_and_search(rust_http):
    base, project = rust_http
    status, data = request(base, '/api/project')
    assert status == 200 and data['capabilities']['flow_languages'] == ['rust']
    assert data['capabilities']['simulation_languages'] == []
    assert data['entries'][0]['target'] == function_id(project, 'main')
    assert data['entries'][0]['kind'] == 'cargo-bin'
    status, hits = request(base, '/api/search', {'q': 'save'})
    assert status == 200 and hits[0]['id'] == function_id(project, 'Repo.save')


def test_flow_trail_mermaid_map_and_source(rust_http):
    base, project = rust_http
    root = function_id(project, 'run')
    status, flow = request(base, '/api/flow', {'root': root, 'depth': 1})
    assert status == 200 and any('Repo.save' in node['label'] for node in flow['nodes'])
    status, trail = request(base, '/api/trail', {'root': root, 'at': 's', 'var': 'count', 'view': 'journey'})
    assert status == 200 and trail['trail']['var'] == 'count'
    status, mermaid = request(base, '/api/mermaid', {'root': root, 'depth': 2})
    assert status == 200 and 'Repo.save' in mermaid and 'store' in mermaid
    status, overview = request(base, '/api/overview', {'path': 'src/main.rs'})
    edge = next(e for e in overview['edges'] if e['source'] == 'file:src/main.rs')
    assert edge['confidence'] == 'verified'
    status, source = request(base, '/api/source', {'file': 'src/repo.rs', 'start': 5, 'end': 5})
    assert status == 200 and source['lines'] == ['    pub fn save(&self, value: i32) { self.store(value); }']


def test_rust_simulation_and_expects_are_rejected(rust_http):
    base, project = rust_http
    root = function_id(project, 'run')
    for route in ('/api/simulate', '/api/expects'):
        status, data = request(base, route, {'root': root})
        assert status == 400 and 'rust' in data['error'] and 'not supported' in data['error']


def test_cli_lists_and_exports(rust_root, capsys):
    assert main([str(rust_root), '--list']) == 0
    listing = capsys.readouterr().out
    assert 'cargo run --bin shop' in listing and 'cargo-bin' in listing
    assert main([str(rust_root), '--mermaid', '1', '--depth', '3']) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith('flowchart LR') and 'Repo.save' in rendered
