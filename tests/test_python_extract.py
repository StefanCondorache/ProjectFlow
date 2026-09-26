import textwrap

from flowmap.ir import walk
from flowmap.lang.python.extract import extract_module


def ex(src: str, path: str = "m.py"):
    return extract_module(path, textwrap.dedent(src).lstrip("\n").encode())


def shape(items):
    """Compact, comparable view of a flow body (test-only helper)."""
    out = []
    for it in items:
        kind = type(it).__name__
        if kind == "Call":
            out.append(("call", it.callee, it.defs))
        elif kind == "Assign":
            out.append(("assign", it.defs, sorted(it.uses)))
        elif kind == "If":
            out.append(("if", it.cond, shape(it.then), shape(it.orelse)))
        elif kind == "Loop":
            out.append(("loop", it.kind, it.header, shape(it.body)))
        elif kind == "Try":
            handlers = [(h.types, h.name, shape(h.body)) for h in it.handlers]
            out.append(("try", shape(it.body), handlers, shape(it.orelse), shape(it.final)))
        elif kind == "Match":
            out.append(("match", it.subject, [(c.pattern, shape(c.body)) for c in it.cases]))
        elif kind == "Return":
            out.append((it.kind, sorted(it.uses)))
        elif kind == "Raise":
            out.append(("raise", it.exc))
        else:
            raise AssertionError(f"unexpected item {kind}")
    return out


def body(src: str, qualname: str):
    return shape(ex(src).functions[f"m.py::{qualname}"].body)


# ── definitions ────────────────────────────────────────────────────────────


def test_function_signature_docstring_and_span():
    mod = ex('''
        def load(path: str, *, strict=False, **opts) -> "Config":
            """Read the config file.

            Longer text."""
            return None
    ''')
    f = mod.functions["m.py::load"]
    assert (f.name, f.qualname, f.file, f.line, f.end_line) == ("load", "load", "m.py", 1, 5)
    assert [(p.name, p.annotation, p.kind) for p in f.params] == [
        ("path", "str", "normal"),
        ("strict", None, "normal"),
        ("opts", None, "kwarg"),
    ]
    assert f.returns == "Config"
    assert f.doc == "Read the config file."


def test_typed_star_params_and_async():
    mod = ex('''
        async def run(*args: str, **kw: dict): pass
    ''')
    f = mod.functions["m.py::run"]
    assert [(p.name, p.annotation, p.kind) for p in f.params] == [("args", "str", "vararg"), ("kw", "dict", "kwarg")]
    assert f.is_async


def test_classes_methods_and_nested_definitions():
    mod = ex('''
        class Store(Base, metaclass=Meta):
            """Keeps orders."""
            def save(self, order): pass
            @staticmethod
            def make(): pass
            class Inner:
                def go(self): pass

        def outer():
            def inner(): pass
    ''')
    assert set(mod.functions) == {
        "m.py::Store.save",
        "m.py::Store.make",
        "m.py::Store.Inner.go",
        "m.py::outer",
        "m.py::outer.<locals>.inner",
    }
    store = mod.classes["m.py::Store"]
    assert store.bases == ["Base"]
    assert store.methods == {"save": "m.py::Store.save", "make": "m.py::Store.make"}
    assert store.doc == "Keeps orders."
    assert mod.functions["m.py::Store.save"].cls == "m.py::Store"
    assert mod.functions["m.py::Store.make"].decorators == ["staticmethod"]
    assert mod.classes["m.py::Store.Inner"].methods == {"go": "m.py::Store.Inner.go"}
    assert mod.functions["m.py::outer.<locals>.inner"].cls is None


# ── calls and the data they move ───────────────────────────────────────────


def test_call_arguments_and_assignment_targets():
    mod = ex('''
        def run(cfg):
            data = fetch(cfg.tickers, days=30)
            a, b = split(data)
    ''')
    c1, c2 = mod.functions["m.py::run"].body
    assert (c1.callee, c1.defs, c1.line) == ("fetch", ["data"], 2)
    assert [(a.text, a.keyword, a.uses) for a in c1.args] == [("cfg.tickers", None, ["cfg"]), ("30", "days", [])]
    assert (c2.callee, c2.defs, c2.args[0].uses) == ("split", ["a", "b"], ["data"])


def test_nested_calls_run_inner_first_through_temporaries():
    assert body('''
        def f(x):
            return outer(inner(x))
    ''', "f") == [("call", "inner", ["$1"]), ("call", "outer", ["$2"]), ("return", ["$2"])]


def test_method_call_on_self_field_reads_and_writes_fields():
    mod = ex('''
        class A:
            def m(self):
                self.cache = self.store.load(self.key)
    ''')
    (call,) = mod.functions["m.py::A.m"].body
    assert call.callee == "self.store.load"
    assert call.receiver_uses == ["self.store"]
    assert call.args[0].uses == ["self.key"]
    assert call.defs == ["self.cache"]


