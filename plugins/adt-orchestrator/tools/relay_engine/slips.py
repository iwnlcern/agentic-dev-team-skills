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
from types import MappingProxyType

from . import claims, header, queries, restore_cancel, strings, submit

# Erratum 3 r2 B3: a commit's answer by the committing seat's role word; a role absent
# from the table answers slip-missing and never allow.
COMMIT_ROLE_RULE = MappingProxyType({
    'planner': 'planner', 'orchestrator-planner': 'planner',
    'master-planner': 'planner', 'domain-planner': 'planner',
    'implementer': 'slip',
    'reviewer': 'slip', 'orchestrator-reviewer': 'slip',
    'master-reviewer': 'slip', 'domain-reviewer': 'slip',
})
_REVIEW_KINDS = {kind: {'PLAN-REVIEW': 'plan', 'DESIGN-REVIEW': 'design'}.get(kind, 'plain')
                 for kind, verdicts in header.VERDICTS.items() if 'approve' in verdicts}


def git_digest_algorithm(object_id):
    """The working-copy algorithm for a recorded Git object id (erratum 4 r2 R4).

    Engine records only 40-digit ids today, since every `git` REF resolves through
    rules.xroot_repository_and_commit, which refuses a commit that is not 40
    lowercase hex; a 64-digit id is a SHA-256 repository's.
    """
    if len(object_id) == 40:
        return 'git-sha1'
    if len(object_id) == 64:
        return 'git-sha256'
    raise ValueError('a Git object id has 40 or 64 digits')

ANSWER_CODES = {
    'authority.' + code: ('deny' if code in ('slip-missing', 'slip-invalid', 'lineage-incomplete', 'not-issuer') else 'held')
    for code in ('slip-missing', 'slip-invalid', 'lineage-incomplete', 'not-issuer',
                 'plan-changed', 'design-changed', 'verification-pending', 'verification-objected', 'check-unavailable', 'restore-held')
}
ANSWER_CODES.update({
    'authority.' + code: ('held' if code in ('project-stopped', 'session-not-current') else 'deny')
    for code in ('project-stopped', 'cross-project-commit', 'session-not-current',
                 'protected-ref-push', 'api-merge-refused', 'merge-head-unpinned',
                 'merge-flag-refused', 'hook-bypass', 'hook-disable', 'key-file-read',
                 'operator-endpoint', 'operator-only', 'checkout-unregistered')
})
AFTER_ADMIT_WRITES = frozenset({'slips', 'slip_events', 'verification_requests', 'restore_refusals'})
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


def project_stopped_answer() -> Answer:
    return _answer('project-stopped', strings.render('error-authority-project-stopped-cause'),
                   'operator')


UNAVAILABLE_CODES = (
    'no-service', 'connect-refused', 'deadline', 'fault', 'seat-found-unavailable', 'unanswerable',
)
NOT_CURRENT_CAUSES = (
    'ended', 'not-occupant', 'stood-down', 'team-closed', 'record-other-seat',
    'record-without-row', 'project-unreadable',
)


def unavailable_words(code: str) -> tuple[str, str]:
    if code not in UNAVAILABLE_CODES:
        raise KeyError('authority-unavailable-' + code + '-reason')
    return tuple(strings.render('authority-unavailable-' + code + '-' + part)
                 for part in ('reason', 'remedy'))


def session_not_current_words(cause: str, project_id: str | None) -> tuple[str, str]:
    # Unknown causes are never parameter errors.
    if (cause not in NOT_CURRENT_CAUSES
            or 'authority-not-current-' + cause + '-reason' not in strings.INVENTORY):
        raise KeyError(cause)
    if (cause == 'project-unreadable') != (project_id is not None):
        raise ValueError('only project-unreadable takes a project id')
    params = {'project': project_id} if cause == 'project-unreadable' else {}
    return tuple(strings.render('authority-not-current-' + cause + '-' + part, **params)
                 for part in ('reason', 'remedy'))


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


def _references(conn, relay_id):
    """(artifact, reference type, recorded digest) for each artifact edge of a relay, in order."""
    found = []
    for edge in queries.points_to(conn, relay_id):
        artifact = claims.edge_artifact(edge)
        if artifact is not None:
            found.append((artifact, edge.target_type, edge.digest_at_submit))
    return found


