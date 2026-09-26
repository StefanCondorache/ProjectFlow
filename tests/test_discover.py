import subprocess
from pathlib import Path

from flowmap.discover import discover_files


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_walk_skips_virtualenvs_caches_and_hidden_dirs(tmp_path):
    write(tmp_path, "app/main.py")
    write(tmp_path, "app/util.py")
    write(tmp_path, ".venv/lib/site.py")
    write(tmp_path, "myenv/pyvenv.cfg")  # a virtualenv with an unusual name
    write(tmp_path, "myenv/lib/pkg.py")
    write(tmp_path, "node_modules/x/y.py")
    write(tmp_path, "app/__pycache__/main.py")
    write(tmp_path, ".tox/py314/z.py")
    write(tmp_path, "notes.txt")

    assert discover_files(tmp_path) == ["app/main.py", "app/util.py"]


def test_git_repo_respects_gitignore_and_includes_untracked(tmp_path):
    git(tmp_path, "init", "-q")
    write(tmp_path, ".gitignore", "generated/\n")
    write(tmp_path, "src/a.py")
    write(tmp_path, "generated/b.py")
    write(tmp_path, "new_untracked.py")
    git(tmp_path, "add", "src/a.py", ".gitignore")

    assert discover_files(tmp_path) == ["new_untracked.py", "src/a.py"]


def test_git_repo_skips_a_committed_virtualenv(tmp_path):
    git(tmp_path, "init", "-q")
    write(tmp_path, "app.py")
    write(tmp_path, "venv/pyvenv.cfg")
    write(tmp_path, "venv/lib/dep.py")
    git(tmp_path, "add", ".")

    assert discover_files(tmp_path) == ["app.py"]


def test_git_repo_ignores_files_deleted_from_disk(tmp_path):
    git(tmp_path, "init", "-q")
    write(tmp_path, "keep.py")
    write(tmp_path, "gone.py")
    git(tmp_path, "add", ".")
    (tmp_path / "gone.py").unlink()

    assert discover_files(tmp_path) == ["keep.py"]


def test_only_requested_extensions_are_returned(tmp_path):
    write(tmp_path, "a.py")
    write(tmp_path, "b.ts")
    write(tmp_path, "c.md")

    assert discover_files(tmp_path, extensions=(".py", ".ts")) == ["a.py", "b.ts"]


def test_patterns_select_extra_files_by_name(tmp_path):
    write(tmp_path, "Dockerfile")
    write(tmp_path, "deploy/api.Dockerfile")
    write(tmp_path, "run.sh")
    write(tmp_path, "notes.txt")
    write(tmp_path, "a.py")

    assert discover_files(tmp_path, extensions=(), patterns=("Dockerfile", "*.Dockerfile", "*.sh")) == [
        "Dockerfile",
        "deploy/api.Dockerfile",
        "run.sh",
    ]