def test_call_on_a_call_result_uses_the_temporary_as_receiver():
    mod = ex('''
        def f():
            return get_store().save()
    ''')
    first, second, ret = mod.functions["m.py::f"].body
    assert (first.callee, first.defs) == ("get_store", ["$1"])
    assert (second.callee, second.receiver_uses, second.defs) == ("$1.save", ["$1"], ["$2"])
    assert ret.uses == ["$2"]


def test_plain_assignments_keep_data_dependencies():
    assert body('''
        def f(a, b, items, key):
            total = a + helper(b)
            items[key] = total
            obj.attr = total
    ''', "f") == [
        ("call", "helper", ["$1"]),
        ("assign", ["total"], ["$1", "a"]),
        ("assign", ["items"], ["items", "key", "total"]),
        ("assign", ["obj"], ["obj", "total"]),
    ]


def test_comprehension_becomes_a_loop_around_its_calls():
    mod = ex('''
        def f(tickers):
            out = [load(t) for t in tickers if ok(t)]
    ''')
    loop, assign = mod.functions["m.py::f"].body
    assert shape([loop]) == [
        ("loop", "comprehension", "for t in tickers if ok(t)", [("call", "ok", ["$1"]), ("call", "load", ["$2"])])
    ]
    assert (loop.defs, loop.uses) == (["t"], ["tickers"])
    assert shape([assign]) == [("assign", ["out"], ["$2", "tickers"])]


def test_await_fstring_generator_argument_and_lambda():
    mod = ex('''
        async def f(vals):
            x = await fetch()
            msg = f"total={total(vals)}"
            s = sum(g(v) for v in vals)
            cb = lambda q: work(q)
    ''')
    items = mod.functions["m.py::f"].body
    assert items[0].awaited
    assert shape(items) == [
        ("call", "fetch", ["x"]),
        ("call", "total", ["$1"]),
        ("assign", ["msg"], ["$1"]),
        ("loop", "comprehension", "for v in vals", [("call", "g", ["$2"])]),
        ("call", "sum", ["s"]),
        ("assign", ["cb"], []),
    ]
    assert sorted(items[4].args[0].uses) == ["$2", "vals"]


def test_with_statement_binds_the_context_value():
    assert body('''
        def f(p):
            with open(p) as fh, lock:
                rows = parse(fh)
    ''', "f") == [("call", "open", ["fh"]), ("call", "parse", ["rows"])]


# ── control flow ───────────────────────────────────────────────────────────


def test_if_elif_else_chain_nests_elif_in_else():
    assert body('''
        def f(x):
            if check(x):
                a()
            elif x > 3:
                b()
            else:
                c()
    ''', "f") == [
        ("call", "check", ["$1"]),
        ("if", "check(x)", [("call", "a", [])], [("if", "x > 3", [("call", "b", [])], [("call", "c", [])])]),
    ]


def test_for_and_while_loops():
    assert body('''
        def f():
            for i, j in pairs():
                step(i)
            while more():
                tick()
    ''', "f") == [
        ("call", "pairs", ["$1"]),
        ("loop", "for", "for i, j in pairs()", [("call", "step", [])]),
        ("loop", "while", "while more()", [("call", "more", ["$2"]), ("call", "tick", [])]),
    ]


def test_try_with_handlers_else_and_finally():
    assert body('''
        def f():
            try:
                risky()
            except (ValueError, KeyError) as e:
                handle(e)
            except Exception:
                raise
            else:
                ok()
            finally:
                close()
    ''', "f") == [
        (
            "try",
            [("call", "risky", [])],
            [("ValueError, KeyError", "e", [("call", "handle", [])]), ("Exception", None, [("raise", "")])],
            [("call", "ok", [])],
            [("call", "close", [])],
        )
    ]


def test_match_cases_with_guard():
    assert body('''
        def f(cmd):
            match cmd:
                case "go" if ready():
                    go()
                case _:
                    idle()
    ''', "f") == [
        ("match", "cmd", [('"go" if ready()', [("call", "ready", ["$1"]), ("call", "go", [])]), ("_", [("call", "idle", [])])])
    ]


def test_return_raise_and_yield():
    assert body('''
        def f(x):
            if not x:
                raise ValueError("empty")
            yield emit(x)
            return done(x)
    ''', "f") == [
        ("if", "not x", [("call", "ValueError", ["$1"]), ("raise", 'ValueError("empty")')], []),
        ("call", "emit", ["$2"]),
        ("yield", ["$2"]),
        ("call", "done", ["$3"]),
        ("return", ["$3"]),
    ]


# ── module level ───────────────────────────────────────────────────────────


def test_imports_at_module_and_function_level():
    mod = ex('''
        import os, a.b.c as abc
        from . import sib
        from ..pkg.mod import f as g, h
        from x import *
        def fn():
            from lazy import thing
    ''')
    assert [(i.module, i.name, i.alias, i.line) for i in mod.imports] == [
        ("os", None, None, 1),
        ("a.b.c", None, "abc", 1),
        (".", "sib", None, 2),
        ("..pkg.mod", "f", "g", 3),
        ("..pkg.mod", "h", None, 3),
        ("x", "*", None, 4),
    ]
    assert [(i.module, i.name) for i in mod.functions["m.py::fn"].imports] == [("lazy", "thing")]


