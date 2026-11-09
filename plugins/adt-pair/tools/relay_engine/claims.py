"""Approval claims and reviewer verification, using admission metadata only."""
from dataclasses import dataclass

from . import errors, queries, submit

CLAIM_KINDS = ('design', 'plan')
CLAIM_CHECKS = {
    'design': ('approval names this exact document digest',
               'quoted operator answers match the operator relays'),
    'plan': ('approval names this exact plan digest and lineage',
             'independent completeness search with a known-positive control'),
}
CLAIM_WRITES = frozenset({'verification_requests'})


class AuthorityFilingFault(Exception):
    """Authority could not file its own verification request."""


@dataclass(frozen=True)
class ClaimLock:
    artifact: str
    snapshot_handle: int
    request_relay_id: str
    content_sha256: str


def edge_artifact(edge):
    """The snapshot artifact key an edge references, or None for a relay or parent edge."""
    if edge.kind != 'ref' or edge.target_type not in ('path', 'git'):
        return None
    return (edge.target_path if edge.target_type == 'path' else
            edge.target_repository_id + ':' + edge.target_path)


def claim_of(conn, admitted, draft, project_id):
    if draft.verdict != 'approve' or draft.kind not in ('DESIGN-REVIEW', 'PLAN-REVIEW'):
        return None
    locks = {}
    for edge in queries.points_to(conn, admitted.relay_id):
        artifact = edge_artifact(edge)
        if artifact is None:
            continue
        snap = queries.locked_snapshot(conn, artifact, project_id=project_id)
        if snap is not None and snap.locked_by == admitted.relay_id:
            locks[artifact] = ClaimLock(artifact, snap.handle, snap.relay_id, snap.content_sha256)
    return (draft.kind.partition('-')[0].lower(), tuple(locks.values())) if locks else None


def file_verification_request(conn, ctx, claim_relay_id, kind, locks, now_ms):
    if kind not in CLAIM_KINDS or not locks:
        raise ValueError('a verification needs a supported claim and its locks')
    team, bundle = conn.execute('SELECT r.team_instance_id, b.name FROM relays r'
                                ' JOIN bundles b ON b.id = r.bundle_id WHERE r.id = ?',
                                (claim_relay_id,)).fetchone()
    reviewer = reviewer_for(conn, team)
    address = conn.execute('SELECT t.name, s.role FROM seats s JOIN team_instances t'
                           ' ON t.id = s.team_instance_id WHERE s.id = ?', (reviewer,)).fetchone()
    refs = ['REF: ' + claim_relay_id]
    # Reuse the exact resolved edge spelling; a Git artifact key is not a REF spelling.
    for lock in locks:
        spelling = conn.execute('SELECT e.spelling FROM snapshots s JOIN edges e'
                                ' ON e.id = s.edge_id WHERE s.handle = ?',
                                (lock.snapshot_handle,)).fetchone()[0]
        refs.append('REF: ' + spelling)
    data = ('\n'.join(['KIND: REVIEW', 'TO: ' + address[0] + '.' + address[1],
                       'BUNDLE: ' + bundle, 'NEEDS: review',
                       'SUBJECT: ' + kind + ' claim verification', *refs]) +
            '\n\n' + '\n'.join(CLAIM_CHECKS[kind]) + '\n').encode('utf-8')
    try:
        request = submit.file_system_relay(conn, ctx, data)
    except errors.EngineError:
        raise AuthorityFilingFault() from None
    for lock in locks:
        conn.execute('INSERT INTO verification_requests (claim_relay_id, artifact, kind,'
                     ' snapshot_handle, snapshot_request_relay_id, content_sha256,'
                     ' request_relay_id, reviewer_seat_id, created_at)'
                     ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (claim_relay_id, lock.artifact, kind, lock.snapshot_handle,
                      lock.request_relay_id, lock.content_sha256, request.relay_id, reviewer, now_ms))
    return request.relay_id


