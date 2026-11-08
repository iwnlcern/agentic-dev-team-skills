"""Authority value contracts, admission-time implement slips and act queries."""

from dataclasses import dataclass
from typing import Literal

from .guardrail import EFFECT_CLASSES


@dataclass(frozen=True)
class Answer:
    outcome: Literal["allow", "deny", "held"]
    code: str | None
    reason: str | None
    unblocker: str | None
    slip_id: str | None
    revision: int | None

    def __post_init__(self):
        if self.outcome not in ("allow", "deny", "held"):
            raise ValueError("unknown answer outcome")
        if self.outcome == "allow":
            if self.code is not None:
                raise ValueError("allow has no answer code")
        elif not all(isinstance(value, str) and value
                     for value in (self.code, self.reason, self.unblocker)):
            raise ValueError("deny and held require code, reason and unblocker")
        if (self.slip_id is None) != (self.revision is None):
            raise ValueError("slip id and revision must travel together")
        if self.slip_id is not None:
            if not isinstance(self.slip_id, str) or not self.slip_id:
                raise ValueError("slip id must be a nonempty string")
            if type(self.revision) is not int:
                raise ValueError("slip revision must be an integer")


@dataclass(frozen=True)
class Effect:
    kind: str
    repository_id: str | None
    ref: str | None = None
    pr: int | None = None
    head: str | None = None
    path: str | None = None

    def __post_init__(self):
        if self.kind not in EFFECT_CLASSES:
            raise ValueError("unknown effect kind")

import json
from pathlib import Path

from . import claims, queries, strings, submit

ANSWER_CODES = {
    'authority.' + code: ('deny' if code in ('slip-missing', 'slip-invalid', 'lineage-incomplete', 'not-issuer') else 'held')
    for code in ('slip-missing', 'slip-invalid', 'lineage-incomplete', 'not-issuer',
                 'plan-changed', 'design-changed', 'verification-pending', 'verification-objected', 'check-unavailable')
}
AFTER_ADMIT_WRITES = frozenset({'slips', 'slip_events', 'verification_requests'})
AFTER_ADMIT_READS_DENIED = frozenset({'bodies'})
_SLIP_FIELDS = ('id', 'handle', 'project_id', 'seat_id', 'bundle_id', 'repository_id',
                'issued_in', 'binds', 'state', 'revision', 'issuer_seat_id', 'issuer_special')
_SLIP_COLUMNS = ', '.join(_SLIP_FIELDS)


def _slip(row):
    return None if row is None else dict(zip(_SLIP_FIELDS, row))


def _seat(conn, seat_id):
    return conn.execute('SELECT s.team_instance_id, t.project_id, s.role, t.name, t.parent_instance_id'
                        ' FROM seats s JOIN team_instances t ON t.id = s.team_instance_id WHERE s.id = ?',
                        (seat_id,)).fetchone()


def _planner(conn, seat_id):
    row = conn.execute("SELECT t.name FROM seats s JOIN team_instances t ON t.id = s.team_instance_id"
                       " WHERE t.id = (SELECT team_instance_id FROM seats WHERE id = ?)"
                       " AND s.role = 'planner'", (seat_id,)).fetchone()
    return row[0] + '.planner' if row else 'operator'


def _orchestrator(conn, project_id):
    row = conn.execute("SELECT t.name FROM seats s JOIN team_instances t ON t.id = s.team_instance_id"
                       " WHERE t.project_id = ? AND s.role = 'orchestrator-planner'"
                       " AND t.closed_at IS NULL ORDER BY t.opened_at DESC LIMIT 1", (project_id,)).fetchone()
    return row[0] + '.orchestrator-planner' if row else 'operator'


def _answer(code, reason, unblocker, slip=None):
    return Answer(ANSWER_CODES['authority.' + code], 'authority.' + code, reason, unblocker,
                  slip['id'] if slip else None, slip['revision'] if slip else None)


def _allow(slip=None):
    return Answer('allow', None, None, None, slip['id'] if slip else None,
                  slip['revision'] if slip else None)


def _issuer(conn, principal, target):
    if principal == 'operator':
        return 'operator'
    sender = _seat(conn, principal)
    if sender is None or sender[1] != target[1]:
        return None
    if sender[2] == 'orchestrator-planner':
        return 'direct'
    if sender[2] == 'master-planner' and sender[0] == target[4]:
        return 'direct'
    if sender[2] == 'planner' and sender[0] == target[0]:
        return 'pair'
    return None


