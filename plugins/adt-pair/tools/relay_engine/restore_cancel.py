"""Project restore cancellation, approval cuts and operator-only release."""
from dataclasses import dataclass

from . import submit

CUT_SQL = 'SELECT cut_handle FROM restore_cancels WHERE project_id = ?'


@dataclass(frozen=True)
class Cancelled:
    revoked: tuple[str, ...]
    cut_handle: int
    already: bool


@dataclass(frozen=True)
class Held:
    job_id: str
    cut_handle: int
    held_at: int


def cut_of(conn, project_id: str) -> int | None:
    with submit.read_guard(conn):
        row = conn.execute(CUT_SQL, (project_id,)).fetchone()
    return row[0] if row else None


def held(conn, project_id: str) -> Held | None:
    with submit.read_guard(conn):
        row = conn.execute('SELECT job_id, cut_handle, held_at FROM restore_cancels'
                           ' WHERE project_id = ? AND released_at IS NULL', (project_id,)).fetchone()
    return Held(*row) if row else None


def cancel_all(conn, project_id: str, job_id: str, now_ms: int) -> Cancelled:
    if conn.in_transaction:
        raise ValueError('cancellation needs a clean connection')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with submit.read_guard(conn):
            if conn.execute('SELECT 1 FROM projects WHERE id = ?', (project_id,)).fetchone() is None:
                raise ValueError('unknown project')
            previous = conn.execute('SELECT job_id, cut_handle, released_at FROM restore_cancels'
                                    ' WHERE project_id = ?', (project_id,)).fetchone()
            if previous and previous[0] == job_id and previous[2] is None:
                result = Cancelled((), previous[1], True)
            else:
                cut = conn.execute('SELECT coalesce(max(handle), 0) FROM relays WHERE project_id = ?',
                                   (project_id,)).fetchone()[0]
                cut = max(cut, previous[1] if previous else 0)
                live = conn.execute("SELECT id, revision FROM slips WHERE project_id = ? AND state = 'live'"
                                    ' ORDER BY issued_at, handle', (project_id,)).fetchall()
                for sid, revision in live:
                    conn.execute("UPDATE slips SET state = 'revoked', ended_at = ?, end_reason = 'revoked',"
                                 ' revision = revision + 1 WHERE id = ?', (now_ms, sid))
                    conn.execute('INSERT INTO slip_events'
                                 ' (slip_id, revision, event, at, by_seat_id, by_special, detail)'
                                 " VALUES (?, ?, 'revoke', ?, NULL, 'operator', ?)",
                                 (sid, revision + 1, now_ms, 'restore:' + job_id))
                conn.execute('INSERT INTO restore_cancels (project_id, job_id, cut_handle, held_at)'
                             ' VALUES (?, ?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET'
                             ' job_id=excluded.job_id, cut_handle=excluded.cut_handle,'
                             ' held_at=excluded.held_at, released_at=NULL, released_by=NULL',
                             (project_id, job_id, cut, now_ms))
                result = Cancelled(tuple(sid for sid, _ in live), cut, False)
            conn.execute('COMMIT')
            return result
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


def release(conn, project_id: str, by_principal: str, now_ms: int) -> None:
    if conn.in_transaction:
        raise ValueError('release needs a clean connection')
    if by_principal != 'operator':
        raise ValueError('operator only')
    conn.execute('BEGIN IMMEDIATE')
    try:
        with submit.read_guard(conn):
            if held(conn, project_id) is None:
                raise ValueError('no restore hold')
            conn.execute("UPDATE restore_cancels SET released_at = ?, released_by = 'operator'"
                         ' WHERE project_id = ?', (now_ms, project_id))
            conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise


CLAIM_CUT_SQL = (
    'SELECT r.handle, c.cut_handle FROM relays r LEFT JOIN restore_cancels c'
    ' ON c.project_id = r.project_id WHERE r.id = ?')
CUT_VERDICTS_SQL = (
    'SELECT r.verdict, r.handle, EXISTS(SELECT 1 FROM imported_relays i WHERE i.relay_id = r.id)'
    ' FROM relays r WHERE r.parent_id = ? AND r.sender_seat_id = ?'
    " AND r.kind = 'REVIEW' AND r.verdict IS NOT NULL ORDER BY r.created_at DESC")


def claim_is_cut(cut: int | None, claim_handle: int) -> bool:
    return cut is not None and claim_handle <= cut


def approve_counts(cut: int | None, claim_handle: int, verdict_handle: int, imported: bool) -> bool:
    return cut is None or (not claim_is_cut(cut, claim_handle)
                           and not (imported and verdict_handle > cut))


def marks(conn, claim_ids, review_ids) -> tuple[dict[str, int], frozenset[str]]:
    ids = tuple(sorted(set(claim_ids) | set(review_ids)))
    if not ids:
        return {}, frozenset()
    with submit.read_guard(conn):
        rows = conn.execute('SELECT r.id, r.handle, EXISTS(SELECT 1 FROM imported_relays i'
                            ' WHERE i.relay_id = r.id) FROM relays r WHERE r.id IN ('
                            + ','.join('?' for _ in ids) + ')', ids).fetchall()
    claims = set(claim_ids)
    reviews = set(review_ids)
    return ({rid: handle for rid, handle, _ in rows if rid in claims},
            frozenset(rid for rid, _, imported in rows if rid in reviews and imported))