def _decide(conn, walk, holder):
    """Erratum 6 r5's sections D and R over the walk, oldest first: each referenced path's
    newest reference, the design paths, each decided path's state, ('locked', claim relay,
    kind, lock) or ('objected', review relay, request relay, the deciding review's kind), and
    the issuance block, (review relay, request relay) of a holder refusal that names no path
    under a parent holding no snapshot with no later deciding approve, or None, and each
    undecided path's binding pin, the content an approve locked it at (R-E)."""
    oldest_first = walk[::-1]
    kinds = {row.id: row.kind for row in oldest_first}
    references = {row.id: _references(conn, row.id) for row in oldest_first}
    verification_replies = {row.id for row in oldest_first if row.kind == 'REVIEW'
                            and conn.execute('SELECT 1 FROM verification_requests'
                                             ' WHERE request_relay_id = ?',
                                             (row.parent_id,)).fetchone()}
    held_cache = {}
    def held_by(relay_id):
        if relay_id not in held_cache:
            held = {}
            for artifact, content in conn.execute('SELECT artifact, content_sha256 FROM snapshots'
                                                  ' WHERE relay_id = ?', (relay_id,)):
                held.setdefault(artifact, set()).add(content)
            held_cache[relay_id] = held
        return held_cache[relay_id]
    def qualifies(row):
        # Erratum 5 r4 E1: a verdict, sent by the holder, answering a DESIGN relay.
        return (row.kind == 'DESIGN-REVIEW' and row.verdict is not None and row.sender_seat_id == holder
                and kinds.get(row.parent_id) == 'DESIGN')
    def reaches(row):
        # D: the held paths a refusal names, or every held path when it names none of them (the
        # names-none fallback, marked True), and each path it names that its parent does not hold.
        held, named = held_by(row.parent_id), {}
        for artifact, _, digest in references[row.id]:
            named.setdefault(artifact, digest)
        inside = [artifact for artifact in named if artifact in held]
        out = {artifact: (held[artifact], False) for artifact in inside}
        if held and not inside:
            out.update((artifact, (contents, True)) for artifact, contents in held.items())
        out.update((artifact, ({digest}, False)) for artifact, digest in named.items() if artifact not in held)
        return out
    def run(design_paths):
        state, block, fallback_design = {}, None, set()
        for row in oldest_first:
            kind = _REVIEW_KINDS.get(row.kind)
            # A review request carries no verdict and decides nothing (erratum 4 r2 R1).
            if kind is None or row.verdict is None or row.id in verification_replies:
                continue
            if row.verdict == 'approve':
                # D1: the holder's PLAN-REVIEW approve of a plan path; D2: a qualifying DESIGN-REVIEW approve.
                if kind == 'plan' and row.sender_seat_id == holder:
                    source = 'plan'
                elif qualifies(row):
                    source = 'design'
                else:
                    continue
                decided = False
                for lock in claims.claim_locks(conn, row.id):
                    if source == 'plan' and lock.artifact in design_paths:
                        continue
                    decided = True
                    current = state.get(lock.artifact)
                    # R-B: an objection a DESIGN-REVIEW set, or one on a design path, is lifted only by D2.
                    if (current and current[0] == 'objected' and source != 'design'
                            and (current[3] == 'design' or lock.artifact in design_paths)):
                        continue
                    if current and current[0] == 'locked' and current[3].content_sha256 == lock.content_sha256:
                        reviewer = conn.execute(
                            'SELECT reviewer_seat_id FROM verification_requests'
                            ' WHERE claim_relay_id = ? LIMIT 1', (current[1],)).fetchone()
                        verification = claims.verification_state(conn, current[1])
                        recoverable = (verification == 'pending'
                                       and not claims.reviewer_can_answer(conn, reviewer[0] if reviewer else None))
                        old_handle, cut = conn.execute(restore_cancel.CLAIM_CUT_SQL, (current[1],)).fetchone()
                        restored = (restore_cancel.claim_is_cut(cut, old_handle)
                                    and not restore_cancel.claim_is_cut(cut, row.handle)
                                    and verification != 'waived')
                        if not (recoverable or restored):
                            continue  # keep recorded verdicts/waivers and answerable pending claims
                    state[lock.artifact] = ('locked', row.id, source, lock)
                if decided:
                    block = None
                continue
            # D3: a refusal decides what it reaches when the holder sent it or it is a DESIGN-REVIEW
            # from any seat (R-C); another seat's PLAN-REVIEW decides nothing.
            if row.sender_seat_id != holder and kind != 'design':
                continue
            if row.sender_seat_id == holder and not held_by(row.parent_id) and not references[row.id]:
                block = (row.id, row.parent_id)
            for artifact, (contents, fallback) in sorted(reaches(row).items()):
                current = state.get(artifact)
                # R-D: only a path the names-none fallback reached at its locked bytes is left alone.
                if fallback and current and current[0] == 'locked' and contents == {current[3].content_sha256}:
                    continue
                # A later objection replaces one a DESIGN-REVIEW set only when it is a DESIGN-REVIEW.
                if current and current[0] == 'objected' and current[3] == 'design' and kind != 'design':
                    continue
                state[artifact] = ('objected', row.id, row.parent_id, kind)
                if fallback and qualifies(row):
                    fallback_design.add(artifact)
        return state, block, fallback_design
    # R-A: a design path is one a qualifying DESIGN-REVIEW decided.  Its fallback objections and
    # the locks they leave alone depend on each other, so the least fixed point is taken.
    design_paths = set()
    for row in oldest_first:
        if qualifies(row):
            if row.verdict == 'approve':
                design_paths |= {lock.artifact for lock in claims.claim_locks(conn, row.id)}
            else:
                design_paths |= {artifact for artifact, (_, fallback) in reaches(row).items() if not fallback}
    while True:
        state, block, fallback_design = run(design_paths)
        if fallback_design <= design_paths:
            break
        design_paths |= fallback_design
    newest = {}
    for row in oldest_first:
        for artifact, reference, digest in references[row.id]:
            newest[artifact] = (reference, digest)
    # R-E: an approve's lock that decides nothing pins the path; an undecided path binds at its
    # newest pin by the holder, else at its newest pin by any seat (erratum 6 r5).
    found = {}
    for row in oldest_first:
        if (_REVIEW_KINDS.get(row.kind) is None or row.verdict != 'approve'
                or row.id in verification_replies):
            continue
        for artifact, content in conn.execute('SELECT artifact, content_sha256 FROM snapshots'
                                              ' WHERE locked_by = ?', (row.id,)):
            found.setdefault(artifact, {})['holder' if row.sender_seat_id == holder else 'other'] = content
    pins = {artifact: by.get('holder', by.get('other')) for artifact, by in found.items() if artifact not in state}
    return newest, design_paths, state, block, pins


