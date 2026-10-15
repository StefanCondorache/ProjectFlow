"""Tree-sitter grammars and syntax utilities shared by ECMAScript adapters."""
from pathlib import PurePosixPath

from tree_sitter import Language, Node, Parser
import tree_sitter_javascript
import tree_sitter_typescript

_LANGUAGES = {
    'javascript': Language(tree_sitter_javascript.language()),
    'typescript': Language(tree_sitter_typescript.language_typescript()),
    'tsx': Language(tree_sitter_typescript.language_tsx()),
}
FUNCTIONS = {'function_declaration', 'generator_function_declaration', 'function_expression',
             'generator_function', 'arrow_function', 'method_definition'}
DECLARATIONS = {'function_declaration', 'generator_function_declaration'}
CLASSES = {'class_declaration', 'class'}


def parser_for(file: str) -> tuple[Parser, str]:
    suffix = PurePosixPath(file).suffix.lower()
    language = 'typescript' if suffix in {'.ts', '.tsx', '.mts', '.cts'} else 'javascript'
    return Parser(_LANGUAGES['tsx' if suffix == '.tsx' else language]), language


def text(node: Node | None) -> str:
    return node.text.decode('utf-8', 'replace') if node is not None else ''


def named(node: Node | None) -> list[Node]:
    return [child for child in node.named_children if child.type != 'comment'] if node is not None else []


def line(node: Node) -> int:
    return node.start_point[0] + 1


def col(node: Node) -> int:
    return node.start_point[1]


def short(value: str) -> str:
    value = ' '.join(value.split())
    return value if len(value) <= 100 else value[:99] + '…'


def unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def annotation(node: Node | None) -> str | None:
    return text(node).lstrip(':').strip() or None


def unparen(node: Node | None) -> Node | None:
    while node is not None and node.type == 'parenthesized_expression' and named(node):
        node = named(node)[0]
    return node


def dotted(node: Node | None) -> str:
    node = unparen(node)
    if node is None:
        return ''
    if node.type in {'identifier', 'this', 'super', 'import', 'type_identifier'}:
        return text(node)
    if node.type == 'member_expression':
        root = dotted(node.child_by_field_name('object'))
        prop = node.child_by_field_name('property')
        return f'{root}.{text(prop)}' if root and prop is not None and prop.type == 'property_identifier' else ''
    return ''


def pattern_names(node: Node | None) -> list[str]:
    if node is None:
        return []
    kind = node.type
    if kind in {'identifier', 'shorthand_property_identifier_pattern', 'shorthand_property_identifier'}:
        return [text(node)]
    if kind in {'pair_pattern', 'pair'}:
        return pattern_names(node.child_by_field_name('value'))
    if kind in {'assignment_pattern', 'object_assignment_pattern'}:
        return pattern_names(node.child_by_field_name('left'))
    if kind in {'required_parameter', 'optional_parameter'}:
        return pattern_names(node.child_by_field_name('pattern'))
    if kind in {'object_pattern', 'array_pattern', 'rest_pattern'}:
        return unique([name for child in named(node) for name in pattern_names(child)])
    return []


def member_targets(node: Node | None) -> list[Node]:
    """Member/subscript expressions assigned by a target, including inside patterns."""
    node = unparen(node)
    if node is None:
        return []
    kind = node.type
    if kind in {'member_expression', 'subscript_expression'}:
        return [node]
    if kind == 'pair_pattern':
        return member_targets(node.child_by_field_name('value'))
    if kind in {'assignment_pattern', 'object_assignment_pattern'}:
        return member_targets(node.child_by_field_name('left'))
    if kind in {'object_pattern', 'array_pattern', 'rest_pattern'}:
        return [member for child in named(node) for member in member_targets(child)]
    return []


def member_name(node: Node | None) -> str | None:
    """The written property of ``a.b``, ``a.#b``, ``a['b']`` or ``a[`b`]``;
    None for computed members."""
    if node is not None and node.type == 'subscript_expression':
        key = unparen(node.child_by_field_name('index'))
        if key is not None and key.type == 'template_string':
            raw = text(key)
            return raw[1:-1] if not any(c.type == 'template_substitution' for c in named(key)) and '\\' not in raw else None
        return literal_string(key)
    if node is None or node.type != 'member_expression':
        return None
    prop = node.child_by_field_name('property')
    return text(prop) if prop is not None and prop.type in {'property_identifier', 'private_property_identifier'} else None


def literal_string(node: Node | None) -> str | None:
    if node is None or node.type != 'string':
        return None
    raw = text(node)
    # Escaped specifiers need a dedicated JS string decoder before resolution.
    return raw[1:-1] if len(raw) >= 2 and '\\' not in raw else None


def descendants(node: Node):
    yield node
    for child in named(node):
        yield from descendants(child)
