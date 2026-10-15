"""Tree-sitter Rust grammar and syntax utilities."""
from __future__ import annotations

import tree_sitter_rust
from tree_sitter import Language, Node, Parser

_LANGUAGE = Language(tree_sitter_rust.language())
COMMENTS = {'line_comment', 'block_comment'}
TYPE_ITEMS = {'struct_item': 'struct', 'enum_item': 'enum', 'union_item': 'union', 'trait_item': 'trait'}


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


def descendants(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def erase(node: Node | None) -> str | None:
    """Type text without generic arguments: Option<&str> -> Option, &mut Vec<T> -> &mut Vec."""
    if node is None:
        return None
    kind = node.type
    if kind in {'type_identifier', 'primitive_type'}:
        return text(node)
    if kind == 'scoped_type_identifier':
        path = node.child_by_field_name('path')
        return f"{text(path)}::{text(node.child_by_field_name('name'))}" if path is not None else text(node.child_by_field_name('name'))
    if kind == 'generic_type':
        return erase(node.child_by_field_name('type'))
    if kind == 'reference_type':
        mutable = any(c.type == 'mutable_specifier' for c in node.children)
        return ('&mut ' if mutable else '&') + (erase(node.child_by_field_name('type')) or '')
    if kind in {'dynamic_type', 'abstract_type'}:
        return ('dyn ' if kind == 'dynamic_type' else 'impl ') + (erase(node.child_by_field_name('trait')) or text(node))
    return ' '.join(text(node).split()) or None


def deref(type_text: str | None) -> str | None:
    """The referenced type of &T / &mut T (auto-deref for method calls)."""
    while type_text and type_text.startswith('&'):
        type_text = type_text[1:].removeprefix('mut ').strip()
    return type_text or None


def path_text(node: Node | None) -> str:
    """crate::a::b for identifier/scoped_identifier/self/super/crate paths; '' otherwise."""
    if node is None:
        return ''
    if node.type in {'identifier', 'self', 'super', 'crate', 'type_identifier', 'metavariable'}:
        return text(node)
    if node.type in {'scoped_identifier', 'scoped_type_identifier'}:
        path = node.child_by_field_name('path')
        name = text(node.child_by_field_name('name'))
        if path is None:
            return f'::{name}'
        head = path_text(path)
        return f'{head}::{name}' if head else ''
    if node.type == 'generic_type':
        return path_text(node.child_by_field_name('type'))
    return ''


def doc_comment(node: Node) -> str | None:
    """Text of the /// (or /** */) outer doc comments immediately above an item."""
    parts = []
    previous = node.prev_sibling
    while previous is not None and previous.type in {'line_comment', 'block_comment', 'attribute_item'}:
        if previous.type == 'attribute_item':
            previous = previous.prev_sibling
            continue
        doc = next((c for c in previous.children if c.type == 'doc_comment'), None)
        if doc is None or not any(c.type == 'outer_doc_comment_marker' for c in previous.children):
            break
        parts.append(text(doc).strip())
        previous = previous.prev_sibling
    value = '\n'.join(reversed(parts)).strip()
    return value.split('\n\n')[0].strip() or None


def attributes(node: Node) -> list[Node]:
    """Attribute nodes (#[...]) directly above an item."""
    found = []
    previous = node.prev_sibling
    while previous is not None and previous.type in {'attribute_item', 'line_comment', 'block_comment'}:
        if previous.type == 'attribute_item':
            found += [c for c in named(previous) if c.type == 'attribute']
        previous = previous.prev_sibling
    return found