def _evidence(conn, relay_id, principal, holder):
    """(failure, binds, objected) for an IMPL relay: failure is None, 'not-issuer',
    'lineage-long' or 'lineage-incomplete'; objected names (artifact, review, request)
    when an objected path is why, and (None, review, request) when an issuance block is."""
    target = _seat(conn, holder)
    mode = None if target is None else _issuer(conn, principal, target)
    binds = {'claims': [], 'digests': [], 'waived': []}
    if mode is None or target[2] != 'implementer':
        return 'not-issuer', binds, None
    walk = queries.lineage(conn, relay_id)
    if not walk or walk[-1].parent_id is not None:
        return 'lineage-long', binds, None
    if mode == 'pair':
        delegated = any(row.kind == 'PLAN' and row.needs == 'act'
                        and _issuer(conn, row.sender_seat_id, target) == 'direct'
                        and conn.execute("SELECT 1 FROM recipients WHERE relay_id = ?"
                                         " AND seat_id = ? AND role = 'to'",
                                         (row.id, principal)).fetchone()
                        for row in walk)
        if not delegated:
            return 'lineage-incomplete', binds, None
    newest, design_paths, state, block, pins = _decide(conn, walk, holder)
    grouped = {}
    for artifact, (reference, digest) in newest.items():
        current = state.get(artifact)
        if current and current[0] == 'locked':
            _, claim, kind, lock = current
            grouped.setdefault(claim, {'kind': kind, 'claim': claim, 'locks': []})['locks'].append(
                {'artifact': lock.artifact, 'snapshot_handle': lock.snapshot_handle,
                 'request_relay_id': lock.request_relay_id, 'content_sha256': lock.content_sha256})
            continue
        if current:
            if mode != 'operator':
                return 'lineage-incomplete', binds, (artifact, current[1], current[2])
            binds['waived'].append('objected:' + artifact)
        if not current and artifact in pins:
            # R-E: an undecided path binds at its binding pin's locked content, as a plan path.
            reference, digest = 'path', pins[artifact]
        binds['digests'].append({'artifact': artifact, 'kind': 'design' if artifact in design_paths else 'plan',
                                 'reference': reference, 'digest': digest})
    binds['claims'] = list(grouped.values())
    # R-B: a holder refusal naming no path under a parent with no snapshot blocks issuance.
    if block is not None:
        if mode != 'operator':
            return 'lineage-incomplete', binds, (None, block[0], block[1])
        binds['waived'].append('blocked:' + block[0])
    # Erratum 3 r2 B2: digests alone never issue a slip.
    if not any(claim['kind'] == 'plan' for claim in binds['claims']):
        if mode == 'operator':
            binds['waived'].append('plan-lock')
        else:
            return 'lineage-incomplete', binds, None
    if mode == 'operator':
        # Erratum 5 r4 E2: issuance preconditions only; act checks every claim's verification,
        # and the operator waives one through claims.waive.
        binds['waived'].append('issuer-preconditions')
    return None, binds, None


