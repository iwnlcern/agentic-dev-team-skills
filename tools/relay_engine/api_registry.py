"""Compile the immutable discovery graph and interpret its closed validation rules."""
import base64
import binascii
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType

from relay_engine import api_definitions as d
from relay_engine import errors

COMPILE_REASONS = frozenset({
    'duplicate_type', 'conflicting_type', 'dangling_ref', 'invalid_definition',
    'unsupported_shape', 'invalid_rule', 'duplicate_operation', 'unbound_handler',
    'missing_error', 'extra_error', 'duplicate_error', 'invalid_error', 'recursive_type',
})


class RegistryCompileError(ValueError):
    def __init__(self, reason: str):
        if reason not in COMPILE_REASONS:
            raise ValueError('invalid compiler reason')
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ErrorArm:
    code: str
    wire_class: str
    cause_type: d.TypeRef
    remedy: str


@dataclass(frozen=True, slots=True)
class FrozenApiRegistry:
    types: Mapping[str, d.TypeDef]
    operations: Mapping[str, d.OperationDef]
    handlers: Mapping[str, Callable]
    arms: tuple[ErrorArm, ...]

    def capabilities(self) -> tuple[str, ...]:
        return tuple(self.operations)

    def operation(self, name: str) -> d.OperationDef | None:
        return self.operations.get(name)

    def error_arms(self) -> tuple[ErrorArm, ...]:
        return self.arms


@dataclass(frozen=True, slots=True)
class ValidationFailure:
    rule: str
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidatedValue:
    value: object


@dataclass(frozen=True, slots=True)
class ValidatedResult:
    operation: str
    value: object


@dataclass(frozen=True, slots=True)
class DiscoveryValidationContext:
    kind: str
    negotiated_protocol: str
    negotiated_schema_digest: str


def _ref(value, types):
    if not isinstance(value, d.TypeRef) or value.name not in types:
        raise RegistryCompileError('dangling_ref')
    return types[value.name]


def _members(node):
    if not isinstance(node.shape, d.Record):
        raise RegistryCompileError('invalid_rule')
    return {f.name: f for f in node.shape.fields}


def _rule(rule, node, types, operation=None):
    if isinstance(rule, d.NegotiatedSchemaV1):
        expected = d.NegotiatedSchemaV1(d.TypeRef('SchemaResult'))
        if operation is None or operation.name != 'protocol.schema' or rule != expected or operation.result_ref != rule.result_ref:
            raise RegistryCompileError('invalid_rule')
        return
    if operation is not None:
        raise RegistryCompileError('invalid_rule')
    if isinstance(rule, d.SortedUnique):
        if rule != d.SortedUnique(d.TypeRef(node.name)) or not isinstance(node.shape, d.Array):
            raise RegistryCompileError('invalid_rule')
        members = _members(_ref(node.shape.item, types))
        if set(members) != {'operation'} or members['operation'].type_ref != d.TypeRef('OperationName'):
            raise RegistryCompileError('invalid_rule')
        return
    if isinstance(rule, d.Utf8MaxBytes):
        expected = d.Utf8MaxBytes(d.TypeRef('OpaqueId'), d.MAX_OPAQUE_ID_BYTES)
    elif isinstance(rule, d.CanonicalUInt64):
        expected = d.CanonicalUInt64(d.TypeRef(node.name))
        if node.name not in {'UInt64', 'RequestId'}:
            raise RegistryCompileError('invalid_rule')
    elif isinstance(rule, d.CanonicalBase64):
        expected = d.CanonicalBase64(d.TypeRef('CanonicalSchemaBase64'))
    elif isinstance(rule, d.ProtocolWindowV1):
        expected = d.ProtocolWindowV1(d.TypeRef('ProtocolWindow'))
        members = _members(node)
        if set(members) != {'min', 'max'} or any(f.optional or f.type_ref != d.TypeRef('ProtocolVersion') for f in members.values()):
            raise RegistryCompileError('invalid_rule')
    elif isinstance(rule, d.SchemaPayloadV1):
        expected = d.SchemaPayloadV1(d.TypeRef('SchemaResult'))
        members = _members(node)
        required = {'protocol': 'ProtocolVersion', 'schema_digest': 'Digest',
                    'schema_encoding': 'SchemaEncoding', 'schema_byte_count': 'UInt64',
                    'schema_bytes_base64': 'CanonicalSchemaBase64'}
        if set(members) != set(required) or any(members[k].optional or members[k].type_ref.name != v for k, v in required.items()):
            raise RegistryCompileError('invalid_rule')
        if _ref(members['schema_encoding'].type_ref, types).shape != d.LiteralSet(('base64',)):
            raise RegistryCompileError('invalid_rule')
    else:
        raise RegistryCompileError('invalid_rule')
    if rule != expected or rule.type_ref.name != node.name:
        raise RegistryCompileError('invalid_rule')
    if isinstance(rule, (d.Utf8MaxBytes, d.CanonicalUInt64, d.CanonicalBase64)) and not isinstance(node.shape, d.String):
        raise RegistryCompileError('invalid_rule')


