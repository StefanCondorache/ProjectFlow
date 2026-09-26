from pathlib import Path

import pytest

from flowmap.ir import Call, walk
from flowmap.project import load_project

SHOP = Path(__file__).parent / "fixtures" / "shop"


@pytest.fixture(scope="module")
def project():
    return load_project(SHOP)


def resolved(project, func_id: str, callee: str) -> tuple[str, str | None, str]:
    """(kind, target-or-external, confidence) of the first call to ``callee``."""
    for item in walk(project.functions[func_id].body):
        if isinstance(item, Call) and item.callee == callee:
            return item.kind, item.target or item.external, item.confidence
    raise AssertionError(f"no call to {callee} in {func_id}")


CLI = "src/shop/cli.py"
ORDERS = "src/shop/orders.py"
STORE = "src/shop/store.py"
UTIL = "src/shop/util.py"


def test_function_imported_with_from_import(project):
    assert resolved(project, f"{CLI}::main", "load_config") == ("project", "src/shop/config.py::load_config", "exact")


def test_function_in_the_same_module(project):
    assert resolved(project, f"{CLI}::main", "parse") == ("project", f"{CLI}::parse", "exact")


def test_class_call_runs_its_init(project):
    assert resolved(project, f"{ORDERS}::make_service", "Store") == ("project", f"{STORE}::Store.__init__", "exact")
    assert resolved(project, f"{ORDERS}::make_service", "OrderService")[1] == f"{ORDERS}::OrderService.__init__"


def test_class_without_init_is_a_construction(project):
    assert resolved(project, f"{CLI}::main", "Order") == ("class", f"{ORDERS}::Order", "exact")


def test_method_on_self(project):
    assert resolved(project, f"{ORDERS}::OrderService.place", "self.validate")[:2] == (
        "project",
        f"{ORDERS}::OrderService.validate",
    )


def test_inherited_method_on_self(project):
    assert resolved(project, f"{STORE}::Store.__init__", "self.connect")[:2] == ("project", f"{STORE}::BaseStore.connect")


def test_super_call_goes_to_the_base_class(project):
    assert resolved(project, f"{STORE}::AuditStore.save", "$1.save")[:2] == ("project", f"{STORE}::Store.save")


def test_field_typed_by_an_annotated_init_parameter(project):
    assert resolved(project, f"{ORDERS}::OrderService.place", "self.store.save") == (
        "project",
        f"{STORE}::Store.save",
        "inferred",
    )


def test_field_built_from_a_class_in_an_imported_submodule(project):
    assert resolved(project, f"{ORDERS}::OrderService.place", "self.gateway.charge")[:2] == (
        "project",
        "src/shop/payments/gateway.py::Gateway.charge",
    )


def test_relative_import_of_a_module_under_an_alias(project):
    assert resolved(project, f"{ORDERS}::OrderService.place", "u.log_event")[:2] == ("project", f"{UTIL}::log_event")


def test_name_re_exported_by_a_package_init(project):
    assert resolved(project, f"{CLI}::main", "shop.slugify")[:2] == ("project", f"{UTIL}::slugify")


def test_variable_typed_by_a_return_annotation(project):
    assert resolved(project, f"{CLI}::main", "service.place") == ("project", f"{ORDERS}::OrderService.place", "inferred")


def test_variable_typed_by_an_inferred_return(project):
    assert resolved(project, f"{ORDERS}::archive", "st.save")[:2] == ("project", f"{STORE}::Store.save")


def test_method_called_on_a_fresh_instance(project):
    assert resolved(project, "scripts/report.py::run", "$1.load_all")[:2] == ("project", f"{STORE}::Store.load_all")


def test_import_resolved_from_the_script_folder(project):
    assert resolved(project, "scripts/report.py::run", "helpers.render")[:2] == ("project", "scripts/helpers.py::render")
    assert resolved(project, "scripts/report.py::run", "s.add")[:2] == ("project", "scripts/helpers.py::Summary.add")


def test_function_local_import(project):
    assert resolved(project, f"{CLI}::version", "s")[:2] == ("project", f"{UTIL}::slugify")


def test_nested_function(project):
    assert resolved(project, f"{UTIL}::outer_fn", "fmt")[:2] == ("project", f"{UTIL}::outer_fn.<locals>.fmt")


def test_outside_libraries_keep_their_dotted_names(project):
    gateway = "src/shop/payments/gateway.py::Gateway.charge"
    assert resolved(project, gateway, "requests.post")[:2] == ("external", "requests.post")
    assert resolved(project, gateway, "resp.json")[:2] == ("external", "requests.post().json")
    assert resolved(project, "src/shop/config.py::load_config", "json.load")[:2] == ("external", "json.load")
    assert resolved(project, "src/shop/config.py::load_config", "open")[:2] == ("builtin", "open")
    assert resolved(project, f"{CLI}::parse", "p.add_argument")[:2] == ("external", "argparse.ArgumentParser().add_argument")


def test_receiver_named_like_a_project_class_is_a_guess(project):
    assert resolved(project, f"{UTIL}::total", "store.load_all") == ("project", f"{STORE}::Store.load_all", "guess")


def test_unknown_receiver_stays_unresolved(project):
    assert resolved(project, f"{UTIL}::describe", "obj.summary")[:2] == ("unresolved", None)


def test_annotation_with_a_nested_union_does_not_hang(tmp_path):
    (tmp_path / "m.py").write_text(
        "def f(opts: dict[str, int | None]):\n"
        "    return opts.get('a')\n"
    )
    project = load_project(tmp_path)
    assert resolved(project, "m.py::f", "opts.get")[:2] == ("external", "dict.get")


def test_types_resolve_annotations_and_what_classes_derive_from(tmp_path):
    (tmp_path / "models.py").write_text(
        "class ShopError(Exception):\n    pass\n\n\nclass NotFound(ShopError, KeyError):\n    pass\n\n\nclass Order:\n    pass\n"
    )
    (tmp_path / "app.py").write_text("from models import Order\n")
    types = load_project(tmp_path).resolvers["python"]
    assert types.class_of("app.py", "Optional[Order]") == "models.py::Order"
    assert types.class_of("app.py", "int") is None
    lineage = types.lineage("models.py::NotFound")
    assert lineage[:2] == ["NotFound", "ShopError"]
    assert {"KeyError", "LookupError", "Exception", "BaseException"} <= set(lineage)
