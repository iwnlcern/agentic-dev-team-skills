"""Caller-owned lifecycle appends and a snapshot reader.

start is the start settlement, which finishes or undoes.
running denotes "a void by the running writer of a number its owner had not yet voided".
stop is the graceful stop's void of the same.
resolve is Store's turn after the operator answers "no" at resolve-reservation;
it discards and never finishes. running, stop and resolve rows are settlement_undone.
The caller owns the transaction and rolls it back if any append raises.
"""
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Sequence

SEAT_SUBKINDS = ('seat_registered', 'seat_replaced', 'seat_stood_down')
SETTLEMENT_SUBKINDS = ('settlement_finished', 'settlement_undone')
RESTART_SUBKIND = 'engine_restart_requested'
INSTRUCTED_ACT_SUBKIND = 'operator_act_carried'
MERGE_ALARM_SUBKIND = 'merge_target_changed'
UNSEAT_SUBKIND = 'seat_unseated'
UNSEAT_SURFACES = ('terminal', 'page')
ACTOR_KINDS = ('occupant', 'operator', 'program')
SETTLEMENT_OUTCOMES = {'finished': 'settlement_finished', 'undone': 'settlement_undone'}
OCCASIONS = ('start', 'running', 'stop', 'resolve')


@dataclass(frozen=True)
class SettledEvent:
    number: int
    kind: str
    subject: str


@dataclass(frozen=True)
class SeatRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    actor_kind: str
    actor_occupant_id: str | None
    team_instance_id: str
    seat_id: str
    seat_address: str
    old_occupant_id: str | None
    new_occupant_id: str | None


@dataclass(frozen=True)
class SettlementRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    chain_id: str
    runtime_instance_id: str
    transaction_ordinal: int
    events: tuple[SettledEvent, ...]
    occasion: str


@dataclass(frozen=True)
class RestartRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    actor_occupant_id: str
    seat_id: str
    seat_address: str


@dataclass(frozen=True)
class InstructedActRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    act_kind: str
    seat_id: str
    seat_address: str
    instruction_relay_id: str
    chain_id: str
    event_number: int


@dataclass(frozen=True)
class MergeAlarmRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    slip_id: str
    seat_id: str
    seat_address: str
    pr: int
    merge_commit: str
    allowed_target: str
    landed_target: str


@dataclass(frozen=True)
class UnseatRow:
    position: int
    row_id: str
    subkind: str
    committed_at_ms: int
    seat_id: str
    seat_address: str
    generation: int
    surface: str | None
    carrying_seat_id: str | None
    carrying_seat_address: str | None
    instruction_relay_id: str | None


LifecycleRow = SeatRow | SettlementRow | RestartRow | InstructedActRow | MergeAlarmRow | UnseatRow


def _transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise ValueError('an append requires the caller transaction')


def _nonempty(*values: str | None) -> None:
    if any(value == '' for value in values):
        raise ValueError('an append argument is empty')


def _append(conn: sqlite3.Connection, *, project_id: str, subkind: str,
            committed_at_ms: int, actor_kind: str, **fields) -> int:
    # All column names come from the append functions below, never from callers.
    values = dict(project_id=project_id, row_id=uuid.uuid4().hex, subkind=subkind,
                  committed_at_ms=committed_at_ms, actor_kind=actor_kind, **fields)
    columns = ', '.join(values)
    placeholders = ', '.join('?' for _ in values)
    return conn.execute(
        'INSERT INTO lifecycle_outbox (position, ' + columns + ') '
        'SELECT coalesce(max(position), 0) + 1, ' + placeholders + ' '
        'FROM lifecycle_outbox WHERE project_id = ? RETURNING position',
        (*values.values(), project_id)).fetchone()[0]


