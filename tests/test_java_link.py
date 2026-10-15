"""Java linking: type resolution, overloads and conservative dispatch."""
from pathlib import Path

import pytest

from flowmap.ir import Call, walk
from flowmap.lang.java.extract import extract_module
from flowmap.lang.java.link import link_java


def linked(files: dict[str, str], root: Path):
    modules = {file: extract_module(file, source.encode()) for file, source in files.items()}
    link_java(root, modules)
    return modules


def fn(modules, qualname):
    found = [f for m in modules.values() for f in m.functions.values() if f.qualname == qualname]
    assert len(found) == 1, (qualname, found)
    return found[0]


def calls(function):
    return [i for i in walk(function.body) if isinstance(i, Call)]


def test_same_class_static_and_overload_by_arity_and_type(tmp_path):
    modules = linked({'A.java': '''class A {
      static int f(int x) { return x; } static int f(String s) { return 0; } static int f(int a, int b) { return a; }
      void run(int n) { f(n); f("s"); f(n, n); g(n); } }'''}, tmp_path)
    targets = [c.target for c in calls(fn(modules, 'A.run'))]
    ids = {m.metadata['java']['functions'][f.id]['param_types'][0] + str(len(f.params)): f.id
           for m in modules.values() for f in m.functions.values() if f.name == 'f'}
    assert targets == [ids['int1'], ids['String1'], ids['int2'], None]
    assert calls(fn(modules, 'A.run'))[0].confidence == 'exact'


def test_imports_same_package_and_external(tmp_path):
    modules = linked({
        'src/a/Repo.java': 'package a; public class Repo { public void save(int v) {} }',
        'src/a/Util.java': 'package a; class Util { static int twice(int v) { return v; } }',
        'src/b/Svc.java': '''package b; import a.Repo; import java.util.List;
          class Svc { void run(Repo r, List<String> xs) { r.save(1); xs.add("x"); a.Util.twice(1); System.out.println(1); } }'''}, tmp_path)
    c = calls(fn(modules, 'Svc.run'))
    assert (c[0].kind, c[0].target, c[0].confidence) == ('project', fn(modules, 'Repo.save').id, 'exact')
    assert (c[1].kind, c[1].external) == ('external', 'java.util.List.add')
    assert c[2].target == fn(modules, 'Util.twice').id
    assert (c[3].kind, c[3].external, c[3].io) == ('external', 'java.lang.System.out.println', ('console', 'out'))


def test_same_package_without_import(tmp_path):
    modules = linked({
        'a/Repo.java': 'package a; class Repo { void save() {} }',
        'a/Svc.java': 'package a; class Svc { void run() { new Repo().save(); } }'}, tmp_path)
    assert [c.target for c in calls(fn(modules, 'Svc.run'))][-1] == fn(modules, 'Repo.save').id


def test_constructor_overloads_and_chained_return_types(tmp_path):
    modules = linked({'A.java': '''class B { B() {} B(int v) {} C next() { return null; } }
      class C { void done() {} }
      class A { void run() { new B(); new B(1).next().done(); } }'''}, tmp_path)
    c = calls(fn(modules, 'A.run'))
    ctors = sorted((f for m in modules.values() for f in m.functions.values() if f.is_constructor), key=lambda f: len(f.params))
    assert [x.target for x in c] == [ctors[0].id, ctors[1].id, fn(modules, 'B.next').id, fn(modules, 'C.done').id]


def test_implicit_default_constructor_targets_the_class(tmp_path):
    modules = linked({'A.java': 'class B { void go() {} } class A { void run() { new B().go(); } }'}, tmp_path)
    c = calls(fn(modules, 'A.run'))
    assert (c[0].kind, c[0].target) == ('class', next(cid for m in modules.values() for cid, cls in m.classes.items() if cls.name == 'B'))
    assert c[1].target == fn(modules, 'B.go').id


def test_virtual_dispatch_rules(tmp_path):
    modules = linked({'A.java': '''interface Port { void send(); }
      abstract class Base { abstract void hook(); void tmpl() { hook(); step(); helper(); } void step() {} private void helper() {} }
      class Impl extends Base { void hook() {} void step() {} }
      final class Leaf { void x() {} }
      class Use { void run(Port p, Base b, Leaf l, Impl i) { p.send(); b.step(); l.x(); i.step(); } }'''}, tmp_path)
    tmpl = [c.target for c in calls(fn(modules, 'Base.tmpl'))]
    assert tmpl == [None, None, fn(modules, 'Base.helper').id]
    use = [c.target for c in calls(fn(modules, 'Use.run'))]
    assert use == [None, None, fn(modules, 'Leaf.x').id, fn(modules, 'Impl.step').id]