def _check_type(node, types):
    if not isinstance(node, d.TypeDef) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,127}', node.name) or type(node.rules) is not tuple:
        raise RegistryCompileError('invalid_definition')
    shape = node.shape
    if isinstance(shape, d.Record):
        if type(shape.fields) is not tuple or any(not isinstance(f, d.FieldDef) for f in shape.fields) or len({f.name for f in shape.fields}) != len(shape.fields):
            raise RegistryCompileError('invalid_definition')
        for f in shape.fields:
            if not isinstance(f, d.FieldDef) or type(f.optional) is not bool or not re.fullmatch(r'[a-z][a-z0-9_]*', f.name):
                raise RegistryCompileError('invalid_definition')
            _ref(f.type_ref, types)
    elif isinstance(shape, d.TaggedUnion):
        if not shape.arms or type(shape.arms) is not tuple or shape.tag != 'kind':
            raise RegistryCompileError('unsupported_shape')
        tags = []
        for ref in shape.arms:
            leaf = _ref(ref, types)
            members = _members(leaf)
            if 'kind' not in members or members['kind'].optional:
                raise RegistryCompileError('unsupported_shape')
            literal = _ref(members['kind'].type_ref, types).shape
            if not isinstance(literal, d.LiteralSet) or len(literal.values) != 1:
                raise RegistryCompileError('unsupported_shape')
            tags.append(literal.values[0])
        if len(set(tags)) != len(tags):
            raise RegistryCompileError('unsupported_shape')
    elif isinstance(shape, d.LiteralSet):
        if type(shape.values) is not tuple or not shape.values or any(type(v) is not str or any(0xD800 <= ord(c) <= 0xDFFF for c in v) for v in shape.values) or len(set(shape.values)) != len(shape.values):
            raise RegistryCompileError('invalid_definition')
    elif isinstance(shape, d.String):
        if type(shape.pattern) is not str or type(shape.min_length) is not int or shape.min_length < 0 or (shape.max_length is not None and (type(shape.max_length) is not int or shape.max_length < shape.min_length)):
            raise RegistryCompileError('invalid_definition')
        try:
            re.compile(shape.pattern)
        except re.error as error:
            raise RegistryCompileError('unsupported_shape') from error
    elif isinstance(shape, d.Array):
        _ref(shape.item, types)
        if type(shape.max_items) is not int or not 0 <= shape.max_items <= d.MAX_CAPABILITIES:
            raise RegistryCompileError('invalid_definition')
    elif isinstance(shape, d.Nullable):
        _ref(shape.inner, types)
    else:
        raise RegistryCompileError('unsupported_shape')
    rule_types = (d.Utf8MaxBytes, d.CanonicalUInt64, d.CanonicalBase64, d.SortedUnique,
                  d.ProtocolWindowV1, d.SchemaPayloadV1, d.NegotiatedSchemaV1)
    if any(not isinstance(r, rule_types) for r in node.rules) or len({r.kind for r in node.rules}) != len(node.rules):
        raise RegistryCompileError('invalid_rule')
    for rule in node.rules:
        _rule(rule, node, types)


