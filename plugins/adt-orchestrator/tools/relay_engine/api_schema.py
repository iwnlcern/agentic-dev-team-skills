"""Deterministic, local-only JSON Schema projection of a frozen API registry."""
import base64
from dataclasses import asdict, dataclass
import hashlib

from relay_engine import api_definitions as d, api_wire, jcs


@dataclass(frozen=True, slots=True)
class SchemaArtifact:
    protocol: str
    canonical_bytes: bytes
    digest: str
    byte_count: int


class SchemaExportError(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _ref(ref):
    return {'$ref': '#/$defs/' + ref.name}


def _record(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': False}


def _shape(shape):
    if isinstance(shape, d.Record):
        return _record({f.name: _ref(f.type_ref) for f in shape.fields},
                       [f.name for f in shape.fields if not f.optional])
    if isinstance(shape, d.TaggedUnion):
        return {'oneOf': [_ref(ref) for ref in shape.arms]}
    if isinstance(shape, d.LiteralSet):
        return {'type': 'string', 'enum': list(shape.values)}
    if isinstance(shape, d.String):
        # The explicit end lookahead preserves full-match semantics across regex engines.
        result = {'type': 'string', 'pattern': '^(?:' + shape.pattern + ')$(?![\\s\\S])',
                  'minLength': shape.min_length}
        if shape.max_length is not None:
            result['maxLength'] = shape.max_length
        return result
    if isinstance(shape, d.Array):
        return {'type': 'array', 'items': _ref(shape.item), 'maxItems': shape.max_items}
    if isinstance(shape, d.Nullable):
        return {'oneOf': [{'type': 'null'}, _ref(shape.inner)]}
    raise SchemaExportError('unsupported_shape')


def _descriptor(rule):
    result = asdict(rule)
    for key in ('type_ref', 'array_ref', 'result_ref'):
        if key in result:
            result[key] = result[key]['name']
    return {'kind': rule.kind, **result}


def _profile(value):
    """JCS is used only within the design's exact, bounded integer profile."""
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -d.MAX_SAFE_JSON_INTEGER <= value <= d.MAX_SAFE_JSON_INTEGER:
            raise SchemaExportError('unrepresentable_profile')
        return
    if type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise SchemaExportError('unrepresentable_profile')
        return
    if type(value) is list:
        for item in value:
            _profile(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise SchemaExportError('unrepresentable_profile')
            _profile(key)
            _profile(item)
        return
    raise SchemaExportError('unrepresentable_profile')


def export_schema(registry, protocol: str) -> SchemaArtifact:
    if protocol != d.PROTOCOL:
        raise SchemaExportError('unsupported_protocol')
    definitions = {name: _shape(node.shape) for name, node in registry.types.items()}
    rules = [_descriptor(rule) for node in registry.types.values() for rule in node.rules]
    request_refs, success_refs, operations = [], [], []
    def add(name, schema):
        if name in definitions:
            raise SchemaExportError('definition_collision')
        definitions[name] = schema
        return _ref(d.TypeRef(name))
    for name, op in sorted(registry.operations.items()):
        suffix = name.replace('.', '_').replace('-', '_')
        request_refs.append(add('Request_' + suffix, _record({
            'request_id': _ref(d.TypeRef('RequestId')), 'op': {'const': name},
            'args': _ref(op.request_ref)}, ['request_id', 'op', 'args'])))
        success_refs.append(add('Success_' + suffix, _record({
            'request_id': _ref(d.TypeRef('RequestId')), 'result': _ref(op.result_ref)},
            ['request_id', 'result'])))
        operations.append({'name': op.name, 'request_ref': op.request_ref.name,
                           'result_ref': op.result_ref.name, 'error_ref': op.error_ref.name,
                           'principal': op.principal, 'effect': op.effect,
                           'retry': op.retry, 'browser_get': op.browser_get})
        rules.extend(_descriptor(rule) for rule in op.rules)
    rules.sort(key=lambda rule: (rule.get('type_ref', rule.get('array_ref', rule.get('operation'))), rule['kind']))
    add('ApiRequest', {'oneOf': request_refs})
    failure = add('ApiFailure', _record({
        'request_id': {'oneOf': [_ref(d.TypeRef('RequestId')), {'type': 'null'}]},
        'error': _ref(d.TypeRef('ApiError'))}, ['request_id', 'error']))
    add('ApiResponse', {'oneOf': success_refs + [failure]})
    doc = {'$schema': 'https://json-schema.org/draft/2020-12/schema',
           '$id': d.SCHEMA_ID, '$defs': definitions,
           'oneOf': [_ref(d.TypeRef('ApiRequest')), _ref(d.TypeRef('ApiResponse'))],
           'x-adt': {'format': d.SCHEMA_FORMAT, 'protocol': protocol,
                     'operations': operations, 'rules': rules}}
    _profile(doc)
    def check_refs(value):
        if type(value) is dict:
            if '$ref' in value:
                ref = value['$ref']
                if not ref.startswith('#/$defs/') or ref[8:] not in definitions:
                    raise SchemaExportError('dangling_ref')
            for item in value.values():
                check_refs(item)
        elif type(value) is list:
            for item in value:
                check_refs(item)
    check_refs(doc)
    try:
        raw = jcs.jcs_encode(doc)
    except (TypeError, ValueError) as error:
        raise SchemaExportError('unrepresentable_profile') from error
    if len(raw) > d.MAX_SCHEMA_BYTES:
        raise SchemaExportError('schema_too_large')
    digest = hashlib.sha256(raw).hexdigest()
    reply = {'request_id': str(d.MAX_UINT64), 'result': {
        'protocol': protocol, 'schema_digest': digest, 'schema_encoding': 'base64',
        'schema_byte_count': str(len(raw)), 'schema_bytes_base64': base64.b64encode(raw).decode('ascii')}}
    if len(jcs.jcs_encode(reply)) > api_wire.MAX_API_FRAME:
        raise SchemaExportError('schema_reply_too_large')
    return SchemaArtifact(protocol, raw, digest, len(raw))
