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
import zlib

from relay_engine import queries, store_layout, submit

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

CODEC_WRITE = 'zstd' if _zstd is not None else 'zlib'
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
