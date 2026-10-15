"""Rust crates, modules, paths, impls and receiver types."""
from pathlib import Path

import pytest

from flowmap.ir import Call, walk
from flowmap.lang.rust.extract import extract_module
from flowmap.lang.rust.link import link_rust


def linked(root: Path, files: dict[str, str]):
    for path, source in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(source)
    modules = {p: extract_module(p, s.encode()) for p, s in files.items() if p.endswith('.rs')}
    return modules, link_rust(root, modules)


def fn(modules, qualname, file=None):
    found = [f for m in modules.values() for f in m.functions.values()
             if f.qualname == qualname and (file is None or f.file == file)]
    assert len(found) == 1, (qualname, found)
    return found[0]


def calls(function):
    return [i for i in walk(function.body) if isinstance(i, Call)]


def name_of(modules, call):
    return None if call.target is None else next(f.qualname for m in modules.values()
                                                 for f in m.functions.values() if f.id == call.target)


CARGO = '[package]\nname = "shop-core"\nversion = "0.1.0"\n'


def test_module_files_and_paths(tmp_path):
    modules, linker = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': 'pub mod store;\nmod util;\npub fn run() { store::save(); util::helper::go(); crate::store::save(); self::local(); }\nfn local() {}',
        'src/store.rs': 'pub mod ledger;\npub fn save() { ledger::write(); super::local(); }',
        'src/store/ledger.rs': 'pub fn write() {}',
        'src/util/mod.rs': 'pub mod helper;',
        'src/util/helper.rs': 'pub fn go() {}',
    })
    assert [name_of(modules, c) for c in calls(fn(modules, 'run'))] == ['save', 'go', 'save', 'local']
    assert [name_of(modules, c) for c in calls(fn(modules, 'save'))] == ['write', 'local']
    assert linker.module_path('src/store/ledger.rs') == 'shop_core::store::ledger'
    assert calls(fn(modules, 'run'))[0].confidence == 'exact'


def test_path_attribute_inline_modules_and_unreachable_files(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/main.rs': '#[path = "other/x.rs"]\nmod x;\nmod inner { pub fn go() { super::top(); } }\nfn top() {}\nfn main() { x::f(); inner::go(); }',
        'src/other/x.rs': 'pub fn f() {}',
        'src/stray.rs': 'pub fn f() {}',
    })
    assert [name_of(modules, c) for c in calls(fn(modules, 'main'))] == ['f', 'inner.go']
    assert name_of(modules, calls(fn(modules, 'inner.go'))[0]) == 'top'
    assert any(d['code'] == 'unreachable-module' for d in modules['src/stray.rs'].diagnostics)


def test_use_forms(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''mod a; mod b; mod c;
use crate::a::{one, two as second};
use a::three;
use b::*;
use c::*;
pub fn run() { one(); second(); three(); only_b(); shared(); }''',
        'src/a.rs': 'pub fn one() {} pub fn two() {} pub fn three() {}',
        'src/b.rs': 'pub fn only_b() {} pub fn shared() {}',
        'src/c.rs': 'pub fn shared() {}',
    })
    assert [name_of(modules, c) for c in calls(fn(modules, 'run'))] == ['one', 'two', 'three', 'only_b', None]


def test_workspace_crates_by_name_and_std_externals(tmp_path):
    modules, _ = linked(tmp_path, {
        'core/Cargo.toml': '[package]\nname = "shop-core"\n',
        'core/src/lib.rs': 'pub mod store; pub use store::Store;',
        'core/src/store.rs': 'pub struct Store; impl Store { pub fn new() -> Self { Store } pub fn add(&self) {} }',
        'app/Cargo.toml': '[package]\nname = "app"\n',
        'app/src/main.rs': '''use shop_core::store::Store;
use std::fs;
fn main() { let s = Store::new(); s.add(); let text = fs::read_to_string("x"); std::env::var("HOME"); }''',
    })
    c = calls(fn(modules, 'main'))
    assert [name_of(modules, x) for x in c[:2]] == ['Store.new', 'Store.add']
    assert (c[2].kind, c[2].external, c[2].io) == ('external', 'std::fs::read_to_string', ('file', 'in'))
    assert (c[3].external, c[3].io) == ('std::env::var', ('env', 'in'))


def test_impls_traits_and_receivers(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''pub struct Repo;
impl Repo { pub fn save(&self) {} }
pub struct Store { repo: Repo, total: i32 }
pub trait Named { fn name(&self) -> String { String::new() } fn id(&self) -> i32; }
impl Named for Store { fn id(&self) -> i32 { 1 } }
impl Store {
    pub fn new() -> Self { Store { repo: Repo, total: 0 } }
    pub fn add(&mut self, v: i32) { self.check(v); self.repo.save(); self.name(); self.id(); Self::helper(); }
    fn check(&self, v: i32) -> i32 { v }
    fn helper() {}
    fn by_ref(other: &Store, typed: &mut Repo) { other.check(1); typed.save(); }
}
pub fn run() { let mut s = Store::new(); s.add(1); let t: Store = make(); t.add(2); Store { repo: Repo, total: 0 }.add(3); Store::new().check(4); }
fn make() -> Store { Store::new() }''',
    })
    assert [name_of(modules, c) for c in calls(fn(modules, 'Store.add'))] == [
        'Store.check', 'Repo.save', 'Named.name', 'Store.id', 'Store.helper']
    assert [name_of(modules, c) for c in calls(fn(modules, 'Store.by_ref'))] == ['Store.check', 'Repo.save']
    run = [name_of(modules, c) for c in calls(fn(modules, 'run'))]
    assert run == ['Store.new', 'Store.add', 'make', 'Store.add', 'Store.add', 'Store.new', 'Store.check']