def test_imports_inside_type_checking_and_try_blocks():
    mod = ex('''
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from app.store import Store
        try:
            import ujson as json
        except ImportError:
            import json
    ''')
    assert [(i.module, i.name, i.alias) for i in mod.imports] == [
        ("typing", "TYPE_CHECKING", None),
        ("app.store", "Store", None),
        ("ujson", None, "json"),
        ("json", None, None),
    ]


def test_main_guard_becomes_the_main_pseudo_function():
    mod = ex('''
        """Tool entry."""
        import sys
        CONFIG = load_defaults()
        def main(): pass
        if __name__ == "__main__":
            main()
    ''')
    assert mod.has_main_guard
    assert mod.doc == "Tool entry."
    assert shape(mod.functions["m.py::<main>"].body) == [("call", "load_defaults", ["CONFIG"]), ("call", "main", [])]


def test_module_without_top_level_calls_has_no_main():
    mod = ex('''
        def f(): pass
        X = 1
    ''')
    assert "m.py::<main>" not in mod.functions
    assert not mod.has_main_guard


def test_package_dunder_main_always_gets_main():
    mod = extract_module("pkg/__main__.py", b"from .cli import run\nrun()\n")
    assert shape(mod.functions["pkg/__main__.py::<main>"].body) == [("call", "run", [])]


# ── type hints used later for call resolution ──────────────────────────────


def test_type_hints_for_variables_and_fields():
    mod = ex('''
        class Svc:
            limit: int
            def __init__(self, store: "Store", cfg):
                self.store = store
                self.client = Client(cfg)
                self.cache: Cache = make_cache()
            def run(self):
                repo = Repo()
                x: Model = build()
                return get_store().save()
    ''')
    assert mod.classes["m.py::Svc"].fields == {
        "limit": [("ann", "int")],
        "store": [("ann", "Store")],
        "client": [("call", "Client")],
        "cache": [("ann", "Cache"), ("call", "make_cache")],
    }
    hints = mod.functions["m.py::Svc.run"].hints
    assert hints["repo"] == [("call", "Repo")]
    assert hints["x"] == [("ann", "Model"), ("call", "build")]
    assert hints["$1"] == [("call", "get_store")]


# ── full expression text, for the simulation ──────────────────────────────


def test_expressions_keep_their_full_text_with_nested_calls_as_temporaries():
    mod = ex('''
        def f(a, items, key, ok=True):
            total = a + helper(len(items)) * 2
            items[key] = total
            if check(total) and total > 10:
                pass
            for i, x in enumerate(items):
                pass
            return {"total": total, "n": count(items)}
    ''')
    fn = mod.functions["m.py::f"]
    assert [(p.name, p.default) for p in fn.params] == [("a", None), ("items", None), ("key", None), ("ok", "True")]
    items = list(walk(fn.body))
    helper = next(i for i in items if type(i).__name__ == "Call" and i.callee == "helper")
    assert helper.args[0].expr == "__t1"  # len(items) ran first and is held in $1
    assigns = [i for i in items if type(i).__name__ == "Assign"]
    assert [(a.target, a.value) for a in assigns] == [("total", "a + __t2 * 2"), ("items[key]", "total")]
    assert next(i for i in items if type(i).__name__ == "If").test == "__t3 and total > 10"
    loop = next(i for i in items if type(i).__name__ == "Loop")
    assert (loop.target, loop.iter) == ("i, x", "__t4")
    assert next(i for i in items if type(i).__name__ == "Return").expr == '{"total": total, "n": __t5}'


def test_receivers_augmented_assignment_and_while_conditions():
    mod = ex('''
        def f(name, x, y):
            up = name.upper()
            saved = get_store().save()
            x += grow(y)
            while more(x):
                pass
    ''')
    items = list(walk(mod.functions["m.py::f"].body))
    calls = {i.callee: i for i in items if type(i).__name__ == "Call"}
    assert calls["name.upper"].receiver == "name"
    assert calls["$1.save"].receiver == "__t1"
    augmented = next(i for i in items if type(i).__name__ == "Assign")
    assert (augmented.target, augmented.value) == ("x", "x + (__t2)")
    loop = next(i for i in items if type(i).__name__ == "Loop")
    assert loop.test == "__t3"


def test_module_level_constants_are_kept():
    mod = ex('''
        DEFAULTS = {"db": "shop.db", "retries": 3}
        NAME: str = "shop"
        CLIENT = make_client()
        def f(): pass
    ''')
    assert mod.constants == {"DEFAULTS": '{"db": "shop.db", "retries": 3}', "NAME": '"shop"'}


def test_expression_sources_are_exact_when_the_file_starts_with_blank_lines():
    mod = extract_module("m.py", b"\n\n# comment\ndef f(names):\n    for name in names:\n        pass\n")
    loop = mod.functions["m.py::f"].body[0]
    assert (loop.target, loop.iter) == ("name", "names")