def compile_registry(definitions: d.DefinitionGraph, handlers: Mapping[str, Callable],
                     catalogues: Sequence[d.ClosedErrorDefinitions]) -> FrozenApiRegistry:
    types = {}
    for declarations in (definitions.types, *(catalog.types for catalog in catalogues)):
        seen = set()
        for node in declarations:
            if node.name in seen:
                raise RegistryCompileError('duplicate_type')
            seen.add(node.name)
            if node.name in types and types[node.name] != node:
                raise RegistryCompileError('conflicting_type')
            # A shared core node has one producer; catalogues include its transitive ref closure.
            types[node.name] = node
    for node in types.values():
        _check_type(node, types)
    # Refuse definition cycles in this bounded export profile.
    complete, active = set(), set()
    def visit(name):
        if name in active:
            raise RegistryCompileError('recursive_type')
        if name in complete:
            return
        active.add(name)
        shape = types[name].shape
        refs = ()
        if isinstance(shape, d.Record):
            refs = tuple(f.type_ref for f in shape.fields)
        elif isinstance(shape, d.TaggedUnion):
            refs = shape.arms
        elif isinstance(shape, d.Array):
            refs = (shape.item,)
        elif isinstance(shape, d.Nullable):
            refs = (shape.inner,)
        for ref in refs:
            visit(ref.name)
        active.remove(name)
        complete.add(name)
    for name in types:
        visit(name)
    variants = {}
    for catalog in catalogues:
        for variant in catalog.variants:
            if variant.code in variants:
                raise RegistryCompileError('duplicate_error')
            variants[variant.code] = variant
    # Compare against the owner producer, never a second hand-maintained code/class table.
    authoritative = {v.code: v for v in errors.api_error_catalog().variants}
    if set(variants) - set(authoritative):
        raise RegistryCompileError('extra_error')
    if set(authoritative) - set(variants):
        raise RegistryCompileError('missing_error')
    arms = []
    for code, variant in sorted(variants.items()):
        expected = authoritative[code]
        if variant.api_class != expected.api_class:
            raise RegistryCompileError('invalid_error')
        cause = _ref(variant.cause_ref, types).shape
        if not isinstance(cause, d.TaggedUnion) or cause.tag != 'kind':
            raise RegistryCompileError('invalid_error')
        leaves = {}
        for ref in cause.arms:
            leaf = _ref(ref, types)
            members = _members(leaf)
            kind_shape = _ref(members['kind'].type_ref, types).shape
            if not isinstance(kind_shape, d.LiteralSet) or len(kind_shape.values) != 1:
                raise RegistryCompileError('invalid_error')
            leaves[kind_shape.values[0]] = ref
        branch_map = {b.cause_kind: b.remedy for b in variant.branches}
        owner_pairs = {b.cause_kind: b.remedy for b in expected.branches}
        if len(branch_map) != len(variant.branches) or set(branch_map) != set(leaves) or branch_map != owner_pairs:
            raise RegistryCompileError('invalid_error')
        for kind, remedy in sorted(branch_map.items()):
            arms.append(ErrorArm(code, variant.api_class, leaves[kind], remedy))
    error_refs = []
    for i, arm in enumerate(arms):
        prefix = '__ApiError' + str(i)
        members = []
        for field, value in (('code', arm.code), ('class', arm.wire_class), ('remedy', arm.remedy)):
            name = prefix + field.title()
            types[name] = d.TypeDef(name, d.LiteralSet((value,)))
            members.append(d.FieldDef(field, d.TypeRef(name)))
        members.insert(2, d.FieldDef('cause', arm.cause_type))
        types[prefix] = d.TypeDef(prefix, d.Record(tuple(members)))
        error_refs.append(d.TypeRef(prefix))
    if definitions.error_ref != d.TypeRef('ApiError') or 'ApiError' in types:
        raise RegistryCompileError('invalid_error')
    types['ApiError'] = d.TypeDef('ApiError', d.TaggedUnion('code', tuple(error_refs)))
    operations = {}
    for op in definitions.operations:
        if op.name in operations:
            raise RegistryCompileError('duplicate_operation')
        if not re.fullmatch(r'[a-z][a-z0-9.\-]{0,95}', op.name) or op.principal != 'unsigned' or op.effect not in {'connection_metadata', 'pure_observation'} or op.retry != 'read_repeat' or op.browser_get not in {'connect', 'schema'}:
            raise RegistryCompileError('invalid_definition')
        for ref in (op.request_ref, op.result_ref, op.error_ref):
            _ref(ref, types)
        if op.name not in handlers or not callable(handlers[op.name]):
            raise RegistryCompileError('unbound_handler')
        for rule in op.rules:
            _rule(rule, None, types, op)
        operations[op.name] = op
    if set(handlers) != set(operations):
        raise RegistryCompileError('unbound_handler')
    return FrozenApiRegistry(MappingProxyType(dict(sorted(types.items()))),
                             MappingProxyType(dict(sorted(operations.items()))),
                             MappingProxyType(dict(handlers)), tuple(arms))


