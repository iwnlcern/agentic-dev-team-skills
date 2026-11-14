"""Dependency-neutral immutable definitions for the unsigned discovery API."""
from dataclasses import dataclass
from typing import ClassVar

PROTOCOL = "3.0"
ENGINE_VERSION = "3.0.0"
SCHEMA_ID = "urn:adt:api:3.0"
SCHEMA_FORMAT = "adt-api-v1"
MAX_SCHEMA_BYTES = 8388608
MAX_SCHEMA_BASE64_CHARS = 11184812
MAX_CAPABILITIES = 4096
MAX_OPAQUE_ID_BYTES = 256
MAX_JSON_DEPTH = 64
MAX_SAFE_JSON_INTEGER = 2 ** 53 - 1
MAX_UINT64 = 2 ** 64 - 1


@dataclass(frozen=True, slots=True)
class TypeRef:
    name: str


@dataclass(frozen=True, slots=True)
class FieldDef:
    name: str
    type_ref: TypeRef
    optional: bool = False


@dataclass(frozen=True, slots=True)
class Record:
    fields: tuple[FieldDef, ...]


@dataclass(frozen=True, slots=True)
class TaggedUnion:
    tag: str
    arms: tuple[TypeRef, ...]


@dataclass(frozen=True, slots=True)
class LiteralSet:
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class String:
    pattern: str
    min_length: int = 0
    max_length: int | None = None


@dataclass(frozen=True, slots=True)
class Array:
    item: TypeRef
    max_items: int


@dataclass(frozen=True, slots=True)
class Nullable:
    inner: TypeRef


Shape = Record | TaggedUnion | LiteralSet | String | Array | Nullable


@dataclass(frozen=True, slots=True)
class Utf8MaxBytes:
    type_ref: TypeRef
    limit: int
    kind: ClassVar[str] = "utf8_max_bytes"


@dataclass(frozen=True, slots=True)
class CanonicalUInt64:
    type_ref: TypeRef
    kind: ClassVar[str] = "canonical_uint64"


@dataclass(frozen=True, slots=True)
class CanonicalBase64:
    type_ref: TypeRef
    kind: ClassVar[str] = "canonical_base64"


@dataclass(frozen=True, slots=True)
class SortedUnique:
    array_ref: TypeRef
    key: str = "operation"
    kind: ClassVar[str] = "sorted_unique"


@dataclass(frozen=True, slots=True)
class ProtocolWindowV1:
    type_ref: TypeRef
    min_field: str = "min"
    max_field: str = "max"
    relation: str = "same_major_and_ordered"
    kind: ClassVar[str] = "protocol_window_v1"


@dataclass(frozen=True, slots=True)
class SchemaPayloadV1:
    type_ref: TypeRef
    encoding_field: str = "schema_encoding"
    bytes_field: str = "schema_bytes_base64"
    count_field: str = "schema_byte_count"
    digest_field: str = "schema_digest"
    encoding: str = "base64"
    digest_algorithm: str = "sha256"
    max_decoded_bytes: int = MAX_SCHEMA_BYTES
    kind: ClassVar[str] = "schema_payload_v1"


@dataclass(frozen=True, slots=True)
class NegotiatedSchemaV1:
    result_ref: TypeRef
    operation: str = "protocol.schema"
    protocol_field: str = "protocol"
    digest_field: str = "schema_digest"
    context_kind: str = "schema_reply"
    protocol_slot: str = "negotiated_protocol"
    digest_slot: str = "negotiated_schema_digest"
    kind: ClassVar[str] = "negotiated_schema_v1"


RuleDescriptor = (Utf8MaxBytes | CanonicalUInt64 | CanonicalBase64 | SortedUnique
                  | ProtocolWindowV1 | SchemaPayloadV1 | NegotiatedSchemaV1)


@dataclass(frozen=True, slots=True)
class TypeDef:
    name: str
    shape: Shape
    rules: tuple[RuleDescriptor, ...] = ()