def test_super_calls_are_non_virtual_and_inherited_methods_resolve(tmp_path):
    modules = linked({'A.java': '''class P { void go() {} void only() {} }
      class K extends P { void go() { super.go(); only(); } }'''}, tmp_path)
    assert [c.target for c in calls(fn(modules, 'K.go'))] == [fn(modules, 'P.go').id, fn(modules, 'P.only').id]


def test_super_and_this_constructor_invocations(tmp_path):
    modules = linked({'A.java': '''class P { P(int v) {} }
      class K extends P { K() { this(1); } K(int v) { super(v); } }'''}, tmp_path)
    ks = sorted((f for f in modules['A.java'].functions.values() if f.qualname == 'K.<init>'), key=lambda f: len(f.params))
    p = fn(modules, 'P.<init>')
    assert calls(ks[0])[0].target == ks[1].id and calls(ks[1])[0].target == p.id


def test_static_imports_and_ambiguous_wildcards(tmp_path):
    modules = linked({
        'x/M.java': 'package x; public class M { public static int calc(int v) { return v; } }',
        'p/T.java': 'package p; public class T { public void go() {} }', 'q/T.java': 'package q; public class T { public void go() {} }',
        'r/U.java': '''package r; import static x.M.calc; import p.*; import q.*;
          class U { void run(T t) { calc(1); t.go(); } }'''}, tmp_path)
    c = calls(fn(modules, 'U.run'))
    assert c[0].target == fn(modules, 'M.calc').id and c[1].kind == 'unresolved'


def test_generic_type_parameters_stay_opaque(tmp_path):
    modules = linked({'A.java': 'class T { void go() {} } class A<T> { void run(T t) { t.go(); } }'}, tmp_path)
    assert calls(fn(modules, 'A.run'))[0].kind == 'unresolved'


def test_fields_inner_classes_and_lambdas_use_enclosing_types(tmp_path):
    modules = linked({'A.java': '''class Repo { void save() {} }
      class A { Repo repo = null; void run() { repo.save(); this.repo.save(); Runnable r = () -> helper(); }
        void helper() {}
        class Inner { void go() { helper(); repo.save(); } } }'''}, tmp_path)
    assert [c.target for c in calls(fn(modules, 'A.run'))] == [fn(modules, 'Repo.save').id] * 2
    lam = next(f for f in modules['A.java'].functions.values() if '<lambda' in f.qualname)
    assert calls(lam)[0].target == fn(modules, 'A.helper').id
    assert [c.target for c in calls(fn(modules, 'A.Inner.go'))] == [fn(modules, 'A.helper').id, fn(modules, 'Repo.save').id]


def test_no_global_method_name_guessing(tmp_path):
    modules = linked({'A.java': 'class B { void save() {} } class A { void run(Object o) { o.save(); save(); } }'}, tmp_path)
    found = calls(fn(modules, 'A.run'))
    assert [c.target for c in found] == [None, None]
    assert (found[0].kind, found[0].external) == ('external', 'java.lang.Object.save') and found[1].kind == 'unresolved'


def test_new_targets_synthesized_constructor(tmp_path):
    modules = linked({'A.java': '''class Store {}
      class A { Store store = new Store(); }
      class Use { void run() { new A(); } }'''}, tmp_path)
    assert calls(fn(modules, 'Use.run'))[0].target == fn(modules, 'A.<init>').id


def test_unprovable_library_subtyping_keeps_overloads_unresolved(tmp_path):
    modules = linked({'A.java': '''import java.util.List; import java.util.ArrayList;
      class A { static void f(List<String> xs) {} static void f(Object o) {}
        void run(ArrayList<String> xs, Object o) { f(xs); f(o); } }'''}, tmp_path)
    targets = [c.target for c in calls(fn(modules, 'A.run'))]
    obj = next(f.id for f in modules['A.java'].functions.values()
               if f.name == 'f' and modules['A.java'].metadata['java']['functions'][f.id]['param_types'] == ['Object'])
    assert targets == [None, obj]


