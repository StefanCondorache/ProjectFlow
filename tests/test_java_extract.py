"""Java declarations: types, members, signatures and metadata."""
from flowmap.lang.java.extract import extract_module

SRC = b'''package shop.core;
import java.util.List; import static java.lang.Math.max; import shop.data.*;
/** Sells things. */
public final class Shop<T> extends Base implements Api, Other {
  private final Repo repo;
  /** Builds a shop. */
  Shop(Repo repo) { this.repo = repo; }
  public static void main(String... args) {}
  int run(int n, List<String> names) { return n; }
  int run(String s) { return 0; }
  static class Inner { void go() {} }
  record Point(int x, int y) {}
  enum Kind { A, B; void f() {} }
  interface Port { void send(String s); default void ping() {} }
}
'''


def fns(module):
    return {f.qualname: f for f in module.functions.values()}


def test_types_and_metadata():
    m = extract_module('src/main/java/shop/core/Shop.java', SRC)
    meta = m.metadata['java']
    assert meta['package'] == 'shop.core'
    assert {'name': 'java.util.List', 'static': False, 'wildcard': False, 'line': 2} in meta['imports']
    assert any(i['name'] == 'java.lang.Math.max' and i['static'] for i in meta['imports'])
    assert any(i['name'] == 'shop.data' and i['wildcard'] for i in meta['imports'])
    shop = next(t for t in meta['types'].values() if t['name'] == 'Shop')
    assert (shop['fqn'], shop['kind'], shop['superclass'], shop['interfaces']) == ('shop.core.Shop', 'class', 'Base', ['Api', 'Other'])
    assert 'final' in shop['modifiers'] and shop['fields'] == {'repo': 'Repo'} and shop['type_params'] == ['T']
    kinds = {t['fqn']: t['kind'] for t in meta['types'].values()}
    assert kinds['shop.core.Shop.Inner'] == 'class' and kinds['shop.core.Shop.Point'] == 'record'
    assert kinds['shop.core.Shop.Kind'] == 'enum' and kinds['shop.core.Shop.Port'] == 'interface'
    assert m.classes[next(c for c in m.classes if c.endswith('Shop@4:0'))].doc == 'Sells things.'


def test_methods_overloads_constructors_and_docs():
    m = extract_module('Shop.java', SRC)
    f = fns(m)
    runs = [fn for fn in m.functions.values() if fn.qualname == 'Shop.run']
    assert len(runs) == 2 and len({fn.id for fn in runs}) == 2
    assert all(fn.id.startswith('Shop.java::Shop.run@') for fn in runs)
    ctor = f['Shop.<init>']
    assert ctor.is_constructor and ctor.doc == 'Builds a shop.' and [p.name for p in ctor.params] == ['repo']
    main = f['Shop.main']
    meta = m.metadata['java']['functions'][main.id]
    assert meta['static'] and meta['varargs'] and meta['param_types'] == ['String[]']
    assert main.params[0].kind == 'vararg'
    assert all(fn.language == 'java' and fn.receiver == 'this' for fn in m.functions.values())
    send = f['Shop.Port.send']
    assert m.metadata['java']['functions'][send.id]['abstract']
    assert not m.metadata['java']['functions'][f['Shop.Port.ping'].id]['abstract']
    assert m.classes[f['Shop.Inner.go'].cls].qualname == 'Shop.Inner'
    assert m.metadata['java']['functions'][runs[0].id]['param_types'] in (['int', 'List'], ['String'])


def test_unicode_and_malformed_source_report_diagnostics():
    m = extract_module('café/Ünï.java', 'class Ünï { void ok() {} void broken( { }'.encode())
    assert any(d['code'] == 'parse-error' for d in m.diagnostics)
    assert any(f.qualname == 'Ünï.ok' for f in m.functions.values())
