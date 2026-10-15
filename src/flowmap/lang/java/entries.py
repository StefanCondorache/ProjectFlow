"""Java launch entries: main methods, ranked by literal build and launch evidence.

Build files are read as bounded, contained text and never run. Only literal
class names count; property placeholders (``${main}``) and computed values
are ignored. Classes under test source folders are not automatic entries.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

from flowmap.discover import discover_files
from flowmap.entries import Command, Entry, Source
from flowmap.inventory import read_text
from flowmap.ir import Module

_CLASS = re.compile(r'[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*')
_POM = re.compile(r'<(mainClass|start-class)>\s*([^<]*?)\s*</\1>')
_POM_PLUGIN = re.compile(r'<plugin>.*?</plugin>', re.DOTALL)
_GRADLE = re.compile(r'''\bmainClass(?:Name)?\s*(?:=|\.set\()\s*["']([^"']*)["']''')
_GRADLE_MANIFEST = re.compile(r'''["']Main-Class["']\s*(?::|to)\s*["']([^"']*)["']''')
# Plugins whose main class becomes the jar manifest's Main-Class (what `java -jar` runs).
_JAR_PLUGINS = {'maven-jar-plugin', 'maven-shade-plugin', 'maven-assembly-plugin', 'spring-boot-maven-plugin'}
_XML_COMMENT = re.compile(r'<!--.*?-->', re.DOTALL)
_GRADLE_COMMENT = re.compile(r'/\*.*?\*/|//[^\n]*', re.DOTALL)
_VALUE_OPTIONS = {'-cp', '-classpath', '--class-path', '-p', '--module-path', '--add-modules'}
_ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=.*')


def _is_test(path: str) -> bool:
    """Test code: test/tests folders and test source sets (src/test, src/integrationTest, src/testFixtures)."""
    parts = path.split('/')
    if {'test', 'tests'} & set(parts):
        return True
    return any(part == 'src' and i + 1 < len(parts) - 1 and 'test' in parts[i + 1].lower()
               for i, part in enumerate(parts))


def _mains(modules: dict[str, Module]) -> tuple[dict[str, str], dict[str, str]]:
    """Main methods by declaring class FQN, and the first main per file."""
    by_class: dict[str, str] = {}
    by_file: dict[str, str] = {}
    for file, module in modules.items():
        if _is_test(file):
            continue
        meta = module.metadata.get('java', {})
        for fid, info in meta.get('functions', {}).items():
            fn = module.functions[fid]
            cls = meta['types'].get(info.get('cls') or '', {})
            params = [(t or '').removeprefix('java.lang.') for t in info['param_types']]
            if (fn.name == 'main' and info['kind'] == 'method' and info['static'] and fn.returns == 'void'
                    and params == ['String[]'] and cls and not cls['scope_fid'] and not cls['anonymous']):
                by_class.setdefault(cls['fqn'], fid)
                by_file.setdefault(file, fid)
    return by_class, by_file


def _build_mains(root: Path) -> list[tuple[str, str, bool]]:
    """(class name, source text, is the jar manifest main class) declared in build files.

    Comments are removed first; only literal class names count.
    """
    found = []
    for rel in discover_files(root, extensions=(), patterns=('pom.xml', 'build.gradle', 'build.gradle.kts')):
        text = read_text(root, rel)
        if text is None:
            continue
        if rel.endswith('pom.xml'):
            text = _XML_COMMENT.sub('', text)
            jar_spans = [m.span() for m in _POM_PLUGIN.finditer(text)
                         if any(f'<artifactId>{name}</artifactId>' in m.group(0) for name in _JAR_PLUGINS)]
            for match in _POM.finditer(text):
                jar = match.group(1) == 'start-class' or any(a <= match.start() < b for a, b in jar_spans)
                found.append((match.group(2), f'{rel}: {match.group(0).strip()}', jar))
        else:
            text = _GRADLE_COMMENT.sub('', text)
            found += [(m.group(1), f'{rel}: {m.group(0).strip()}', False) for m in _GRADLE.finditer(text)]
            found += [(m.group(1), f'{rel}: {m.group(0).strip()}', True) for m in _GRADLE_MANIFEST.finditer(text)]
    return [item for item in found if _CLASS.fullmatch(item[0])]


def _launch(command: Command, by_class: dict[str, str], by_file: dict[str, str],
            declared: list[tuple[str, str, bool]], modules: dict[str, Module]) -> str | None:
    args = list(command.argv)
    while args and (args[0] == 'exec' or posixpath.basename(args[0]) == 'env' or _ENV.fullmatch(args[0])):
        args.pop(0)
    if not args or posixpath.basename(args.pop(0)) != 'java':
        return None
    while args:
        token = args.pop(0)
        if token in _VALUE_OPTIONS:
            if args:
                args.pop(0)
            continue
        if token == '-jar':
            classes = {name for name, _, jar in declared if jar}
            return by_class.get(classes.pop()) if len(classes) == 1 else None
        if token in {'-m', '--module'}:
            return None
        if token.startswith('-'):
            continue
        if token.endswith('.java'):
            path = posixpath.normpath(posixpath.join(command.base_dir, token))
            return by_file.get(path) if not path.startswith('../') and path in modules else None
        name = token.replace('$', '.')  # binary names of nested classes
        return by_class.get(name) if _CLASS.fullmatch(name) else None
    return None


def java_entries(root: Path, modules: dict[str, Module], commands: list[Command]) -> list[Entry]:
    by_class, by_file = _mains(modules)
    if not by_class:
        return []
    declared = _build_mains(root)
    entries = []
    for command in commands:
        if command.ambiguous:
            continue
        target = _launch(command, by_class, by_file, declared, modules)
        if target:
            entries.append(Entry(target, f"java {_fqn(modules, target)}", command.kind, [Source(command.kind, command.where)]))
    for name, text, _ in declared:
        if name in by_class:
            entries.append(Entry(by_class[name], f'java {name}', 'build-main', [Source('build-main', text)]))
    for name, fid in by_class.items():
        entries.append(Entry(fid, f'java {name}', 'java-main', [Source('java-main', f"{fid.split('::')[0]}: main")]))
    return entries


def _fqn(modules: dict[str, Module], fid: str) -> str:
    file = fid.split('::')[0]
    meta = modules[file].metadata['java']
    return meta['types'][meta['functions'][fid]['cls']]['fqn']
