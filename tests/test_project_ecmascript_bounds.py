"""Loading ECMAScript preserves the existing bounded source inventory."""

import pytest

from flowmap.inventory import MAX_TEXT_BYTES
from flowmap.lang import Adapter
from flowmap.lang.ecmascript.extract import extract_module
from flowmap.lang.python import ADAPTER as PYTHON
from flowmap.project import load_project


@pytest.fixture
def ecmascript_loader(monkeypatch):
    # Use the real extractor before registry integration is complete.
    ecmascript = [
        Adapter(language, extensions, extract_module, lambda root, modules: None,
                lambda root, modules, commands: [], family="ecmascript")
        for language, extensions in [
            ("javascript", (".js", ".jsx", ".mjs", ".cjs")),
            ("typescript", (".ts", ".tsx", ".mts", ".cts")),
        ]
    ]
    monkeypatch.setattr("flowmap.project.adapters", lambda: [PYTHON, *ecmascript])


@pytest.mark.parametrize("suffix,language", [(".JS", "javascript"), (".TSX", "typescript")])
def test_uppercase_extensions_receive_semantic_analysis(tmp_path, ecmascript_loader, suffix, language):
    file = "worker" + suffix
    (tmp_path / file).write_text("export function run(value) { return value; }")
    project = load_project(tmp_path)
    assert project.modules[file].language == language
    assert any(function.name == "run" for function in project.functions.values())
    assert project.inventory.files[file].analysis == "semantic"


def test_oversized_ecmascript_remains_structural(tmp_path, ecmascript_loader):
    source = b"export function run() {}\n"
    (tmp_path / "bounded.ts").write_bytes(source + b" " * (MAX_TEXT_BYTES - len(source)))
    (tmp_path / "oversized.ts").write_bytes(source + b" " * (MAX_TEXT_BYTES + 1 - len(source)))
    project = load_project(tmp_path)
    assert "bounded.ts" in project.modules
    assert "oversized.ts" not in project.modules
    assert project.inventory.files["oversized.ts"].analysis == "structural"
    assert not any(function.file == "oversized.ts" for function in project.functions.values())


@pytest.mark.parametrize("source", [
    "const x = " + "+".join(["a"] * 1500) + ";",
    "function f(a) {" + "if (a) {" * 600 + "g();" + "}" * 600 + "}",
], ids=["plus-chain", "nested-if"])
def test_too_deeply_nested_source_does_not_abort_the_project(tmp_path, source):
    (tmp_path / "deep.js").write_text(source)
    (tmp_path / "ok.js").write_text("export function run(v) { return v; }")
    (tmp_path / "worker.py").write_text("def process(items):\n    return items\n")
    project = load_project(tmp_path)
    assert {"ok.js", "worker.py"} <= set(project.modules)
    assert "deep.js" not in project.modules
    assert project.inventory.files["deep.js"].analysis != "semantic"
