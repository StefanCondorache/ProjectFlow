"""End-to-end Rust crates through the shared consumers."""
from pathlib import Path

import pytest

from flowmap.dataflow import trace
from flowmap.flow import build_flow
from flowmap.ir import Call, walk
from flowmap.journey import build_journey
from flowmap.mermaid import to_mermaid
from flowmap.project import load_project
from flowmap.server import project_summary
from flowmap.simulate import expects, simulate

FILES = {
    'Cargo.toml': '[package]\nname = "shop"\n',
    'src/main.rs': '''mod store;

use crate::store::Store;
use serde::Serialize;

fn main() { run(3); }

fn run(count: i32) {
    let mut store = Store::new();
    store.add(count);
}
''',
    'src/store.rs': '''pub struct Store { total: i32 }

impl Store {
    pub fn new() -> Self { Store { total: 0 } }
    pub fn add(&mut self, amount: i32) { if amount > 0 { self.total = self.check(amount); } }
    fn check(&self, value: i32) -> i32 { value }
}
''',
    'tool.py': 'def helper(v):\n    return v\n',
}


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
    write(tmp_path, FILES)
    return load_project(tmp_path)


def test_capabilities_languages_and_status(project):
    summary = project_summary(project)
    assert summary['capabilities']['flow_languages'] == ['python', 'rust']
    assert summary['capabilities']['simulation_languages'] == ['python']
    assert summary['file_languages']['src/store.rs'] == 'rust'
    assert project.inventory.files['src/store.rs'].analysis == 'semantic'


def test_flow_trail_journey_and_mermaid(project):
    run, add, check = function(project, 'run'), function(project, 'Store.add'), function(project, 'Store.check')
    call = [c for c in walk(run.body) if isinstance(c, Call)][-1]
    assert (call.kind, call.target, call.confidence) == ('project', add.id, 'exact')
    graph = build_flow(project, run.id, depth=2)
    start = next(n for n in graph.nodes if n.kind == 'start' and n.detail.get('function') == add.id)
    assert start.detail['params'] == ['amount']
    assert any(n.kind == 'step' and n.detail.get('target') == check.id for n in graph.nodes)
    trail = trace(project, run.id, 's', 'count')
    assert {c.function for c in trail.contexts.values() if c.active} == {run.id, add.id, check.id}
    journey = build_journey(project, trail)
    assert {n.detail['target'] for n in journey.nodes if n.kind == 'step'} == {run.id, add.id, check.id}
    assert 'Store.add' in to_mermaid(graph)


def test_module_and_use_evidence(project):
    found = {(r.source, r.target, r.evidence['text']): r.confidence for r in project.relationships}
    assert found[('src/main.rs', 'file:src/store.rs', 'mod store;')] == 'verified'
    assert found[('src/main.rs', 'file:src/store.rs', 'use crate::store::Store;')] == 'verified'
    assert found[('src/main.rs', 'external:serde', 'use serde::Serialize;')] == 'inferred'


def test_simulation_is_rejected(project):
    for operation in (simulate, expects):
        with pytest.raises(ValueError, match='rust'):
            operation(project, function(project, 'run').id)
