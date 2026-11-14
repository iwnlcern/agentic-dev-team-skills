"""Occupant-authenticated session jobs; the writer owns each transaction."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import sqlite3

from relay_engine.api_keys import verifier_matches
from relay_engine.api_protocol import SeatBinding, SessionOutcome, SessionRefusal
from relay_engine.api_writer import JobOutcome, StoreWriter, WriterUnavailable
from relay_engine.jcs import jcs_encode
from relay_engine.queries import end_session_step, record_session_step


class BindingNotCurrent(Exception):
    def __init__(self, reason: str):
        if reason not in {'key_replaced', 'seat_stood_down', 'session_not_current', 'session_unknown'}:
            raise ValueError('invalid_binding_reason')
        self.reason = reason
        super().__init__(reason)


class BindingUnavailable(Exception):
    """The supplied store connection could not establish a current binding."""


def _one(conn, sql, args):
    cursor = conn.execute(sql, args)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _session(conn, session_id):
    columns = ('project_id', 'team_instance_id', 'seat_id', 'occupant_id', 'key_id',
               'session_id', 'generation', 'signed_in_at', 'ended_at', 'occupant_ended_at', 'seat_state')
    row = _one(conn,
        'SELECT t.project_id,t.id,s.id,o.id,o.key_id,ss.id,ss.generation,ss.signed_in_at,'
        'ss.ended_at,o.ended_at,s.state FROM seat_sessions ss'
        ' JOIN seat_occupants o ON o.id=ss.occupant_id AND o.seat_id=ss.seat_id'
        ' JOIN seats s ON s.id=ss.seat_id JOIN team_instances t ON t.id=s.team_instance_id'
        ' WHERE ss.id=?', (session_id,))
    return dict(zip(columns, row)) if row else None


def _binding(row):
    return SeatBinding(*(row[k] for k in ('project_id', 'team_instance_id', 'seat_id',
        'occupant_id', 'key_id', 'session_id', 'generation')))


def current_binding(conn, session_id: str) -> SeatBinding:
    try:
        row = _session(conn, session_id)
    except sqlite3.Error:
        raise BindingUnavailable('store_unavailable') from None
    if row is None:
        raise BindingNotCurrent('session_unknown')
    if row['occupant_ended_at'] is not None:
        raise BindingNotCurrent('key_replaced')
    if row['seat_state'] == 'stood-down':
        raise BindingNotCurrent('seat_stood_down')
    if row['ended_at'] is not None:
        raise BindingNotCurrent('session_not_current')
    return _binding(row)


def request_digest(operation: str, args: Mapping) -> str:
    """The receipt scope carries the operation; its digest excludes the secret."""
    return hashlib.sha256(jcs_encode({k: v for k, v in args.items() if k != 'key_secret'})).hexdigest()


def _refuse(kind):
    code = {'operation_key_reused': 'E_API_CONFLICT', 'runtime_not_ready': 'E_API_UNAVAILABLE',
            'handler_fault': 'E_API_INTERNAL'}.get(kind, 'E_API_AUTH')
    return SessionRefusal(code, kind)


def _identity(row):
    result = {k: row[k] for k in ('project_id', 'team_instance_id', 'seat_id',
                                 'occupant_id', 'session_id')}
    result.update(generation=str(row['generation']), signed_in_at=str(row['signed_in_at']))
    return result


@dataclass(frozen=True, slots=True)
class _SessionJob:
    service: 'SessionService'
    operation: str
    args: Mapping
    binding: SeatBinding | None

    def run(self, conn):
        return self.service._run_job(conn, self.operation, self.args, self.binding)


class SessionService:
    def __init__(self, writer: StoreWriter, clock: Callable[[], int], *,
                 step_hook: Callable[[str], None] | None = None):
        self.writer = writer
        self.clock = clock
        self.step_hook = step_hook

    def run(self, operation: str, args: Mapping,
            binding: SeatBinding | None) -> SessionOutcome | SessionRefusal:
        try:
            outcome = self.writer.submit(_SessionJob(self, operation, dict(args), binding))
            if outcome.indeterminate:
                return _refuse('handler_fault')
            return outcome.value
        except WriterUnavailable:
            return _refuse('runtime_not_ready')
        except Exception:
            return _refuse('handler_fault')

    def _phase(self, name):
        if self.step_hook is not None:
            self.step_hook(name)

    def _run_job(self, conn, operation, args, binding):
        # All authentication and receipt checks run under the writer's write lock.
        row = _one(conn,
            'SELECT o.id,o.seat_id,o.key_verifier,o.ended_at,s.state'
            ' FROM seat_occupants o JOIN seats s ON s.id=o.seat_id WHERE o.key_id=?',
            (args['key_id'],))
        if row is None or not verifier_matches(row[2], args['key_id'], args['key_secret']):
            return JobOutcome(False, _refuse('key_rejected'))
        occupant, seat_id, _, ended_at, seat_state = row
        if ended_at is not None:
            return JobOutcome(False, _refuse('key_replaced'))
        if seat_state == 'stood-down':
            return JobOutcome(False, _refuse('seat_stood_down'))
        self._phase('checked')
        if operation not in {'session.sign-in', 'session.resume', 'session.end'}:
            raise ValueError('unknown_session_operation')
        now = self.clock()
        digest = request_digest(operation, args)
        if operation != 'session.resume':
            receipt = _one(conn,
                "SELECT request_digest,result FROM mutation_receipts WHERE principal_kind='occupant'"
                ' AND principal_id=? AND operation=? AND operation_key=?',
                (occupant, operation, args['operation_key']))
            if receipt is not None:
                if receipt[0] != digest:
                    return JobOutcome(False, _refuse('operation_key_reused'))
                result = json.loads(receipt[1])
                if operation == 'session.sign-in':
                    current = _session(conn, result['session_id'])
                    if current is None:
                        raise ValueError('receipt_session_missing')
                    result['session_state'] = 'open' if current['ended_at'] is None else 'ended'
                result['receipt'] = 'replayed'
                return JobOutcome(False, SessionOutcome(result, 'keep', None))
        if operation == 'session.sign-in':
            if binding is not None:
                return JobOutcome(False, _refuse('already_bound'))
            session_id = record_session_step(conn, args['host_session_id'], args['host'], seat_id, now)
            current = _session(conn, session_id)
            if current is None or current['occupant_id'] != occupant or current['seat_id'] != seat_id:
                raise ValueError('session_occupant_mismatch')
            self._phase('stepped')
            result = dict(_identity(current), session_state='open')
            action, new_binding = 'set', _binding(current)
        else:
            if operation == 'session.end' and binding is None:
                return JobOutcome(False, _refuse('not_signed_in'))
            current = _session(conn, args['session_id'])
            if (current is None or current['ended_at'] is not None
                    or current['occupant_id'] != occupant or current['seat_id'] != seat_id
                    or (binding is not None and binding != _binding(current))):
                return JobOutcome(False, _refuse('session_not_current'))
            if operation == 'session.resume':
                return JobOutcome(False, SessionOutcome(_identity(current), 'set', _binding(current)))
            end_session_step(conn, args['session_id'], now)
            self._phase('stepped')
            result = {'session_id': args['session_id'], 'ended_at': str(now)}
            action, new_binding = 'clear', None
        conn.execute('INSERT INTO mutation_receipts(principal_kind,principal_id,operation,'
            'operation_key,request_digest,result,committed_at) VALUES (?,?,?,?,?,?,?)',
            ('occupant', occupant, operation, args['operation_key'], digest,
             jcs_encode(result).decode('utf-8'), now)).close()
        return JobOutcome(True, SessionOutcome(dict(result, receipt='original'), action, new_binding))
