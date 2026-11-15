"""Approval claims and reviewer verification, using admission metadata only."""
from dataclasses import dataclass, replace

from . import errors, header, queries, refs, strings, submit

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
    # A plain REVIEW's approve can pin bytes, but never files a D1 or D2 claim.
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
    address = None
    if reviewer is not None:
        team_name, role = conn.execute(
            'SELECT t.name, s.role FROM seats s JOIN team_instances t'
            ' ON t.id = s.team_instance_id WHERE s.id = ?', (reviewer,)).fetchone()
        address = team_name + '.' + role
        system = submit.SeatRef(submit.SYSTEM, submit.SYSTEM, submit.SYSTEM,
                                ctx.sender.team_instance_id, ctx.sender.project_id)
        try:
            resolved = ctx.directory.resolve(address, system)
        except header.AddressRefused:
            resolved = None
        if resolved is None or resolved.seat_id != reviewer:
            reviewer = None
    request_id = None
    if reviewer is not None:
        references = ['REF: ' + claim_relay_id]
        for lock in locks:
            spelling, reference_type, commit = conn.execute(
                'SELECT e.spelling, e.target_type, e.target_commit FROM snapshots s'
                ' JOIN edges e ON e.id = s.edge_id WHERE s.handle = ?',
                (lock.snapshot_handle,)).fetchone()
            if reference_type == 'git':
                spelling = refs.render_ref(replace(refs.parse_ref(spelling), rev=commit))
            references.append('REF: ' + spelling)
        body = list(CLAIM_CHECKS[kind])
        body.extend(strings.render('authority-claim-lock', artifact=lock.artifact,
                                   digest=lock.content_sha256) for lock in locks)
        data = ('\n'.join(['KIND: REVIEW', 'TO: ' + address,
                           'BUNDLE: ' + bundle, 'NEEDS: review',
                           'SUBJECT: ' + kind + ' claim verification', *references]) +
                '\n\n' + '\n'.join(body) + '\n').encode('utf-8')
        try:
            request_id = submit.file_system_relay(conn, ctx, data).relay_id
        except errors.EngineError:
            raise AuthorityFilingFault() from None
    for lock in locks:
        conn.execute('INSERT INTO verification_requests (claim_relay_id, artifact, kind,'
                     ' snapshot_handle, snapshot_request_relay_id, content_sha256,'
                     ' request_relay_id, reviewer_seat_id, created_at)'
                     ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (claim_relay_id, lock.artifact, kind, lock.snapshot_handle,
                      lock.request_relay_id, lock.content_sha256, request_id, reviewer, now_ms))
    return request_id


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
        if request is None or reviewer is None:
            return 'pending'
        verdict = conn.execute("SELECT verdict FROM relays WHERE parent_id = ?"
                               " AND sender_seat_id = ? AND kind = 'REVIEW'"
                               " AND verdict IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                               (request, reviewer)).fetchone()
        if verdict is None:
            return 'pending'
        return 'approved' if verdict[0] == 'approve' else 'objected'


@dataclass(frozen=True)
class StandingObjection:
    review_relay_id: str
    claim_relay_id: str
    verdict: str


def standing_objection(conn, project_id: str, artifact: str,
                       content_sha256: str) -> StandingObjection | None:
    """Newest standing reviewer objection over the project's same locked bytes."""
    with submit.read_guard(conn):
        verdicts = conn.execute(
            'SELECT v.claim_relay_id, v.reviewer_seat_id, r.id, r.verdict, r.handle'
            ' FROM verification_requests v JOIN relays c ON c.id = v.claim_relay_id'
            ' LEFT JOIN relays r ON r.parent_id = v.request_relay_id'
            " AND r.sender_seat_id = v.reviewer_seat_id AND r.kind = 'REVIEW'"
            ' AND r.verdict IS NOT NULL'
            ' WHERE c.project_id = ? AND v.artifact = ? AND v.content_sha256 = ?',
            (project_id, artifact, content_sha256)).fetchall()
        waivers = conn.execute(
            'SELECT w.claim_relay_id, w.after_handle FROM waivers w'
            ' JOIN verification_requests v ON v.claim_relay_id = w.claim_relay_id'
            ' AND v.kind = w.kind JOIN relays c ON c.id = v.claim_relay_id'
            ' WHERE c.project_id = ? AND v.artifact = ? AND v.content_sha256 = ?',
            (project_id, artifact, content_sha256)).fetchall()
    waived = {claim for claim, _ in waivers}
    frontier = max((after_handle for _, after_handle in waivers), default=0)
    newest = {}
    for claim, reviewer, review, verdict, handle in verdicts:
        if claim in waived or review is None:
            continue
        if reviewer not in newest or handle > newest[reviewer][0]:
            newest[reviewer] = (handle, review, claim, verdict)
    standing = [row for row in newest.values() if row[3] != 'approve' and row[0] > frontier]
    if not standing:
        return None
    _, review, claim, verdict = max(standing)
    return StandingObjection(review, claim, verdict)


def reviewer_can_answer(conn, seat_id: str | None) -> bool:
    """Whether the recorded reviewer is a standing seat of an open team."""
    if seat_id is None:
        return False
    with submit.read_guard(conn):
        return conn.execute(
            'SELECT 1 FROM seats s JOIN team_instances t ON t.id = s.team_instance_id'
            " WHERE s.id = ? AND s.state != 'stood-down' AND t.closed_at IS NULL",
            (seat_id,)).fetchone() is not None


def reviewer_for(conn, team_instance_id):
    with submit.read_guard(conn):
        own = conn.execute('SELECT project_id FROM team_instances WHERE id = ?',
                           (team_instance_id,)).fetchone()
        if own is None:
            return None
        eligible = conn.execute(
            'SELECT s.id, s.team_instance_id, s.role FROM seats s'
            ' JOIN team_instances t ON t.id = s.team_instance_id'
            " WHERE t.project_id = ? AND t.closed_at IS NULL AND s.state != 'stood-down'",
            (own[0],)).fetchall()
        assigned = conn.execute('SELECT seat_id FROM reviewer_assignments WHERE team_instance_id = ?',
                                (team_instance_id,)).fetchone()
        if assigned:
            return assigned[0] if any(row[0] == assigned[0] for row in eligible) else None
        candidates = {team: seat for seat, team, role in eligible if role == 'orchestrator-reviewer'}
        current, seen = team_instance_id, set()
        while current is not None and current not in seen:
            seen.add(current)
            if current in candidates:
                return candidates[current]
            parent = conn.execute('SELECT parent_instance_id FROM team_instances'
                                  ' WHERE id = ? AND project_id = ?', (current, own[0])).fetchone()
            current = parent[0] if parent else None
        return next(iter(candidates.values())) if len(candidates) == 1 else None


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
            if not reviewer_can_answer(conn, seat_id):
                raise ValueError('reviewer must be a seat that can answer')
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
            claim = conn.execute(
                'SELECT r.project_id FROM verification_requests v JOIN relays r'
                ' ON r.id = v.claim_relay_id WHERE v.claim_relay_id = ? AND v.kind = ?',
                (claim_relay_id, kind)).fetchone()
            if claim is None:
                raise ValueError('the claim has no verification row')
            after_handle = conn.execute(
                'SELECT coalesce(max(handle), 0) FROM relays WHERE project_id = ?',
                (claim[0],)).fetchone()[0]
            conn.execute('INSERT INTO waivers (claim_relay_id, kind, by_special, words,'
                         ' created_at, after_handle)'
                         " VALUES (?, ?, 'operator', ?, ?, ?)",
                         (claim_relay_id, kind, words, now_ms, after_handle))
            conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def on_admitted_claims(conn, admitted, draft, ctx):
    claim = claim_of(conn, admitted, draft, ctx.sender.project_id)
    if claim is not None:
        file_verification_request(conn, ctx, admitted.relay_id, *claim, ctx.now_ms())
