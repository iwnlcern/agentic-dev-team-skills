"""Runtime-only, read-only serving for the inbox operation."""
from collections.abc import Mapping, Sequence
from contextlib import closing
from functools import cache
import sqlite3

from relay_engine import addresses, api_definitions as d, api_registry as r, jcs
from relay_engine import queries, store_layout
from relay_engine.api_protocol import ComponentRefusal
from relay_engine.api_submit import ServiceContext
from relay_engine.api_wire import MAX_API_FRAME
from relay_engine.api_writer import WriterUnavailable


_MAX_REQUEST_ID = str(d.MAX_UINT64)
_INVALID_ARGS = ComponentRefusal('E_API_PROTOCOL', 'invalid_args')
_NOT_READY = ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
_HANDLER_FAULT = ComponentRefusal('E_API_INTERNAL', 'handler_fault')


def party_address(conn, party: str) -> str:
    if party in ('operator', 'system'):
        return party
    with closing(conn.execute(
        'SELECT t.name, s.role, p.name FROM seats s'
        ' JOIN team_instances t ON t.id=s.team_instance_id'
        ' JOIN projects p ON p.id=t.project_id WHERE s.id=?', (party,)
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ValueError('sender_address_unavailable')
    team, role, project = row
    return addresses.render(addresses.Address(team=team, role=role, project=project))


def resolve_seat(conn, project_id: str, team: str, role: str) -> str | None:
    with closing(conn.execute(
        'SELECT s.id FROM team_instances t JOIN seats s ON s.team_instance_id=t.id'
        ' WHERE t.project_id=? AND t.name=? AND t.closed_at IS NULL AND s.role=?',
        (project_id, team, role),
    )) as cursor:
        row = cursor.fetchone()
    return None if row is None else row[0]


def project_row(registry, conn, row: queries.AddressedRow) -> Mapping:
    over_bound = isinstance(r.validate_type(registry, d.TypeRef('RelaySubject'), row.subject),
                            r.ValidationFailure)
    return {
        'relay_id': row.relay_id,
        'handle': str(row.handle),
        'created_at': str(row.created_at),
        'sender': party_address(conn, row.sender),
        'subject': None if over_bound else row.subject,
        'subject_over_bound': 'true' if over_bound else 'false',
        'needs': row.needs,
        'state': row.state,
    }


def fit_rows(head: Mapping, rows: Sequence[Mapping]) -> int:
    """Count the leading rows that fit with the longest legal request ID."""
    total = int(head['total'])
    empty = dict(head, truncated='true', rows=[])
    size = len(jcs.jcs_encode({'request_id': _MAX_REQUEST_ID, 'result': empty}))
    for index, row in enumerate(rows):
        size += len(jcs.jcs_encode(row)) + bool(index)
        # A complete page uses "false", one byte longer than "true".
        if size + (index + 1 == total) > MAX_API_FRAME:
            if index == 0:
                raise ValueError('inbox_row_does_not_fit')
            return index
    return len(rows)


@cache
def _registry():
    # The runtime service is not part of Platform's generator dependency graph.
    from relay_engine.api_platform import build_platform
    return build_platform().registry


class RelayReadsService:
    def __init__(self, context: ServiceContext):
        self.context = context

    def inbox(self, args: Mapping) -> Mapping | ComponentRefusal:
        try:
            address = addresses.parse_address(args['seat_address'])
        except addresses.AddressRefused:
            return _INVALID_ARGS
        if address.special is not None or address.project is None:
            return _INVALID_ARGS
        limit = int(args['limit'])
        since = int(args['since_ms']) if 'since_ms' in args else None
        before = int(args['before_handle']) if 'before_handle' in args else None
        if (since is not None and not 0 <= since < 2**48
                or before is not None and not 1 <= before < 2**63):
            return _INVALID_ARGS

        try:
            with closing(store_layout.open_catalogue_readonly(self.context.store_root)) as catalogue:
                project_id = store_layout.route(catalogue, name=address.project).project_id
        except store_layout.RouteUnavailable as error:
            if error.cause == 'not-found':
                return ComponentRefusal('E_API_NOT_FOUND', 'project_unknown')
            if error.cause == 'creating':
                return _NOT_READY
            return _HANDLER_FAULT
        except (store_layout.LayoutFault, OSError, sqlite3.Error):
            return _NOT_READY

        def page(conn):
            seat = resolve_seat(conn, project_id, address.team, address.role)
            if seat is None:
                return ComponentRefusal('E_API_NOT_FOUND', 'seat_unknown')
            inbox = queries.addressed_to(conn, project_id, seat, limit=limit,
                                         since_ms=since, before_handle=before)
            registry = _registry()
            head = {
                'total': str(inbox.total),
                'answered': str(inbox.answered),
                'unanswered': str(inbox.unanswered),
                'imported': str(inbox.imported),
            }
            rows = [project_row(registry, conn, row) for row in inbox.rows]
            count = fit_rows(head, rows)
            return dict(head, truncated='true' if inbox.total > count else 'false',
                        rows=rows[:count])

        try:
            if project_id not in self.context.port.served_projects():
                return _NOT_READY
            return self.context.port.read(project_id, page)
        except (WriterUnavailable, store_layout.LayoutFault, OSError, sqlite3.Error):
            return _NOT_READY
        except ValueError:
            return _HANDLER_FAULT