def _evidence(conn, relay_id, principal, holder):
    target = _seat(conn, holder)
    mode = None if target is None else _issuer(conn, principal, target)
    binds = {'claims': [], 'waived': []}
    if mode is None or target[2] != 'implementer':
        return 'not-issuer', binds
    walk = queries.lineage(conn, relay_id)
    if not walk or walk[-1].parent_id is not None:
        return 'lineage-long', binds
    if mode == 'pair':
        delegated = any(row.kind == 'PLAN' and row.needs == 'act'
                        and _issuer(conn, row.sender_seat_id, target) == 'direct'
                        and conn.execute("SELECT 1 FROM recipients WHERE relay_id = ?"
                                         " AND seat_id = ? AND role = 'to'",
                                         (row.id, principal)).fetchone()
                        for row in walk)
        if not delegated:
            return 'lineage-incomplete', binds
    seen_plan = False
    approved_design_requests = set()
    for row in walk:
        kind = {'PLAN-REVIEW': 'plan', 'DESIGN-REVIEW': 'design'}.get(row.kind)
        if kind is None or row.verdict != 'approve' or (kind == 'plan' and (seen_plan or row.sender_seat_id != holder)):
            continue
        locks = claims.claim_locks(conn, row.id)
        if not locks:
            continue
        if kind == 'plan':
            seen_plan = True
        else:
            approved_design_requests.update((lock.request_relay_id, lock.artifact) for lock in locks)
        binds['claims'].append({'kind': kind, 'claim': row.id, 'locks': [
            {'artifact': lock.artifact, 'snapshot_handle': lock.snapshot_handle,
             'request_relay_id': lock.request_relay_id, 'content_sha256': lock.content_sha256}
            for lock in locks]})
    if not seen_plan:
        if mode == 'operator':
            binds['waived'].append('plan-lock')
        else:
            return 'lineage-incomplete', binds
    for row in walk:
        if row.kind == 'DESIGN':
            for edge in queries.points_to(conn, row.id):
                if edge.kind != 'ref' or edge.target_type not in ('path', 'git'):
                    continue
                artifact = (edge.target_path if edge.target_type == 'path' else
                            edge.target_repository_id + ':' + edge.target_path)
                if (row.id, artifact) not in approved_design_requests:
                    if mode == 'operator':
                        binds['waived'].append('design-lock:' + artifact)
                    else:
                        return 'lineage-incomplete', binds
    if mode == 'operator':
        binds['waived'].append('issuer-preconditions')
        for claim in binds['claims']:
            if claims.verification_state(conn, claim['claim']) not in ('approved', 'waived'):
                binds['waived'].append('verification:' + claim['claim'])
    return None, binds


def on_admitted(conn, admitted, draft, ctx):
    claims.on_admitted_claims(conn, admitted, draft, ctx)
    if draft.kind != 'IMPL' or draft.needs != 'act':
        return
    targets = conn.execute("SELECT seat_id FROM recipients WHERE relay_id = ? AND role = 'to'",
                           (admitted.relay_id,)).fetchall()
    if len(targets) != 1 or targets[0][0] is None:
        return
    holder = targets[0][0]
    target = _seat(conn, holder)
    if target is None or target[1] != ctx.sender.project_id or target[2] != 'implementer':
        return
    failed, binds = _evidence(conn, admitted.relay_id, ctx.sender.seat_id, holder)
    if failed:
        return
    repo = ctx.git_state.repository_id if ctx.git_state else None
    if repo is None or conn.execute('SELECT 1 FROM repositories WHERE id = ? AND project_id = ?',
                                    (repo, ctx.sender.project_id)).fetchone() is None:
        return
    bundle = conn.execute('SELECT bundle_id FROM relays WHERE id = ?', (admitted.relay_id,)).fetchone()[0]
    handle = conn.execute('SELECT coalesce(max(handle), 0) + 1 FROM slips WHERE project_id = ?',
                          (ctx.sender.project_id,)).fetchone()[0]
    sid = submit.new_uuid7(admitted.created_at_ms)
    encoded = json.dumps(binds, sort_keys=True, separators=(',', ':'))
    conn.execute('INSERT INTO slips (id, handle, project_id, kind, seat_id, issuer_seat_id,'
                 ' issuer_special, bundle_id, repository_id, issued_in, binds, state, issued_at, revision)'
                 " VALUES (?, ?, ?, 'implement', ?, ?, ?, ?, ?, ?, ?, 'live', ?, 1)",
                 (sid, handle, ctx.sender.project_id, holder, *submit.party_columns(ctx.sender.seat_id),
                  bundle, repo, admitted.relay_id, encoded, admitted.created_at_ms))
    conn.execute('INSERT INTO slip_events (slip_id, revision, event, at, by_seat_id, by_special, detail)'
                 " VALUES (?, 1, 'issue', ?, ?, ?, ?)",
                 (sid, admitted.created_at_ms, *submit.party_columns(ctx.sender.seat_id), encoded))