@dataclass(frozen=True, slots=True)
class OperationDef:
    name: str
    request_ref: TypeRef
    result_ref: TypeRef
    error_ref: TypeRef
    principal: str
    effect: str
    retry: str
    browser_get: str
    rules: tuple[RuleDescriptor, ...] = ()


@dataclass(frozen=True, slots=True)
class ErrorBranchDef:
    cause_kind: str
    remedy: str


@dataclass(frozen=True, slots=True)
class ErrorVariantDef:
    code: str
    api_class: str
    cause_ref: TypeRef
    branches: tuple[ErrorBranchDef, ...]


@dataclass(frozen=True, slots=True)
class ClosedErrorDefinitions:
    types: tuple[TypeDef, ...]
    variants: tuple[ErrorVariantDef, ...]


@dataclass(frozen=True, slots=True)
class DefinitionGraph:
    types: tuple[TypeDef, ...]
    operations: tuple[OperationDef, ...]
    error_ref: TypeRef


def discovery_definitions() -> DefinitionGraph:
    """Return the complete core type graph, with its central catalog reference."""
    component = (r"(?:0|[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
                 r"65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5])")
    types = (
        TypeDef("UInt64", String(r"(?:0|[1-9][0-9]{0,19})", 1, 20),
                (CanonicalUInt64(TypeRef("UInt64")),)),
        TypeDef("RequestId", String(r"[1-9][0-9]{0,19}", 1, 20),
                (CanonicalUInt64(TypeRef("RequestId")),)),
        TypeDef("OpaqueId", String(r"[^\x00]+", 1, 256),
                (Utf8MaxBytes(TypeRef("OpaqueId"), MAX_OPAQUE_ID_BYTES),)),
        TypeDef("Digest", String(r"[0-9a-f]{64}", 64, 64)),
        TypeDef("ProtocolVersion", String(component + r"\." + component, 3, 11)),
        TypeDef("EngineVersion", String(component + r"\." + component + r"\." + component, 5, 17)),
        TypeDef("OperationName", String(r"[a-z][a-z0-9.\-]{0,95}", 1, 96)),
        TypeDef("CanonicalSchemaBase64", String(
            r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?",
            0, MAX_SCHEMA_BASE64_CHARS), (CanonicalBase64(TypeRef("CanonicalSchemaBase64")),)),
        TypeDef("ProtocolWindow", Record((
            FieldDef("min", TypeRef("ProtocolVersion")),
            FieldDef("max", TypeRef("ProtocolVersion")),
        )), (ProtocolWindowV1(TypeRef("ProtocolWindow")),)),
        TypeDef("Capability", Record((FieldDef("operation", TypeRef("OperationName")),))),
        TypeDef("Capabilities", Array(TypeRef("Capability"), MAX_CAPABILITIES),
                (SortedUnique(TypeRef("Capabilities")),)),
        TypeDef("AcceptedOlderKind", LiteralSet(("accepted_older",))),
        TypeDef("UpdateClientRemedy", LiteralSet(("update_client",))),
        TypeDef("AcceptedOlderNotice", Record((
            FieldDef("kind", TypeRef("AcceptedOlderKind")),
            FieldDef("negotiated", TypeRef("ProtocolVersion")),
            FieldDef("newest", TypeRef("ProtocolVersion")),
            FieldDef("remedy", TypeRef("UpdateClientRemedy")),
        ))),
        TypeDef("DeprecationNotice", Nullable(TypeRef("AcceptedOlderNotice"))),
        TypeDef("StoreKind", LiteralSet(("production", "isolated"))),
        TypeDef("SchemaEncoding", LiteralSet(("base64",))),
        TypeDef("ConnectArgs", Record((
            FieldDef("supported", TypeRef("ProtocolWindow")),
            FieldDef("required_build_id", TypeRef("Digest"), True),
            FieldDef("required_schema_digest", TypeRef("Digest"), True),
        ))),
        TypeDef("ConnectResult", Record((
            FieldDef("protocol", TypeRef("ProtocolVersion")),
            FieldDef("engine_version", TypeRef("EngineVersion")),
            FieldDef("build_id", TypeRef("Digest")),
            FieldDef("schema_digest", TypeRef("Digest")),
            FieldDef("store_id", TypeRef("OpaqueId")),
            FieldDef("store_kind", TypeRef("StoreKind")),
            FieldDef("runtime_instance_id", TypeRef("OpaqueId")),
            FieldDef("supported", TypeRef("ProtocolWindow")),
            FieldDef("capabilities", TypeRef("Capabilities")),
            FieldDef("deprecation", TypeRef("DeprecationNotice")),
        ))),
        TypeDef("SchemaArgs", Record(())),
        TypeDef("SchemaResult", Record((
            FieldDef("protocol", TypeRef("ProtocolVersion")),
            FieldDef("schema_digest", TypeRef("Digest")),
            FieldDef("schema_encoding", TypeRef("SchemaEncoding")),
            FieldDef("schema_byte_count", TypeRef("UInt64")),
            FieldDef("schema_bytes_base64", TypeRef("CanonicalSchemaBase64")),
        )), (SchemaPayloadV1(TypeRef("SchemaResult")),)),
    )
    operations = (
        OperationDef("protocol.connect", TypeRef("ConnectArgs"), TypeRef("ConnectResult"),
                     TypeRef("ApiError"), "unsigned", "connection_metadata", "read_repeat", "connect"),
        OperationDef("protocol.schema", TypeRef("SchemaArgs"), TypeRef("SchemaResult"),
                     TypeRef("ApiError"), "unsigned", "pure_observation", "read_repeat", "schema",
                     (NegotiatedSchemaV1(TypeRef("SchemaResult")),)),
    )
    return DefinitionGraph(types, operations, TypeRef("ApiError"))


