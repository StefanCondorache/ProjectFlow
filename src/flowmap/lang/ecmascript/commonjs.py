"""Ordered CommonJS export evidence, including replacements and unknown writes."""
from .syntax import CLASSES, FUNCTIONS, dotted, literal_string, named, text


def export_event(module, scope, node, state):
    # Updates carry an argument and no RHS; their resulting value stays unknown.
    left = node.child_by_field_name('argument' if node.type == 'update_expression' else 'left')
    right = node.child_by_field_name('right')
    chain = dotted(left)
    computed = left is not None and left.type == 'subscript_expression'
    if computed:
        chain = dotted(left.child_by_field_name('object'))
        if chain == 'module' and literal_string(left.child_by_field_name('index')) == 'exports':
            chain, computed = 'module.exports', False
    root = chain.split('.')[0]
    if module.lookup(scope, root):
        return
    replace = chain == 'module.exports' and not computed
    conditional = scope != module.scope
    name = None if replace or computed else chain.removeprefix('module.exports.').removeprefix('exports.')
    event = {'kind': 'replace' if replace else 'set', 'scope': scope,
             'start_byte': node.start_byte, 'conditional': conditional,
             'name': name, 'source': text(right), 'unknown_export': False}
    module.meta['commonjs_events'].append(event)
    exports = module.meta['exports']
    if conditional:
        # A deferred/conditional event may occur after source-later writes.
        state['uncertain'] = True
    if (chain == 'exports' or chain.startswith('exports.')) and state['detached']:
        event['detached'] = True
        return
    if replace:
        exports[:] = [e for e in exports if not e.get('commonjs')]
        state['detached'] = True
        entries = replacement_entries(module, scope, right) if right is not None else None
        state['unknown'] = entries is None
        if entries is None or state['uncertain']:
            event['unknown_export'] = True
            emit(module, '*', scope, node, unknown_export=True)
            return
        event['names'] = [entry['name'] for entry in entries]
        for entry in entries:
            emit(module, entry.pop('name'), scope, node, **entry)
        return
    if computed or state['uncertain'] or name is None or '.' in name:
        exports[:] = [e for e in exports if not e.get('commonjs')]
        event['unknown_export'] = True
        state['unknown'] = True
        emit(module, '*', scope, node, unknown_export=True)
        return
    exports[:] = [e for e in exports if not (e.get('commonjs') and e['name'] == name)]
    target = (module.function_nodes.get(right.id) or module.class_nodes.get(right.id)) if right is not None else None
    local = text(right) if right is not None and right.type == 'identifier' else None
    unknown = state['unknown'] or (local is None and target is None)
    event['unknown_export'] = unknown
    emit(module, name, scope, node, local=None if unknown else local,
         target=None if unknown else target, unknown_export=unknown)


def emit(module, name, scope, node, **extras):
    module.export(name, scope, commonjs=True, start_byte=node.start_byte, **extras)


def replacement_entries(module, scope, node):
    if node.type == 'identifier':
        # Unknown replacement values do not establish an exported callable.
        return [{'name': 'default', 'local': text(node)}] if module.lookup(scope, text(node)) else None
    if node.type in FUNCTIONS | CLASSES:
        target = module.function_nodes.get(node.id) or module.class_nodes.get(node.id)
        return [{'name': 'default', 'target': target}] if target else None
    if node.type != 'object':
        return None
    entries = []
    for child in named(node):
        if child.type == 'shorthand_property_identifier':
            entries.append({'name': text(child), 'local': text(child)})
        elif child.type == 'pair':
            key, value = child.child_by_field_name('key'), child.child_by_field_name('value')
            if key.type not in {'property_identifier', 'string', 'number'}:
                return None
            name = literal_string(key) if key.type == 'string' else text(key)
            if name is None:
                return None
            if value.type == 'identifier':
                entries.append({'name': name, 'local': text(value)})
            elif value.type in FUNCTIONS | CLASSES:
                target = module.function_nodes.get(value.id) or module.class_nodes.get(value.id)
                if target is None:
                    return None
                entries.append({'name': name, 'target': target})
            else:
                return None
        else:
            # Spreads, computed entries and unknown values may replace keys.
            return None
    return entries
