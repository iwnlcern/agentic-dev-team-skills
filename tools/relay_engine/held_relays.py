"""Store's held-relay rules. Callers own the transaction and receiver proof."""

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
import hashlib
from typing import Literal


Decision = Literal['allowed', 'held', 'denied']


@dataclass(frozen=True)
class ReceiverFacts:
    route: Literal['active', 'not_active']
    stopped: bool
    seats: Literal['present', 'missing', 'unavailable', 'not_read']


@dataclass(frozen=True)
class Recipient:
    seat_id: str
    address: str
    role: Literal['to', 'cc']


@dataclass(frozen=True)
class State:
    state: str
    reason: str | None


@dataclass(frozen=True)
class TransferStatus:
    relay_id: str
    receiver_project_id: str
    body_sha256: str
    state: str
    reason: str | None
    changed_at: int


@dataclass(frozen=True)
class TransferKey:
    relay_id: str
    receiver_project_id: str


_TERMINAL = ('delivered', 'denied', 'returned')


def _require_decision(decision: Decision) -> None:
    if decision not in ('allowed', 'held', 'denied'):
        raise ValueError('unknown cross-project decision')


def _next_state(decision: Decision, facts: ReceiverFacts) -> State:
    if facts.route == 'not_active':
        return State('returned', 'destination_not_active')
    if facts.route != 'active':
        raise ValueError('unknown receiver route')
    if decision == 'denied':
        return State('denied', None)
    if decision == 'held':
        return State('held', None)
    if facts.stopped:
        return State('allowed', 'receiver_stopped')
    if facts.seats in ('unavailable', 'not_read'):
        return State('allowed', 'receiver_unavailable')
    if facts.seats == 'missing':
        return State('returned', 'recipient_not_found')
    if facts.seats == 'present':
        return State('delivered', None)
    raise ValueError('unknown receiver seat proof')


def _record_transition(conn, relay_id, receiver_project_id, digest, before, after, now_ms):
    if after.state == 'delivered':
        conn.execute(
            'INSERT INTO xproject_deliveries '
            '(relay_id,receiver_project_id,body_sha256,delivered_at) VALUES (?,?,?,?)',
            (relay_id, receiver_project_id, digest, now_ms),
        ).close()
    conn.execute(
        'INSERT INTO xproject_events '
        '(relay_id,receiver_project_id,from_state,to_state,reason,at) VALUES (?,?,?,?,?,?)',
        (relay_id, receiver_project_id, before, after.state, after.reason, now_ms),
    ).close()


def admit(conn, relay_id: str, receiver_project_id: str, body_sha256: str,
          recipients: Sequence[Recipient], decision: Decision,
          facts: ReceiverFacts, now_ms: int) -> State:
    """Insert one destination at its first state, using the same rule as step."""
    _require_decision(decision)
    first = _next_state(decision, facts)
    conn.execute(
        'INSERT INTO xproject_transfers '
        '(relay_id,receiver_project_id,body_sha256,state,reason,created_at,changed_at) '
        'VALUES (?,?,?,?,?,?,?)',
        (relay_id, receiver_project_id, body_sha256, first.state, first.reason, now_ms, now_ms),
    ).close()
    conn.executemany(
        'INSERT INTO xproject_recipients (relay_id,receiver_project_id,seat_id,address,role) '
        'VALUES (?,?,?,?,?)',
        ((relay_id, receiver_project_id, r.seat_id, r.address, r.role) for r in recipients),
    ).close()
    _record_transition(conn, relay_id, receiver_project_id, body_sha256, None, first, now_ms)
    return first


def step(conn, relay_id: str, receiver_project_id: str, decision: Decision,
         facts: ReceiverFacts, now_ms: int) -> State:
    _require_decision(decision)
    key = (relay_id, receiver_project_id)
    with closing(conn.execute(
        'SELECT state,reason,body_sha256 FROM xproject_transfers '
        'WHERE relay_id=? AND receiver_project_id=?', key,
    )) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ValueError('unknown transfer')
    before = State(row[0], row[1])
    if before.state in _TERMINAL:
        return before
    after = _next_state(decision, facts)
    if before == after:
        return before
    conn.execute(
        'UPDATE xproject_transfers SET state=?,reason=?,changed_at=? '
        'WHERE relay_id=? AND receiver_project_id=?',
        (after.state, after.reason, now_ms, *key),
    ).close()
    _record_transition(conn, *key, row[2], before.state, after, now_ms)
    return after


