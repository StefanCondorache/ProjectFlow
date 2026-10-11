"""Project structure and observed imports, projected into a bounded map."""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from flowmap.flow import Edge, Graph, Node, display_name
from flowmap.inventory import read_text
from flowmap.project import Project

MAX_VISIBLE_NODES = 200
MAX_EVIDENCE = 20
_JS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_JS_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_JS_REGEX_WORDS = {"return", "throw", "case", "delete", "void", "typeof", "new", "in", "instanceof", "yield", "await", "else", "do"}
_JS_CONTROL_WORDS = {"if", "while", "for", "with", "switch", "catch"}


@dataclass
class Relationship:
    source: str  # inventory file path
    target: str  # stable file:/dir:/external: id
    confidence: str
    evidence: dict


def _tokens(source: str):
    """Small lexical scanner: strings/comments/templates never become import keywords."""
    i, line, previous = 0, 1, ""
    can_start_regex = True
    control_header = False
    parens: list[bool] = []  # a closed control header starts a statement, unlike a call
    braces: list[bool] = []  # blocks end statements; object literals end expressions
    while i < len(source):
        ch = source[i]
        if ch.isspace():
            line += ch == "\n"
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) if end < 0 else end + 2
            line += source.count("\n", i, end)
            i = end
            continue
        at = line
        if ch in "'\"`":
            quote, start = ch, i
            i += 1
            escaped = False
            while i < len(source):
                if source[i] == "\\":
                    escaped = True
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            line += source.count("\n", start, i)
            # Escaped and template specifiers need evaluation, so omit them.
            value = source[start + 1 : i - 1] if i <= len(source) and source[i - 1] == quote else ""
            yield ("string" if quote != "`" and not escaped else "opaque", value, at)
            previous = "string"
            can_start_regex = False
            control_header = False
            continue
        if ch == "/" and can_start_regex:
            # A slash starts a regex where an operand or statement may start.
            # After a completed value it remains division, preserving imports
            # in its right-hand expression and subsequent statements.
            i += 1
            in_class = False
            while i < len(source) and source[i] != "\n":
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "[":
                    in_class = True
                elif source[i] == "]":
                    in_class = False
                elif source[i] == "/" and not in_class:
                    i += 1
                    break
                i += 1
            while i < len(source) and source[i].isalpha():
                i += 1  # regex flags are part of the opaque literal
            previous = "opaque"
            can_start_regex = False
            control_header = False
            yield ("opaque", "", at)
            continue
        match = _JS_WORD.match(source, i)
        if match:
            value = match.group()
            i += len(value)
            yield ("word", value, at)
            can_start_regex = previous not in (".", "?.") and value in _JS_REGEX_WORDS
            control_header = previous not in (".", "?.") and value in _JS_CONTROL_WORDS
            previous = value
        else:
            punct = next((p for p in ("=>", "++", "--", "?.") if source.startswith(p, i)), ch)
            i += len(punct)
            if punct == "(":
                parens.append(control_header)
                can_start_regex = True
            elif punct == ")":
                can_start_regex = parens.pop() if parens else False
            elif punct == "{":
                braces.append(previous in ("", ";", "{", "}", ")", "=>", "else", "do", "try", "finally"))
                can_start_regex = True
            elif punct == "}":
                can_start_regex = braces.pop() if braces else True
            elif punct in ("++", "--"):
                pass  # prefix expects a value; postfix has already completed one
            else:
                can_start_regex = punct == "[" or punct == "=>" or punct in ";,:?=+-*%&|^!~<>/"
            yield ("punct", punct, at)
            previous = punct
            control_header = False


def _js_imports(source: str):
    tokens = list(_tokens(source))
    for i, (kind, word, line) in enumerate(tokens):
        if kind != "word" or word not in ("import", "export", "require"):
            continue
        if i and tokens[i - 1][1] in (".", "?."):
            continue
        following = tokens[i + 1 : i + 121]
        if not following:
            continue
        if word in ("require", "import") and len(following) >= 3 and following[0][1] == "(" and following[1][0] == "string" and following[2][1] == ")":
            yield following[1][1], line
        elif word == "import" and following[0][0] == "string":
            yield following[0][1], line
        elif word in ("import", "export"):
            for j, token in enumerate(following[:-1]):
                if token[1] == ";" or (j and token[0] == "word" and token[1] in ("import", "export", "const", "let", "function")):
                    break
                if token[:2] == ("word", "from") and following[j + 1][0] == "string":
                    yield following[j + 1][1], line
                    break


