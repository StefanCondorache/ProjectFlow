"""Java bodies lowered to shared IR in execution order."""
from flowmap.ir import Assign, Call, If, Loop, Raise, Return, Try, walk
from flowmap.lang.java.extract import extract_module


def method(src, name):
    m = extract_module('A.java', src.encode())
    return m, next(f for f in m.functions.values() if f.name == name)


def calls(fn):
    return [i for i in walk(fn.body) if isinstance(i, Call)]


def calls_in(items):
    return [i for i in walk(items) if isinstance(i, Call)]


def test_nested_calls_in_evaluation_order_share_temporaries():
    _, fn = method('class A { int run(int v) { return finish(load(v)); } }', 'run')
    c = calls(fn)
    assert [x.callee for x in c] == ['load', 'finish'] and c[1].args[0].uses == c[0].defs
    assert isinstance(fn.body[-1], Return) and fn.body[-1].uses == c[1].defs
    assert c[0].args[0].uses == ['v']


def test_receiver_types_and_chains_are_recorded():
    m, fn = method('''class A { Repo repo;
      void run(Store s, int n) { var b = new Builder(); s.save(n).flush(); repo.put("k", n); b.add(1); helper(n, "x"); Util.go(); } }''', 'run')
    meta = m.metadata['java']['calls']
    rec = {r['name']: r for r in meta.values()}
    assert rec['save']['receiver'] == 'name' and rec['save']['receiver_type'] == 'Store'
    assert rec['flush']['receiver'] == 'call' and meta[rec['flush']['receiver_call']]['name'] == 'save'
    assert rec['put']['receiver'] == 'field' and rec['put']['receiver_type'] == 'Repo'
    assert rec['add']['receiver_type'] == 'Builder'
    assert rec['helper']['receiver'] == 'none' and rec['helper']['arg_types'] == ['int', 'String']
    assert rec['Builder']['kind'] == 'new' and rec['Builder']['type'] == 'Builder'
    assert rec['go']['receiver'] == 'type' and rec['go']['receiver_text'] == 'Util'


def test_branches_loops_short_circuit_and_ternary_are_conditional():
    _, fn = method('''class A { void run(int n, boolean ok) {
      if (ok && check(n)) { a(); } else { b(); }
      for (int i = 0; i < n; i++) { c(i); }
      for (String s : names()) { d(s); }
      while (more()) { e(); }
      int x = ok ? f() : g(); } }''', 'run')
    top = [type(i).__name__ for i in fn.body]
    assert top.count('If') >= 2 and top.count('Loop') == 3
    assert any(isinstance(i, If) and [c.callee for c in calls_in(i.then)] == ['a'] for i in fn.body)
    assert not any(c.callee in {'a', 'b', 'check', 'f', 'g'} for c in fn.body if isinstance(c, Call))
    loops = [i for i in fn.body if isinstance(i, Loop)]
    assert [c.callee for c in calls_in(loops[0].body)] == ['c']
    assert loops[1].target == 's' and loops[1].kind == 'for'
    assert loops[2].head == 1 and [c.callee for c in calls_in(loops[2].body)] == ['more', 'e']


def test_switch_rules_map_to_branches_and_fallthrough_is_opaque():
    m, fn = method('''class A { void run(int n) {
      switch (n) { case 1 -> one(); default -> other(); }
      switch (n) { case 1: two(); case 2: three(); break; }
      switch (n) { case 1: four(); break; default: five(); } } }''', 'run')
    names = [c.callee for c in calls(fn)]
    assert {'one', 'other', 'four', 'five'} <= set(names) and 'two' not in names
    assert any(d['code'] == 'unsupported-switch-fallthrough' for d in m.diagnostics)
    assert not any(isinstance(c, Call) for c in fn.body)


def test_switch_expression_yields_into_a_value():
    _, fn = method('class A { int run(int n) { int y = switch (n) { case 1 -> one(); default -> { yield two(); } }; return use(y); } }', 'run')
    assert [c.callee for c in calls(fn)] == ['one', 'two', 'use']
    assert any(isinstance(i, Assign) and i.defs == ['y'] for i in fn.body)


def test_try_with_resources_catch_finally_and_throw():
    _, fn = method('''class A { void run() {
      try (var in = open()) { use(in); } catch (IOException | RuntimeException e) { throw new Fail(e); } finally { close(); } } }''', 'run')
    t = next(i for i in fn.body if isinstance(i, Try))
    assert [c.callee for c in calls_in(t.body)] == ['open', 'use']
    assert t.handlers[0].types == 'IOException | RuntimeException' and t.handlers[0].name == 'e'
    assert any(isinstance(i, Raise) for i in walk(t.handlers[0].body))
    assert [c.callee for c in calls_in(t.final)] == ['close']