def _validate(registry, ref, value, path, decoded, depth=0):
    if depth > 64 or ref.name not in registry.types:
        return ValidationFailure('depth_or_ref', path)
    node = registry.types[ref.name]
    shape = node.shape
    if isinstance(shape, d.String):
        if type(value) is not str or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            return ValidationFailure('string', path)
        if len(value) < shape.min_length or (shape.max_length is not None and len(value) > shape.max_length):
            return ValidationFailure('length', path)
        if not any(isinstance(r, d.CanonicalBase64) for r in node.rules) and re.fullmatch(shape.pattern, value) is None:
            return ValidationFailure('pattern', path)
    elif isinstance(shape, d.LiteralSet):
        if type(value) is not str or value not in shape.values:
            return ValidationFailure('literal', path)
    elif isinstance(shape, d.Record):
        if type(value) is not dict or any(type(k) is not str for k in value):
            return ValidationFailure('record', path)
        names = {f.name for f in shape.fields}
        if set(value) - names:
            return ValidationFailure('unknown_member', path)
        for field in shape.fields:
            if field.name not in value:
                if field.optional:
                    continue
                return ValidationFailure('missing_member', path + (field.name,))
            failure = _validate(registry, field.type_ref, value[field.name], path + (field.name,), decoded, depth + 1)
            if failure:
                return failure
    elif isinstance(shape, d.Array):
        if type(value) is not list or len(value) > shape.max_items:
            return ValidationFailure('array', path)
        for index, item in enumerate(value):
            failure = _validate(registry, shape.item, item, path + (str(index),), decoded, depth + 1)
            if failure:
                return failure
    elif isinstance(shape, d.Nullable):
        if value is not None:
            failure = _validate(registry, shape.inner, value, path, decoded, depth + 1)
            if failure:
                return failure
    elif isinstance(shape, d.TaggedUnion):
        matches = 0
        for arm in shape.arms:
            if _validate(registry, arm, value, path, decoded, depth + 1) is None:
                matches += 1
        if matches != 1:
            return ValidationFailure('union', path)
    else:
        return ValidationFailure('unsupported_shape', path)
    for rule in node.rules:
        if isinstance(rule, d.Utf8MaxBytes):
            if len(value.encode('utf-8')) > rule.limit:
                return ValidationFailure(rule.kind, path)
        elif isinstance(rule, d.CanonicalUInt64):
            if len(value) > 20 or (len(value) == 20 and value > '18446744073709551615'):
                return ValidationFailure(rule.kind, path)
        elif isinstance(rule, d.CanonicalBase64):
            if value not in decoded:
                try:
                    raw = base64.b64decode(value, validate=True)
                except (ValueError, binascii.Error):
                    return ValidationFailure(rule.kind, path)
                if len(raw) > d.MAX_SCHEMA_BYTES or base64.b64encode(raw).decode('ascii') != value:
                    return ValidationFailure(rule.kind, path)
                decoded[value] = raw
        elif isinstance(rule, d.SortedUnique):
            names = [item['operation'] for item in value]
            if any(a >= b for a, b in zip(names, names[1:])):
                return ValidationFailure(rule.kind, path)
        elif isinstance(rule, d.ProtocolWindowV1):
            low = tuple(int(part) for part in value['min'].split('.'))
            high = tuple(int(part) for part in value['max'].split('.'))
            if low[0] != high[0] or low > high:
                return ValidationFailure(rule.kind, path)
        elif isinstance(rule, d.SchemaPayloadV1):
            raw = decoded[value['schema_bytes_base64']]
            if len(raw) != int(value['schema_byte_count']) or hashlib.sha256(raw).hexdigest() != value['schema_digest']:
                return ValidationFailure(rule.kind, path)
    return None


def validate_type(registry, type_ref: d.TypeRef, value: object) -> ValidatedValue | ValidationFailure:
    if not isinstance(type_ref, d.TypeRef):
        return ValidationFailure('type_ref', ())
    failure = _validate(registry, type_ref, value, (), {})
    return failure if failure else ValidatedValue(value)


def validate_operation_result(registry, operation: str, value: object,
                              context: DiscoveryValidationContext | None) -> ValidatedResult | ValidationFailure:
    op = registry.operation(operation)
    if op is None:
        return ValidationFailure('operation', ())
    checked = validate_type(registry, op.result_ref, value)
    if isinstance(checked, ValidationFailure):
        return checked
    for rule in op.rules:
        if isinstance(rule, d.NegotiatedSchemaV1):
            if not isinstance(context, DiscoveryValidationContext) or context.kind != 'schema_reply' or context.negotiated_protocol != value['protocol'] or context.negotiated_schema_digest != value['schema_digest']:
                return ValidationFailure(rule.kind, ())
    return ValidatedResult(operation, value)
