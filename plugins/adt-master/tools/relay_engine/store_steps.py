"""Declared turns and journaled, checkpointed file moves for Store jobs."""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Protocol, TypeVar

from relay_engine import store_layout
from relay_engine.store_evidence import _sync_dir, _sync_file


T = TypeVar('T')


@dataclass(frozen=True)
class Declared:
    catalogue: str | None
    ledgers: Mapping[str, str]


class TurnRunner(Protocol):
    def run(self, declared: Declared,
            body: Callable[[Mapping[str, sqlite3.Connection]], T], *,
            fence_project: str | None = None) -> T: ...


class DirectTurns:
    """Own connections for one in-process turn; model the writer's fence only."""
    def __init__(self, root):
        self.root = store_layout.resolve_root(root)
        self.fenced_projects = set()

    def run(self, declared: Declared,
            body: Callable[[Mapping[str, sqlite3.Connection]], T], *,
            fence_project: str | None = None) -> T:
        ledgers = dict(declared.ledgers)
        if declared.catalogue not in (None, 'read', 'written'):
            raise ValueError('unknown catalogue declaration')
        if any(value not in ('read', 'written', 'created') for value in ledgers.values()):
            raise ValueError('unknown project declaration')
        order = store_layout.declared_order(declared.catalogue is not None, ledgers)
        modes = dict(ledgers)
        if declared.catalogue is not None:
            modes['catalogue'] = declared.catalogue
        written = [name for name in order if modes[name] in ('written', 'created')]
        if len(written) > 1:
            raise ValueError('a turn can write only one file')
        if fence_project is not None:
            store_layout._canonical_id(fence_project)
        if self.fenced_projects.intersection(ledgers):
            raise store_layout.RouteUnavailable('fenced')
        conns = {}
        try:
            for name in order:
                if name == 'catalogue':
                    conns[name] = store_layout.open_catalogue(self.root)
                else:
                    value = ledgers[name]
                    conns[name] = store_layout.open_ledger(
                        self.root, name, create=(value == 'created'))
            store_layout.begin_declared(conns, root=self.root)
            result = body(conns)
            if written:
                store_layout._pragma(conns[written[0]], 'COMMIT')
        except BaseException as original:
            for label, error in store_layout.rollback_declared(conns).items():
                original.add_note(f'{label}: {type(error).__name__}: {error}')
            raise
        else:
            # A committed write is reported committed even beside failed read rollback.
            store_layout.rollback_declared(conns)
            if fence_project is not None:
                self.fenced_projects.add(fence_project)
            return result
        finally:
            for conn in conns.values():
                conn.close()


class StepStateUnknown(Exception):
    def __init__(self, before, after, observed):
        self.before, self.after, self.observed = before, after, observed
        super().__init__(f'expected {before!r} or {after!r}; observed {observed!r}')


class FenceBreach(StepStateUnknown):
    """A prepared file or its side files changed before its move completed."""


@dataclass(frozen=True)
class Prepared:
    size: int
    sha256: str


@dataclass(frozen=True)
class Intent:
    id: int
    step: str
    target: object
    before: object
    after: object


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def _mkdir(path):
    missing = []
    cursor = Path(path)
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _sync_dir(directory.parent)
    return Path(path)


def migration_dir(root, job_id: str) -> Path:
    return _mkdir(Path(root) / 'migrations' / store_layout._canonical_job(job_id))


def recovery_dir(root, job_id: str) -> Path:
    return _mkdir(Path(root) / 'recovery' / store_layout._canonical_job(job_id))


_JOURNAL_TABLES = (
    'CREATE TABLE IF NOT EXISTS checkpoints '
    '(id INTEGER PRIMARY KEY, phase TEXT NOT NULL, data TEXT NOT NULL) STRICT',
    'CREATE TABLE IF NOT EXISTS intents '
    '(id INTEGER PRIMARY KEY, step TEXT NOT NULL, target TEXT NOT NULL, '
    'before_state TEXT NOT NULL, after_state TEXT NOT NULL) STRICT',
    'CREATE TABLE IF NOT EXISTS receipts '
    '(intent_id INTEGER PRIMARY KEY REFERENCES intents(id), observed TEXT NOT NULL) STRICT',
)


class Journal:
    def __init__(self, conn):
        self._conn = conn

    def close(self):
        self._conn.close()

    def _write(self, sql, args):
        store_layout._pragma(self._conn, 'BEGIN IMMEDIATE')
        try:
            cursor = self._conn.execute(sql, args)
            try:
                row_id = cursor.lastrowid
            finally:
                cursor.close()
            store_layout._pragma(self._conn, 'COMMIT')
            return row_id
        except BaseException:
            if self._conn.in_transaction:
                store_layout._pragma(self._conn, 'ROLLBACK')
            raise

    def checkpoint(self, phase, data):
        self._write('INSERT INTO checkpoints(phase,data) VALUES (?,?)', (phase, _json(data)))

    def last_checkpoint(self):
        row = store_layout._row(self._conn, 'SELECT phase,data FROM checkpoints ORDER BY id DESC LIMIT 1')
        return None if row is None else (row[0], json.loads(row[1]))

    def intent(self, step, target, before, after) -> int:
        return self._write('INSERT INTO intents(step,target,before_state,after_state) VALUES (?,?,?,?)',
                           (step, _json(target), _json(before), _json(after)))

    def receipt(self, intent_id, observed):
        encoded = _json(observed)
        existing = store_layout._row(self._conn, 'SELECT observed FROM receipts WHERE intent_id=?', (intent_id,))
        if existing is not None:
            if existing != (encoded,):
                raise StepStateUnknown(existing[0], encoded, observed)
            return
        self._write('INSERT INTO receipts(intent_id,observed) VALUES (?,?)', (intent_id, encoded))

    def pending(self) -> list[Intent]:
        cursor = self._conn.execute(
            'SELECT id,step,target,before_state,after_state FROM intents '
            'WHERE NOT EXISTS (SELECT 1 FROM receipts WHERE intent_id=intents.id) ORDER BY id')
        try:
            return [Intent(row[0], row[1], *(json.loads(value) for value in row[2:]))
                    for row in cursor]
        finally:
            cursor.close()


