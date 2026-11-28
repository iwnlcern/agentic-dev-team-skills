"""Catalogue records within the caller's transaction, never its transaction owner."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from sqlite3 import Connection
from typing import Protocol, TypeVar

from relay_engine import addresses, store_layout
from relay_engine.api_principals import OperatorPrincipal, ServicePrincipal
from relay_engine.api_protocol import SeatBinding
from relay_engine.api_sessions import request_digest
from relay_engine.jcs import jcs_encode
from relay_engine.submit import new_uuid7
from relay_engine.operator_queue import kinds


@dataclass(frozen=True)
class InternalWriter:
    """Admission token for queue-owned ingests, separate from public service entry."""


Principal = OperatorPrincipal | ServicePrincipal | SeatBinding | InternalWriter


@dataclass(frozen=True)
class QueueRefusal:
    code: str
    cause_kind: str
    payload: Mapping | None = None


@dataclass(frozen=True)
class ItemView:
    item_id: str
    kind: str
    subkind: str | None
    project_id: str | None
    raiser: str
    asking_seat_id: str | None
    actor_address: str | None
    fields: str | None
    answer_to: str | None
    source_handle: int | None
    source_relay_id: str | None
    revision: int
    state: str
    texts: str
    popup_text: str | None
    choices: str | None
    choices_reason: str | None
    pins: str
    condition_key: str
    raised_position: int
    raised_at_ms: int
    events_position: int


@dataclass(frozen=True)
class RaiseRequest:
    kind: str
    subkind: str | None
    project_id: str | None
    texts: Mapping[str, str]
    pins: Mapping
    condition_key: Mapping | str
    operation_key: str
    actor_address: str | None = None


@dataclass(frozen=True)
class RaiseResult:
    item_id: str
    revision: int
    state: str
    receipt_id: str
    replayed: bool


@dataclass(frozen=True)
class ClearResult:
    outcome: str


@dataclass(frozen=True)
class OpenCondition:
    kind: str
    subkind: str | None
    condition_key: Mapping | str
    item_id: str


@dataclass(frozen=True)
class Cursor:
    position: int
    epoch: int
    anchor_id: str | None


@dataclass(frozen=True)
class Stale:
    """A stale batch wrote nothing and may be read again."""


@dataclass(frozen=True)
class Reread:
    """The source's rewind check failed before any write."""


@dataclass(frozen=True)
class BlockingItem:
    item_id: str
    kind: str
    state: str
    names_as: str


class UndoBlocked(Exception):
    def __init__(self, items: tuple[BlockingItem, ...]):
        self.items = items
        super().__init__('queue items block project undo')


@dataclass(frozen=True)
class UndoQueueResult:
    retired_item_ids: tuple[str, ...]
    closed_condition_item_ids: tuple[str, ...]
    retired_outbox: int
    epoch: int


class JobUnavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


T = TypeVar('T')


class JobPort(Protocol):
    def catalogue(self, fn: Callable[[Connection], T]) -> T: ...
    def catalogue_with_ledger_read(self, project_id: str,
                                  fn: Callable[[Connection, Connection], T]) -> T: ...
    def callee_ledger(self, project_id: str, fn: Callable[[Connection], T]) -> T: ...
    def read_ledger(self, project_id: str, fn: Callable[[Connection], T]) -> T: ...
    def read_catalogue(self, fn: Callable[[Connection], T]) -> T: ...


