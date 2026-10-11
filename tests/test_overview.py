from dataclasses import asdict

import pytest

from flowmap.inventory import read_source
from flowmap.overview import build_overview
from flowmap.project import load_project
from flowmap.server import project_summary


def write(root, path, text=""):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def test_unknown_language_has_a_map(tmp_path):
    write(tmp_path, "app.custom", "a project without a parser")
    project = load_project(tmp_path)
    graph = build_overview(project)
    assert graph.root == "@project"
    node = next(n for n in graph.nodes if n.detail.get("path") == "app.custom")
    assert node.id == "file:app.custom" and node.kind == "document"
    assert not project.functions
    assert graph.overview["total_files"] == 1


def test_inventory_classifies_all_files_without_changing_semantic_stats(tmp_path):
    write(tmp_path, "app.py", "def run():\n    pass\n")
    write(tmp_path, "main.ts", "export const value = 1")
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "README.md", "hello")
    (tmp_path / "photo.png").write_bytes(b"\x89PNG\x00\x01")
    write(tmp_path, "Dockerfile", "FROM alpine")
    project = load_project(tmp_path)
    nodes = {n.detail["path"]: n for n in build_overview(project).nodes}
    assert {p: n.kind for p, n in nodes.items()} == {
        "app.py": "module", "main.ts": "module", "package.json": "config",
        "README.md": "document", "photo.png": "asset", "Dockerfile": "config",
    }
    assert nodes["app.py"].detail["analysis"] == "semantic"
    assert nodes["main.ts"].detail["analysis"] == "imports"
    assert nodes["photo.png"].detail["readable"] is False
    assert nodes["app.py"].detail["functions"][0]["id"] == "app.py::run"
    summary = project_summary(project)
    assert summary["stats"]["files"] == 1 and summary["files"] == ["app.py"]
    assert summary["inventory"]["files"] == 6
    assert summary["inventory"]["parsed_files"] == 1
    assert summary["inventory"]["languages"]["typescript"] == 1
    assert summary["capabilities"] == {
        "overview": True, "flow_languages": ["python"], "simulation_languages": ["python"],
    }


def test_collapsed_folders_aggregate_dependencies_and_expansion_restores_files(tmp_path):
    write(tmp_path, "app/one.ts", "import { x } from '../lib/value';\nimport './two';")
    write(tmp_path, "app/two.ts", "export { x } from '../lib/value';")
    write(tmp_path, "lib/value.ts", "export const x = 1")
    project = load_project(tmp_path)
    closed = build_overview(project)
    assert {n.id for n in closed.nodes} == {"dir:app", "dir:lib"}
    app = next(n for n in closed.nodes if n.id == "dir:app")
    assert app.detail["files"] == 2 and not app.detail["container"]
    assert len(closed.edges) == 1
    edge = closed.edges[0]
    assert (edge.source, edge.target, edge.kind, edge.count, edge.confidence) == (
        "dir:app", "dir:lib", "dependency", 2, "inferred",
    )
    assert edge.data == []
    assert {e["file"] for e in edge.evidence} == {"app/one.ts", "app/two.ts"}
    opened = build_overview(project, expanded={"dir:app"})
    assert next(n for n in opened.nodes if n.id == "dir:app").detail["container"]
    assert {n.id for n in opened.nodes if n.parent == "dir:app"} == {"file:app/one.ts", "file:app/two.ts"}
    assert all(e.source != e.target for e in opened.edges)


def test_python_dependencies_resolve_relative_and_script_imports_and_keep_evidence(tmp_path):
    write(tmp_path, "pkg/__init__.py")
    write(tmp_path, "pkg/a.py", "from . import b\nimport os\nfrom .missing import thing\n")
    write(tmp_path, "pkg/b.py", "from .a import thing\n")
    write(tmp_path, "scripts/run.py", "import helper\n")
    write(tmp_path, "scripts/helper.py")
    graph = build_overview(load_project(tmp_path), expanded={"dir:pkg", "dir:scripts"})
    pairs = {(e.source, e.target): e for e in graph.edges}
    edge = pairs[("file:pkg/a.py", "file:pkg/b.py")]
    assert edge.confidence == "verified" and edge.evidence == [{"file": "pkg/a.py", "line": 1, "text": "from . import b"}]
    assert pairs[("file:scripts/run.py", "file:scripts/helper.py")].confidence == "verified"
    assert pairs[("file:pkg/b.py", "file:pkg/a.py")].confidence == "verified"
    assert pairs[("file:pkg/a.py", "external:os")].confidence == "inferred"
    assert not any(n.id.startswith("external:.") for n in graph.nodes)