def _js_target(project: Project, file: str, spec: str) -> str | None:
    if not spec or len(spec) > 500:
        return None
    if not spec.startswith("."):
        # No alias or package-manifest interpretation: bare imports remain inferred externals.
        return None if spec.startswith("/") else f"external:{spec}"
    path = posixpath.normpath(posixpath.join(posixpath.dirname(file), spec))
    if path == ".." or path.startswith("../"):
        return None
    candidates = [path]
    suffix = Path(path).suffix.lower()
    if suffix in _JS_EXTENSIONS:
        # Common TS source imports use the emitted .js extension.
        base = path[: -len(suffix)]
        candidates.extend(base + ext for ext in _JS_EXTENSIONS)
    elif not suffix:
        candidates.extend(path + ext for ext in _JS_EXTENSIONS)
    candidates.extend(f"{path}/index{ext}" for ext in _JS_EXTENSIONS)
    found = next((p for p in candidates if p in project.inventory.files), None)
    return f"file:{found}" if found else None


def extract_relationships(project: Project) -> list[Relationship]:
    relationships = []
    for file, item in project.inventory.files.items():
        module = project.modules.get(file)
        if module is not None and module.language == "python":
            resolver = project.resolvers.get("python")
            if resolver is None:
                continue
            imports = {(imp.module, imp.name, imp.line): imp for imp in module.imports}
            for fn in module.functions.values():
                imports.update(((imp.module, imp.name, imp.line), imp) for imp in fn.imports)
            source = read_text(project.root, file)
            lines = source.splitlines() if source is not None else []
            for imp in sorted(imports.values(), key=lambda imp: (imp.line, imp.module, imp.name or "")):
                if imp.module == "__future__":
                    continue
                ref = resolver.module_ref(file, imp.module)
                if imp.name and imp.name != "*":
                    spec = imp.module + ("" if imp.module.endswith(".") else ".") + imp.name
                    submodule = resolver.module_ref(file, spec)
                    if submodule is not None and submodule.kind in ("module", "namespace"):
                        ref = submodule
                if ref is None:
                    continue
                if ref.kind == "module" and ref.key in project.inventory.files:
                    target, confidence = f"file:{ref.key}", "verified"
                elif ref.kind == "namespace" and ref.key in project.inventory.directories:
                    target, confidence = f"dir:{ref.key}", "verified"
                elif ref.kind == "external" and not imp.module.startswith("."):
                    target, confidence = f"external:{imp.module}", "inferred"
                else:
                    continue
                text = lines[imp.line - 1].strip()[:300] if 0 < imp.line <= len(lines) else ""
                relationships.append(Relationship(file, target, confidence, {"file": file, "line": imp.line, "text": text}))
        elif item.language in ("javascript", "typescript"):
            source = read_text(project.root, file)
            if source is None:
                continue
            item.analysis = "imports"
            lines = source.splitlines()
            for spec, line in _js_imports(source):
                target = _js_target(project, file, spec)
                if target is not None:
                    relationships.append(Relationship(file, target, "inferred", {"file": file, "line": line, "text": lines[line - 1].strip()[:300]}))
    return relationships


