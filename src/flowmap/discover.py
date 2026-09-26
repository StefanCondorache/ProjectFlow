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
    extensions: tuple[str, ...] = (".py",),
    patterns: tuple[str, ...] = (),
) -> list[str]:
    """Return project files as sorted, root-relative posix paths.

    A file is kept when its suffix is in ``extensions`` or its name matches
    one of the fnmatch ``patterns`` (e.g. ``"Dockerfile"``, ``"*.sh"``)."""
    root = Path(root).resolve()
    candidates = _git_files(root) or _walk(root)
    skip = _SkipRules(root)

    def wanted(rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        return (bool(extensions) and rel.endswith(extensions)) or any(fnmatch.fnmatchcase(name, p) for p in patterns)

    return sorted(rel for rel in candidates if wanted(rel) and not skip.excludes(rel) and (root / rel).is_file())


def _git_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def _walk(root: Path) -> list[str]:
    skip = _SkipRules(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not skip.excludes_dir((rel_dir / d).as_posix()))
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