def _lineage_answer(failed, objected, relay, unblocker, slip=None):
    if failed == 'lineage-long':
        return _answer('lineage-incomplete', strings.render('authority-lineage-long', relay=relay), unblocker, slip)
    if objected is not None and objected[0] is None:
        return _answer('lineage-incomplete', strings.render('authority-blocked', relay=relay, review=objected[1],
                                                            request=objected[2]),
                       unblocker, slip)
    if objected is not None:
        return _answer('lineage-incomplete', strings.render('authority-objected', relay=relay, artifact=objected[0],
                                                            review=objected[1], request=objected[2]),
                       unblocker, slip)
    return _answer('not-issuer' if failed == 'not-issuer' else 'lineage-incomplete',
                   strings.render('authority-lineage', relay=relay), unblocker, slip)


def _open_principal(conn, principal):
    return principal == 'operator' or conn.execute(
        'SELECT 1 FROM seats s JOIN team_instances t ON t.id = s.team_instance_id'
        ' WHERE s.id = ? AND t.closed_at IS NULL', (principal,)).fetchone() is not None


def _open_delegation(conn, relay_id, principal, target):
    return any(row.kind == 'PLAN' and row.needs == 'act'
               and _issuer(conn, row.sender_seat_id, target) == 'direct'
               and _open_principal(conn, row.sender_seat_id)
               and conn.execute("SELECT 1 FROM recipients WHERE relay_id = ?"
                                " AND seat_id = ? AND role = 'to'",
                                (row.id, principal)).fetchone()
               for row in queries.lineage(conn, relay_id))


def _supersede(conn, sid: str, seat_id: str, bundle_id: str, repository_id: str,
               at_ms: int, party: tuple) -> list[str]:
    previous = conn.execute(
        "SELECT id, revision FROM slips WHERE seat_id = ? AND bundle_id = ?"
        " AND repository_id = ? AND kind = 'implement' AND state = 'live' AND id != ?"
        ' ORDER BY issued_at, handle', (seat_id, bundle_id, repository_id, sid)).fetchall()
    detail = json.dumps({'superseded_by': sid}, sort_keys=True, separators=(',', ':'))
    for old_id, revision in previous:
        conn.execute("UPDATE slips SET state = 'ended', end_reason = 'superseded', ended_at = ?,"
                     ' revision = revision + 1 WHERE id = ?', (at_ms, old_id))
        conn.execute('INSERT INTO slip_events (slip_id, revision, event, at, by_seat_id, by_special, detail)'
                     " VALUES (?, ?, 'end', ?, ?, ?, ?)",
                     (old_id, revision + 1, at_ms, *party, detail))
    return [old_id for old_id, _ in previous]


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
    failed, binds, _ = _evidence(conn, admitted.relay_id, ctx.sender.seat_id, holder)
    if failed:
        return
    if not _open_principal(conn, ctx.sender.seat_id):
        return
    if (_issuer(conn, ctx.sender.seat_id, target) == 'pair'
            and not _open_delegation(conn, admitted.relay_id, ctx.sender.seat_id, target)):
        return
    repo = conn.execute('SELECT repository_id FROM relays WHERE id = ?',
                        (admitted.relay_id,)).fetchone()[0]
    if repo is None or conn.execute('SELECT 1 FROM repositories WHERE id = ? AND project_id = ?',
                                    (repo, ctx.sender.project_id)).fetchone() is None:
        return
    bundle = conn.execute('SELECT bundle_id FROM relays WHERE id = ?', (admitted.relay_id,)).fetchone()[0]
    hold = restore_cancel.held(conn, ctx.sender.project_id)
    if hold is not None and _issuer(conn, ctx.sender.seat_id, target) != 'operator':
        restore_cancel.record_refusal(conn, admitted.relay_id, ctx.sender.project_id,
                                      holder, bundle, repo, hold.job_id)
        return
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
    _supersede(conn, sid, holder, bundle, repo, admitted.created_at_ms,
               submit.party_columns(ctx.sender.seat_id))


