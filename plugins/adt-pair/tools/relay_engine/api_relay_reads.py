"""Generator-safe definitions and handlers for Platform relay reads."""
from collections.abc import Callable, Mapping
from typing import Protocol

from relay_engine import api_definitions as d
from relay_engine.api_protocol import ComponentRefusal, SeatBinding


MAX_RELAY_BODY_BYTES = 8_388_608


class RelayReadsPort(Protocol):
    def read(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal: ...

    def inbox(self, args: Mapping) -> Mapping | ComponentRefusal: ...


def reads_definitions() -> d.DefinitionGraph:
    ref = d.TypeRef
    field = d.FieldDef
    error = ref('ApiError')
    def record(name, columns):
        return d.TypeDef(name, d.Record(tuple(field(key, ref(type_name)) for key, type_name in columns)))

    types = (
        d.TypeDef('NullableUInt64', d.Nullable(ref('UInt64'))),
        d.TypeDef('NullableOpaqueId', d.Nullable(ref('OpaqueId'))),
        d.TypeDef('RelayParties', d.Array(ref('OpaqueId'), 2048)),
        d.TypeDef('RelayBodyBase64', d.String(
            r'(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?',
            0, 4 * ((MAX_RELAY_BODY_BYTES + 2) // 3)),
            (d.CanonicalBase64(ref('RelayBodyBase64'), MAX_RELAY_BODY_BYTES),)),
        record('RelayReadArgs', (('relay_id', 'OpaqueId'),)),
        record('RelayEnvelope', (
            ('handle', 'UInt64'), ('kind', 'OpaqueId'), ('sender', 'OpaqueId'),
            ('to', 'RelayParties'), ('cc', 'RelayParties'), ('subject', 'RelaySubject'),
            ('parent', 'NullableUInt64'), ('parent_id', 'NullableOpaqueId'),
            ('verdict', 'NullableOpaqueId'), ('needs', 'OpaqueId'), ('created_at', 'UInt64'),
            ('imported', 'InboxFlag'),
        )),
        record('RelayPointer', (('relay_id', 'OpaqueId'), ('handle', 'UInt64'))),
        d.TypeDef('RelayPointers', d.Array(ref('RelayPointer'), 255)),
        d.TypeDef('RefSpelling', d.String(r'[^\x00]*', 0, 8192)),
        record('RefLine', (('ordinal', 'UInt64'), ('spelling', 'RefSpelling'))),
        d.TypeDef('RefLines', d.Array(ref('RefLine'), 1024)),
        d.TypeDef('FindingSeverity', d.LiteralSet(('blocking', 'advisory'))),
        d.TypeDef('FindingStatus', d.LiteralSet(('open', 'fixed', 'fix-accepted', 'withdrawn'))),
        record('RaisedFinding', (
            ('handle', 'UInt64'), ('severity', 'FindingSeverity'), ('status', 'FindingStatus'),
            ('summary', 'Utf8'), ('check', 'NullableUInt64'),
        )),
        d.TypeDef('RaisedFindings', d.Array(ref('RaisedFinding'), 409)),
        record('RelayReadResult', (
            ('relay_id', 'OpaqueId'), ('envelope', 'RelayEnvelope'), ('body_base64', 'RelayBodyBase64'),
            ('unopened', 'RelayPointers'), ('refs', 'RefLines'), ('findings', 'RaisedFindings'),
        )),
        d.TypeDef('InboxLimit', d.String(r'(?:[1-9][0-9]?|[1-4][0-9]{2}|500)', 1, 3)),
        d.TypeDef('InboxState', d.LiteralSet(('answered', 'unanswered', 'imported'))),
        d.TypeDef('InboxFlag', d.LiteralSet(('false', 'true'))),
        d.TypeDef('RelaySubject', d.String(r'[^\x00]*', 0, 16_384)),
        d.TypeDef('NullableRelaySubject', d.Nullable(ref('RelaySubject'))),
        d.TypeDef('InboxRow', d.Record((
            field('relay_id', ref('OpaqueId')),
            field('handle', ref('UInt64')),
            field('created_at', ref('UInt64')),
            field('sender', ref('OpaqueId')),
            field('subject', ref('NullableRelaySubject')),
            field('subject_over_bound', ref('InboxFlag')),
            field('needs', ref('OpaqueId')),
            field('state', ref('InboxState')),
        ))),
        d.TypeDef('InboxRows', d.Array(ref('InboxRow'), 500)),
        d.TypeDef('RelayInboxArgs', d.Record((
            field('seat_address', ref('OpaqueId')),
            field('limit', ref('InboxLimit')),
            field('since_ms', ref('UInt64'), optional=True),
            field('before_handle', ref('UInt64'), optional=True),
        ))),
        d.TypeDef('RelayInboxResult', d.Record((
            field('total', ref('UInt64')),
            field('answered', ref('UInt64')),
            field('unanswered', ref('UInt64')),
            field('imported', ref('UInt64')),
            field('truncated', ref('InboxFlag')),
            field('rows', ref('InboxRows')),
        ))),
    )
    operations = (
        d.OperationDef('relay.inbox', ref('RelayInboxArgs'), ref('RelayInboxResult'),
                       error, 'unsigned', 'pure_observation', 'read_repeat', 'none'),
        d.OperationDef('relay.read', ref('RelayReadArgs'), ref('RelayReadResult'),
                       error, 'seat_session', 'relay_open', 'read_repeat', 'none'),
    )
    return d.DefinitionGraph(types, operations, error)


def _inbox(args, principal, port):
    return port.reads.inbox(args)


def _read(args, binding, port):
    return port.reads.read(args, binding)


def reads_handlers() -> Mapping[str, Callable]:
    return {'relay.inbox': _inbox, 'relay.read': _read}
