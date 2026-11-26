"""Store's held-relay rules. Callers own the transaction and receiver proof."""

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
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