def platform_definitions() -> DefinitionGraph:
    """Discovery plus the native session operations, without runtime imports."""
    base = discovery_definitions()
    def record(name, fields):
        return TypeDef(name, Record(tuple(FieldDef(field, TypeRef(ref)) for field, ref in fields)))
    credentials = (('key_id', 'KeyId'), ('key_secret', 'KeySecret'))
    identity = tuple((key, 'OpaqueId') for key in
                     ('project_id', 'team_instance_id', 'seat_id', 'occupant_id', 'session_id'))
    identity += (('generation', 'UInt64'), ('signed_in_at', 'UInt64'))
    types = (
        TypeDef('KeyId', String(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 36, 36)),
        TypeDef('KeySecret', String(r'[A-Za-z0-9_-]{43}', 43, 43)),
        TypeDef('SessionHost', LiteralSet(('claude-code', 'codex', 'frank', 'other'))),
        TypeDef('SessionReceipt', LiteralSet(('original', 'replayed'))),
        TypeDef('SessionState', LiteralSet(('open', 'ended'))),
        record('SessionSignInArgs', credentials + (('host', 'SessionHost'),
               ('host_session_id', 'OpaqueId'), ('operation_key', 'OpaqueId'))),
        record('SessionSignInResult', identity + (('receipt', 'SessionReceipt'),
                                                ('session_state', 'SessionState'))),
        record('SessionResumeArgs', credentials + (('session_id', 'OpaqueId'),)),
        record('SessionResumeResult', identity),
        record('SessionEndArgs', credentials + (('session_id', 'OpaqueId'), ('operation_key', 'OpaqueId'))),
        record('SessionEndResult', (('session_id', 'OpaqueId'), ('ended_at', 'UInt64'),
                                    ('receipt', 'SessionReceipt'))),
    )
    operations = tuple(OperationDef(name, TypeRef(args), TypeRef(result), TypeRef('ApiError'),
        'seat_key', effect, retry, 'none') for name, args, result, effect, retry in (
        ('session.sign-in', 'SessionSignInArgs', 'SessionSignInResult', 'session_write', 'operation_key'),
        ('session.resume', 'SessionResumeArgs', 'SessionResumeResult', 'connection_binding', 'read_repeat'),
        ('session.end', 'SessionEndArgs', 'SessionEndResult', 'session_write', 'operation_key')))
    return DefinitionGraph(base.types + types, base.operations + operations, base.error_ref)