def open_journal(job_path: Path, *, create: bool = False) -> Journal:
    path = Path(job_path) / 'job.sqlite3'
    if create:
        _mkdir(path.parent)
    conn = sqlite3.connect(path.absolute().as_uri() + ('?mode=rwc' if create else '?mode=rw'),
                           uri=True, isolation_level=None)
    try:
        store_layout._configure(conn)
        store_layout._pragma(conn, 'PRAGMA foreign_keys=ON')
        if create:
            store_layout._pragma(conn, 'BEGIN IMMEDIATE')
            try:
                for statement in _JOURNAL_TABLES:
                    store_layout._pragma(conn, statement)
                store_layout._pragma(conn, 'COMMIT')
            except BaseException:
                store_layout._pragma(conn, 'ROLLBACK')
                raise
            _sync_dir(path.parent)
        return Journal(conn)
    except BaseException:
        conn.close()
        raise


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as file:
        while block := file.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _side_files(path):
    return [suffix for suffix in ('-wal', '-shm') if Path(str(path) + suffix).exists()]


def prepare_database(path) -> Prepared:
    path = Path(path)
    if Path(str(path) + '-shm').exists():
        raise FenceBreach('closed file', 'prepared file', {'sidefiles': _side_files(path)})
    conn = sqlite3.connect(path.absolute().as_uri() + '?mode=rw', uri=True, isolation_level=None)
    try:
        store_layout._configure(conn)
        store_layout._pragma(conn, 'PRAGMA foreign_keys=ON')
        row = store_layout._row(conn, 'PRAGMA wal_checkpoint(TRUNCATE)')
        if row is None or row[0] != 0 or row[1] != row[2]:
            raise FenceBreach('checkpoint complete', 'prepared file', row)
    finally:
        conn.close()
    wal = Path(str(path) + '-wal')
    if (wal.exists() and wal.stat().st_size) or Path(str(path) + '-shm').exists():
        raise FenceBreach('closed file', 'prepared file', {'sidefiles': _side_files(path)})
    # An empty WAL is harmless at preparation but must be gone for the move.
    if wal.exists():
        wal.unlink()
        _sync_dir(path.parent)
    with path.open('rb') as file:
        _sync_file(file)
    return Prepared(path.stat().st_size, _sha(path))


def _file_state(path):
    if not path.exists():
        return None
    if not path.is_file():
        return {'kind': 'not-file'}
    return {'size': path.stat().st_size, 'sha256': _sha(path)}


def _observed(src, dst):
    return {'src': _file_state(src), 'dst': _file_state(dst),
            'sidefiles': {'src': _side_files(src), 'dst': _side_files(dst)}}


def _states(prepared):
    value = {'size': prepared.size, 'sha256': prepared.sha256}
    sides = {'src': [], 'dst': []}
    return ({'src': value, 'dst': None, 'sidefiles': sides},
            {'src': None, 'dst': value, 'sidefiles': sides})


def _rename(src, dst):
    _mkdir(dst.parent)
    os.rename(src, dst)
    _sync_dir(src.parent)
    _sync_dir(dst.parent)


def move_database(journal, prepared: Prepared, src, dst) -> None:
    src, dst = Path(src), Path(dst)
    before, after = _states(prepared)
    ident = journal.intent('move', {'src': str(src), 'dst': str(dst)}, before, after)
    if (_side_files(src) or _side_files(dst) or not src.is_file()
            or dst.exists() or src.stat().st_size != prepared.size):
        raise FenceBreach(before, after, _observed(src, dst))
    if _sha(src) != prepared.sha256:
        raise FenceBreach(before, after, _observed(src, dst))
    _rename(src, dst)
    journal.receipt(ident, after)


def resume_move(journal, intent: Intent) -> str:
    if intent.step != 'move':
        raise StepStateUnknown(intent.before, intent.after, 'not a move intent')
    src, dst = Path(intent.target['src']), Path(intent.target['dst'])
    observed = _observed(src, dst)
    if observed == intent.before:
        _rename(src, dst)
        result = 'rename'
    elif observed == intent.after:
        # A crash may have followed rename before either directory was synced.
        _sync_dir(src.parent)
        _sync_dir(dst.parent)
        result = 'receipt'
    else:
        raise FenceBreach(intent.before, intent.after, observed)
    journal.receipt(intent.id, intent.after)
    return result
