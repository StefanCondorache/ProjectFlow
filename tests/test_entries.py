from pathlib import Path

import pytest

from flowmap.project import load_project

SHOP = Path(__file__).parent / "fixtures" / "shop"


@pytest.fixture(scope="module")
def entries():
    return load_project(SHOP).entries


def by_target(entries):
    return {e.target: e for e in entries}


def kinds(entry):
    return sorted(s.kind for s in entry.sources)


def test_each_start_point_is_listed_once_and_tests_are_left_out(entries):
    assert sorted(e.target for e in entries) == [
        "scripts/report.py::<main>",
        "src/shop/__main__.py::<main>",
        "src/shop/cli.py::main",
        "src/shop/worker.py::<main>",
    ]


def test_python_dash_m_on_a_package_runs_its_dunder_main(entries):
    entry = by_target(entries)["src/shop/__main__.py::<main>"]
    assert entry.label == "python -m shop"
    assert kinds(entry) == ["compose", "docker", "package-main"]


def test_compose_command_that_relies_on_the_image_entrypoint(entries):
    sources = by_target(entries)["src/shop/__main__.py::<main>"].sources
    assert any(s.kind == "compose" and "api" in s.text for s in sources)


def test_shell_script_started_by_compose_is_followed(entries):
    entry = by_target(entries)["src/shop/worker.py::<main>"]
    assert entry.label == "python -m shop.worker"
    assert kinds(entry) == ["compose", "main-guard", "shell"]
    assert any(s.kind == "compose" and "worker" in s.text and "run.sh" in s.text for s in entry.sources)


def test_script_path_command(entries):
    entry = by_target(entries)["scripts/report.py::<main>"]
    assert entry.label == "python scripts/report.py"
    assert kinds(entry) == ["compose", "main-guard"]


def test_console_script_declared_in_pyproject(entries):
    entry = by_target(entries)["src/shop/cli.py::main"]
    assert entry.label == "shop"
    assert kinds(entry) == ["pyproject"]


def test_deployed_entries_rank_before_declared_scripts(entries):
    assert entries[-1].target == "src/shop/cli.py::main"


def test_reach_counts_project_functions_reachable_from_the_start(entries):
    # work, default_store, Store.__init__, BaseStore.connect, Store.load_all,
    # archive, Store.save, slugify
    assert by_target(entries)["src/shop/worker.py::<main>"].reach == 8