def claim_locks(conn, claim_relay_id):
    with submit.read_guard(conn):
        return tuple(ClaimLock(*row) for row in conn.execute(
            'SELECT artifact, snapshot_handle, snapshot_request_relay_id, content_sha256'
            ' FROM verification_requests WHERE claim_relay_id = ? ORDER BY rowid',
            (claim_relay_id,)))


def verification_state(conn, claim_relay_id):
    with submit.read_guard(conn):
        rows = conn.execute('SELECT DISTINCT request_relay_id, reviewer_seat_id, kind'
                            ' FROM verification_requests WHERE claim_relay_id = ?',
                            (claim_relay_id,)).fetchall()
        if not rows:
            return 'pending'
        request, reviewer, kind = rows[0]
        if conn.execute('SELECT 1 FROM waivers WHERE claim_relay_id = ? AND kind = ?',
                        (claim_relay_id, kind)).fetchone():
            return 'waived'
        verdict = conn.execute("SELECT verdict FROM relays WHERE parent_id = ?"
                               " AND sender_seat_id = ? AND kind = 'REVIEW'"
                               " AND verdict IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                               (request, reviewer)).fetchone()
        if verdict is None:
            return 'pending'
        return 'approved' if verdict[0] == 'approve' else 'objected'


def reviewer_for(conn, team_instance_id):
    with submit.read_guard(conn):
        assigned = conn.execute('SELECT seat_id FROM reviewer_assignments WHERE team_instance_id = ?',
                                (team_instance_id,)).fetchone()
        if assigned:
            return assigned[0]
        row = conn.execute("SELECT s.id FROM seats s JOIN team_instances t ON t.id = s.team_instance_id"
                           " JOIN team_instances own ON own.project_id = t.project_id"
                           " WHERE own.id = ? AND s.role = 'orchestrator-reviewer'"
                           " AND t.closed_at IS NULL ORDER BY t.opened_at DESC LIMIT 1",
                           (team_instance_id,)).fetchone()
        if row is None:
            raise ValueError('the project has no reviewer')
        return row[0]


def assign_reviewer(conn, team_instance_id, seat_id, by_principal, now_ms):
    if conn.in_transaction:
        raise ValueError('assignment needs a clean connection')
    if by_principal != 'operator':
        raise ValueError('only the operator assigns reviewers')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with submit.read_guard(conn):
            row = conn.execute('SELECT t.project_id FROM team_instances t JOIN seats s'
                               ' JOIN team_instances st ON st.id = s.team_instance_id'
                               ' WHERE t.id = ? AND s.id = ? AND st.project_id = t.project_id',
                               (team_instance_id, seat_id)).fetchone()
            if row is None:
                raise ValueError('reviewer must belong to the same project')
            conn.execute('INSERT INTO reviewer_assignments (project_id, team_instance_id,'
                         ' seat_id, assigned_at) VALUES (?, ?, ?, ?) ON CONFLICT(team_instance_id)'
                         ' DO UPDATE SET seat_id = excluded.seat_id, assigned_at = excluded.assigned_at',
                         (row[0], team_instance_id, seat_id, now_ms))
            conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def waive(conn, claim_relay_id, kind, by_principal, words, now_ms):
    if conn.in_transaction:
        raise ValueError('waiver needs a clean connection')
    if by_principal != 'operator' or kind not in CLAIM_KINDS or not words:
        raise ValueError('only the operator waives a supported claim with recorded words')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with submit.read_guard(conn):
            if conn.execute('SELECT 1 FROM verification_requests WHERE claim_relay_id = ?'
                            ' AND kind = ?', (claim_relay_id, kind)).fetchone() is None:
                raise ValueError('the claim has no verification request')
            conn.execute('INSERT INTO waivers (claim_relay_id, kind, by_special, words, created_at)'
                         " VALUES (?, ?, 'operator', ?, ?)", (claim_relay_id, kind, words, now_ms))
            conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def on_admitted_claims(conn, admitted, draft, ctx):
    claim = claim_of(conn, admitted, draft, ctx.sender.project_id)
    if claim is not None:
        file_verification_request(conn, ctx, admitted.relay_id, *claim, ctx.now_ms())