def append_seat_row(ledger_conn: sqlite3.Connection, *, project_id: str, subkind: str,
                    seat_id: str, old_occupant_id: str | None, new_occupant_id: str | None,
                    actor_kind: str, actor_occupant_id: str | None,
                    committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    if actor_kind == 'program':
        raise ValueError('a seat row requires an occupant or operator actor')
    return _append(ledger_conn, project_id=project_id, subkind=subkind,
                   committed_at_ms=committed_at_ms, actor_kind=actor_kind,
                   actor_occupant_id=actor_occupant_id, seat_id=seat_id,
                   old_occupant_id=old_occupant_id, new_occupant_id=new_occupant_id)


def append_settlement_row(ledger_conn: sqlite3.Connection, *, project_id: str,
                          outcome: str, occasion: str, chain_id: str,
                          runtime_instance_id: str, transaction_ordinal: int,
                          events: Sequence[SettledEvent], committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    if outcome not in SETTLEMENT_OUTCOMES or occasion not in OCCASIONS:
        raise ValueError('unknown settlement outcome or occasion')
    if outcome == 'finished' and occasion != 'start':
        raise ValueError('a finished settlement requires start')
    if not events or any(a.number >= b.number for a, b in zip(events, events[1:])):
        raise ValueError('settlement events must be nonempty and strictly increasing')
    position = _append(ledger_conn, project_id=project_id,
                       subkind=SETTLEMENT_OUTCOMES[outcome], committed_at_ms=committed_at_ms,
                       actor_kind='program', chain_id=chain_id,
                       runtime_instance_id=runtime_instance_id,
                       transaction_ordinal=transaction_ordinal, occasion=occasion)
    for index, event in enumerate(events, 1):
        ledger_conn.execute(
            'INSERT INTO lifecycle_outbox_events '
            '(project_id, position, event_index, number, kind, subject) VALUES (?, ?, ?, ?, ?, ?)',
            (project_id, position, index, event.number, event.kind, event.subject))
    return position


def append_restart_row(ledger_conn: sqlite3.Connection, *, project_id: str,
                       seat_id: str, actor_occupant_id: str, committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    _nonempty(project_id, seat_id, actor_occupant_id)
    return _append(ledger_conn, project_id=project_id, subkind=RESTART_SUBKIND,
                   committed_at_ms=committed_at_ms, actor_kind='occupant',
                   actor_occupant_id=actor_occupant_id, seat_id=seat_id)


def append_instructed_act_row(ledger_conn: sqlite3.Connection, *, project_id: str,
                              act_kind: str, carrying_seat_id: str,
                              instruction_relay_id: str, chain_id: str,
                              event_number: int, committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    _nonempty(project_id, act_kind, carrying_seat_id, instruction_relay_id, chain_id)
    if event_number < 1:
        raise ValueError('an event number must be positive')
    return _append(ledger_conn, project_id=project_id, subkind=INSTRUCTED_ACT_SUBKIND,
                   committed_at_ms=committed_at_ms, actor_kind='operator',
                   seat_id=carrying_seat_id, act_kind=act_kind,
                   instruction_relay_id=instruction_relay_id, chain_id=chain_id,
                   event_number=event_number)


def append_merge_alarm_row(ledger_conn: sqlite3.Connection, *, project_id: str,
                           slip_id: str, holder_seat_id: str, pr: int,
                           merge_commit: str, allowed_target: str, landed_target: str,
                           committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    _nonempty(project_id, slip_id, holder_seat_id, merge_commit, allowed_target, landed_target)
    if pr < 1 or allowed_target == landed_target:
        raise ValueError('a merge alarm requires a positive PR and differing targets')
    if len(merge_commit) not in (40, 64) or any(c not in '0123456789abcdef' for c in merge_commit):
        raise ValueError("the merge's Git object id must be 40 or 64 lowercase hex characters")
    return _append(ledger_conn, project_id=project_id, subkind=MERGE_ALARM_SUBKIND,
                   committed_at_ms=committed_at_ms, actor_kind='program',
                   seat_id=holder_seat_id, slip_id=slip_id, pr=pr, merge_commit=merge_commit,
                   allowed_target=allowed_target, landed_target=landed_target)


def append_unseat_row(ledger_conn: sqlite3.Connection, *, project_id: str, seat_id: str,
                      generation: int, surface: str | None, carrying_seat_id: str | None,
                      instruction_relay_id: str | None, committed_at_ms: int) -> int:
    _transaction(ledger_conn)
    _nonempty(project_id, seat_id, surface, carrying_seat_id, instruction_relay_id)
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError('an unseat generation must be a positive integer')
    if surface is not None:
        if surface not in UNSEAT_SURFACES or carrying_seat_id is not None or instruction_relay_id is not None:
            raise ValueError('an unseat requires either a surface or an instruction')
    elif carrying_seat_id is None or instruction_relay_id is None:
        raise ValueError('an instructed unseat requires its carrying seat and relay')
    return _append(ledger_conn, project_id=project_id, subkind=UNSEAT_SUBKIND,
                   committed_at_ms=committed_at_ms, actor_kind='operator', seat_id=seat_id,
                   generation=generation, surface=surface, carrying_seat_id=carrying_seat_id,
                   instruction_relay_id=instruction_relay_id)


def read_after(ledger_conn: sqlite3.Connection, project_id: str, after_position: int,
               *, limit: int) -> tuple[LifecycleRow, ...]:
    """Read complete headers and their events in one ordered snapshot statement."""
    for value, minimum in ((after_position, 0), (limit, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError('invalid outbox cursor or limit')
    cursor = ledger_conn.execute('''
        WITH headers AS (
            SELECT * FROM lifecycle_outbox
            WHERE project_id = ? AND position > ? ORDER BY position LIMIT ?
        )
        SELECT h.*, s.team_instance_id, t.name || '.' || s.role AS seat_address,
               ct.name || '.' || cs.role AS carrying_seat_address,
               e.event_index, e.number, e.kind, e.subject
        FROM headers AS h
        LEFT JOIN seats AS s ON s.id = h.seat_id
        LEFT JOIN team_instances AS t ON t.id = s.team_instance_id
        LEFT JOIN seats AS cs ON cs.id = h.carrying_seat_id
        LEFT JOIN team_instances AS ct ON ct.id = cs.team_instance_id
        LEFT JOIN lifecycle_outbox_events AS e
          ON e.project_id = h.project_id AND e.position = h.position
        ORDER BY h.position, e.event_index
        ''', (project_id, after_position, limit))
    columns = tuple(column[0] for column in cursor.description)
    headers = {}
    events = {}
    for record in cursor:
        row = dict(zip(columns, record))
        position = row['position']
        headers[position] = row
        if row['event_index'] is not None:
            events.setdefault(position, []).append(SettledEvent(row['number'], row['kind'], row['subject']))
    result = []
    for position, row in headers.items():
        common = (position, row['row_id'], row['subkind'], row['committed_at_ms'])
        if row['subkind'] in SEAT_SUBKINDS:
            result.append(SeatRow(*common, row['actor_kind'], row['actor_occupant_id'],
                                  row['team_instance_id'], row['seat_id'], row['seat_address'],
                                  row['old_occupant_id'], row['new_occupant_id']))
        elif row['subkind'] in SETTLEMENT_SUBKINDS:
            result.append(SettlementRow(*common, row['chain_id'], row['runtime_instance_id'],
                                        row['transaction_ordinal'], tuple(events.get(position, ())),
                                        row['occasion']))
        elif row['subkind'] == RESTART_SUBKIND:
            result.append(RestartRow(*common, row['actor_occupant_id'], row['seat_id'], row['seat_address']))
        elif row['subkind'] == INSTRUCTED_ACT_SUBKIND:
            result.append(InstructedActRow(*common, row['act_kind'], row['seat_id'], row['seat_address'],
                                           row['instruction_relay_id'], row['chain_id'], row['event_number']))
        elif row['subkind'] == MERGE_ALARM_SUBKIND:
            result.append(MergeAlarmRow(*common, row['slip_id'], row['seat_id'], row['seat_address'],
                                        row['pr'], row['merge_commit'], row['allowed_target'], row['landed_target']))
        else:
            result.append(UnseatRow(*common, row['seat_id'], row['seat_address'], row['generation'],
                                    row['surface'], row['carrying_seat_id'], row['carrying_seat_address'],
                                    row['instruction_relay_id']))
    return tuple(result)