def test_boxing_class_literals_and_typed_call_results_select_overloads(tmp_path):
    modules = linked({'A.java': '''class P {
        P(Boolean b) {} P(Number n) {} P(String s) {} P(Character c) {}
        static String name() { return ""; }
        static void g(Class<?> c) {} static void g(String s) {} }
      class A { void run() { new P(1); new P(true); new P('c'); P.g(A.class); new P(P.name()); } }'''}, tmp_path)
    meta = modules['A.java'].metadata['java']['functions']
    by_type = {(f.name, meta[f.id]['param_types'][0]): f.id
               for f in modules['A.java'].functions.values() if f.qualname in {'P.<init>', 'P.g'}}
    targets = [c.target for c in calls(fn(modules, 'A.run')) if c.callee != 'P.name']
    assert targets == [by_type['P', 'Number'], by_type['P', 'Boolean'], by_type['P', 'Character'],
                       by_type['g', 'Class'], by_type['P', 'String']]


def test_project_subtype_arguments_pick_the_most_specific_overload(tmp_path):
    modules = linked({'A.java': '''class Base {} class Sub extends Base {} class Other {}
      class A { static void f(Base b) {} static void f(Sub s) {} static void f(Other o) {}
        void run(Sub s, Base b) { f(s); f(b); } }'''}, tmp_path)
    ids = {modules['A.java'].metadata['java']['functions'][f.id]['param_types'][0]: f.id
           for f in modules['A.java'].functions.values() if f.name == 'f'}
    assert [c.target for c in calls(fn(modules, 'A.run'))] == [ids['Sub'], ids['Base']]


# ── review fix wave ──────────────────────────────────────────────────────


def target_name(modules, call):
    return None if call.target is None else next(f.qualname for m in modules.values()
                                                 for f in m.functions.values() if f.id == call.target)


def test_class_methods_beat_interface_defaults(tmp_path):
    modules = linked({'A.java': '''class T { void m() {} } class S extends T {}
      interface I { default void m() {} } class C extends S implements I {}
      class Use { void go(C c) { c.m(); } }'''}, tmp_path)
    assert target_name(modules, calls(fn(modules, 'Use.go'))[0]) == 'T.m'


def test_dotted_receiver_rooted_at_inherited_field(tmp_path):
    modules = linked({'A.java': '''class Run { void run() {} } class Holder { Run sub; }
      class Parent { Holder helper; } class Child extends Parent { void go() { helper.sub.run(); } }'''}, tmp_path)
    assert target_name(modules, calls(fn(modules, 'Child.go'))[0]) == 'Run.run'


def test_library_wildcard_does_not_invent_a_package(tmp_path):
    modules = linked({'A.java': 'import java.util.*; class A { void go() { ThreadLocal.withInitial(null); } }'}, tmp_path)
    call = calls(fn(modules, 'A.go'))[0]
    assert call.external != 'java.util.ThreadLocal.withInitial'


def test_single_static_import_shadows_on_demand(tmp_path):
    modules = linked({
        'p/U.java': 'package p; public class U { public static void m(int x) {} }',
        'q/V.java': 'package q; public class V { public static void m(int x) {} }',
        'p/Main.java': 'package p; import static p.U.*; import static q.V.m; class Main { void go() { m(1); } }'}, tmp_path)
    assert target_name(modules, calls(fn(modules, 'Main.go'))[0]) == 'V.m'


def test_library_superclass_with_static_import_is_unknown(tmp_path):
    modules = linked({
        'p/U.java': 'package p; public class U { public static void helper() {} }',
        'p/T.java': 'package p; import static p.U.helper; import junit.framework.TestCase; class T extends TestCase { void go() { helper(); } }'}, tmp_path)
    assert calls(fn(modules, 'T.go'))[0].kind == 'unresolved'


def test_duplicate_fully_qualified_names_are_ambiguous(tmp_path):
    modules = linked({
        'm1/p/Svc.java': 'package p; public class Svc { public static void run() {} }',
        'm2/p/Svc.java': 'package p; public class Svc { public static void run() {} }',
        'm3/q/Main.java': 'package q; import p.Svc; class Main { void go() { Svc.run(); } }'}, tmp_path)
    assert calls(fn(modules, 'Main.go'))[0].target is None


def test_overrides_match_qualified_and_generic_parameter_types(tmp_path):
    modules = linked({'A.java': '''class Foo {}
      class Base<T> { void accept(T t) {} void m(String s) {} }
      class Sub extends Base<Foo> { void accept(Foo f) {} void m(java.lang.String s) {} }
      class Use { void go(Base<Foo> b, Foo f) { b.accept(f); b.m("x"); } }'''}, tmp_path)
    assert [c.target for c in calls(fn(modules, 'Use.go'))] == [None, None]


