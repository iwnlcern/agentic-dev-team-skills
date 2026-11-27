"""Local, one-way cross-project permissions on the sending project's ledger."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
from typing import Literal

from . import held_relays, jcs, submit

__all__ = (
    'Decision', 'ApplyOutcome', 'ApplyRefused', 'decide', 'allow_revision',
    'restore_generation', 'release', 'answer', 'revoke_always', 'revoke_all_for_restore',
)

Decision = Literal['allowed', 'held', 'denied']


@dataclass(frozen=True)
class ApplyOutcome:
    status: Literal['applied', 'stale']
    receipt_id: str
    stale_pin: str | None


class ApplyRefused(Exception):
    def __init__(self, code: Literal['not-operator', 'surface-not-allowed', 'subject-unknown']):
        super().__init__(code)
        self.code = code


def _one(conn, sql, parameters=()):
    with closing(conn.execute(sql, parameters)) as cursor:
        return cursor.fetchone()


def decide(conn, relay_id, receiver_project_id, body_sha256) -> Decision:
    """Read eligibility, without consulting routes or existing deliveries."""
    decision = _one(conn, 'SELECT choice, body_sha256 FROM cross_project_decisions '
                         'WHERE relay_id=? AND receiver_project_id=?',
                    (relay_id, receiver_project_id))
    if decision is not None:
        if decision[0] == 'deny':
            return 'denied'
        if decision[0] == 'once' and decision[1] == body_sha256:
            return 'allowed'
    current = _one(conn, 'SELECT 1 FROM cross_project_allows '
                         'WHERE receiver_project_id=? AND revoked_at IS NULL', (receiver_project_id,))
    return 'allowed' if current is not None else 'held'


def allow_revision(conn, receiver_project_id) -> int:
    row = _one(conn, 'SELECT revision FROM cross_project_allows WHERE receiver_project_id=?',
               (receiver_project_id,))
    return 0 if row is None else row[0]


def restore_generation(conn, project_id) -> str:
    row = _one(conn, 'SELECT job_id FROM restore_cancels WHERE project_id=?', (project_id,))
    return '' if row is None else row[0]


def release(conn, target) -> bool:
    return _one(conn, 'SELECT 1 FROM xproject_deliveries d JOIN relays r ON r.id=d.relay_id '
                      'WHERE d.relay_id=? AND d.receiver_project_id=? AND d.body_sha256=? '
                      'AND r.project_id=?', (target.relay_id, target.destination_project_id,
                                             target.body_sha256, target.source_project_id)) is not None


def _require_turn(conn):
    if not conn.in_transaction:
        raise ValueError('permission change needs an open transaction')


def _lookup(conn, apply_key, arguments):
    _require_turn(conn)
    if not isinstance(apply_key, str) or '\x00' in apply_key or not 1 <= len(apply_key.encode('utf-8')) <= 256:
        raise ValueError('invalid permission apply key')
    arguments_sha256 = hashlib.sha256(jcs.jcs_encode(arguments)).hexdigest()
    previous = _one(conn, 'SELECT arguments_sha256,status,receipt_id,stale_pin '
                          'FROM cross_project_outcomes WHERE apply_key=?', (apply_key,))
    if previous is None:
        return arguments_sha256, None
    saved_sha256, *outcome = previous
    if saved_sha256 != arguments_sha256:
        raise ValueError('permission apply key has different arguments')
    return arguments_sha256, ApplyOutcome(*outcome)


def _record(conn, key, call, digest, now_ms, stale_pin=None):
    outcome = ApplyOutcome('applied' if stale_pin is None else 'stale',
                           submit.new_uuid7(now_ms), stale_pin)
    conn.execute(
        'INSERT INTO cross_project_outcomes '
        '(apply_key,call,arguments_sha256,status,stale_pin,receipt_id,recorded_at) '
        'VALUES (?,?,?,?,?,?,?)',
        (key, call, digest, outcome.status, stale_pin, outcome.receipt_id, now_ms),
    ).close()
    return outcome


def _require_operator(by_principal, surface, surfaces):
    if by_principal != 'operator':
        raise ApplyRefused('not-operator')
    if surface not in surfaces:
        raise ApplyRefused('surface-not-allowed')


def _status(conn, relay_id, receiver_project_id):
    return next((t for t in held_relays.status(conn, relay_id)
                 if t.receiver_project_id == receiver_project_id), None)


def answer(conn, relay_id, receiver_project_id, choice, by_principal, now_ms, *, surface,
           apply_key, expected_body_sha256, expected_allow_revision, expected_restore_job,
           facts) -> ApplyOutcome:
    """Apply one answer inside the caller's transaction or return its saved outcome."""
    digest, prior = _lookup(conn, apply_key, dict(
        relay_id=relay_id, receiver_project_id=receiver_project_id, choice=choice,
        by_principal=by_principal, surface=surface, apply_key=apply_key,
        expected_body_sha256=expected_body_sha256, expected_allow_revision=expected_allow_revision,
        expected_restore_job=expected_restore_job,
    ))
    if prior is not None:
        return prior
    _require_operator(by_principal, surface, ('page', 'frank_panel', 'terminal'))
    subject = _one(conn, 'SELECT project_id FROM relays WHERE id=?', (relay_id,))
    if subject is None:
        raise ApplyRefused('subject-unknown')
    if choice not in ('always', 'once', 'deny'):
        raise ValueError('invalid cross-project permission answer')

    pin = None
    if restore_generation(conn, subject[0]) != expected_restore_job:
        pin = 'source_restored'
    else:
        transfer = _status(conn, relay_id, receiver_project_id)
        if transfer is None or transfer.state != 'held':
            pin = 'relay_not_held'
        elif transfer.body_sha256 != expected_body_sha256:
            pin = 'body_changed'
        elif allow_revision(conn, receiver_project_id) != expected_allow_revision:
            pin = 'allow_changed'
        elif facts.route == 'not_active':
            pin = 'destination_unavailable'
    if pin is not None:
        return _record(conn, apply_key, 'answer', digest, now_ms, pin)

    if choice == 'always':
        conn.execute(
            'INSERT INTO cross_project_allows (receiver_project_id,revision,granted_at,granted_by) '
            "VALUES (?,1,?,'operator') ON CONFLICT (receiver_project_id) DO UPDATE SET "
            'revision=revision+1,granted_at=excluded.granted_at,granted_by=excluded.granted_by,'
            'revoked_at=NULL,revoked_by=NULL', (receiver_project_id, now_ms),
        ).close()
    else:
        conn.execute(
            'INSERT INTO cross_project_decisions '
            '(relay_id,receiver_project_id,body_sha256,choice,decided_at,decided_by) '
            "VALUES (?,?,?,?,?,'operator')",
            (relay_id, receiver_project_id, transfer.body_sha256, choice, now_ms),
        ).close()
    decision = decide(conn, relay_id, receiver_project_id, transfer.body_sha256)
    held_relays.step(conn, relay_id, receiver_project_id, decision, facts, now_ms)
    return _record(conn, apply_key, 'answer', digest, now_ms)