def mark_opened(conn, relay_id: str, receiver_project_id: str, seat_id: str, at_ms: int) -> bool:
    """Set first-open for a recipient already verified by the caller."""
    key = (relay_id, receiver_project_id, seat_id)
    where = 'WHERE relay_id=? AND receiver_project_id=? AND seat_id=?'
    with closing(conn.execute('SELECT opened_at FROM xproject_recipients ' + where, key)) as cursor:
        row = cursor.fetchone()
    if row is None:
        raise ValueError('unknown transfer recipient')
    if row[0] is not None:
        return False
    conn.execute('UPDATE xproject_recipients SET opened_at=? ' + where, (at_ms, *key)).close()
    return True


def status(conn, relay_id: str) -> tuple[TransferStatus, ...]:
    with closing(conn.execute(
        'SELECT relay_id,receiver_project_id,body_sha256,state,reason,changed_at '
        'FROM xproject_transfers WHERE relay_id=? ORDER BY receiver_project_id', (relay_id,),
    )) as cursor:
        return tuple(TransferStatus(*row) for row in cursor)


def non_terminal(conn, receiver_project_id: str | None = None) -> tuple[TransferKey, ...]:
    sql = "SELECT relay_id,receiver_project_id FROM xproject_transfers WHERE state IN ('held','allowed')"
    parameters = ()
    if receiver_project_id is not None:
        sql += ' AND receiver_project_id=?'
        parameters = (receiver_project_id,)
    with closing(conn.execute(sql + ' ORDER BY relay_id,receiver_project_id', parameters)) as cursor:
        return tuple(TransferKey(*row) for row in cursor)


AskKey = tuple[str, str]
Pin = str | int


@dataclass(frozen=True)
class DesiredAsk:
    key: AskKey
    body_sha256: str
    allow_revision: int
    pins: Mapping[str, Pin]


@dataclass(frozen=True)
class OpenAsk:
    item_id: str
    key: AskKey
    pins: Mapping[str, Pin]
    state: str


@dataclass(frozen=True)
class ClearOutcome:
    item_id: str
    outcome: Literal['cleared', 'closed', 'already_closed', 'clear_pending']


@dataclass(frozen=True)
class PlannedRaise:
    key: AskKey
    pins: Mapping[str, Pin]
    incarnation: int
    operation_key: str


@dataclass(frozen=True)
class RaiseOutcome:
    item_id: str
    replayed: bool
    operation_key: str


@dataclass(frozen=True)
class AskRow:
    key: AskKey
    incarnation: int
    last_item_id: str


@dataclass(frozen=True)
class DeliveredRecipient:
    relay_id: str
    receiver_project_id: str
    seat_id: str
    role: Literal['to', 'cc']
    delivered_at: int


@dataclass(frozen=True)
class InboxRow:
    source_project_id: str
    relay_id: str
    receiver_project_id: str
    seat_id: str
    role: Literal['to', 'cc']
    delivered_at: int

    def key(self) -> tuple[str, str, str, str]:
        return self.source_project_id, self.relay_id, self.receiver_project_id, self.seat_id


@dataclass(frozen=True)
class InboxPlan:
    inserts: tuple[InboxRow, ...]
    deletes: tuple[InboxRow, ...]


def desired_asks(conn, allow_revisions: Mapping[str, int],
                 restore_generation: str) -> tuple[DesiredAsk, ...]:
    result = []
    with closing(conn.execute(
        'SELECT relay_id,receiver_project_id,body_sha256 FROM xproject_transfers '
        "WHERE state='held' ORDER BY relay_id,receiver_project_id",
    )) as cursor:
        for relay_id, receiver, digest in cursor:
            revision = allow_revisions.get(receiver, 0)
            result.append(DesiredAsk((relay_id, receiver), digest, revision, {
                'relay_id': relay_id,
                'expected_body_sha256': digest,
                'expected_allow_revision': revision,
                'expected_restore_job': restore_generation,
            }))
    return tuple(result)


def delivered_recipients(conn) -> tuple[DeliveredRecipient, ...]:
    with closing(conn.execute(
        'SELECT r.relay_id,r.receiver_project_id,r.seat_id,r.role,d.delivered_at '
        'FROM xproject_deliveries d JOIN xproject_transfers t USING (relay_id,receiver_project_id) '
        'JOIN xproject_recipients r USING (relay_id,receiver_project_id) '
        "WHERE t.state='delivered' AND d.body_sha256=t.body_sha256 "
        'ORDER BY r.relay_id,r.receiver_project_id,r.seat_id',
    )) as cursor:
        return tuple(DeliveredRecipient(*row) for row in cursor)