def test_javascript_scanner_ignores_comments_strings_and_unresolved_relative_paths(tmp_path):
    write(tmp_path, "main.js", "// import './fake';\nconst s = \"import './fake'\";\n/* require('./fake') */\nimport value from './value.js';\nconst fs = require('node:fs');\nexport { value } from './value.js';\nimport('./value.js');\nimport './missing';\n")
    write(tmp_path, "value.ts", "export const value = 1")
    write(tmp_path, "fake.js")
    graph = build_overview(load_project(tmp_path))
    assert not any(e.target == "file:fake.js" for e in graph.edges)
    edge = next(e for e in graph.edges if e.target == "file:value.ts")
    assert edge.count == 3 and edge.confidence == "inferred"
    assert [e["line"] for e in edge.evidence] == [4, 6, 7]
    assert any(n.id == "external:node:fs" for n in graph.nodes)
    assert not any(n.id == "external:./missing" for n in graph.nodes)


def test_folder_and_file_focus_preserve_neighbour_dependencies(tmp_path):
    write(tmp_path, "app/start.ts", "import '../lib/value';")
    write(tmp_path, "app/next.ts")
    write(tmp_path, "lib/value.ts", "import '../app/start';")
    project = load_project(tmp_path)
    focused = build_overview(project, path="app")
    assert focused.overview["path"] == "app" and focused.overview["files"] == 2
    assert focused.overview["total_files"] == 3
    assert {n.id for n in focused.nodes} == {"file:app/start.ts", "file:app/next.ts", "dir:lib"}
    file_graph = build_overview(project, path="app/start.ts")
    assert file_graph.overview["files"] == 1
    assert {n.id for n in file_graph.nodes} == {"file:app/start.ts", "dir:lib"}
    assert len(file_graph.edges) == 2
    assert all(n.parent is None for n in file_graph.nodes)


def test_empty_project_and_empty_folders_are_explorable(tmp_path):
    project = load_project(tmp_path)
    assert build_overview(project).nodes == []
    assert build_overview(project).overview["files"] == 0
    (tmp_path / "notes").mkdir()
    project = load_project(tmp_path)
    folder = build_overview(project).nodes[0]
    assert folder.id == "dir:notes" and folder.detail["files"] == 0
    assert build_overview(project, path="notes").nodes == []


def test_visible_map_is_bounded_and_retains_total_counts(tmp_path):
    for i in range(240):
        write(tmp_path, f"wide/file{i:03}.custom", "text")
    project = load_project(tmp_path)
    graph = build_overview(project, expanded={"dir:wide"})
    assert len(graph.nodes) <= 200
    assert graph.overview["truncated"] is True
    assert graph.overview["files"] == 240 and graph.overview["total_files"] == 240
    assert all(e.source in {n.id for n in graph.nodes} and e.target in {n.id for n in graph.nodes} for e in graph.edges)


def test_inventory_skips_generated_files_and_escape_symlinks(tmp_path):
    write(tmp_path, "src/keep.custom")
    write(tmp_path, "node_modules/drop.js")
    write(tmp_path, ".cache/drop.txt")
    write(tmp_path, "myenv/pyvenv.cfg")
    write(tmp_path, "myenv/drop.py")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    (tmp_path / "escaped.txt").symlink_to(outside)
    (tmp_path / "outside-dir").symlink_to(tmp_path.parent, target_is_directory=True)
    project = load_project(tmp_path)
    assert set(project.inventory.files) == {"src/keep.custom"}
    assert not any(n.detail.get("path") == "outside-dir" for n in build_overview(project).nodes)


