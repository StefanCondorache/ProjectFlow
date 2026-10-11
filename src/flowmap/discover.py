"""Find the source files of a project folder.

Inside a git work tree the file list comes from git, so .gitignore is honoured.
Anywhere else (or when git knows no files there) the folder is walked. Both
paths drop directories that never hold project code: virtualenvs, caches,
dependency folders, build output and hidden directories.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

SKIP_DIRS = {"__pycache__", "node_modules", "site-packages", "build", "dist"}


def discover_files(
    root: str | Path,
    extensions: tuple[str, ...] | None = (".py",),
    patterns: tuple[str, ...] = (),
) -> list[str]:
    """Return project files as sorted, root-relative posix paths.

    A file is kept when its suffix is in ``extensions`` or its name matches
    one of the fnmatch ``patterns`` (e.g. ``"Dockerfile"``, ``"*.sh"``)."""
    root = Path(root).resolve()
    git_files = _git_files(root)
    candidates = git_files if git_files is not None else _walk(root)
    skip = _SkipRules(root)

    def wanted(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        return extensions is None or (bool(extensions) and rel.endswith(extensions)) or any(fnmatch.fnmatchcase(name, p) for p in patterns)

    return sorted(rel for rel in candidates if wanted(rel) and not skip.excludes(rel) and _contained_file(root, rel))


def _contained_file(root: Path, rel: str) -> bool:
    try:
        target = (root / rel).resolve()
        return target.is_relative_to(root) and target.is_file()
    except (OSError, RuntimeError):
        return False


def discover_directories(root: str | Path) -> list[str]:
    """Include empty project folders while applying the same skip rules."""
    root = Path(root).resolve()
    skip = _SkipRules(root)
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames
            if not skip.excludes_dir((rel_dir / d).as_posix())
            and not (Path(dirpath) / d).is_symlink()
        )
        if rel_dir != Path("."):
            found.append(rel_dir.as_posix())
    if found and _git_files(root) is not None:
        try:
            ignored = subprocess.run(
                ["git", "-C", str(root), "check-ignore", "-z", "--stdin"],
                input="\0".join(found).encode() + b"\0", capture_output=True,
            )
            if ignored.returncode in (0, 1):
                excluded = set(ignored.stdout.decode("utf-8", "surrogateescape").split("\0"))
                found = [d for d in found if d not in excluded]
        except OSError:
            pass
    return sorted(found)


def _git_files(root: Path) -> list[str] | None:
    try:
        # An explicitly selected folder ignored by an enclosing repository is
        # still its own project (e.g. fixtures and temporary checkouts).
        ignored_root = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", str(root)], capture_output=True,
        )
        if ignored_root.returncode == 0:
            top = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
            )
            if top.returncode != 0 or Path(top.stdout.decode().strip()).resolve() != root:
                return None
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _walk(root: Path) -> list[str]:
    skip = _SkipRules(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not skip.excludes_dir((rel_dir / d).as_posix()) and not (Path(dirpath) / d).is_symlink())
        found.extend((rel_dir / name).as_posix() for name in filenames)
    return found


class _SkipRules:
    def __init__(self, root: Path):
        self.root = root
        self._dir_cache: dict[str, bool] = {}

    def excludes(self, rel_file: str) -> bool:
        parts = rel_file.split("/")[:-1]
        return any(self.excludes_dir("/".join(parts[: i + 1])) for i in range(len(parts)))

    def excludes_dir(self, rel_dir: str) -> bool:
        cached = self._dir_cache.get(rel_dir)
        if cached is None:
            name = rel_dir.rsplit("/", 1)[-1]
            cached = (
                name.startswith(".")
                or name in SKIP_DIRS
                or name.endswith(".egg-info")
                or (self.root / rel_dir / "pyvenv.cfg").exists()
            )
            self._dir_cache[rel_dir] = cached
        return cached