def working_path(conn, artifact, effect):
    with submit.read_guard(conn):
        if artifact.startswith('file:'):
            artifact = artifact[5:]
        if Path(artifact).is_absolute():
            return artifact
        repository, marker, relative = artifact.partition(':')
        if not marker or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
            return None
        if repository == effect.repository_id:
            # The commit's own worktree is its repository's working copy (M1).
            if not effect.path:
                return None
            base = effect.path
        else:
            rows = conn.execute('SELECT path FROM repository_checkouts WHERE repository_id = ? AND ended_at IS NULL',
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
    failed, _, objected = _evidence(conn, slip['issued_in'], principal, slip['seat_id'])
    if failed:
        return _lineage_answer(failed, objected, slip['issued_in'], planner, slip)
    binds = json.loads(slip['binds'])
    for claim in binds['claims']:
        for lock in claim['locks']:
            path = working_path(conn, lock['artifact'], effect)
            if path is None or digest(path) != lock['content_sha256']:
                return _answer(claim['kind'] + '-changed',
                               strings.render('authority-artifact', artifact=lock['artifact']), planner, slip)
    checked_digests = []
    for bound in binds['digests']:
        path = working_path(conn, bound['artifact'], effect)
        recorded = bound['digest']
        if path is None or recorded is None:
            now = content = None
        elif bound['reference'] == 'git':
            observed = digest(path, (git_digest_algorithm(recorded), 'sha256'))
            now, content = (None, None) if observed is None else observed
        else:
            now = content = digest(path)
        if now is None or now != recorded or content is None:
            return _answer(bound['kind'] + '-changed',
                           strings.render('authority-artifact', artifact=bound['artifact']), planner, slip)
        checked_digests.append((bound, content))
    for bound, content in checked_digests:
        objection = claims.standing_objection(conn, slip['project_id'], bound['artifact'], content)
        if objection is not None:
            unblocker = 'operator' if objection.verdict == 'human-decision-required' else planner
            return _answer('verification-objected',
                           strings.render('authority-verification', kind=bound['kind'],
                                          state='objected', relay=objection.review_relay_id),
                           unblocker, slip)
    for claim in binds['claims']:
        for lock in claim['locks']:
            objection = claims.standing_objection(
                conn, slip['project_id'], lock['artifact'], lock['content_sha256'])
            if objection is not None:
                unblocker = 'operator' if objection.verdict == 'human-decision-required' else planner
                return _answer('verification-objected',
                               strings.render('authority-verification', kind=claim['kind'],
                                              state='objected', relay=objection.review_relay_id),
                               unblocker, slip)
        state = claims.verification_state(conn, claim['claim'])
        if state == 'pending':
            row = conn.execute('SELECT reviewer_seat_id FROM verification_requests WHERE claim_relay_id = ? LIMIT 1',
                               (claim['claim'],)).fetchone()
            reviewer = _seat(conn, row[0]) if row and row[0] is not None else None
            if not claims.reviewer_can_answer(conn, row[0] if row else None):
                reviewer = None
            unblocker = reviewer[3] + '.' + reviewer[2] if reviewer else 'operator'
            if reviewer is None:
                reason = strings.render('authority-no-reviewer', relay=claim['claim'])
            else:
                reason = strings.render('authority-verification', kind=claim['kind'],
                                        state=state, relay=claim['claim'])
            return _answer('verification-pending', reason, unblocker, slip)
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
    rule = COMMIT_ROLE_RULE.get(seat[2])
    if rule == 'planner':
        return _allow()
    if rule is None:
        return _answer('slip-missing', strings.render('authority-missing'), _planner(conn, seat_id))
    if not conn.in_transaction:
        raise ValueError('act needs the caller transaction')
    # S1: only a seat that holds a slip of the kind is answered from slips.
    if conn.execute("SELECT 1 FROM slips WHERE seat_id = ? AND kind = 'implement' LIMIT 1",
                    (seat_id,)).fetchone() is None:
        refused = restore_cancel.held_refusal(conn, seat_id, None, effect.repository_id, 0)
        if refused:
            return _answer('restore-held', strings.render('authority-restore-held',
                           job=refused.job_id, relay=refused.relay_id), 'operator')
        return _diagnostic(conn, seat, seat_id)
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
    if slip is None or slip['state'] != 'live':
        after = conn.execute('SELECT handle FROM relays WHERE id = ?',
                             (slip['issued_in'],)).fetchone()[0] if slip else 0
        refused = restore_cancel.held_refusal(conn, seat_id, binding[1] if binding else None,
                                              effect.repository_id, after)
        if refused:
            return _answer('restore-held', strings.render('authority-restore-held',
                           job=refused.job_id, relay=refused.relay_id), 'operator', slip)
    if slip:
        answer = _checked_slip(conn, slip, effect, digest)
        if answer.outcome == 'allow' and binding is None:
            conn.execute('INSERT INTO branch_bindings (repository_id, branch, seat_id, bundle_id, bound_at)'
                         ' VALUES (?, ?, ?, ?, ?)',
                         (effect.repository_id, effect.ref, seat_id, slip['bundle_id'], now_ms))
        return answer
    # The seat holds a slip of the kind, so only a bound branch with no slip for its bundle is left.
    return _answer('slip-invalid', strings.render('authority-binding'), root)


def _diagnostic(conn, seat, seat_id):
    """Line 148's answer for a seat holding no implement slip: its newest IMPL, else slip-missing."""
    planner = _planner(conn, seat_id)
    row = conn.execute("SELECT r.id, coalesce(r.sender_seat_id, r.sender_special) FROM relays r"
                       " JOIN recipients c ON c.relay_id = r.id WHERE r.project_id = ?"
                       " AND r.kind = 'IMPL' AND c.role = 'to' AND c.seat_id = ?"
                       " AND (r.needs = 'act' OR EXISTS"
                       ' (SELECT 1 FROM imported_relays i WHERE i.relay_id = r.id))'
                       ' ORDER BY r.created_at DESC LIMIT 1', (seat[1], seat_id)).fetchone()
    if row:
        failed, _, objected = _evidence(conn, row[0], row[1], seat_id)
        if failed:
            return _lineage_answer(failed, objected, row[0], planner)
        targets = conn.execute("SELECT seat_id FROM recipients WHERE relay_id = ? AND role = 'to'",
                               (row[0],)).fetchall()
        repository = conn.execute('SELECT repository_id FROM relays WHERE id = ?',
                                  (row[0],)).fetchone()[0]
        if len(targets) != 1 or targets[0][0] != seat_id:
            reason = strings.render('authority-missing-target')
        elif repository is None:
            reason = strings.render('authority-missing-repo')
        elif conn.execute('SELECT 1 FROM repositories WHERE id = ? AND project_id = ?',
                          (repository, seat[1])).fetchone() is None:
            reason = strings.render('authority-missing-repository')
        elif not _open_principal(conn, row[1]):
            reason = strings.render('authority-missing-issuer')
        elif (_issuer(conn, row[1], seat) == 'pair'
              and not _open_delegation(conn, row[0], row[1], seat)):
            reason = strings.render('authority-missing-delegation')
        else:
            reason = strings.render('authority-missing-admission')
        return _answer('slip-missing', reason, planner)
    return _answer('slip-missing', strings.render('authority-missing'), planner)


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
                permitted = (by_principal == 'operator' or
                             (_open_principal(conn, by_principal) and
                              (by_principal == issuer or
                               (actor is not None and actor[1] == slip['project_id']
                                and actor[2] == 'orchestrator-planner'))))
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