def _sender_project(conn):
    # Each production ledger belongs to exactly one sending project.
    with closing(conn.execute('SELECT id FROM projects LIMIT 2')) as cursor:
        projects = cursor.fetchall()
    if len(projects) != 1:
        raise ValueError('permission change needs a single-project ledger')
    return projects[0][0]


def revoke_always(conn, receiver_project_id, by_principal, now_ms, *, surface, apply_key,
                  expected_allow_revision, expected_restore_job, facts) -> ApplyOutcome:
    """Revoke a standing allow, stepping only its affected undelivered transfers."""
    digest, prior = _lookup(conn, apply_key, dict(
        receiver_project_id=receiver_project_id, by_principal=by_principal,
        surface=surface, apply_key=apply_key, expected_allow_revision=expected_allow_revision,
        expected_restore_job=expected_restore_job,
    ))
    if prior is not None:
        return prior
    _require_operator(by_principal, surface, ('page', 'terminal'))
    current = _one(conn, 'SELECT revision FROM cross_project_allows '
                         'WHERE receiver_project_id=? AND revoked_at IS NULL', (receiver_project_id,))
    if current is None:
        raise ApplyRefused('subject-unknown')
    if restore_generation(conn, _sender_project(conn)) != expected_restore_job:
        return _record(conn, apply_key, 'revoke_always', digest, now_ms, 'source_restored')
    if current[0] != expected_allow_revision:
        return _record(conn, apply_key, 'revoke_always', digest, now_ms, 'allow_changed')

    conn.execute(
        "UPDATE cross_project_allows SET revoked_at=?,revoked_by='operator',revision=revision+1 "
        'WHERE receiver_project_id=?', (now_ms, receiver_project_id),
    ).close()
    unread = held_relays.ReceiverFacts(facts.route, facts.stopped, 'not_read')
    keys = held_relays.non_terminal(conn, receiver_project_id)
    for key in sorted(keys, key=lambda item: item.relay_id):
        transfer = _status(conn, key.relay_id, receiver_project_id)
        if transfer.state == 'allowed' and decide(conn, key.relay_id, receiver_project_id,
                                                  transfer.body_sha256) == 'held':
            held_relays.step(conn, key.relay_id, receiver_project_id, 'held', unread, now_ms)
    return _record(conn, apply_key, 'revoke_always', digest, now_ms)


def revoke_all_for_restore(conn, now_ms) -> tuple[str, ...]:
    """Revoke every current allow in the staged ledger without stepping transfers."""
    _require_turn(conn)
    with closing(conn.execute(
        "UPDATE cross_project_allows SET revoked_at=?,revoked_by='operator',revision=revision+1 "
        'WHERE revoked_at IS NULL RETURNING receiver_project_id', (now_ms,),
    )) as cursor:
        return tuple(sorted(row[0] for row in cursor))
