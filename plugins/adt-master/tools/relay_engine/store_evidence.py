"""Content-addressed evidence on the caller's project-ledger transaction."""
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
import zlib

from relay_engine import queries, store_layout, submit

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

CODEC_WRITE = 'zstd' if _zstd is not None else 'zlib'
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
RETENTION_PAGE_ROWS = 500
UPLOAD_IDLE_MS = 24 * 60 * 60 * 1000
RETENTION_AGE_MS = 14 * 24 * 60 * 60 * 1000
_SHA = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class Capture:
    source: str
    captured_by: str
    party_seat_id: str | None
    party_special: str | None
    bundle_id: str | None


@dataclass(frozen=True)
class EvidenceRead:
    data: bytes
    offset: int
    bytes_len: int


def _one(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    try:
        return cursor.fetchone()
    finally:
        cursor.close()


def _execute(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    try:
        return cursor.rowcount
    finally:
        cursor.close()


def _all(conn, sql, parameters=()):
    cursor = conn.execute(sql, parameters)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def _digest(value):
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError('expected a lowercase sha256')
    return value


def _sync_file(file):
    file.flush()
    if sys.platform == 'darwin':
        fcntl.fcntl(file.fileno(), fcntl.F_FULLFSYNC)
    else:
        os.fsync(file.fileno())


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        if sys.platform == 'darwin':
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
        else:
            os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir(path):
    if path.is_dir():
        return
    _mkdir(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    _sync_dir(path.parent)


def _object_path(root, project_id, sha, codec):
    _digest(sha)
    if codec not in ('raw', 'zstd', 'zlib'):
        raise submit.EvidenceUnavailable('unsupported-codec')
    return store_layout.evidence_dir(root, project_id) / 'objects' / sha[:2] / (sha + '.' + codec)


def _encode(data):
    encoded = _zstd.compress(data, level=3) if _zstd is not None else zlib.compress(data, level=6)
    return (CODEC_WRITE, encoded) if len(encoded) < len(data) else ('raw', data)


def _stage(root, project_id, data):
    codec, encoded = _encode(data)
    directory = store_layout.evidence_dir(root, project_id) / 'tmp'
    _mkdir(directory)
    with tempfile.NamedTemporaryFile(mode='wb', dir=directory, delete=False) as file:
        file.write(encoded)
        _sync_file(file)
        name = Path(file.name)
    return codec, name, len(encoded)


def _sequence(conn, project_id):
    _execute(conn, 'INSERT OR IGNORE INTO evidence_retention (project_id,next_seq) VALUES (?,1)',
             (project_id,))
    seq = _one(conn, 'SELECT next_seq FROM evidence_retention WHERE project_id=?', (project_id,))[0]
    _execute(conn, 'UPDATE evidence_retention SET next_seq=? WHERE project_id=?',
             (seq + 1, project_id))
    return seq


def _invalidate_derived(conn, project_id, object_id):
    _execute(conn, 'DELETE FROM evidence_derive WHERE run_id='
             '(SELECT derive_run_id FROM evidence_retention WHERE project_id=?) AND object_id=?',
             (project_id, object_id))


def _capture(conn, project_id, object_id, sha, capture, now_ms):
    closed = None
    if capture.bundle_id is not None:
        closed = queries.bundle_closed_at(conn, project_id, [capture.bundle_id])[capture.bundle_id]
    seq = _sequence(conn, project_id)
    _execute(conn, 'INSERT INTO evidence_captures '
             '(object_id,sha256,source,captured_by,party_seat_id,party_special,bundle_id,captured_at) '
             'VALUES (?,?,?,?,?,?,?,?)',
             (object_id, sha, capture.source, capture.captured_by, capture.party_seat_id,
              capture.party_special, capture.bundle_id, now_ms))
    _execute(conn, 'UPDATE evidence_objects SET latest_capture_at=MAX(latest_capture_at,?), '
             'bundleless_latest_at=CASE WHEN ? THEN MAX(COALESCE(bundleless_latest_at,?),?) '
             'ELSE bundleless_latest_at END, any_host_observed=MAX(any_host_observed,?), '
             'state_seq=? WHERE object_id=?',
             (now_ms, capture.bundle_id is None, now_ms, now_ms,
              capture.captured_by == 'host-observed', seq, object_id))
    _invalidate_derived(conn, project_id, object_id)
    if capture.bundle_id is not None:
        added = _execute(conn, 'INSERT OR IGNORE INTO evidence_object_bundles '
                         '(bundle_id,object_id,counted_open) VALUES (?,?,?)',
                         (capture.bundle_id, object_id, closed is None))
        _execute(conn, 'INSERT OR IGNORE INTO evidence_bundles (bundle_id,state_seen,closed_at) '
                 'VALUES (?,?,?)', (capture.bundle_id, 'open' if closed is None else 'closed', closed))
        if added:
            _execute(conn, 'UPDATE evidence_objects SET open_bundle_count=open_bundle_count+?, '
                     'latest_bundle_close_at=CASE WHEN ? IS NULL THEN latest_bundle_close_at '
                     'ELSE MAX(COALESCE(latest_bundle_close_at,?),?) END WHERE object_id=?',
                     (closed is None, closed, closed, closed, object_id))


def _record_object(conn, root, project_id, sha, bytes_len, codec, staged, stored_len, capture, now_ms):
    existing = _one(conn, 'SELECT object_id FROM evidence_objects WHERE sha256=?', (sha,))
    if existing is None:
        # A deferred caller must hold the ledger's write lock before publication.
        _execute(conn, 'UPDATE evidence_objects SET state_seq=state_seq WHERE 0')
        path = _object_path(root, project_id, sha, codec)
        _mkdir(path.parent)
        os.replace(staged, path)
        _sync_dir(path.parent)
        # The capture assigns the final project-wide sequence in this transaction.
        _execute(conn, 'INSERT INTO evidence_objects '
                 '(sha256,bytes_len,stored_len,codec,stored_at,latest_capture_at,'
                 'bundleless_latest_at,any_host_observed,open_bundle_count,latest_bundle_close_at,state_seq) '
                 'VALUES (?,?,?,?,?,?,NULL,0,0,NULL,1)',
                 (sha, bytes_len, stored_len, codec, now_ms, now_ms))
        existing = _one(conn, 'SELECT object_id FROM evidence_objects WHERE sha256=?', (sha,))
    _capture(conn, project_id, existing[0], sha, capture, now_ms)


class LedgerEvidence:
    """The Engine evidence seam, bound to its caller's connection and capture."""

    def __init__(self, conn, root, project_id: str, capture: Capture):
        self.conn = conn
        self.root = Path(root)
        store_layout.evidence_dir(root, project_id)
        self.project_id = project_id
        self.capture = capture

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        now_ms = time.time_ns() // 1_000_000
        row = _one(self.conn, 'SELECT object_id FROM evidence_objects WHERE sha256=?', (sha,))
        if row is not None:
            _capture(self.conn, self.project_id, row[0], sha, self.capture, now_ms)
            return sha
        codec, staged, stored_len = _stage(self.root, self.project_id, data)
        _record_object(self.conn, self.root, self.project_id, sha, len(data), codec,
                       staged, stored_len, self.capture, now_ms)
        return sha

    def get(self, sha256: str) -> bytes:
        return _read(self.conn, self.root, self.project_id, sha256)

    def captured_by(self, sha256: str) -> str | None:
        row = _one(self.conn, 'SELECT any_host_observed FROM evidence_objects WHERE sha256=?',
                   (_digest(sha256),))
        return None if row is None else ('host-observed' if row[0] else 'seat-supplied')


def _read(conn, root, project_id, sha):
    _digest(sha)
    owns_read = not conn.in_transaction
    for attempt in range(2):
        row = _one(conn, 'SELECT codec,bytes_len,stored_len FROM evidence_objects WHERE sha256=?', (sha,))
        if row is None:
            raise KeyError(sha)
        codec, bytes_len, stored_len = row
        if codec == 'zstd' and _zstd is None:
            raise submit.EvidenceUnavailable('unsupported-codec')
        try:
            encoded = _object_path(root, project_id, sha, codec).read_bytes()
        except FileNotFoundError:
            # With no caller-owned transaction, the exhausted SELECT released
            # its snapshot; the second SELECT observes a concurrent sweep.
            if owns_read and attempt == 0:
                continue
            raise submit.EvidenceUnavailable('missing') from None
        try:
            if codec == 'raw':
                data = encoded
            elif codec == 'zlib':
                data = zlib.decompress(encoded)
            else:
                data = _zstd.decompress(encoded)
        except Exception as exc:
            raise submit.EvidenceUnavailable('corrupt') from exc
        if (len(encoded) != stored_len or len(data) != bytes_len
                or hashlib.sha256(data).hexdigest() != sha):
            raise submit.EvidenceUnavailable('corrupt')
        return data
    raise submit.EvidenceUnavailable('missing')


def read_range(conn, root, project_id: str, sha256: str, offset: int, count: int) -> EvidenceRead:
    """Read verified object bytes at a nonnegative byte offset and count."""
    if type(offset) is not int or type(count) is not int or offset < 0 or count < 0:
        raise ValueError('offset and count must be nonnegative integers')
    data = _read(conn, root, project_id, sha256)
    return EvidenceRead(data[offset:offset + count], offset, len(data))


class UploadConflict(ValueError):
    """The upload's declaration, progress or state refuses this request."""


@dataclass(frozen=True)
class UploadState:
    upload_id: str
    sha256: str
    bytes_len: int
    received_len: int
    state: str


@dataclass(frozen=True)
class PreparedUpload:
    upload_id: str
    sha256: str
    bytes_len: int
    codec: str
    encoded_name: str
    encoded_len: int


@dataclass(frozen=True)
class UploadResult:
    sha256: str
    bytes_len: int
    captured_by: str
    replayed: bool


def _upload_path(root, project_id, upload_id):
    if str(uuid.UUID(upload_id)) != upload_id:
        raise UploadConflict('invalid upload id')
    return store_layout.evidence_dir(root, project_id) / 'uploads' / (upload_id + '.part')


def _upload_row(conn, upload_id):
    row = _one(conn, 'SELECT declared_sha256,declared_len,received_len,state,'
               'last_chunk_offset,last_chunk_sha256,source,captured_by,party_seat_id,'
               'party_special,bundle_id,result_captured_by FROM evidence_uploads WHERE upload_id=?',
               (upload_id,))
    if row is None:
        raise UploadConflict('unknown upload')
    return row


def _upload_state(upload_id, row):
    return UploadState(upload_id, row[0], row[1], row[2], row[3])


def begin_upload(conn, root, project_id, capture, declared_sha256: str, declared_len: int) -> UploadState:
    _digest(declared_sha256)
    if type(declared_len) is not int or not 0 <= declared_len <= MAX_UPLOAD_BYTES:
        raise UploadConflict('upload length exceeds bounds')
    upload_id = str(uuid.uuid4())
    path = _upload_path(root, project_id, upload_id)
    _execute(conn, 'UPDATE evidence_objects SET state_seq=state_seq WHERE 0')
    _mkdir(path.parent)
    with path.open('xb') as file:
        _sync_file(file)
    _sync_dir(path.parent)
    now_ms = time.time_ns() // 1_000_000
    _execute(conn, 'INSERT INTO evidence_uploads '
             '(upload_id,party_seat_id,party_special,source,captured_by,declared_sha256,declared_len,'
             'received_len,bundle_id,state,started_at,updated_at) VALUES (?,?,?,?,?,?,?,0,?,\'open\',?,?)',
             (upload_id, capture.party_seat_id, capture.party_special, capture.source,
              capture.captured_by, declared_sha256, declared_len, capture.bundle_id, now_ms, now_ms))
    return UploadState(upload_id, declared_sha256, declared_len, 0, 'open')


def append_chunk(conn, root, project_id, upload_id: str, offset: int, data: bytes) -> UploadState:
    path = _upload_path(root, project_id, upload_id)
    if type(offset) is not int or offset < 0 or len(data) > MAX_CHUNK_BYTES:
        raise UploadConflict('chunk exceeds bounds')
    row = _upload_row(conn, upload_id)
    if row[3] != 'open':
        raise UploadConflict('upload is not open')
    digest = hashlib.sha256(data).hexdigest()
    end = offset + len(data)
    if row[4] == offset and row[5] == digest and end == row[2]:
        return _upload_state(upload_id, row)
    if offset != row[2] or end > row[1]:
        raise UploadConflict('chunk offset or length conflicts')
    _execute(conn, 'UPDATE evidence_objects SET state_seq=state_seq WHERE 0')
    try:
        with path.open('r+b') as file:
            if file.seek(0, os.SEEK_END) < row[2]:
                raise UploadConflict('staging file is incomplete')
            file.truncate(row[2])
            file.seek(offset)
            file.write(data)
            _sync_file(file)
    except FileNotFoundError:
        raise UploadConflict('staging file is missing') from None
    _execute(conn, 'UPDATE evidence_uploads SET received_len=?,last_chunk_offset=?,'
             'last_chunk_sha256=?,updated_at=? WHERE upload_id=?',
             (end, offset, digest, time.time_ns() // 1_000_000, upload_id))
    return UploadState(upload_id, row[0], row[1], end, 'open')


def prepare_upload(root, project_id, upload_id: str) -> PreparedUpload:
    path = _upload_path(root, project_id, upload_id)
    conn = store_layout.open_ledger_readonly(root, project_id)
    try:
        row = _upload_row(conn, upload_id)
    finally:
        conn.close()
    if row[3] != 'open' or row[2] != row[1]:
        raise UploadConflict('upload is not complete and open')
    try:
        with path.open('rb') as file:
            data = file.read(row[2])
    except FileNotFoundError:
        raise UploadConflict('staging file is missing') from None
    if len(data) != row[1] or hashlib.sha256(data).hexdigest() != row[0]:
        raise UploadConflict('upload digest or length conflicts')
    codec, staged, encoded_len = _stage(root, project_id, data)
    return PreparedUpload(upload_id, row[0], row[1], codec, staged.name, encoded_len)


def commit_upload(conn, root, project_id, prepared: PreparedUpload) -> UploadResult:
    _upload_path(root, project_id, prepared.upload_id)
    row = _upload_row(conn, prepared.upload_id)
    matches = prepared.sha256 == row[0] and prepared.bytes_len == row[1]
    if row[3] == 'committed':
        if not matches:
            raise UploadConflict('committed upload conflicts')
        return UploadResult(row[0], row[1], row[11], True)
    if row[3] != 'open' or not matches or row[2] != row[1]:
        raise UploadConflict('prepared upload is stale')
    if (prepared.codec not in ('raw', 'zlib', 'zstd')
            or not prepared.encoded_name or Path(prepared.encoded_name).name != prepared.encoded_name
            or prepared.encoded_name in ('.', '..') or prepared.encoded_len < 0):
        raise UploadConflict('invalid prepared file')
    staged = store_layout.evidence_dir(root, project_id) / 'tmp' / prepared.encoded_name
    try:
        valid_file = staged.is_file() and staged.stat().st_size == prepared.encoded_len
    except FileNotFoundError:
        valid_file = False
    if not valid_file:
        raise UploadConflict('prepared file is missing or changed')
    capture = Capture(row[6], row[7], row[8], row[9], row[10])
    now_ms = time.time_ns() // 1_000_000
    _record_object(conn, root, project_id, row[0], row[1], prepared.codec,
                   staged, prepared.encoded_len, capture, now_ms)
    label = _one(conn, 'SELECT any_host_observed FROM evidence_objects WHERE sha256=?', (row[0],))[0]
    captured_by = 'host-observed' if label else 'seat-supplied'
    _execute(conn, "UPDATE evidence_uploads SET state='committed',result_captured_by=?,updated_at=? "
             'WHERE upload_id=?', (captured_by, now_ms, prepared.upload_id))
    return UploadResult(row[0], row[1], captured_by, False)


@dataclass(frozen=True)
class RetentionReport:
    visited: int = 0
    changed: int = 0
    drift: int = 0
    finished: bool = False
    run_id: int | None = None
    tracked: int = 0


def _retention(conn, root, project_id):
    store_layout.evidence_dir(root, project_id)
    _execute(conn, 'INSERT OR IGNORE INTO evidence_retention (project_id,next_seq) VALUES (?,1)',
             (project_id,))


def follow_up(conn, root, project_id, now_ms) -> RetentionReport:
    """Observe a tracker page and follow at most 500 associations in that page."""
    _retention(conn, root, project_id)
    cursor = _one(conn, 'SELECT tracker_cursor FROM evidence_retention WHERE project_id=?',
                  (project_id,))[0]
    trackers = _all(conn, 'SELECT bundle_id,state_seen,closed_at,follow_up_cursor '
                    'FROM evidence_bundles WHERE bundle_id>? ORDER BY bundle_id LIMIT ?',
                    (cursor, RETENTION_PAGE_ROWS))
    opened = [row[0] for row in trackers if row[1] == 'open']
    observed = queries.bundle_closed_at(conn, project_id, opened) if opened else {}
    visited = changed = drift = 0
    for bundle_id, state, closed, continuation in trackers:
        if state == 'open':
            closed = observed[bundle_id]
            if closed is None:
                continue
            _execute(conn, "UPDATE evidence_bundles SET state_seen='closed',closed_at=? WHERE bundle_id=?",
                     (closed, bundle_id))
        if visited == RETENTION_PAGE_ROWS:
            continue
        associations = _all(conn, 'SELECT association_id,object_id,counted_open '
                            'FROM evidence_object_bundles WHERE bundle_id=? AND object_id>? '
                            'ORDER BY object_id LIMIT ?',
                            (bundle_id, continuation, RETENTION_PAGE_ROWS - visited))
        visited += len(associations)
        for association_id, object_id, counted_open in associations:
            if not counted_open:
                continue
            _execute(conn, 'UPDATE evidence_object_bundles SET counted_open=0 WHERE association_id=?',
                     (association_id,))
            obj = _one(conn, 'SELECT open_bundle_count FROM evidence_objects WHERE object_id=?',
                       (object_id,))
            if obj is None:
                continue
            drift += obj[0] == 0
            seq = _sequence(conn, project_id)
            _execute(conn, 'UPDATE evidence_objects SET open_bundle_count=MAX(open_bundle_count-1,0),'
                     'latest_bundle_close_at=MAX(COALESCE(latest_bundle_close_at,?),?),state_seq=? '
                     'WHERE object_id=?', (closed, closed, seq, object_id))
            _invalidate_derived(conn, project_id, object_id)
            changed += 1
        if associations:
            _execute(conn, 'UPDATE evidence_bundles SET follow_up_cursor=? WHERE bundle_id=?',
                     (associations[-1][1], bundle_id))
    next_cursor = trackers[-1][0] if len(trackers) == RETENTION_PAGE_ROWS else ''
    _execute(conn, 'UPDATE evidence_retention SET tracker_cursor=? WHERE project_id=?',
             (next_cursor, project_id))
    return RetentionReport(visited, changed, drift,
                           len(trackers) < RETENTION_PAGE_ROWS and visited < RETENTION_PAGE_ROWS,
                           tracked=len(trackers))


def derive(conn, root, project_id, now_ms) -> RetentionReport:
    """Accumulate one keyset page; changed objects cannot supply a run result."""
    _retention(conn, root, project_id)
    run_id, start_seq, cursor, finished = _one(conn, 'SELECT derive_run_id,derive_start_seq,'
                                               'derive_cursor,derive_finished '
                                               'FROM evidence_retention WHERE project_id=?', (project_id,))
    if run_id is None or finished:
        run_id = start_seq = _sequence(conn, project_id)
        cursor = 0
        _execute(conn, 'UPDATE evidence_retention SET derive_run_id=?,derive_start_seq=?,'
                 'derive_cursor=0,derive_finished=0,mark_cursor=0 WHERE project_id=?',
                 (run_id, start_seq, project_id))
    associations = _all(conn, 'SELECT association_id,object_id,bundle_id '
                        'FROM evidence_object_bundles WHERE association_id>? ORDER BY association_id LIMIT ?',
                        (cursor, RETENTION_PAGE_ROWS))
    bundles = list(dict.fromkeys(row[2] for row in associations))
    observed = queries.bundle_closed_at(conn, project_id, bundles) if bundles else {}
    changed = 0
    for association_id, object_id, bundle_id in associations:
        obj = _one(conn, 'SELECT state_seq FROM evidence_objects WHERE object_id=?', (object_id,))
        if obj is None or obj[0] >= start_seq:
            _execute(conn, 'DELETE FROM evidence_derive WHERE run_id=? AND object_id=?', (run_id, object_id))
            continue
        closed = observed[bundle_id]
        _execute(conn, 'INSERT INTO evidence_derive (run_id,object_id,open_count,latest_close_at) '
                 'VALUES (?,?,?,?) ON CONFLICT(run_id,object_id) DO UPDATE SET '
                 'open_count=open_count+excluded.open_count,latest_close_at='
                 'CASE WHEN excluded.latest_close_at IS NULL THEN latest_close_at '
                 'ELSE MAX(COALESCE(latest_close_at,excluded.latest_close_at),excluded.latest_close_at) END',
                 (run_id, object_id, closed is None, closed))
        changed += 1
    finished = len(associations) < RETENTION_PAGE_ROWS
    next_cursor = associations[-1][0] if associations else cursor
    _execute(conn, 'UPDATE evidence_retention SET derive_cursor=?,derive_finished=? WHERE project_id=?',
             (next_cursor, finished, project_id))
    return RetentionReport(len(associations), changed, finished=finished, run_id=run_id)


def _prunable(open_count, latest_close, bundleless, latest_capture, now_ms):
    clocks = [value for value in (latest_close, bundleless) if value is not None]
    return (open_count == 0 and bool(clocks) and max(clocks) <= now_ms - RETENTION_AGE_MS
            and latest_capture < now_ms - UPLOAD_IDLE_MS)


def _abandon_uploads(conn, project_id, now_ms):
    cursor = _one(conn, 'SELECT upload_cursor FROM evidence_retention WHERE project_id=?', (project_id,))[0]
    uploads = _all(conn, 'SELECT upload_id,state,updated_at FROM evidence_uploads '
                   'WHERE upload_id>? ORDER BY upload_id LIMIT ?', (cursor, RETENTION_PAGE_ROWS))
    for upload_id, state, updated in uploads:
        if state == 'open' and updated < now_ms - UPLOAD_IDLE_MS:
            _execute(conn, "UPDATE evidence_uploads SET state='abandoned',updated_at=? WHERE upload_id=?",
                     (now_ms, upload_id))
    next_cursor = uploads[-1][0] if len(uploads) == RETENTION_PAGE_ROWS else ''
    _execute(conn, 'UPDATE evidence_retention SET upload_cursor=? WHERE project_id=?',
             (next_cursor, project_id))
    return len(uploads)


def mark(conn, root, project_id, now_ms) -> RetentionReport:
    """Repair from a finished derivation and mark one object page."""
    _retention(conn, root, project_id)
    tracked = _abandon_uploads(conn, project_id, now_ms)
    run_id, start_seq, finished, cursor = _one(conn, 'SELECT derive_run_id,derive_start_seq,'
                                               'derive_finished,mark_cursor FROM evidence_retention '
                                               'WHERE project_id=?', (project_id,))
    if run_id is None or not finished:
        return RetentionReport(tracked=tracked)
    objects = _all(conn, 'SELECT object_id,sha256,state_seq,open_bundle_count,latest_bundle_close_at,'
                   'bundleless_latest_at,latest_capture_at FROM evidence_objects '
                   'WHERE object_id>? ORDER BY object_id LIMIT ?', (cursor, RETENTION_PAGE_ROWS))
    pointed = queries.evidence_pointed(conn, project_id, [row[1] for row in objects])
    changed = 0
    for object_id, sha, seq, live_count, live_close, bundleless, captured in objects:
        if sha in pointed:
            _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=?', (object_id,))
            continue
        if seq >= start_seq:
            _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=? AND state_seq!=?',
                     (object_id, seq))
            continue
        derived = _one(conn, 'SELECT open_count,latest_close_at FROM evidence_derive '
                       'WHERE run_id=? AND object_id=?', (run_id, object_id))
        open_count, latest_close = derived if derived is not None else (0, None)
        if (live_count, live_close) != (open_count, latest_close):
            seq = _sequence(conn, project_id)
            _execute(conn, 'UPDATE evidence_objects SET open_bundle_count=?,latest_bundle_close_at=?,'
                     'state_seq=? WHERE object_id=?', (open_count, latest_close, seq, object_id))
        if _prunable(open_count, latest_close, bundleless, captured, now_ms):
            _execute(conn, 'INSERT INTO evidence_prune_marks (object_id,sha256,state_seq,marked_at) '
                     'VALUES (?,?,?,?) ON CONFLICT(object_id) DO UPDATE SET '
                     'sha256=excluded.sha256,state_seq=excluded.state_seq,marked_at=excluded.marked_at '
                     'WHERE evidence_prune_marks.state_seq!=excluded.state_seq',
                     (object_id, sha, seq, now_ms))
            changed += 1
        else:
            _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=?', (object_id,))
    next_cursor = objects[-1][0] if len(objects) == RETENTION_PAGE_ROWS else 0
    _execute(conn, 'UPDATE evidence_retention SET mark_cursor=? WHERE project_id=?',
             (next_cursor, project_id))
    return RetentionReport(len(objects), changed, finished=len(objects) < RETENTION_PAGE_ROWS,
                           run_id=run_id, tracked=tracked)


def sweep(conn, root, project_id, now_ms) -> RetentionReport:
    """Recheck one mark page in the caller's write transaction; leave files."""
    _retention(conn, root, project_id)
    cursor = _one(conn, 'SELECT sweep_cursor FROM evidence_retention WHERE project_id=?', (project_id,))[0]
    marks = _all(conn, 'SELECT object_id,sha256,state_seq,marked_at FROM evidence_prune_marks '
                 'WHERE object_id>? ORDER BY object_id LIMIT ?', (cursor, RETENTION_PAGE_ROWS))
    pointed = queries.evidence_pointed(conn, project_id, [row[1] for row in marks])
    changed = 0
    for object_id, sha, seq, marked_at in marks:
        obj = _one(conn, 'SELECT sha256,bytes_len,state_seq,open_bundle_count,latest_bundle_close_at,'
                   'bundleless_latest_at,latest_capture_at FROM evidence_objects WHERE object_id=?',
                   (object_id,))
        if obj is None or obj[2] != seq or sha in pointed:
            _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=?', (object_id,))
            continue
        if not _prunable(obj[3], obj[4], obj[5], obj[6], now_ms):
            _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=?', (object_id,))
            continue
        if marked_at > now_ms - UPLOAD_IDLE_MS:
            continue
        _execute(conn, 'DELETE FROM evidence_objects WHERE object_id=?', (object_id,))
        _execute(conn, 'DELETE FROM evidence_prune_marks WHERE object_id=?', (object_id,))
        _execute(conn, 'INSERT INTO evidence_pruned (sha256,bytes_len,pruned_at) VALUES (?,?,?)',
                 (sha, obj[1], now_ms))
        changed += 1
    next_cursor = marks[-1][0] if len(marks) == RETENTION_PAGE_ROWS else 0
    _execute(conn, 'UPDATE evidence_retention SET sweep_cursor=? WHERE project_id=?', (next_cursor, project_id))
    return RetentionReport(len(marks), changed, finished=len(marks) < RETENTION_PAGE_ROWS)


def _unlink(path, directories):
    try:
        path.unlink()
    except FileNotFoundError:
        return 0
    directories.add(path.parent)
    return 1


def unlink_swept(conn, root, project_id) -> RetentionReport:
    """Recheck every candidate file name under E-1's ledger write lock."""
    _retention(conn, root, project_id)
    cursor = _one(conn, 'SELECT unlink_cursor FROM evidence_retention WHERE project_id=?', (project_id,))[0]
    pruned = _all(conn, 'SELECT pruned_id,sha256 FROM evidence_pruned '
                  'WHERE pruned_id>? ORDER BY pruned_id LIMIT ?', (cursor, RETENTION_PAGE_ROWS))
    changed = 0
    directories = set()
    for pruned_id, sha in pruned:
        current = _one(conn, 'SELECT codec FROM evidence_objects WHERE sha256=?', (sha,))
        for codec in ('raw', 'zlib', 'zstd'):
            if current is not None and current[0] == codec:
                continue
            changed += _unlink(_object_path(root, project_id, sha, codec), directories)
    for directory in sorted(directories):
        _sync_dir(directory)
    if pruned:
        _execute(conn, 'UPDATE evidence_retention SET unlink_cursor=? WHERE project_id=?',
                 (pruned[-1][0], project_id))
    return RetentionReport(len(pruned), changed, finished=len(pruned) < RETENTION_PAGE_ROWS)


def _candidate_path(root, project_id, name):
    if not isinstance(name, str):
        raise ValueError('orphan candidate must be a relative path')
    relative = Path(name)
    parts = relative.parts
    if relative.is_absolute() or '..' in parts or relative.as_posix() != name:
        raise ValueError('orphan candidate escapes evidence')
    if len(parts) == 2 and parts[0] == 'tmp':
        pass
    elif len(parts) == 2 and parts[0] == 'uploads' and parts[1].endswith('.part'):
        _upload_path(root, project_id, parts[1][:-5])
    elif len(parts) == 3 and parts[0] == 'objects':
        sha, separator, codec = parts[2].rpartition('.')
        _digest(sha)
        if not separator or codec not in ('raw', 'zlib', 'zstd') or parts[1] != sha[:2]:
            raise ValueError('invalid object candidate')
    else:
        raise ValueError('orphan candidate is not a store entry')
    evidence = store_layout.evidence_dir(root, project_id)
    path = evidence / relative
    if not path.resolve().is_relative_to(evidence.resolve()):
        raise ValueError('orphan candidate escapes evidence')
    return path


def list_orphan_candidates(root, project_id, now_ms) -> list[str]:
    """List owned file names outside the writer job; eligibility is rechecked."""
    evidence = store_layout.evidence_dir(root, project_id)
    candidates = []
    directories = [evidence / 'tmp', evidence / 'uploads']
    objects = evidence / 'objects'
    if not objects.is_symlink() and objects.is_dir():
        directories.extend(path for path in objects.iterdir()
                           if not path.is_symlink() and path.is_dir()
                           and re.fullmatch('[0-9a-f]{2}', path.name))
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            name = path.relative_to(evidence).as_posix()
            try:
                _candidate_path(root, project_id, name)
            except ValueError:
                continue
            candidates.append(name)
    return sorted(candidates)


def _history_page(conn, project_id):
    changed = tracked = 0
    finished = True
    for table, key, column in (
            ('evidence_captures', 'capture_id', 'orphan_capture_cursor'),
            ('evidence_object_bundles', 'association_id', 'orphan_association_cursor')):
        cursor = _one(conn, 'SELECT ' + column + ' FROM evidence_retention WHERE project_id=?', (project_id,))[0]
        rows = _all(conn, 'SELECT ' + key + ',object_id FROM ' + table + ' WHERE ' + key + '>? '
                    'ORDER BY ' + key + ' LIMIT ?', (cursor, RETENTION_PAGE_ROWS))
        tracked += len(rows)
        for row_id, object_id in rows:
            if _one(conn, 'SELECT 1 FROM evidence_objects WHERE object_id=?', (object_id,)) is None:
                changed += _execute(conn, 'DELETE FROM ' + table + ' WHERE ' + key + '=?', (row_id,))
        next_cursor = rows[-1][0] if len(rows) == RETENTION_PAGE_ROWS else 0
        finished = finished and next_cursor == 0
        _execute(conn, 'UPDATE evidence_retention SET ' + column + '=? WHERE project_id=?',
                 (next_cursor, project_id))
    run_id, cursor_run, cursor_object = _one(conn, 'SELECT derive_run_id,orphan_derive_run,'
                                           'orphan_derive_object FROM evidence_retention WHERE project_id=?',
                                           (project_id,))
    rows = _all(conn, 'SELECT run_id,object_id FROM evidence_derive WHERE (run_id,object_id)>(?,?) '
                'ORDER BY run_id,object_id LIMIT ?', (cursor_run, cursor_object, RETENTION_PAGE_ROWS))
    tracked += len(rows)
    for old_run, object_id in rows:
        if run_id is not None and old_run < run_id:
            changed += _execute(conn, 'DELETE FROM evidence_derive WHERE run_id=? AND object_id=?',
                                (old_run, object_id))
    cursor = rows[-1] if len(rows) == RETENTION_PAGE_ROWS else (0, 0)
    _execute(conn, 'UPDATE evidence_retention SET orphan_derive_run=?,orphan_derive_object=? WHERE project_id=?',
             (*cursor, project_id))
    return changed, tracked, finished and cursor == (0, 0)


def orphans(conn, root, project_id, now_ms, *, candidates=()) -> RetentionReport:
    """Recheck one prelisted file page and one page of each history kind."""
    _retention(conn, root, project_id)
    page = list(candidates[:RETENTION_PAGE_ROWS])
    paths = [_candidate_path(root, project_id, name) for name in page]
    changed = 0
    directories = set()
    for name, path in zip(page, paths):
        try:
            old = path.stat().st_mtime_ns // 1000000 < now_ms - UPLOAD_IDLE_MS
        except FileNotFoundError:
            continue
        if not old:
            continue
        parts = Path(name).parts
        if parts[0] == 'objects':
            sha, _, codec = parts[2].rpartition('.')
            current = _one(conn, 'SELECT codec FROM evidence_objects WHERE sha256=?', (sha,))
            if current is not None and current[0] == codec:
                continue
        elif parts[0] == 'uploads':
            upload = _one(conn, 'SELECT state FROM evidence_uploads WHERE upload_id=?', (parts[1][:-5],))
            if upload is not None and upload[0] == 'open':
                continue
        changed += _unlink(path, directories)
    for directory in sorted(directories):
        _sync_dir(directory)
    history_changed, tracked, history_finished = _history_page(conn, project_id)
    return RetentionReport(len(page), changed + history_changed,
                           finished=len(page) < RETENTION_PAGE_ROWS and history_finished, tracked=tracked)