def build_overview(project: Project, path: str = "", expanded: set[str] | frozenset[str] = frozenset()) -> Graph:
    """Draw direct children and folded dependencies, or an incident focused map."""
    inventory = project.inventory
    if path and (Path(path).is_absolute() or ".." in Path(path).parts or path not in inventory.files and path not in inventory.directories):
        raise ValueError(f"unknown project path: {path!r}")
    focus_file = path in inventory.files
    scope = {p for p in inventory.files if not path or p == path or not focus_file and p.startswith(path + "/")}
    graph = Graph("@project")
    nodes: dict[str, Node] = {}
    children = defaultdict(list)
    for directory in inventory.directories:
        children[posixpath.dirname(directory)].append((directory, True))
    for file in inventory.files:
        children[posixpath.dirname(file)].append((file, False))
    for entries in children.values():
        entries.sort(key=lambda entry: (not entry[1], entry[0]))
    truncated = False

    def add_file(file: str, parent: str | None = None) -> None:
        nonlocal truncated
        ident = f"file:{file}"
        if ident in nodes:
            return
        if len(nodes) >= MAX_VISIBLE_NODES:
            truncated = True
            return
        item = inventory.files[file]
        functions = sorted((fn for fn in project.functions.values() if fn.file == file and fn.name != "<main>"), key=lambda fn: (fn.line, fn.qualname))
        detail = {"path": file, "category": item.category, "language": item.language,
                  "size": item.size, "readable": item.readable, "analysis": item.analysis,
                  "functions": [{"id": fn.id, "label": display_name(project, fn), "line": fn.line} for fn in functions]}
        node = Node(ident, item.category, posixpath.basename(file), parent, detail)
        nodes[ident] = node
        graph.nodes.append(node)

    def add_folder(folder: str, parent: str | None = None, open_allowed: bool = True) -> None:
        nonlocal truncated
        ident = f"dir:{folder}"
        if ident in nodes:
            return
        if len(nodes) >= MAX_VISIBLE_NODES:
            truncated = True
            return
        is_open = open_allowed and ident in expanded
        detail = {"path": folder, "category": "folder", "files": sum(p.startswith(folder + "/") for p in inventory.files),
                  "container": is_open, "expanded": is_open, "expandable": bool(children[folder])}
        node = Node(ident, "folder", posixpath.basename(folder), parent, detail)
        nodes[ident] = node
        graph.nodes.append(node)
        if is_open:
            add_children(folder, ident)

    def add_children(folder: str, parent: str | None) -> None:
        for child, directory in children[folder]:
            if directory:
                add_folder(child, parent)
            else:
                add_file(child, parent)

    if focus_file:
        add_file(path)
    elif path and any(rel.target == f"dir:{path}" for rel in project.relationships):
        # A namespace import targets the area itself, never an arbitrary child
        # file. Give it a containing focus frame rather than a duplicate card.
        add_folder(path, open_allowed=False)
        focus_id = f"dir:{path}"
        nodes[focus_id].detail.update(container=True, expanded=True, expandable=False)
        add_children(path, focus_id)
    else:
        add_children(path, None)

    def incident(rel: Relationship) -> bool:
        target_path = rel.target.split(":", 1)[1]
        return not path or rel.source in scope or rel.target.startswith("file:") and target_path in scope or rel.target.startswith("dir:") and (target_path == path or target_path.startswith(path + "/"))

    relationships = [rel for rel in project.relationships if incident(rel)]

    def representative(ident: str, create: bool = False) -> str | None:
        nonlocal truncated
        if ident in nodes:
            return ident
        if ident.startswith("external:"):
            if not create:
                return None
            if len(nodes) >= MAX_VISIBLE_NODES:
                truncated = True
                return None
            name = ident.split(":", 1)[1]
            node = Node(ident, "external", name, detail={"category": "external", "analysis": "inferred", "readable": False})
            nodes[ident] = node
            graph.nodes.append(node)
            return ident
        prefix, target_path = ident.split(":", 1)
        directory = target_path if prefix == "dir" else posixpath.dirname(target_path)
        while directory:
            ancestor = f"dir:{directory}"
            if ancestor in nodes:
                return ancestor
            directory = posixpath.dirname(directory)
        if not create:
            return None
        # Incident context is folded into its top-level area, except nearby file siblings.
        if focus_file and posixpath.dirname(target_path) == posixpath.dirname(path) and prefix == "file":
            add_file(target_path)
            return ident if ident in nodes else None
        top = target_path.split("/", 1)[0]
        if "/" in target_path or prefix == "dir":
            add_folder(top, open_allowed=False)
            return f"dir:{top}" if f"dir:{top}" in nodes else None
        if prefix == "file":
            add_file(target_path)
            return ident if ident in nodes else None
        return None

    projected = {}
    for relationship in relationships:
        source = representative(f"file:{relationship.source}", create=relationship.source not in scope)
        target = representative(relationship.target, create=True)
        if source is None or target is None or source == target:
            continue
        key = (source, target)
        edge = projected.get(key)
        if edge is None:
            edge = Edge(source, target, kind="dependency", count=0, confidence=relationship.confidence, evidence=[])
            projected[key] = edge
        edge.count += 1
        if relationship.confidence == "inferred":
            edge.confidence = "inferred"
        if len(edge.evidence) < MAX_EVIDENCE and relationship.evidence not in edge.evidence:
            edge.evidence.append(relationship.evidence)
    graph.edges = list(projected.values())
    graph.overview = {"path": path, "files": len(scope), "total_files": len(inventory.files),
                      "parsed_files": sum(file in project.modules for file in scope),
                      "relationships": len(relationships), "languages": inventory.languages(scope), "truncated": truncated}
    return graph