def test_source_preview_is_bounded_and_rechecks_containment(tmp_path):
    write(tmp_path, "notes.txt", "\n".join(str(i) for i in range(400)))
    project = load_project(tmp_path)
    assert read_source(project.root, project.inventory, "notes.txt", start=5, end=399) == {
        "file": "notes.txt", "start": 5, "lines": [str(i) for i in range(4, 204)],
    }
    with pytest.raises(ValueError):
        read_source(project.root, project.inventory, "../notes.txt")
    outside = tmp_path.parent / "secret-source.txt"
    outside.write_text("secret")
    (tmp_path / "notes.txt").unlink()
    (tmp_path / "notes.txt").symlink_to(outside)
    with pytest.raises(ValueError):
        read_source(project.root, project.inventory, "notes.txt")


def test_invalid_focus_is_rejected_instead_of_falling_back(tmp_path):
    write(tmp_path, "app.custom")
    project = load_project(tmp_path)
    for path in ("../", "/etc/passwd", "missing"):
        with pytest.raises(ValueError):
            build_overview(project, path=path)


def test_map_mermaid_distinguishes_inferred_edges_and_folder_frames(tmp_path):
    from flowmap.mermaid import to_mermaid

    write(tmp_path, "src/a.ts", "import '../lib/b';")
    write(tmp_path, "lib/b.ts")
    project = load_project(tmp_path)
    closed = to_mermaid(build_overview(project))
    assert "subgraph" not in closed and "-.->" in closed
    opened = to_mermaid(build_overview(project, expanded={"dir:src"}))
    assert 'subgraph n1["src"]' in opened or 'subgraph n0["src"]' in opened
    assert "a.ts" in opened


def test_overview_metadata_matches_shared_contract(tmp_path):
    write(tmp_path, "note.md", "hello")
    graph = build_overview(load_project(tmp_path))
    assert set(asdict(graph)["overview"]) == {
        "path", "files", "total_files", "parsed_files", "relationships", "languages", "truncated",
    }


def test_future_directives_are_not_external_dependencies(tmp_path):
    write(tmp_path, "app.py", "from __future__ import annotations\nimport json\n")
    graph = build_overview(load_project(tmp_path))
    assert any(n.id == "external:json" for n in graph.nodes)
    assert not any(n.id == "external:__future__" for n in graph.nodes)


def test_scanner_does_not_claim_analysis_of_oversized_source(tmp_path):
    write(tmp_path, "huge.ts", "import 'hidden';\n" + " " * 1_000_001)
    graph = build_overview(load_project(tmp_path))
    assert next(n for n in graph.nodes if n.id == "file:huge.ts").detail["analysis"] == "structural"
    assert not graph.edges


def test_js_regex_templates_and_multiline_imports_are_conservative(tmp_path):
    write(tmp_path, "main.ts", "const re = /import 'fake'/;\nconst template = `import 'fake'`;\nimport {\n  value,\n} from './value';\nconst next = import('./value');\nobj.require('fake');\n")
    write(tmp_path, "value.ts")
    graph = build_overview(load_project(tmp_path))
    assert {n.id for n in graph.nodes} == {"file:main.ts", "file:value.ts"}
    assert len(graph.edges) == 1 and graph.edges[0].count == 2