def test_dynamic_generic_wrapped_and_unknown_receivers_stay_unresolved(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''pub trait Shape { fn area(&self) -> f64; }
pub struct Sq; impl Shape for Sq { fn area(&self) -> f64 { 1.0 } }
pub struct Other; impl Other { fn area(&self) -> f64 { 2.0 } }
pub fn run<T: Shape>(d: &dyn Shape, g: T, b: Box<Sq>, i: impl Shape, u: Vec<Sq>) {
    d.area(); g.area(); b.area(); i.area(); u.len(); unknown().area(); }''',
    })
    c = calls(fn(modules, 'run'))
    assert [x.callee for x in c] == ['d.area', 'g.area', 'b.area', 'i.area', 'u.len', 'unknown', '$6.area']
    assert [x.target for x in c] == [None] * 7
    assert (c[4].kind, c[4].external) == ('external', 'Vec::len')


def test_two_traits_with_the_same_method_are_ambiguous(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''pub trait A { fn go(&self); } pub trait B { fn go(&self); }
pub struct S; impl A for S { fn go(&self) {} } impl B for S { fn go(&self) {} }
pub fn run(s: &S) { s.go(); }''',
    })
    assert calls(fn(modules, 'run'))[0].target is None


def test_nested_functions_closures_and_no_name_guessing(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''mod other { pub fn helper() {} }
pub fn run(f: fn()) { fn inner() {} inner(); helper(); f(); let c = || inner(); c(); }''',
    })
    c = calls(fn(modules, 'run'))
    assert [name_of(modules, x) for x in c] == ['run.inner', None, None, None]
    closure = next(f for f in modules['src/lib.rs'].functions.values() if '<closure' in f.qualname)
    assert name_of(modules, calls(closure)[0]) == 'run.inner'


def test_derived_self_returning_std_traits_keep_the_receiver_type(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''#[derive(Default, Clone)]
pub struct Ledger { n: i32 }
impl Ledger { pub fn record(&mut self) {} }
impl From<i32> for Ledger { fn from(n: i32) -> Self { Ledger { n } } }
pub fn run(base: &Ledger) {
    let mut a = Ledger::default(); a.record();
    let mut b = base.clone(); b.record();
    Ledger::from(3).record();
}''',
    })
    c = calls(fn(modules, 'run'))
    assert [name_of(modules, x) for x in c] == [None, 'Ledger.record', None, 'Ledger.record', 'Ledger.from', 'Ledger.record']


def test_function_local_items_and_uses_do_not_leak_into_the_module(tmp_path):
    modules, _ = linked(tmp_path, {
        'Cargo.toml': CARGO,
        'src/lib.rs': '''mod a { pub fn go() {} }
mod b { pub fn go() {} }
use b::*;
pub fn first() { use a::go; go(); struct Local; impl Local { fn hi(&self) {} } Local.hi(); helper(); }
pub fn second() { go(); }
fn helper() {}''',
    })
    first = [name_of(modules, c) for c in calls(fn(modules, 'first'))]
    go_a = next(f for f in modules['src/lib.rs'].functions.values() if f.qualname == 'a.go')
    assert calls(fn(modules, 'first'))[0].target == go_a.id
    assert first[-1] == 'helper' and 'first.Local.hi' in first
    go_b = next(f for f in modules['src/lib.rs'].functions.values() if f.qualname == 'b.go')
    assert calls(fn(modules, 'second'))[0].target == go_b.id
