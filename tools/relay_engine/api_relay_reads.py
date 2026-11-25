"""Generator-safe definitions and handlers for Platform relay reads."""
from collections.abc import Callable, Mapping
from typing import Protocol

from relay_engine import api_definitions as d
from relay_engine.api_protocol import ComponentRefusal, SeatBinding


class RelayReadsPort(Protocol):
    def read(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal: ...

    def inbox(self, args: Mapping) -> Mapping | ComponentRefusal: ...


def reads_definitions() -> d.DefinitionGraph:
    ref = d.TypeRef
    field = d.FieldDef
    error = ref('ApiError')
    types = (
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
    operation = d.OperationDef('relay.inbox', ref('RelayInboxArgs'), ref('RelayInboxResult'),
                               error, 'unsigned', 'pure_observation', 'read_repeat', 'none')
    return d.DefinitionGraph(types, (operation,), error)


def _inbox(args, principal, port):
    return port.reads.inbox(args)


def reads_handlers() -> Mapping[str, Callable]:
    return {'relay.inbox': _inbox}
