"""Tree-sitter Java grammar and syntax utilities."""
from __future__ import annotations

import re

import tree_sitter_java
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_java.language())
TYPES = {'class_declaration', 'interface_declaration', 'enum_declaration', 'record_declaration',
         'annotation_type_declaration'}
TYPE_KINDS = {'class_declaration': 'class', 'interface_declaration': 'interface',
              'enum_declaration': 'enum', 'record_declaration': 'record',
              'annotation_type_declaration': 'interface'}
COMMENTS = {'block_comment', 'line_comment'}
PRIMITIVES = {'integral_type', 'floating_point_type', 'boolean_type', 'void_type'}


def parser() -> Parser:
    return Parser(_LANGUAGE)


def text(node: Node | None) -> str:
    return node.text.decode('utf-8', 'replace') if node is not None else ''


def named(node: Node | None) -> list[Node]:
    return [child for child in node.named_children if child.type not in COMMENTS] if node is not None else []


def line(node: Node) -> int:
    return node.start_point[0] + 1


def col(node: Node) -> int:
    return node.start_point[1]


def short(value: str) -> str:
    value = ' '.join(value.split())
    return value if len(value) <= 100 else value[:99] + '…'


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def unparen(node: Node | None) -> Node | None:
    while node is not None and node.type == 'parenthesized_expression' and named(node):
        node = named(node)[0]
    return node


def modifiers(node: Node) -> list[str]:
    """Keyword modifiers (public, static, final...) of a declaration; annotations excluded."""
    found = next((c for c in node.children if c.type == 'modifiers'), None)
    return [c.type for c in found.children if not c.is_named] if found is not None else []


def erase(node: Node | None) -> str | None:
    """Declared type text without generic arguments: List<String> -> List, int[] stays int[]."""
    if node is None:
        return None
    kind = node.type
    if kind in PRIMITIVES or kind == 'type_identifier':
        return text(node)
    if kind == 'scoped_type_identifier':
        return '.'.join(erase(c) or '' for c in named(node) if c.type in {'type_identifier', 'scoped_type_identifier'})
    if kind == 'generic_type':
        base = next((c for c in named(node) if c.type != 'type_arguments'), None)
        return erase(base)
    if kind == 'array_type':
        dims = node.child_by_field_name('dimensions')
        return (erase(node.child_by_field_name('element')) or '') + '[]' * text(dims).count('[')
    if kind == 'annotated_type':
        return erase(next((c for c in named(node) if c.type not in {'marker_annotation', 'annotation'}), None))
    return re.sub(r'<.*>', '', text(node)).strip() or None


def descendants(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))