def working_path(conn, artifact, effect):
    with submit.read_guard(conn):
        if artifact.startswith('file:'):
            artifact = artifact[5:]
        if Path(artifact).is_absolute():
            return artifact
        repository, marker, relative = artifact.partition(':')
        if not marker or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            return None
        if repository == effect.repository_id and effect.path:
            base = effect.path
        else:
            rows = conn.execute('SELECT path FROM repository_checkouts WHERE repository_id = ?',
                                (repository,)).fetchall()
            if len(rows) != 1:
                return None
            base = rows[0][0]
        if not Path(base).is_absolute():
            return None
        return str(Path(base) / relative)


def _checked_slip(conn, slip, effect, digest):
    planner = _planner(conn, slip['seat_id'])
    if slip['state'] != 'live':
        return _answer('slip-invalid', strings.render('authority-state', state=slip['state']), planner, slip)
    if slip['repository_id'] != effect.repository_id:
        return _answer('slip-invalid', strings.render('authority-repository', repository=slip['repository_id']), planner, slip)
    principal = slip['issuer_seat_id'] or slip['issuer_special']
    failed, _ = _evidence(conn, slip['issued_in'], principal, slip['seat_id'])
    if failed:
        code = 'not-issuer' if failed == 'not-issuer' else 'lineage-incomplete'
        reason = (strings.render('authority-lineage-long', relay=slip['issued_in'])
                  if failed == 'lineage-long' else
                  strings.render('authority-lineage', relay=slip['issued_in']))
        return _answer(code, reason, planner, slip)
    binds = json.loads(slip['binds'])
    for claim in binds['claims']:
        for lock in claim['locks']:
            path = working_path(conn, lock['artifact'], effect)
            if path is None or digest(path) != lock['content_sha256']:
                return _answer(claim['kind'] + '-changed',
                               strings.render('authority-artifact', artifact=lock['artifact']), planner, slip)
        if 'verification:' + claim['claim'] in binds['waived']:
            continue
        state = claims.verification_state(conn, claim['claim'])
        if state not in ('approved', 'waived'):
            row = conn.execute('SELECT reviewer_seat_id FROM verification_requests WHERE claim_relay_id = ? LIMIT 1',
                               (claim['claim'],)).fetchone()
            reviewer = _seat(conn, row[0]) if row else None
            unblocker = reviewer[3] + '.' + reviewer[2] if reviewer else 'operator'
            code = 'verification-pending' if state == 'pending' else 'verification-objected'
            return _answer(code, strings.render('authority-verification', kind=claim['kind'], state=state),
                           unblocker if state == 'pending' else planner, slip)
    return _allow(slip)