def test_enum_constant_bodies_override_enum_methods(tmp_path):
    modules = linked({'Op.java': '''enum Op { PLUS { int apply(int a) { return a + 1; } }, ID; int apply(int a) { return a; } }
      class Use { int go(Op op) { return op.apply(1); } }'''}, tmp_path)
    assert calls(fn(modules, 'Use.go'))[0].target is None


def test_type_parameter_overloads_are_not_rejected(tmp_path):
    modules = linked({'A.java': '''class Box<T> { void m(T t) {} void m(Comparable c) {} }
      class Use { void go(Box<Integer> b, Integer i) { b.m(i); } }'''}, tmp_path)
    assert calls(fn(modules, 'Use.go'))[0].target is None


def test_overload_phases_strict_before_boxing_before_varargs(tmp_path):
    modules = linked({'W.java': '''class W {
        void value(long v) {} void value(double v) {} void value(Number v) {}
        void s(String x) {} void s(int... xs) {}
        void go(Integer boxed) { value(boxed); value(1); s(1); } }'''}, tmp_path)
    assert [target_name(modules, c) for c in calls(fn(modules, 'W.go'))] == ['W.value', 'W.value', 'W.s']
    meta = modules['W.java'].metadata['java']['functions']
    assert [meta[c.target]['param_types'] for c in calls(fn(modules, 'W.go'))] == [['Number'], ['long'], ['int[]']]


def test_argument_types_are_resolved_in_the_callers_file(tmp_path):
    modules = linked({
        'p1/Node.java': 'package p1; public class Node {}',
        'p2/Node.java': 'package p2; public class Node {}',
        'p2/Lib.java': 'package p2; public class Lib { public static void m(Node n) {} public static void m(Object o) {} }',
        'p1/Main.java': 'package p1; import p2.Lib; class Main { void go(Node n) { Lib.m(n); } }'}, tmp_path)
    call = calls(fn(modules, 'Main.go'))[0]
    assert modules['p2/Lib.java'].metadata['java']['functions'][call.target]['param_types'] == ['Object']


@pytest.mark.parametrize('source, function', [
    ('''class Ha { void run() {} } class Hb { void run() {} } class Base { Hb helper; }
        class Outer { Ha helper; class Inner extends Base { void go() { helper.run(); } } }''', 'Outer.Inner.go'),
    ('''class Ha { void run() {} } class Hb { void run() {} } abstract class Base { Hb helper; abstract void go(); }
        class Outer { void make() { Ha helper = new Ha(); new Base() { void go() { helper.run(); } }; } }''', None),
    ('''class Ha { void run() {} } class Hb { void run() {} }
        class A { Ha helper; void go(boolean c) { if (c) { Hb helper = new Hb(); } helper.run(); } }''', 'A.go'),
])
def test_field_and_local_shadowing_follows_java_scoping(tmp_path, source, function):
    modules = linked({'A.java': source}, tmp_path)
    target = fn(modules, function) if function else next(
        f for f in modules['A.java'].functions.values() if f.name == 'go' and '<anonymous' in f.qualname)
    assert target_name(modules, [c for c in calls(target) if c.method == 'run'][-1]) == (
        'Ha.run' if function == 'A.go' else 'Hb.run')


def test_field_initializers_do_not_see_constructor_parameters(tmp_path):
    modules = linked({'A.java': '''class Ha { Ha child() { return this; } } class Hb { Hb child() { return this; } }
      class A { Ha store = new Ha(); Ha c = store.child(); A(Hb store) {} }'''}, tmp_path)
    assert target_name(modules, [c for c in calls(fn(modules, 'A.<init>')) if c.method == 'child'][0]) == 'Ha.child'


@pytest.mark.parametrize('call_text, expected', [
    ('add(x);', None),            # unknown argument: ArrayList.add(Object) may apply
    ('add(new Foo());', 'MyList.add'),  # a project argument type: no library overload is more specific
])
def test_library_superclass_overloads_make_selection_conservative(tmp_path, call_text, expected):
    modules = linked({'A.java': f'''import java.util.ArrayList;
      class Foo {{}}
      class MyList extends ArrayList<Object> {{ public boolean add(Foo f) {{ return true; }}
        void go(java.util.function.Supplier<Object> s) {{ var x = s.get(); {call_text} }} }}'''}, tmp_path)
    assert target_name(modules, [c for c in calls(fn(modules, 'MyList.go')) if c.callee == 'add'][0]) == expected