def test_inventory_honours_gitignored_files_and_empty_generated_folders(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write(tmp_path, ".gitignore", "generated/\n*.ignore\n")
    write(tmp_path, "keep.custom")
    write(tmp_path, "drop.ignore")
    write(tmp_path, "generated/drop.custom")
    (tmp_path / "generated/empty").mkdir()
    project = load_project(tmp_path)
    assert set(project.inventory.files) == {".gitignore", "keep.custom"}
    assert "generated" not in project.inventory.directories


def test_git_repository_with_only_ignored_files_stays_empty(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write(tmp_path, ".gitignore", "*\n")
    write(tmp_path, "drop.custom")
    assert not load_project(tmp_path).inventory.files


def test_semantic_python_analysis_preserves_large_and_coding_cookie_files(tmp_path):
    write(tmp_path, "large.py", "def run():\n    pass\n#" + "x" * 1_000_001)
    (tmp_path / "latin.py").write_bytes(b"# coding: latin-1\ndef hello():\n    return 'caf\xe9'\n")
    project = load_project(tmp_path)
    assert "large.py::run" in project.functions
    assert "latin.py::hello" in project.functions
    assert project.inventory.files["large.py"].analysis == "semantic"
    assert project.inventory.files["latin.py"].analysis == "semantic"


def test_utf8_character_split_by_inventory_prefix_remains_readable(tmp_path):
    write(tmp_path, "README.md", "a" * 8191 + "é\nsecond line\n")
    project = load_project(tmp_path)
    item = project.inventory.files["README.md"]
    assert item.category == "document" and item.readable is True
    assert read_source(project.root, project.inventory, "README.md", start=2)["lines"] == ["second line"]


@pytest.mark.parametrize("tail", [b"\xff", b"\xc3", b"\0"])
def test_invalid_utf8_or_binary_prefix_stays_unreadable(tmp_path, tail):
    (tmp_path / "invalid.txt").write_bytes(b"a" * 8191 + tail)
    item = load_project(tmp_path).inventory.files["invalid.txt"]
    assert item.category == "asset" and item.readable is False


def test_regex_literal_after_arrow_function_does_not_become_an_import(tmp_path):
    write(tmp_path, "app.js", "const pattern = () => /import 'fake-package'/;\nimport 'real-package';\n")
    graph = build_overview(load_project(tmp_path))
    assert {n.id for n in graph.nodes} == {"file:app.js", "external:real-package"}
    assert len(graph.edges) == 1 and graph.edges[0].target == "external:real-package"
    assert graph.edges[0].evidence == [{"file": "app.js", "line": 2, "text": "import 'real-package';"}]


def test_focused_namespace_dependency_targets_the_containing_focus_frame(tmp_path):
    write(tmp_path, "pkg/part.py", "def run():\n    pass\n")
    write(tmp_path, "other.py", "import pkg\n")
    graph = build_overview(load_project(tmp_path), path="pkg")
    nodes = {n.id: n for n in graph.nodes}
    assert set(nodes) == {"dir:pkg", "file:pkg/part.py", "file:other.py"}
    assert nodes["dir:pkg"].detail["container"] is True
    assert nodes["dir:pkg"].detail["expanded"] is True
    assert nodes["dir:pkg"].detail["expandable"] is False
    assert nodes["file:pkg/part.py"].parent == "dir:pkg"
    assert len(graph.edges) == 1
    assert (graph.edges[0].source, graph.edges[0].target, graph.edges[0].confidence) == (
        "file:other.py", "dir:pkg", "verified",
    )
    assert graph.overview["files"] == 1 and graph.overview["relationships"] == 1


@pytest.mark.parametrize("statement", [
    "/import 'fake-package'/.test(text);",
    "const pattern = enabled ? /import 'fake-package'/ : /none/;",
    "if (enabled) { /import 'fake-package'/.test(text); }",
    "value(); /import 'fake-package'/.test(text);",
    "if (enabled) /import 'fake-package'/.test(text);",
    "while (enabled) /import 'fake-package'/.test(text);",
    "const pattern = enabled && /import 'fake-package'/;",
    "const value = typeof /import 'fake-package'/;",
    "const value = input + /import 'fake-package'/;",
    "function fail() { throw /import 'fake-package'/; }",
    "if (enabled) {} /import 'fake-package'/.test(text);",
])
def test_regex_expression_and_statement_boundaries_do_not_create_imports(tmp_path, statement):
    write(tmp_path, "app.js", statement + "\nimport 'real-package';\n")
    graph = build_overview(load_project(tmp_path))
    assert {n.id for n in graph.nodes} == {"file:app.js", "external:real-package"}
    assert len(graph.edges) == 1 and graph.edges[0].target == "external:real-package"
    assert graph.edges[0].evidence == [{"file": "app.js", "line": 2, "text": "import 'real-package';"}]


@pytest.mark.parametrize("value", [
    "24", "read()", "values[0]", "count++", "({ value: 1 })", "{ value: 1 }",
    "obj.catch()", "obj.if()", "obj?.catch()",
])
def test_division_keeps_real_dynamic_and_following_imports(tmp_path, value):
    write(tmp_path, "app.js", f"const ratio = {value} / import('division-package') / scale;\nimport 'real-package';\n")
    graph = build_overview(load_project(tmp_path))
    assert {n.id for n in graph.nodes} == {"file:app.js", "external:division-package", "external:real-package"}
    assert {edge.target for edge in graph.edges} == {"external:division-package", "external:real-package"}
