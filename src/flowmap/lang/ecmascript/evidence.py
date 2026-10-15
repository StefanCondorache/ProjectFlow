"""Conservative lexical evidence from execution subtrees kept opaque in IR."""
from .syntax import (
    CLASSES, DECLARATIONS, FUNCTIONS, dotted, named, pattern_names, text,
)


def collect_opaque(module, node, body):
    """Retain bindings/writes without treating the subtree's calls as executed.

    Function bodies use their own environments. A var in an omitted switch or
    label still belongs to its surrounding execution function; a var inside a
    callback does not. Unknown initializers never establish callable targets.
    """
    kind = node.type
    previous = body.scope
    if kind in {'statement_block', 'switch_body', 'switch_statement', 'with_statement',
                'labeled_statement', 'for_statement', 'for_in_statement', 'catch_clause'}:
        body.scope = module.new_scope('block', node, body.scope, body.fn.id)
    try:
        if kind in FUNCTIONS:
            fid = module.function(node, body, declaration=kind in DECLARATIONS)
            if kind in DECLARATIONS:
                for binding in module.meta['bindings']:
                    if binding['scope'] == body.scope and binding['target'] == fid:
                        binding.update(target=None, conditional=True, opaque=True)
            return
        if kind in CLASSES:
            cid = module.klass(node, body)
            for binding in module.meta['bindings']:
                if binding['scope'] == body.scope and binding['target'] == cid:
                    binding.update(target=None, conditional=True, opaque=True)
            return
        if kind in {'variable_declaration', 'lexical_declaration'}:
            declaration_kind = next((c.type for c in node.children if c.type in {'const', 'let', 'var'}), 'var')
            for declaration in named(node):
                if declaration.type != 'variable_declarator':
                    continue
                pattern, value = declaration.child_by_field_name('name'), declaration.child_by_field_name('value')
                module.check_pattern(pattern)
                for name in pattern_names(pattern):
                    module.binding(body.scope, name, declaration_kind, declaration,
                                   initializer=text(value), conditional=True, opaque=True)
                if value is not None and value.type in FUNCTIONS:
                    module.function(value, body, text(pattern) if pattern.type == 'identifier' else None)
                elif value is not None:
                    collect_opaque(module, value, body)
            return
        if kind == 'catch_clause':
            parameter = node.child_by_field_name('parameter')
            module.check_pattern(parameter)
            for name in pattern_names(parameter):
                module.binding(body.scope, name, 'catch', parameter, opaque=True)
        if kind == 'for_in_statement':
            left = node.child_by_field_name('left')
            module.check_pattern(left)
            declaration_kind = next((c.type for c in node.children if c.type in {'const', 'let', 'var'}), None)
            for name in pattern_names(left):
                if declaration_kind:
                    module.binding(body.scope, name, declaration_kind, left, conditional=True, opaque=True)
                else:
                    module.writes.append((body.scope, name, node))
            if not declaration_kind:
                module.write_targets(body.scope, left)
        if kind in {'assignment_expression', 'augmented_assignment_expression', 'update_expression'}:
            target = node.child_by_field_name('argument') if kind == 'update_expression' else node.child_by_field_name('left')
            module.check_pattern(target)
            for name in pattern_names(target):
                module.writes.append((body.scope, name, node))
            module.write_targets(body.scope, target)
            chain = dotted(target)
            computed_export = target is not None and target.type == 'subscript_expression' and dotted(target.child_by_field_name('object')) in {'exports', 'module.exports', 'module'}
            if chain == 'module.exports' or chain.startswith('exports.') or chain.startswith('module.exports.') or computed_export:
                module.commonjs.append(('export', body.scope, node, None))
        if kind == 'call_expression' and dotted(node.child_by_field_name('function')) == 'require':
            module.commonjs.append(('require-call', body.scope, node, None))
        for child in named(node):
            collect_opaque(module, child, body)
    finally:
        body.scope = previous