def _rows(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _one(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _write(conn, sql, params=()):
    cursor = conn.execute(sql, params)
    try:
        return cursor.rowcount
    finally:
        cursor.close()


def _json(value):
    return jcs_encode(dict(value) if isinstance(value, Mapping) else value).decode('utf-8')


def principal_scope(principal: Principal) -> tuple[str, str]:
    if isinstance(principal, OperatorPrincipal):
        return 'operator', 'operator'
    if isinstance(principal, InternalWriter):
        return 'service', 'console_queue'
    if isinstance(principal, ServicePrincipal):
        return 'service', principal.service
    if isinstance(principal, SeatBinding):
        return 'seat', principal.occupant_id
    raise TypeError('not a queue principal')


def store_id(conn) -> str:
    return _one(conn, 'SELECT store_id FROM catalogue_identity WHERE id=1')[0]


def next_position(conn) -> int:
    return _one(conn, 'SELECT COALESCE(MAX(position),0)+1 FROM queue_events')[0]


def append_event(conn, item_id, revision, state, event_kind, principal, now_ms,
                 *, receipt_id=None, detail=None) -> int:
    position = next_position(conn)
    kind, identity = principal_scope(principal)
    _write(conn, 'INSERT INTO queue_events '
           '(position,item_id,revision,state,event_kind,principal_kind,principal_id,receipt_id,detail,at_ms) '
           'VALUES (?,?,?,?,?,?,?,?,?,?)',
           (position, item_id, revision, state, event_kind, kind, identity,
            receipt_id, None if detail is None else _json(detail), now_ms))
    return position


def replay_or_none(conn, principal, operation: str, operation_key: str, args: Mapping):
    kind, identity = principal_scope(principal)
    row = _one(conn, 'SELECT request_digest,result FROM queue_receipts WHERE '
               'principal_kind=? AND principal_id=? AND operation=? AND operation_key=?',
               (kind, identity, operation, operation_key))
    if row is None:
        return None
    if row[0] != request_digest(operation, args):
        return QueueRefusal('E_API_CONFLICT', 'operation_key_reused')
    return dict(json.loads(row[1]), replayed=True)


def record_receipt(conn, principal, operation, operation_key, args, result: Mapping, now_ms) -> str:
    receipt_id = new_uuid7(now_ms)
    kind, identity = principal_scope(principal)
    value = dict(result)
    value.setdefault('receipt_id', receipt_id)
    _write(conn, 'INSERT INTO queue_receipts VALUES (?,?,?,?,?,?,?,?)',
           (receipt_id, kind, identity, operation, operation_key,
            request_digest(operation, args), _json(value), now_ms))
    return receipt_id


def route_active(catalogue_conn, project_id: str) -> bool:
    try:
        store_layout.route(catalogue_conn, project_id=project_id)
    except store_layout.RouteUnavailable:
        return False
    return True


def _texts_valid(kind, texts):
    if not isinstance(texts, Mapping):
        return False
    members = set(texts)
    if kind == 'question':
        if members != {'subject', 'body'}:
            return False
    elif 'sentence' not in members or not members <= {'sentence', 'subject', 'caused_by', 'ring_text'}:
        return False
    for name, value in texts.items():
        if (not isinstance(value, str)
                or (kind != 'question' and (not value or '\x00' in value))):
            return False
        try:
            size = len(value.encode('utf-8'))
        except UnicodeEncodeError:
            return False
        if kind == 'question':
            if (name == 'subject' and len(value) > 400) or (name == 'body' and size > 65536):
                return False
        elif size > 4096:
            return False
    return True


def _actor_valid(value):
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        address = addresses.parse_address(value)
    except addresses.AddressRefused:
        return False
    return (address.special is None and address.project is None
            and addresses.render(address) == value and len(value.encode('utf-8')) <= 86)


def raise_item(conn, principal, request: RaiseRequest, now_ms, *, enabled: frozenset[str],
               fields: Mapping | None = None):
    service = ('console_queue' if isinstance(principal, InternalWriter) else
               principal.service if isinstance(principal, ServicePrincipal) else None)
    row = kinds.raiser_row(request.kind, request.subkind, service, enabled=enabled)
    entry = 'internal' if isinstance(principal, InternalWriter) else 'queue.raise'
    if row is None or row.entry != entry:
        return QueueRefusal('E_QUEUE_INVALID', 'kind_not_raisable')
    invalid = QueueRefusal('E_QUEUE_INVALID', 'subject_unresolved')
    if (request.subkind not in kinds.EITHER_PROJECT_SUBKINDS
            and (request.project_id is None) != (request.subkind in kinds.NO_PROJECT_SUBKINDS)):
        return invalid
    if row.keys is not None and (not isinstance(request.condition_key, str)
                                 or request.condition_key not in row.keys):
        return invalid
    if request.subkind == 'approval_seal_unusable' and not kinds.seal_key_valid(request.condition_key):
        return invalid
    if request.subkind in ('seat_record_missing', 'seat_ambiguous'):
        if not kinds.act_lookup_args_valid(request.subkind, request.condition_key, request.pins):
            return invalid
    if not _actor_valid(request.actor_address):
        return invalid
    if request.project_id is not None:
        if not route_active(conn, request.project_id):
            return QueueRefusal('E_QUEUE_STATE', 'project_not_active')
        if request.kind == 'cross_project_permission':
            if not isinstance(request.condition_key, Mapping):
                return invalid
            destination = request.condition_key.get('receiver_project_id')
            if not isinstance(destination, str):
                return invalid
            if not route_active(conn, destination):
                return QueueRefusal('E_QUEUE_STATE', 'project_not_active')
    if not _texts_valid(request.kind, request.texts):
        return invalid
    try:
        key = kinds.condition_key(request.kind, request.subkind, request.condition_key)
        pins = _json(request.pins)
        fields_text = None if fields is None else _json(fields)
        texts = dict(request.texts)
        fixed = kinds.fixed_text(request.subkind, request.condition_key if isinstance(request.condition_key, str) else key)
        if fixed is not None:
            token = kinds.PIN_TOKENS.get(request.subkind)
            if token is not None:
                marker, name = token
                fixed = fixed.replace(marker, request.pins[name], 1)
            texts = {'sentence': fixed}
            if not _texts_valid(request.kind, texts):
                return invalid
        texts_text = _json(texts)
        args = dict(kind=request.kind, subkind=request.subkind, project_id=request.project_id,
                    texts=dict(request.texts), pins=dict(request.pins), condition_key=request.condition_key)
        replay = replay_or_none(conn, principal, 'queue.raise', request.operation_key, args)
    except (TypeError, ValueError, KeyError, UnicodeEncodeError):
        return invalid
    if isinstance(replay, QueueRefusal):
        return replay
    if replay is not None:
        return RaiseResult(replay['item_id'], replay['revision'], replay['state'], replay['receipt_id'], True)
    existing = _one(conn, 'SELECT i.item_id,i.revision,i.state FROM queue_conditions c '
                    'JOIN queue_items i ON i.item_id=c.item_id WHERE c.kind=? AND c.subkind IS ? '
                    'AND c.project_id IS ? AND c.condition_key=? AND c.closed_at_ms IS NULL',
                    (request.kind, request.subkind, request.project_id, key))
    if existing is not None:
        result = dict(zip(('item_id', 'revision', 'state'), existing))
        receipt = record_receipt(conn, principal, 'queue.raise', request.operation_key, args, result, now_ms)
        return RaiseResult(*existing, receipt, False)
    item_id = new_uuid7(now_ms)
    position = next_position(conn)
    _write(conn, 'INSERT INTO queue_items '
           '(item_id,kind,subkind,project_id,raiser,actor_address,fields,revision,state,texts,pins,'
           'condition_key,raised_position,raised_at_ms) VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?)',
           (item_id, request.kind, request.subkind, request.project_id, service,
            request.actor_address, fields_text, 'pending', texts_text, pins, key, position, now_ms))
    _write(conn, 'INSERT INTO queue_conditions '
           '(item_id,kind,subkind,project_id,condition_key,raiser,raised_at_ms) VALUES (?,?,?,?,?,?,?)',
           (item_id, request.kind, request.subkind, request.project_id, key, service, now_ms))
    append_event(conn, item_id, 1, 'pending', 'raised', principal, now_ms)
    if kinds.rings(request.kind, request.subkind):
        _write(conn, 'INSERT INTO queue_notifications '
               '(item_id,revision,state,next_attempt_at_ms) VALUES (?,1,?,?)', (item_id, 'pending', now_ms))
    result = dict(item_id=item_id, revision=1, state='pending')
    receipt = record_receipt(conn, principal, 'queue.raise', request.operation_key, args, result, now_ms)
    return RaiseResult(item_id, 1, 'pending', receipt, False)


def read_cursor(conn, project_id: str | None, source) -> Cursor:
    row = _one(conn, 'SELECT position,epoch,anchor_id FROM queue_highwaters '
               'WHERE project_id IS ? AND source=?', (project_id, source))
    return Cursor(*(row if row is not None else (0, 0, None)))


def advance_cursor(conn, project_id: str | None, source, expected: Cursor, position: int,
                   *, anchor_id: str | None = None) -> bool:
    row = _one(conn, 'SELECT position,epoch,anchor_id FROM queue_highwaters '
               'WHERE project_id IS ? AND source=?', (project_id, source))
    if row is None:
        if expected != Cursor(0, 0, None):
            return False
        return _write(conn, 'INSERT OR IGNORE INTO queue_highwaters '
                      '(project_id,source,position,epoch,anchor_id) VALUES (?,?,?,0,?)',
                      (project_id, source, position, anchor_id)) == 1
    return _write(conn, 'UPDATE queue_highwaters SET position=?,anchor_id=? '
                  'WHERE project_id IS ? AND source=? AND position=? AND epoch=? AND anchor_id IS ?',
                  (position, anchor_id, project_id, source, expected.position,
                   expected.epoch, expected.anchor_id)) == 1


def _close_condition(conn, item_id, revision, state, principal, now_ms):
    position = append_event(conn, item_id, revision, state, 'condition_closed', principal, now_ms)
    _write(conn, 'UPDATE queue_conditions SET closed_at_ms=?,closed_position=? '
           'WHERE item_id=? AND closed_at_ms IS NULL', (now_ms, position, item_id))


def clear_condition(conn, principal, item_id, kind, subkind, project_id, condition_key, now_ms):
    invalid = QueueRefusal('E_QUEUE_INVALID', 'subject_unresolved')
    if subkind in kinds.NO_CLEAR_SUBKINDS:
        return invalid
    row = _one(conn, 'SELECT c.kind,c.subkind,c.project_id,c.condition_key,c.raiser,c.closed_at_ms,'
               'c.clear_requested,i.revision,i.state FROM queue_conditions c JOIN queue_items i '
               'ON i.item_id=c.item_id WHERE c.item_id=?', (item_id,))
    try:
        key = kinds.condition_key(kind, subkind, condition_key)
        scope, identity = principal_scope(principal)
    except (TypeError, ValueError):
        return invalid
    if row is None or scope != 'service' or row[:5] != (kind, subkind, project_id, key, identity):
        return invalid
    if row[5] is not None:
        return ClearResult('already_closed')
    revision, state = row[7:]
    if state == 'applying':
        if not row[6]:
            _write(conn, 'UPDATE queue_conditions SET clear_requested=1 WHERE item_id=?', (item_id,))
            append_event(conn, item_id, revision, state, 'clear_requested', principal, now_ms)
        return ClearResult('clear_pending')
    if state == 'pending':
        revision += 1
        state = 'cleared'
        _write(conn, 'UPDATE queue_items SET revision=?,state=? WHERE item_id=?', (revision, state, item_id))
        append_event(conn, item_id, revision, state, 'cleared', principal, now_ms)
        outcome = 'cleared'
    else:
        outcome = 'closed'
    _close_condition(conn, item_id, revision, state, principal, now_ms)
    return ClearResult(outcome)


def open_condition_items(conn, principal, project_id: str | None) -> list[OpenCondition]:
    scope, identity = principal_scope(principal)
    if scope != 'service':
        return []
    return [OpenCondition(kind, subkind, json.loads(key), item_id)
            for kind, subkind, key, item_id in _rows(conn,
                'SELECT kind,subkind,condition_key,item_id FROM queue_conditions '
                'WHERE raiser=? AND project_id IS ? AND closed_at_ms IS NULL ORDER BY item_id',
                (identity, project_id))]


def undo_step(catalogue_conn, project_id: str, undo_job_id: str, *, now_ms: int) -> UndoQueueResult:
    conn = catalogue_conn
    items = _rows(conn, 'SELECT item_id,kind,state,project_id,condition_key,revision FROM queue_items '
                  'ORDER BY raised_position,item_id')
    blockers = []
    for item_id, kind, state, project, key, revision in items:
        destination = (json.loads(key).get('receiver_project_id')
                       if kind == 'cross_project_permission' and isinstance(json.loads(key), dict) else None)
        names_as = 'project' if project == project_id else 'destination' if destination == project_id else None
        if names_as is not None and kind != 'notice' and state in ('pending', 'applying'):
            blockers.append(BlockingItem(item_id, kind, state, names_as))
    if blockers:
        raise UndoBlocked(tuple(blockers))
    principal = InternalWriter()
    retired = []
    for item_id, kind, state, project, key, revision in items:
        if project == project_id and kind == 'notice' and state == 'pending':
            _write(conn, 'UPDATE queue_items SET revision=?,state=? WHERE item_id=?',
                   (revision + 1, 'retired', item_id))
            append_event(conn, item_id, revision + 1, 'retired', 'retired', principal, now_ms,
                         detail={'reason': 'project_undone', 'undo_job_id': undo_job_id})
            retired.append(item_id)
    conditions = _rows(conn, 'SELECT c.item_id,i.revision,i.state FROM queue_conditions c '
                       'JOIN queue_items i ON i.item_id=c.item_id '
                       'WHERE c.project_id=? AND c.closed_at_ms IS NULL ORDER BY i.raised_position,c.item_id',
                       (project_id,))
    for item_id, revision, state in conditions:
        _close_condition(conn, item_id, revision, state, principal, now_ms)
    retired_outbox = _write(conn, "UPDATE queue_notifications SET state='retired' WHERE state='pending' "
                            'AND item_id IN (SELECT item_id FROM queue_items WHERE project_id=?)', (project_id,))
    epoch = 1 + max(read_cursor(conn, project_id, source).epoch for source in ('question', 'lifecycle'))
    for source in ('question', 'lifecycle'):
        # The NULL-aware unique index owns project/source identity, including store-wide rows.
        _write(conn, 'INSERT INTO queue_highwaters (project_id,source,position,epoch,anchor_id) '
               'VALUES (?,?,0,?,NULL) ON CONFLICT DO UPDATE SET position=0,epoch=excluded.epoch,anchor_id=NULL',
               (project_id, source, epoch))
    return UndoQueueResult(tuple(retired), tuple(row[0] for row in conditions), retired_outbox, epoch)
