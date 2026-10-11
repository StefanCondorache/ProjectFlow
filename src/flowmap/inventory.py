"""A language-independent file inventory and bounded, contained text reads."""

from __future__ import annotations

import codecs
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from flowmap.discover import discover_directories, discover_files

MAX_TEXT_BYTES = 1_000_000
MAX_SOURCE_LINES = 200
MAX_LINE_CHARS = 2_000
_LANGUAGES = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript", ".go": "go", ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".swift": "swift", ".scala": "scala", ".sh": "shell", ".bash": "shell",
    ".sql": "sql", ".r": "r", ".lua": "lua", ".vue": "vue", ".svelte": "svelte",
    ".html": "html", ".css": "css", ".scss": "scss", ".less": "less",
    ".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
    ".xml": "xml", ".ini": "ini", ".cfg": "ini", ".md": "markdown", ".rst": "rst",
    ".txt": "text", ".csv": "csv", ".svg": "svg",
}
_CONFIG_EXTS = {".json", ".toml", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".conf", ".lock"}
_DOC_EXTS = {".md", ".rst", ".txt", ".csv", ".adoc"}
_ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".pdf", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".wav", ".ogg",
    ".zip", ".gz", ".tar", ".bin", ".exe", ".dll", ".so", ".wasm",
}
_CONFIG_NAMES = {"dockerfile", "makefile", "cmakelists.txt", "procfile", "justfile", "gemfile", "go.mod", "go.sum", ".gitignore", ".gitattributes", ".editorconfig", ".npmrc", ".env.example"}


@dataclass
class InventoryFile:
    path: str
    category: str
    language: str
    size: int
    readable: bool
    analysis: str = "structural"


@dataclass
class Inventory:
    files: dict[str, InventoryFile] = field(default_factory=dict)
    directories: set[str] = field(default_factory=set)

    def languages(self, paths=None) -> dict[str, int]:
        return dict(sorted(Counter(self.files[p].language for p in (self.files if paths is None else paths)).items()))


def contained_path(root: Path, file: str) -> Path:
    """Validate containment again immediately before a file is opened."""
    relative = Path(file)
    if not file or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("file must be a project-relative path")
    try:
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise ValueError("file is outside the project or no longer exists")
    except (OSError, RuntimeError) as err:
        raise ValueError("file is unavailable") from err
    return target


def read_text(root: Path, file: str) -> str | None:
    """Read at most one MB; large or binary files remain structural assets."""
    try:
        target = contained_path(root, file)
        with target.open("rb") as stream:
            data = stream.read(MAX_TEXT_BYTES + 1)
        if len(data) > MAX_TEXT_BYTES or b"\0" in data:
            return None
        return data.decode("utf-8-sig")
    except (OSError, ValueError, UnicodeError):
        return None


def build_inventory(root: Path) -> Inventory:
    inventory = Inventory(directories=set(discover_directories(root)))
    for path in discover_files(root, extensions=None):
        try:
            target = contained_path(root, path)
            size = target.stat().st_size
        except (OSError, ValueError):
            continue
        suffix = Path(path).suffix.lower()
        name = Path(path).name.lower()
        language = _LANGUAGES.get(suffix, "unknown")
        if suffix in _ASSET_EXTS:
            category = "asset"
        elif suffix in _CONFIG_EXTS or name in _CONFIG_NAMES or name.endswith(".dockerfile"):
            category = "config"
        elif suffix in _DOC_EXTS or language == "unknown":
            category = "document"
        else:
            category = "module"
        # Only a small prefix is needed to identify text; scans use read_text's full bound.
        try:
            with target.open("rb") as stream:
                prefix = stream.read(8192)
            readable = b"\0" not in prefix
            # A bounded prefix may end inside a valid multibyte character.
            decoder = codecs.getincrementaldecoder("utf-8-sig")()
            decoder.decode(prefix, final=len(prefix) >= size)
        except (OSError, UnicodeError):
            readable = False
        if not readable and category == "document":
            category = "asset"
        inventory.files[path] = InventoryFile(path, category, language, size, readable)
        inventory.directories.update(p.as_posix() for p in Path(path).parents if p != Path("."))
    return inventory


def read_source(root: Path, inventory: Inventory, file: str, start: int = 1, end: int | None = None) -> dict:
    """A source window of at most 200 lines and one MB, with capped line lengths."""
    item = inventory.files.get(file)
    if item is None or not item.readable:
        raise ValueError("not a readable inventoried file")
    target = contained_path(root, file)
    start = max(1, start)
    end = min(end if end is not None else start + MAX_SOURCE_LINES - 1, start + MAX_SOURCE_LINES - 1)
    lines = []
    try:
        with target.open("rb") as stream:
            budget = MAX_TEXT_BYTES
            line_number = 0
            while budget > 0 and line_number < end:
                raw = stream.readline(budget + 1)
                if not raw:
                    break
                if len(raw) > budget:
                    break
                budget -= len(raw)
                line_number += 1
                if b"\0" in raw:
                    raise ValueError("file is no longer readable text")
                if line_number >= start:
                    text = raw.decode("utf-8-sig" if line_number == 1 else "utf-8").rstrip("\r\n")
                    lines.append(text[:MAX_LINE_CHARS] + ("…" if len(text) > MAX_LINE_CHARS else ""))
    except (OSError, UnicodeError) as err:
        raise ValueError("file is no longer readable text") from err
    return {"file": file, "start": start, "lines": lines}
