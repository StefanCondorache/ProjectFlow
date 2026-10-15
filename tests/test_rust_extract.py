"""Rust items, impls, traits, modules and use declarations."""
from flowmap.lang.rust.extract import extract_module

SRC = b'''mod store;
#[path = "x/y.rs"]
mod other;
use crate::store::{Store, Ledger as L};
use super::*;
use std::collections::HashMap;

/// Runs the shop.
pub fn run(orders: &[i32], cfg: Option<&str>) -> Result<i32, String> { Ok(0) }

pub struct Point { x: i32, repo: Repo }
enum Kind { A, B(i32) }

impl Point {
    /// Builds a point.
    pub fn new(x: i32) -> Self { Point { x, repo: Repo::new() } }
    fn shift(&mut self, by: i32) -> i32 { self.x + by }
}
impl Shape for Point { fn area(&self) -> f64 { 0.0 } }
trait Shape { fn area(&self) -> f64; fn name(&self) -> String { String::new() } }

mod inner {
    pub fn go() { let f = |x: i32| x + 1; }
    fn outer() { fn nested() {} }
}
'''


def fns(module):
    return {f.qualname: f for f in module.functions.values()}


def test_items_signatures_docs_and_receivers():
    m = extract_module('src/lib.rs', SRC)
    f = fns(m)
    run = f['run']
    assert run.language == 'rust' and run.doc == 'Runs the shop.'
    assert [(p.name, p.annotation) for p in run.params] == [('orders', '&[i32]'), ('cfg', 'Option')]
    assert run.returns == 'Result'
    new, shift = f['Point.new'], f['Point.shift']
    meta = m.metadata['rust']['functions']
    assert new.receiver is None and meta[new.id]['self_type'] == 'Point' and not meta[new.id]['has_self']
    assert shift.receiver == 'self' and [p.name for p in shift.params] == ['by'] and meta[shift.id]['has_self']
    assert new.doc == 'Builds a point.' and meta[new.id]['returns'] == 'Self'
    assert meta[f['Point.area'].id]['trait'] == 'Shape'
    assert meta[f['Shape.name'].id]['kind'] == 'method' and 'Shape.area' not in f
    assert meta[f['inner.go'].id]['scope'] == 'self::inner'
    assert 'inner.outer.nested' in f
    closure = next(fn for q, fn in f.items() if '<closure' in q)
    assert closure.qualname.startswith('inner.go.<closure@') and [p.name for p in closure.params] == ['x']


def test_types_impls_traits_and_items():
    m = extract_module('src/lib.rs', SRC)
    meta = m.metadata['rust']
    point = next(t for t in meta['types'].values() if t['name'] == 'Point')
    assert point['kind'] == 'struct' and point['fields'] == {'x': 'i32', 'repo': 'Repo'} and point['scope'] == 'self'
    shape = next(t for t in meta['types'].values() if t['name'] == 'Shape')
    assert shape['kind'] == 'trait' and shape['methods']['area'] is None and shape['methods']['name']
    impls = {(i['type'], i['trait']) for i in meta['impls']}
    assert impls == {('Point', None), ('Point', 'Shape')}
    assert meta['items']['self::run']['kind'] == 'fn' and meta['items']['self::Kind']['kind'] == 'enum'
    assert meta['items']['self::inner']['kind'] == 'mod' and meta['items']['self::inner::go']['kind'] == 'fn'


def test_mods_and_use_trees():
    m = extract_module('src/lib.rs', SRC)
    meta = m.metadata['rust']
    mods = {d['name']: d for d in meta['mods']}
    assert mods['store'] == {'name': 'store', 'inline': False, 'path_attr': None, 'line': 1, 'scope': 'self'}
    assert mods['other']['path_attr'] == 'x/y.rs' and mods['inner']['inline']
    uses = {(u['path'], u['alias'], u['glob']) for u in meta['uses']}
    assert ('crate::store::Store', 'Store', False) in uses
    assert ('crate::store::Ledger', 'L', False) in uses
    assert ('super', None, True) in uses and ('std::collections::HashMap', 'HashMap', False) in uses


def test_malformed_and_unicode_sources_report_diagnostics():
    m = extract_module('café/ünï.rs', 'fn ok() {} fn broken( {'.encode())
    assert any(d['code'] == 'parse-error' for d in m.diagnostics)
    assert 'ok' in fns(m)
