"""Runtime-only serving for relay reads and inbox pages."""
import base64
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import cache
import sqlite3

from relay_engine import addresses, api_definitions as d, api_registry as r, jcs
from relay_engine import errors, queries, store_layout
from relay_engine.api_protocol import ComponentRefusal, SeatBinding
from relay_engine.api_relay_reads import MAX_RELAY_BODY_BYTES
from relay_engine.api_sessions import BindingUnavailable, require_current_binding
from relay_engine.api_submit import ServiceContext, map_refusal
from relay_engine.api_wire import MAX_API_FRAME
from relay_engine.api_writer import JobOutcome, WriterUnavailable
from relay_engine.submit import party_sql


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


def _decimal(value):
    return None if value is None else str(value)


def _project_envelope(conn, envelope):
    return {
        'handle': str(envelope['handle']),
        'kind': envelope['kind'],
        'sender': party_address(conn, envelope['sender']),
        'to': [party_address(conn, seat) for seat in envelope['to']],
        'cc': [party_address(conn, seat) for seat in envelope['cc']],
        'subject': envelope['subject'],
        'parent': _decimal(envelope['parent']),
        'parent_id': envelope['parent_id'],
        'verdict': envelope['verdict'],
        'needs': envelope['needs'],
        'created_at': str(envelope['created_at']),
        'imported': 'true' if envelope['imported'] else 'false',
    }


def _project_values(conn, relay_id, envelope, body_base64, unopened, refs, findings):
    return {
        'relay_id': relay_id,
        'envelope': _project_envelope(conn, envelope),
        'body_base64': body_base64,
        'unopened': [{'relay_id': rid, 'handle': str(handle)} for rid, handle in unopened],
        'refs': [{'ordinal': str(line.ordinal), 'spelling': line.spelling} for line in refs],
        'findings': [
            {'handle': str(item.handle), 'severity': item.severity, 'status': item.status,
             'summary': item.summary, 'check': _decimal(item.check)} for item in findings
        ],
    }


@dataclass(frozen=True)
class ReadCandidate:
    result: Mapping
    body_bytes: int


def read_candidate(conn, relay_id: str, seat_id: str) -> ReadCandidate:
    """Read the wire values without loading the body or recording an open."""
    with closing(conn.execute(
        'SELECT r.handle, r.kind, ' + party_sql('r.sender_') + ', r.subject, p.handle,'
        ' r.parent_id, r.verdict, r.needs, r.created_at, r.submission_key, r.sender_special,'
        ' r.body_bytes FROM relays r LEFT JOIN relays p ON p.id=r.parent_id WHERE r.id=?',
        (relay_id,),
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise KeyError(relay_id)
    (handle, kind, sender, subject, parent, parent_id, verdict, needs, created_at,
     submission_key, sender_special, body_bytes) = row
    with closing(conn.execute(
        'SELECT ' + party_sql() + ', role FROM recipients WHERE relay_id=? ORDER BY rowid',
        (relay_id,),
    )) as cursor:
        parties = cursor.fetchall()
    to = [seat for seat, role in parties if role == 'to']
    cc = [seat for seat, role in parties if role == 'cc']
    envelope = {
        'handle': handle, 'kind': kind, 'sender': sender, 'to': to, 'cc': cc,
        'subject': subject, 'parent': parent, 'parent_id': parent_id, 'verdict': verdict,
        'needs': needs, 'created_at': created_at,
        'imported': submission_key is None and sender_special != 'system',
    }
    unopened = (queries.unopened_in_lineage(conn, relay_id, seat_id)
                if needs == 'review' and seat_id in to else [])
    edges = [edge for edge in queries.points_to(conn, relay_id) if edge.kind == 'ref']
    refs = [queries.RefLine(edge.ordinal, edge.spelling) for edge in edges]
    ordinals = {edge.id: edge.ordinal for edge in edges}
    with closing(conn.execute(
        'SELECT handle, severity, status, summary, check_edge_id FROM findings'
        ' WHERE raised_in=? ORDER BY handle', (relay_id,),
    )) as cursor:
        findings = [queries.RaisedFinding(handle, severity, status, summary, ordinals[check]
                                         if check is not None else None)
                    for handle, severity, status, summary, check in cursor]
    return ReadCandidate(_project_values(conn, relay_id, envelope, '', unopened, refs, findings),
                         body_bytes)


def answer_size(result: Mapping, body_bytes: int) -> int:
    return (len(jcs.jcs_encode({'request_id': _MAX_REQUEST_ID, 'result': result}))
            + 4 * ((body_bytes + 2) // 3))


class AnswerOutOfBound(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_candidate(registry, candidate: ReadCandidate) -> None:
    if type(candidate.body_bytes) is not int or not 0 <= candidate.body_bytes <= MAX_RELAY_BODY_BYTES:
        raise AnswerOutOfBound('body')
    verdict = r.validate_operation_result(registry, 'relay.read', candidate.result, None)
    if isinstance(verdict, r.ValidationFailure) or candidate.result['body_base64'] != '':
        raise AnswerOutOfBound('values')
    if answer_size(candidate.result, candidate.body_bytes) > MAX_API_FRAME:
        raise AnswerOutOfBound('frame')


def project_read(conn, relay_id: str, result: queries.ReadResult) -> Mapping:
    return _project_values(conn, relay_id, result.envelope, base64.b64encode(result.body).decode('ascii'),
                           result.unopened, result.refs, result.findings)


class RelayReadJob:
    def __init__(self, files: frozenset[str], binding: SeatBinding, relay_id: str,
                 clock: Callable[[], int], registry, *, values=read_candidate, opener=queries.read):
        self.files = files
        self.binding = binding
        self.relay_id = relay_id
        self.clock = clock
        self.registry = registry
        self.values = values
        self.opener = opener

    def run(self, conn) -> JobOutcome:
        require_current_binding(conn, self.binding)
        try:
            candidate = self.values(conn, self.relay_id, self.binding.seat_id)
        except KeyError as error:
            if error.args != (self.relay_id,):
                raise
            return JobOutcome(False, ComponentRefusal('E_API_NOT_FOUND', 'relay_unknown'))
        check_candidate(self.registry, candidate)
        opened = self.opener(conn, self.relay_id, self.binding.seat_id, self.clock())
        return JobOutcome(True, project_read(conn, self.relay_id, opened))


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

    def read(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal:
        try:
            project_id = binding.project_id
            files = frozenset({self.context.port.ledger_file(project_id)})
            job = RelayReadJob(files, binding, args['relay_id'], self.context.clock, _registry())
            return self.context.port.submit_callee(project_id, job).value
        except errors.EngineError as error:
            return map_refusal(error)
        except (WriterUnavailable, BindingUnavailable, store_layout.LayoutFault, OSError, sqlite3.Error):
            return _NOT_READY
        except (AnswerOutOfBound, ValueError):
            return _HANDLER_FAULT

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