def test_lambdas_and_anonymous_classes_are_separate_functions():
    m, fn = method('''class A { void run(Repo r) {
      r.each(v -> log(v)); Runnable x = new Runnable() { public void run() { go(); } }; } }''', 'run')
    assert 'log' not in [c.callee for c in calls(fn)] and 'go' not in [c.callee for c in calls(fn)]
    lam = next(f for f in m.functions.values() if '<lambda' in f.qualname)
    assert [c.callee for c in calls(lam)] == ['log'] and [p.name for p in lam.params] == ['v']
    anon = next(f for f in m.functions.values() if f.name == 'run' and f is not fn)
    assert [c.callee for c in calls(anon)] == ['go']
    anon_type = m.metadata['java']['types'][anon.cls]
    assert anon_type['anonymous'] and anon_type['superclass'] == 'Runnable' and anon_type['scope_fid'] == fn.id


def test_constructor_calls_and_field_writes():
    m, fn = method('class A extends B { int x; A(int v) { super(v); this.x = v; } }', 'A')
    rec = [r for key, r in m.metadata['java']['calls'].items() if key.startswith(fn.id)]
    assert rec[0]['kind'] == 'super-ctor' and rec[0]['arity'] == 1
    assert any(isinstance(i, Assign) and i.defs == ['this.x'] for i in fn.body)


def test_labeled_control_flow_is_reported_not_invented():
    m, fn = method('class A { void run() { outer: for (;;) { go(); break outer; } } }', 'run')
    assert any(d['code'] == 'unsupported-control-flow' for d in m.diagnostics)
    assert 'go' not in [c.callee for c in calls(fn)]


def test_instance_initializers_run_inside_constructors_after_super():
    m = extract_module('A.java', b'''class A extends B {
      Store store = new Store(); int n; { warm(); }
      A(int v) { super(v); start(); }
      A() { this(1); } }''')
    ctors = sorted((f for f in m.functions.values() if f.is_constructor), key=lambda f: len(f.params))
    assert [c.callee for c in calls(ctors[1])] == ['super', 'Store', 'warm', 'start']
    assert any(isinstance(i, Assign) and i.defs == ['this.store'] for i in ctors[1].body)
    assert [c.callee for c in calls(ctors[0])] == ['this']  # delegation runs them once, in the target
    assert not any(d['code'] == 'unsupported-class-initialization' for d in m.diagnostics)


def test_implicit_constructor_is_synthesized_only_when_initializers_call():
    m = extract_module('A.java', b'class A { Store store = new Store(); } class C { int x = 1; }')
    ctor = next(f for f in m.functions.values() if f.qualname == 'A.<init>')
    assert ctor.is_constructor and ctor.params == [] and [c.callee for c in calls(ctor)] == ['Store']
    assert m.metadata['java']['functions'][ctor.id]['implicit']
    assert not any(f.qualname == 'C.<init>' for f in m.functions.values())


def test_static_initializers_stay_diagnostics():
    m = extract_module('A.java', b'class A { static Store s = new Store(); static { boot(); } }')
    assert sum(d['code'] == 'unsupported-class-initialization' for d in m.diagnostics) == 2
    assert not any(f.is_constructor for f in m.functions.values())


def test_array_dimension_calls_are_kept():
    _, fn = method('class A { void go() { int[] a = new int[g()]; } }', 'go')
    assert [c.callee for c in calls(fn)] == ['g']


def test_yield_inside_nested_switch_statement_targets_the_switch_expression():
    _, fn = method('''class A { int go(int x, int y) { int r = switch (x) {
        case 1 -> { switch (y) { case 2 -> { yield 5; } default -> { yield 6; } } }
        default -> 0; }; return r; } }''', 'go')
    assert not any(isinstance(i, Return) and i.kind == 'yield' for i in walk(fn.body))


def test_stacked_case_labels_are_not_fallthrough():
    m, fn = method('class A { void go(int x) { switch (x) { case 1: case 2: two(); break; default: d(); } } }', 'go')
    assert not m.diagnostics
    branch = fn.body[0]
    assert isinstance(branch, If) and 'x == 1' in branch.test and 'x == 2' in branch.test
