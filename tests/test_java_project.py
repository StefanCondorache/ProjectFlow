"""End-to-end Java project behaviour through the shared consumers."""
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

APP = '''package a;

import b.Repo;
import java.util.List;

public class App {
  public static void main(String[] args) { new App().run(args.length); }
  void run(int n) { Repo r = new Repo(); r.save(n); }
}
'''
REPO = '''package b;

public class Repo {
  public void save(int value) { if (value > 0) { store(clean(value)); } }
  private int clean(int v) { return v; }
  private void store(int v) { System.out.println(v); }
}
'''


def write(root: Path, files: dict[str, str]):
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)


def function(project, name):
    found = [fn for fn in project.functions.values() if fn.qualname == name]
    assert len(found) == 1
    return found[0]


@pytest.fixture
def project(tmp_path):
    write(tmp_path, {'src/main/java/a/App.java': APP, 'src/main/java/b/Repo.java': REPO,
                     'tool.py': 'def helper(v):\n    return v\n', 'web/app.ts': 'export function go(v: number) { return v; }'})
    return load_project(tmp_path)


def test_capabilities_and_languages(project):
    summary = project_summary(project)
    assert summary['capabilities']['flow_languages'] == ['java', 'python', 'typescript']
    assert summary['capabilities']['trail_languages'] == ['java', 'python', 'typescript']
    assert summary['capabilities']['simulation_languages'] == ['python']
    assert summary['file_languages']['src/main/java/a/App.java'] == 'java'
    assert project.inventory.files['src/main/java/a/App.java'].analysis == 'semantic'
    assert 'tool.py::helper' in project.functions


def test_flow_trail_journey_and_mermaid(project):
    run, save = function(project, 'App.run'), function(project, 'Repo.save')
    call = [c for c in walk(run.body) if isinstance(c, Call)][-1]
    assert (call.kind, call.target, call.confidence) == ('project', save.id, 'exact')
    graph = build_flow(project, run.id, depth=2)
    start = next(n for n in graph.nodes if n.kind == 'start' and n.detail.get('function') == save.id)
    assert start.detail['params'] == ['value']
    clean, store = function(project, 'Repo.clean'), function(project, 'Repo.store')
    assert any(n.kind == 'step' and n.detail.get('target') == clean.id for n in graph.nodes)
    trail = trace(project, run.id, 's', 'n')
    assert trail.found
    assert {c.function for c in trail.contexts.values() if c.active} == {run.id, save.id, clean.id, store.id}
    journey = build_journey(project, trail)
    assert {n.detail['target'] for n in journey.nodes if n.kind == 'step'} == {run.id, save.id, clean.id, store.id}
    assert 'Repo.save' in to_mermaid(graph)


def test_import_evidence_in_project_map(project):
    found = {(r.source, r.target): r for r in project.relationships}
    edge = found[('src/main/java/a/App.java', 'file:src/main/java/b/Repo.java')]
    assert edge.confidence == 'verified'
    assert edge.evidence == {'file': 'src/main/java/a/App.java', 'line': 3, 'text': 'import b.Repo;'}
    assert found[('src/main/java/a/App.java', 'external:java.util')].confidence == 'inferred'
    assert build_overview(project).edges is not None


def test_simulation_is_rejected_for_java(project):
    run = function(project, 'App.run')
    for operation in (simulate, expects):
        with pytest.raises(ValueError, match='java'):
            operation(project, run.id)


def test_diagnostics_mark_partial_analysis(tmp_path):
    write(tmp_path, {'A.java': 'class A { void run(int n) { switch (n) { case 1: a(); case 2: b(); } } void a() {} void b() {} }'})
    project = load_project(tmp_path)
    assert project.inventory.files['A.java'].analysis == 'partial'
    assert any(d['code'] == 'unsupported-switch-fallthrough' for d in project.modules['A.java'].diagnostics)


def test_java_handlers_are_labelled_catch(tmp_path):
    write(tmp_path, {'A.java': 'class A { void run() { try { go(); } catch (IllegalStateException e) { go(); } } void go() { go2(); } void go2() {} }'})
    project = load_project(tmp_path)
    labels = [n.label for n in build_flow(project, function(project, 'A.run').id).nodes]
    assert 'catch IllegalStateException' in labels and not any(l.startswith('except') for l in labels)