def plan_clears(desired: Sequence[DesiredAsk], open_items: Sequence[OpenAsk]) -> tuple[OpenAsk, ...]:
    wanted = {ask.key: ask.pins for ask in desired}
    return tuple(item for item in open_items
                 if item.state not in ('pending', 'applying') or wanted.get(item.key) != item.pins)


def ask_operation_key(source_project_id: str, relay_id: str, receiver_project_id: str,
                      body_sha256: str, allow_revision: int, incarnation: int) -> str:
    fields = (source_project_id, relay_id, receiver_project_id, body_sha256,
              str(allow_revision), str(incarnation))
    return 'xpa2-' + hashlib.sha256('\0'.join(fields).encode('utf-8')).hexdigest()[:32]


def plan_raises(source_project_id: str, desired: Sequence[DesiredAsk],
                open_items: Sequence[OpenAsk], clears: Mapping[str, ClearOutcome],
                asks: Mapping[AskKey, AskRow]) -> tuple[PlannedRaise, ...]:
    open_keys = set()
    open_ids = set()
    for item in open_items:
        outcome = clears.get(item.item_id)
        if outcome is not None and outcome.item_id != item.item_id:
            raise ValueError('clear outcome names the wrong item')
        if outcome is None or outcome.outcome not in ('cleared', 'closed', 'already_closed'):
            open_keys.add(item.key)
            open_ids.add(item.item_id)
    result = []
    for want in desired:
        if want.key in open_keys:
            continue
        marker = asks.get(want.key)
        incarnation = 0 if marker is None else marker.incarnation + (marker.last_item_id not in open_ids)
        operation_key = ask_operation_key(
            source_project_id, *want.key, want.body_sha256, want.allow_revision, incarnation,
        )
        result.append(PlannedRaise(want.key, want.pins, incarnation, operation_key))
    return tuple(result)


def ask_rows_for(planned: Sequence[PlannedRaise], raised: Mapping[AskKey, RaiseOutcome],
                 open_before: frozenset[str]) -> tuple[AskRow, ...]:
    rows = []
    for raise_ in planned:
        outcome = raised.get(raise_.key)
        if outcome is None or outcome.operation_key != raise_.operation_key:
            raise ValueError('missing or mismatched raise outcome')
        if outcome.item_id in open_before:
            raise ValueError('raise returned a pre-open item')
        rows.append(AskRow(raise_.key, raise_.incarnation, outcome.item_id))
    return tuple(rows)


def plan_inbox(source_project_id: str, delivered: Sequence[DeliveredRecipient],
               inbox: Sequence[InboxRow]) -> InboxPlan:
    existing = {row.key(): row for row in inbox if row.source_project_id == source_project_id}
    desired = {}
    for recipient in delivered:
        row = InboxRow(source_project_id, recipient.relay_id, recipient.receiver_project_id,
                       recipient.seat_id, recipient.role, recipient.delivered_at)
        desired[row.key()] = row
    changed = {key for key in desired.keys() & existing.keys() if desired[key] != existing[key]}
    return InboxPlan(
        tuple(desired[key] for key in sorted((desired.keys() - existing.keys()) | changed)),
        tuple(existing[key] for key in sorted((existing.keys() - desired.keys()) | changed)),
    )


def apply_ask_rows(catalogue_conn, source_project_id: str, rows: Sequence[AskRow]) -> None:
    catalogue_conn.executemany(
        'INSERT INTO xproject_asks (source_project_id,relay_id,receiver_project_id,incarnation,last_item_id) '
        'VALUES (?,?,?,?,?) ON CONFLICT (source_project_id,relay_id,receiver_project_id) '
        'DO UPDATE SET incarnation=excluded.incarnation,last_item_id=excluded.last_item_id',
        ((source_project_id, *row.key, row.incarnation, row.last_item_id) for row in rows),
    ).close()


def apply_inbox(catalogue_conn, source_project_id: str, plan: InboxPlan) -> None:
    if any(row.source_project_id != source_project_id for row in (*plan.inserts, *plan.deletes)):
        raise ValueError('inbox plan crosses source projects')
    catalogue_conn.executemany(
        'DELETE FROM xproject_inbox WHERE source_project_id=? AND relay_id=? AND receiver_project_id=? AND seat_id=?',
        (row.key() for row in plan.deletes),
    ).close()
    catalogue_conn.executemany(
        'INSERT INTO xproject_inbox (source_project_id,relay_id,receiver_project_id,seat_id,role,delivered_at) '
        'VALUES (?,?,?,?,?,?)',
        ((*row.key(), row.role, row.delivered_at) for row in plan.inserts),
    ).close()
