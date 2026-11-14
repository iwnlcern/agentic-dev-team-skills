"""Read-only P9 evaluation with one finite ledger snapshot."""
from dataclasses import dataclass
import sqlite3

from relay_engine import queries
from relay_engine.operator_queue import records


@dataclass(frozen=True)
class Unavailable:
    reason: str


def p9_flags(catalogue_conn, ledger_conn, project_id: str, now_ms: int, **unopened_flags_options):
    waiting = None
    highwater = 0
    anchor = None
    catalogue_started = False
    try:
        if catalogue_conn.in_transaction:
            raise sqlite3.OperationalError('catalogue transaction already open')
        catalogue_conn.execute('BEGIN').close()
        catalogue_started = True
        cursor = records.read_cursor(catalogue_conn, project_id, 'question')
        highwater, anchor = cursor.position, cursor.anchor_id
        waiting = frozenset(row[0] for row in records._rows(catalogue_conn,
            "SELECT asking_seat_id FROM queue_items WHERE project_id=? AND kind='question' "
            "AND state IN ('pending','applying') AND asking_seat_id IS NOT NULL", (project_id,)))
    except (sqlite3.Error, records.JobUnavailable):
        waiting = None
    finally:
        if catalogue_started:
            try:
                catalogue_conn.rollback()
            except sqlite3.Error:
                waiting = None
    ledger_started = False
    try:
        if ledger_conn.in_transaction:
            return Unavailable('ledger transaction already open')
        ledger_conn.execute('BEGIN').close()
        ledger_started = True
        if waiting is not None:
            if highwater > 0:
                row = queries.operator_questions(ledger_conn, project_id, highwater - 1, 1)
                if not row or row[0].handle != highwater or row[0].relay_id != anchor:
                    highwater = 0
            after = highwater
            while True:
                rows = queries.operator_questions(ledger_conn, project_id, after, 100)
                if not rows:
                    break
                waiting = waiting | frozenset(row.sender_seat_id for row in rows if row.sender_seat_id is not None)
                after = rows[-1].handle
        flags = queries.unopened_flags(ledger_conn, project_id, now_ms,
            operator_waiting=waiting, **unopened_flags_options)
        ledger_conn.rollback()
        ledger_started = False
        return flags
    except (sqlite3.Error, records.JobUnavailable) as exc:
        return Unavailable(str(exc))
    finally:
        if ledger_started:
            try:
                ledger_conn.rollback()
            except sqlite3.Error:
                pass
