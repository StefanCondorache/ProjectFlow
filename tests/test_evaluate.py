from flowmap.lang.python.evaluate import NOT_PURE, call_builtin, call_method, evaluate
from flowmap.values import MISSING, Obj, Unknown, diff, encode


def ev(source, **env):
    return evaluate(source, lambda name: env.get(name, MISSING))


def test_literals_and_containers():
    assert ev('{"db": "x", "n": [1, 2.5, None, True], "t": (1, 2)}') == {"db": "x", "n": [1, 2.5, None, True], "t": (1, 2)}


def test_arithmetic_strings_and_fstrings():
    assert ev("a * 2 + 1", a=3) == 7
    assert ev("'x' * 3") == "xxx"
    assert ev("f'{name}-{n:03d}'", name="ab", n=7) == "ab-007"
    assert ev("-x // 2", x=5) == -3


def test_comparisons_and_conditions():
    assert ev("x > 5 and y", x=3, y=True) is False
    assert ev("k in d", k="a", d={"a": 1}) is True
    assert ev("o is None", o=Obj("Store", {})) is False
    assert ev("a if flag else b", flag=False, a=1, b=2) == 2


def test_subscripts_slices_and_fields():
    assert ev('cfg["db"]', cfg={"db": "x"}) == "x"
    assert ev("rows[1:]", rows=[1, 2, 3]) == [2, 3]
    assert ev("store.path", store=Obj("Store", {"path": "p"})) == "p"


def test_temporaries_are_the_dollar_names():
    assert ev("__t1 + 1", **{"$1": 41}) == 42


def test_unknown_values_spread_with_readable_origins():
    spread = ev("__t1 + 2", **{"$1": Unknown("load(path)")})
    assert isinstance(spread, Unknown) and spread.origin == "load(path) + 2"
    assert ev("missing * 2").origin == "missing * 2"
    assert ev('cfg["db"]', cfg=Unknown("load()")).origin == "cfg['db']"  # names stay; only temporaries are spelled out


def test_keys_set_on_an_unknown_value_can_be_read_back():
    assert ev('cfg["mode"]', cfg=Unknown("load()", {"mode": "train"})) == "train"


def test_nothing_is_ever_called():
    assert isinstance(ev("open('/etc/passwd').read()"), Unknown)
    assert isinstance(ev("__import__('os').system('true')"), Unknown)


def test_huge_results_are_not_built():
    assert isinstance(ev("'x' * 10**9"), Unknown)
    assert isinstance(ev("2 ** 100000"), Unknown)
    assert isinstance(ev("[0] * 10**8"), Unknown)


def test_comprehensions_over_known_values():
    assert ev("[x * 2 for x in xs if x > 1]", xs=[1, 2, 3]) == [4, 6]
    assert ev("{k: v for k, v in pairs}", pairs=[("a", 1)]) == {"a": 1}
    assert isinstance(ev("[__t1 for x in xs]", xs=[1], **{"$1": 5}), Unknown)  # a call per item: not knowable


def test_pure_builtins_and_methods():
    assert call_builtin("len", [[1, 2]], {}) == 2
    assert call_builtin("sorted", [[3, 1]], {"reverse": True}) == [3, 1]
    assert call_builtin("open", ["x"], {}) is NOT_PURE
    assert isinstance(call_builtin("len", [Unknown("rows")], {}), Unknown)
    assert call_method("abc", "upper", [], {}) == "ABC"
    assert call_method("a,b", "split", [","], {}) == ["a", "b"]
    assert call_method({"a": 1}, "get", ["b", 0], {}) == 0
    assert call_method({"a": 1}, "items", [], {}) == [("a", 1)]
    assert call_method(Obj("Store", {}), "save", [], {}) is NOT_PURE


def test_mutating_methods_change_the_receiver():
    rows = [1]
    assert call_method(rows, "append", [2], {}) is None
    assert rows == [1, 2]
    seen = {}
    call_method(seen, "setdefault", ["k", []], {})
    assert seen == {"k": []}


def test_encoding_for_the_viewer():
    assert encode({"a": [1, Unknown("f()")], "o": Obj("Store", {"p": "x"})}) == {
        "t": "dict",
        "v": {
            "a": {"t": "list", "v": [{"t": "val", "v": 1}, {"t": "?", "from": "f()"}]},
            "o": {"t": "obj", "cls": "Store", "v": {"p": {"t": "val", "v": "x"}}},
        },
    }
    assert encode(Unknown("load()", {"mode": "train"})) == {"t": "?", "from": "load()", "v": {"mode": {"t": "val", "v": "train"}}}


def test_diff_reports_added_changed_and_removed_paths():
    before = {"cfg": encode({"db": "x"}), "n": encode(1), "old": encode(2)}
    after = {"cfg": encode({"db": "y", "mode": "train"}), "n": encode(1), "new": encode(3)}
    assert diff(before, after) == {"added": ["cfg.mode", "new"], "changed": ["cfg.db"], "removed": ["old"]}