def _act(conn, seat_id, effect, now_ms, digest):
    seat = _seat(conn, seat_id)
    if seat is None:
        raise ValueError('unknown seat')
    root = _orchestrator(conn, seat[1])
    if effect.kind in ('pr-merge', 'api-ref-write'):
        return _answer('slip-missing', strings.render('authority-no-merge'), root)
    if effect.kind != 'commit':
        return _answer('check-unavailable', strings.render('authority-guardrail'), root)
    if seat[2] != 'implementer':
        return _allow()
    if not conn.in_transaction:
        raise ValueError('act needs the caller transaction')
    if not effect.repository_id or not effect.ref:
        return _answer('slip-invalid', strings.render('authority-context'), _planner(conn, seat_id))
    binding = conn.execute('SELECT seat_id, bundle_id FROM branch_bindings WHERE repository_id = ? AND branch = ?',
                           (effect.repository_id, effect.ref)).fetchone()
    if binding and binding[0] != seat_id:
        other = _slip(conn.execute('SELECT ' + _SLIP_COLUMNS + ' FROM slips WHERE seat_id = ? AND bundle_id = ?'
                                  " AND kind = 'implement' ORDER BY issued_at DESC, handle DESC LIMIT 1",
                                  binding).fetchone())
        return _answer('slip-invalid', strings.render('authority-seat', seat=binding[0]), root, other)
    params = [seat_id]
    sql = 'SELECT ' + _SLIP_COLUMNS + " FROM slips WHERE seat_id = ? AND kind = 'implement'"
    if binding:
        sql += ' AND bundle_id = ? AND repository_id = ?'
        params += [binding[1], effect.repository_id]
    else:
        # Prefer a live slip for this repository; an unusable slip still has one meaning.
        sql += ' ORDER BY (repository_id = ?) DESC, (state = \'live\') DESC, issued_at DESC, handle DESC LIMIT 1'
        params.append(effect.repository_id)
    if binding:
        sql += " ORDER BY (state = 'live') DESC, issued_at DESC, handle DESC LIMIT 1"
    slip = _slip(conn.execute(sql, params).fetchone())
    if slip:
        answer = _checked_slip(conn, slip, effect, digest)
        if answer.outcome == 'allow' and binding is None:
            conn.execute('INSERT INTO branch_bindings (repository_id, branch, seat_id, bundle_id, bound_at)'
                         ' VALUES (?, ?, ?, ?, ?)',
                         (effect.repository_id, effect.ref, seat_id, slip['bundle_id'], now_ms))
        return answer
    if binding:
        return _answer('slip-invalid', strings.render('authority-binding'), root)
    row = conn.execute("SELECT r.id, coalesce(r.sender_seat_id, r.sender_special) FROM relays r"
                       " JOIN recipients c ON c.relay_id = r.id WHERE r.project_id = ?"
                       " AND r.kind = 'IMPL' AND r.needs = 'act' AND c.role = 'to' AND c.seat_id = ?"
                       ' ORDER BY r.created_at DESC LIMIT 1', (seat[1], seat_id)).fetchone()
    if row:
        failed, _ = _evidence(conn, row[0], row[1], seat_id)
        code = 'not-issuer' if failed == 'not-issuer' else 'lineage-incomplete'
        reason = (strings.render('authority-lineage-long', relay=row[0])
                  if failed == 'lineage-long' else
                  strings.render('authority-lineage', relay=row[0]))
        return _answer(code, reason, _planner(conn, seat_id))
    return _answer('slip-missing', strings.render('authority-missing'), _planner(conn, seat_id))


def act(conn, seat_id, effect, now_ms, *, digest):
    try:
        with submit.read_guard(conn):
            return _act(conn, seat_id, effect, now_ms, digest)
    except Exception:
        return _answer('check-unavailable', strings.render('authority-fault'), 'operator')


def revoke(conn, slip_id, by_principal, reason, now_ms):
    if conn.in_transaction:
        raise ValueError('revocation needs a clean connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with submit.read_guard(conn):
            slip = _slip(conn.execute('SELECT ' + _SLIP_COLUMNS + ' FROM slips WHERE id = ?', (slip_id,)).fetchone())
            if slip is None:
                answer = _answer('slip-invalid', strings.render('authority-missing'), 'operator')
            else:
                actor = _seat(conn, by_principal)
                issuer = slip['issuer_seat_id'] or slip['issuer_special']
                permitted = (by_principal in ('operator', issuer) or
                             (actor is not None and actor[1] == slip['project_id'] and actor[2] == 'orchestrator-planner'))
                if not permitted:
                    answer = _answer('not-issuer', strings.render('authority-not-issuer'),
                                     _orchestrator(conn, slip['project_id']), slip)
                elif slip['state'] != 'live':
                    answer = _answer('slip-invalid', strings.render('authority-state', state=slip['state']),
                                     _planner(conn, slip['seat_id']), slip)
                else:
                    conn.execute("UPDATE slips SET state = 'revoked', ended_at = ?, end_reason = 'revoked',"
                                 ' revision = revision + 1 WHERE id = ?', (now_ms, slip_id))
                    conn.execute('INSERT INTO slip_events (slip_id, revision, event, at, by_seat_id, by_special, detail)'
                                 " VALUES (?, ?, 'revoke', ?, ?, ?, ?)",
                                 (slip_id, slip['revision'] + 1, now_ms, *submit.party_columns(by_principal), reason))
                    slip['revision'] += 1
                    answer = _allow(slip)
            conn.execute('COMMIT')
            return answer
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
