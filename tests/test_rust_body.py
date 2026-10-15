"""Rust bodies lowered to shared IR in execution order."""
from flowmap.ir import Assign, Call, If, Loop, Match, Raise, Return, walk
from flowmap.lang.rust.extract import extract_module


def function(src, qualname):
    m = extract_module('src/lib.rs', src.encode())
    return m, next(f for f in m.functions.values() if f.qualname == qualname)


def calls(items):
    return [i for i in walk(items if isinstance(items, list) else items.body) if isinstance(i, Call)]


def records(m, fn):
    return [r for k, r in m.metadata['rust']['calls'].items() if k.startswith(fn.id + ':')]


def test_nested_calls_share_temporaries_and_tail_expression_returns():
    _, fn = function('fn run(v: i32) -> i32 { finish(load(v)) }', 'run')
    c = calls(fn)
    assert [x.callee for x in c] == ['load', 'finish'] and c[1].args[0].uses == c[0].defs
    assert isinstance(fn.body[-1], Return) and fn.body[-1].uses == c[1].defs


def test_receiver_records():
    m, fn = function('''struct S { repo: Repo }
      impl S { fn run(&self, store: &mut Store, n: i32) {
        let b = Builder::new(); let p: Point = make(); let q = Point { x: 1 };
        store.add(n); self.repo.save(n); b.push(1); p.go(); q.go(); self.helper(); build().finish(); "x".len();
      } }''', 'S.run')
    rec = {r['name']: r for r in records(m, fn)}
    assert rec['add']['receiver'] == 'name' and rec['add']['receiver_type'] == '&mut Store'
    assert rec['save']['receiver'] == 'field' and rec['save']['receiver_text'] == 'self.repo'
    assert rec['new']['kind'] == 'path' and rec['new']['path'] == 'Builder::new'
    assert rec['push']['receiver'] == 'name' and rec['push']['receiver_call'] is not None
    assert rec['helper']['receiver'] == 'self'
    assert rec['finish']['receiver'] == 'call'
    assert rec['len']['receiver'] == 'expr'
    gos = [r for r in records(m, fn) if r['name'] == 'go']
    assert [g['receiver_type'] for g in gos] == ['Point', 'Point']


def test_if_match_and_values_are_conditional():
    m, fn = function('''fn run(x: Option<i32>, ok: bool) -> i32 {
        if ok && check() { a(); } else if more() { b(); } else { c(); }
        let v = if ok { d() } else { e() };
        match x { Some(n) if n > 1 => f(n), Some(_) => g(), None => h() }
        if let Some(n) = x { i(n); }
        v }''', 'run')
    assert not any(c.callee in {'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'check', 'more'} for c in fn.body if isinstance(c, Call))
    match = next(i for i in fn.body if isinstance(i, Match))
    assert [case.pattern for case in match.cases] == ['Some(n) if n > 1', 'Some(_)', 'None']
    assert [[c.callee for c in calls(case.body)] for case in match.cases] == [['f'], ['g'], ['h']]
    assert any(isinstance(i, Assign) and i.defs == ['v'] for i in fn.body)


def test_loops():
    _, fn = function('''fn run(xs: Vec<i32>) {
        for x in xs.iter() { a(x); }
        while more() { b(); }
        while let Some(y) = next() { c(y); }
        loop { if done() { break; } d(); } }''', 'run')
    loops = [i for i in fn.body if isinstance(i, Loop)]
    assert len(loops) == 4 and loops[0].target == 'x'
    assert loops[1].head == 1 and [c.callee for c in calls(loops[1].body)] == ['more', 'b']
    assert [c.callee for c in calls(loops[2].body)][0] == 'next'
    assert loops[3].test == 'true'


def test_macros_question_mark_await_and_closures():
    m, fn = function('''async fn run(p: &str) -> Result<i32, E> {
        println!("x {}", load(p)); let n = parse(p)?; let data = fetch(p).await;
        let f = |v: i32| twice(v); if n < 0 { panic!("negative"); } vec![1, 2]; Ok(n) }''', 'run')
    c = calls(fn)
    names = [x.callee for x in c]
    assert 'println!' in names and 'load' not in names and 'twice' not in names
    printed = next(x for x in c if x.callee == 'println!')
    assert printed.kind == 'external' and printed.io == ('console', 'out')
    assert 'vec!' in names and next(x for x in c if x.callee == 'vec!').io is None
    assert any(isinstance(i, Raise) for i in walk(fn.body))
    assert next(x for x in c if x.callee == 'fetch').awaited
    assert names.index('parse') < names.index('fetch')
    closure = next(f for f in m.functions.values() if '<closure' in f.qualname)
    assert [x.callee for x in calls(closure)] == ['twice']


def test_labeled_jumps_are_diagnostics():
    m, fn = function("fn run() { 'outer: loop { go(); break 'outer; } }", 'run')
    assert any(d['code'] == 'unsupported-control-flow' for d in m.diagnostics)
    assert 'go' not in [c.callee for c in calls(fn)]


def test_assignments_and_struct_fields():
    _, fn = function('fn run(mut s: S, v: i32) { s.total += v; let mut x = 1; x = calc(x); }', 'run')
    assigns = [i for i in fn.body if isinstance(i, Assign)]
    assert assigns[0].defs == ['s'] and 'v' in assigns[0].uses
    assert assigns[-1].defs == ['x']


def test_pattern_bindings_shadow_and_scope():
    m, fn = function('''fn run(p: Point, o: Option<Repo>) {
        let Point { x, y: renamed } = p; use_it(x, renamed);
        if let Some(repo) = o { repo.save(); }
        let repo = Store::new(); repo.save(); }''', 'run')
    first = next(c for c in calls(fn) if c.callee == 'use_it')
    assert [a.uses for a in first.args] == [['x'], ['renamed']]
    saves = [r for r in records(m, fn) if r['name'] == 'save']
    assert saves[0]['receiver_call'] is None and saves[1]['receiver_call'] is not None
