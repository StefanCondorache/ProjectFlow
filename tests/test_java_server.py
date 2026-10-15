"""Registered Java adapter through HTTP and CLI, without compiling or running anything."""
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

from flowmap.cli import main
from flowmap.project import load_project
from flowmap.server import make_server

APP = '''package a;

import b.Repo;

public class App {
  public static void main(String[] args) { new App().run(args.length); }
  void run(int count) { new Repo().save(count); }
}
'''
REPO = '''package b;

public class Repo {
  public void save(int value) { store(value); }
  private void store(int value) {}
}
'''


@pytest.fixture
def java_root(tmp_path):
    (tmp_path / 'pom.xml').write_text('<project><properties><start-class>a.App</start-class></properties></project>')
    for path, source in {'src/main/java/a/App.java': APP, 'src/main/java/b/Repo.java': REPO}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(source)
    return tmp_path


@pytest.fixture
def java_http(java_root):
    project = load_project(java_root)
    server = make_server(project, web_dir=java_root, port=0)
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


def test_project_entries_and_search(java_http):
    base, project = java_http
    status, data = request(base, '/api/project')
    assert status == 200
    assert data['capabilities']['flow_languages'] == ['java']
    assert data['capabilities']['simulation_languages'] == []
    assert data['entries'][0]['target'] == function_id(project, 'App.main')
    assert data['entries'][0]['kind'] == 'build-main'
    status, hits = request(base, '/api/search', {'q': 'save'})
    assert status == 200 and hits[0]['id'] == function_id(project, 'Repo.save')


def test_flow_trail_mermaid_map_and_source(java_http):
    base, project = java_http
    root = function_id(project, 'App.run')
    status, flow = request(base, '/api/flow', {'root': root, 'depth': 1})
    assert status == 200 and any('Repo.save' in node['label'] for node in flow['nodes'])
    status, trail = request(base, '/api/trail', {'root': root, 'at': 's', 'var': 'count', 'view': 'journey'})
    assert status == 200 and trail['trail']['var'] == 'count'
    status, mermaid = request(base, '/api/mermaid', {'root': root, 'depth': 2})
    assert status == 200 and 'Repo.save' in mermaid and 'store' in mermaid
    status, overview = request(base, '/api/overview', {'path': 'src/main/java/a/App.java'})
    # The focused map folds Repo.java into its collapsed folder; the edge keeps its evidence.
    edge = next(e for e in overview['edges'] if e['source'] == 'file:src/main/java/a/App.java')
    assert edge['confidence'] == 'verified' and edge['evidence'][0]['text'] == 'import b.Repo;'
    status, source = request(base, '/api/source', {'file': 'src/main/java/b/Repo.java', 'start': 4, 'end': 4})
    assert status == 200 and source['lines'] == ['  public void save(int value) { store(value); }']


def test_java_simulation_and_expects_are_rejected(java_http):
    base, project = java_http
    root = function_id(project, 'App.run')
    for route in ('/api/simulate', '/api/expects'):
        status, data = request(base, route, {'root': root})
        assert status == 400 and 'java' in data['error'] and 'not supported' in data['error']
    status, data = request(base, '/api/simulate', body={'root': root, 'inputs': {'count': 1}})
    assert status == 400 and 'java' in data['error']


def test_cli_lists_and_exports(java_root, capsys):
    assert main([str(java_root), '--list']) == 0
    listing = capsys.readouterr().out
    assert 'java a.App' in listing and 'build-main' in listing
    assert main([str(java_root), '--mermaid', '1', '--depth', '3']) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith('flowchart LR') and 'Repo.save' in rendered
