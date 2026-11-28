"""Declared turns and journaled, checkpointed file moves for Store jobs."""
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import secrets
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Literal, Protocol, TypeVar
import uuid

from relay_engine import store_layout
from relay_engine.store_evidence import _sync_dir, _sync_file


T = TypeVar('T')

MARKER_DAEMON_JSON: bytes = (json.dumps({
    'state': 'ready',
    'socket': '/nonexistent/adt-v3-moved/s',
    'identity': {
        'kit': '3.0.0',
        'fp': '0' * 64,
        'install': '/MOVED TO KIT 3.0.0 - this root is served by the global v3 engine; '
                   'finish or restart this session on kit 3.0.0',
    },
    'nonce': '00000000-0000-0000-0000-000000000000',
    'pid_start_time': '0',
}) + '\n').encode('utf-8')


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


@dataclass(frozen=True)
class SourceSnapshot:
    job_dir: str
    sources: tuple[tuple[str, int, int, int, int, str], ...]
    inputs: tuple[tuple[str, int, str], ...]


def _source_file(path):
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f'not an ordinary snapshot file: {path}')
    digest = _sha(path)
    if identity(before) != identity(path.lstat()):
        raise ValueError(f'snapshot file changed during read: {path}')
    return (path.name, *identity(before)[:4], digest)


def _source_files(directory):
    main = _source_file(directory / 'job.sqlite3')
    wal = directory / 'job.sqlite3-wal'
    return (main, _source_file(wal)) if os.path.lexists(wal) else (main,)


def _source_directories(job_dir, unmark_dir):
    job, unmark = (Path(path).resolve(strict=True) for path in (job_dir, unmark_dir))
    origin = job.stat()
    origin_id = origin.st_dev, origin.st_ino
    if any((info.st_dev, info.st_ino) == origin_id
           for info in (part.stat() for part in (unmark, *unmark.parents))):
        raise ValueError(f'snapshot destination aliases source: {unmark}')
    if not job.is_dir() or not unmark.is_dir():
        raise ValueError(f'snapshot needs ordinary directories: {job}, {unmark}')
    return job, unmark


def _remove_source_scratch(path):
    if path.is_symlink() or (os.path.lexists(path) and not path.is_dir()):
        raise ValueError(f'snapshot scratch is not a directory: {path}')
    if path.exists():
        shutil.rmtree(path)


def _assert_source_rows(expected, observed):
    for left, right in zip(expected, observed):
        if tuple(left) != tuple(right):
            raise ValueError(f'snapshot member changed: {left[0]}')
    if len(expected) != len(observed):
        raise ValueError('snapshot member set changed: job.sqlite3-wal')


def snapshot_source(job_dir: str | Path, unmark_dir: str | Path) -> SourceSnapshot:
    """Publish a byte-bound main/WAL pair without opening the orphaned journal."""
    try:
        job, unmark = _source_directories(job_dir, unmark_dir)
        partial, published = unmark / 'source.partial', unmark / 'source-inputs'
        for path in (partial, published):
            _remove_source_scratch(path)
        sources = _source_files(job)
        partial.mkdir(mode=0o700)
        inputs = []
        for name, dev, ino, size, _, digest in sources:
            member = partial / name
            shutil.copyfile(job / name, member, follow_symlinks=False)
            copied = _source_file(member)
            if copied[1:3] == (dev, ino) or (copied[3], copied[5]) != (size, digest):
                raise ValueError(f'snapshot copy differs or aliases source: {name}')
            member.chmod(0o400)
            with member.open('rb') as stream:
                _sync_file(stream)
            inputs.append((name, size, digest))
        _assert_source_rows(sources, _source_files(job))
        _sync_dir(partial)
        os.rename(partial, published)
        _sync_dir(unmark)
        return SourceSnapshot(str(job), sources, tuple(inputs))
    except (OSError, TypeError, ValueError) as error:
        raise StepStateUnknown('stable source pair', 'published source inputs', str(error)) from error


def open_source(snapshot: SourceSnapshot, unmark_dir: str | Path) -> Journal:
    """Verify published bytes and open only a disposable, independent SQLite copy."""
    work = None
    try:
        job, unmark = _source_directories(snapshot.job_dir, unmark_dir)
        _assert_source_rows(snapshot.sources, _source_files(job))
        published = unmark / 'source-inputs'
        if published.is_symlink() or not published.is_dir():
            raise ValueError(f'missing source inputs: {published}')
        names = [row[0] for row in snapshot.inputs]
        if names not in (['job.sqlite3'], ['job.sqlite3', 'job.sqlite3-wal']):
            raise ValueError('invalid source input names')
        if sorted(names) != sorted(item.name for item in published.iterdir()):
            raise ValueError('source input member set changed')
        known_ids = {tuple(row[1:3]) for row in snapshot.sources}
        input_records = {}
        for name, size, digest in snapshot.inputs:
            path = published / name
            read = _source_file(path)
            if read[1:3] in known_ids or stat.S_IMODE(path.lstat().st_mode) != 0o400:
                raise ValueError(f'snapshot input is not private and immutable: {name}')
            if (read[3], read[5]) != (size, digest):
                raise ValueError(f'snapshot input bytes changed: {name}')
            known_ids.add(read[1:3])
            input_records[name] = read
        for stale in unmark.glob('source-work-*'):
            _remove_source_scratch(stale)
        work = unmark / ('source-work-' + str(uuid.uuid4()))
        work.mkdir(mode=0o700)
        for name, source_record in input_records.items():
            file = work / name
            shutil.copyfile(published / name, file, follow_symlinks=False)
            read = _source_file(file)
            if (read[1:3] in known_ids or (read[3], read[5]) != (source_record[3], source_record[5])
                    or _source_file(published / name) != source_record):
                raise ValueError(f'work copy changed or aliases input: {name}')
            known_ids.add(read[1:3])
        _assert_source_rows(snapshot.sources, _source_files(job))
        source = open_journal(work)
        source._source_custody = (snapshot, unmark, work, known_ids)
        return source
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        if work is not None:
            _remove_source_scratch(work)
        raise StepStateUnknown('bound source inputs', 'separate source work copy', str(error)) from error


def close_source(source: Journal) -> None:
    source.close()
    _remove_source_scratch(source._source_custody[2])


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


@dataclass(frozen=True)
class PreimageManifest:
    entries: tuple[dict[str, str | int], ...]


@contextmanager
def _open_preimage_entry(name, *, parent=None, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _preimage_signature(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _verify_preimage_entry(name, parent, original, observed):
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _preimage_signature(original) != _preimage_signature(observed) or (
        _preimage_signature(original) != _preimage_signature(current)
    ):
        raise ValueError(f'legacy preimage entry changed: {name}')


def _copy_preimage_entry(parent, name, target, relative, entries):
    original = os.stat(name, dir_fd=parent, follow_symlinks=False)
    directory = stat.S_ISDIR(original.st_mode)
    if not directory and not stat.S_ISREG(original.st_mode):
        raise ValueError(f'unsupported legacy preimage entry: {relative}')
    mode = stat.S_IMODE(original.st_mode)
    entry = dict(path=relative.as_posix(), kind='directory' if directory else 'file',
                 mode=mode)
    with _open_preimage_entry(name, parent=parent, directory=directory) as descriptor:
        _verify_preimage_entry(name, parent, original, os.fstat(descriptor))
        if directory:
            target.mkdir(mode=0o700)
            for child in sorted(os.listdir(descriptor)):
                _copy_preimage_entry(descriptor, child, target / child, relative / child, entries)
            target.chmod(mode)
            if stat.S_IMODE(target.stat().st_mode) != mode:
                raise ValueError(f'could not preserve legacy preimage mode: {relative}')
            _sync_dir(target)
        else:
            digest = hashlib.sha256()
            copied = 0
            with target.open('xb') as output:
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                    copied += output.write(chunk)
                if copied != original.st_size:
                    raise ValueError(f'legacy preimage size changed: {relative}')
                output.flush()
                os.fchmod(output.fileno(), mode)
                if stat.S_IMODE(os.fstat(output.fileno()).st_mode) != mode:
                    raise ValueError(f'could not preserve legacy preimage mode: {relative}')
                _sync_file(output)
            entry.update(size=copied, sha256=digest.hexdigest())
        _verify_preimage_entry(name, parent, original, os.fstat(descriptor))
    entries.append(entry)


def capture_preimage(root_path: str | Path, dest_dir: str | Path) -> PreimageManifest:
    """Durably capture a caller-held legacy engine before any marker intent or effect."""
    destination = Path(dest_dir)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    parent = destination.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError(f'expected an owned preimage parent directory: {parent}')
    _mkdir(parent)
    entries = []
    with tempfile.TemporaryDirectory(prefix='.preimage-', dir=parent) as temporary:
        staging = Path(temporary)
        with _open_preimage_entry(root_path, directory=True) as source:
            engine = os.stat('.engine', dir_fd=source, follow_symlinks=False)
            if not stat.S_ISDIR(engine.st_mode):
                raise ValueError('expected a legacy engine directory')
            _copy_preimage_entry(source, '.engine', staging / 'payload', Path('.'), entries)
        manifest = PreimageManifest(tuple(sorted(entries, key=lambda entry: entry['path'])))
        manifest_path = staging / 'manifest.json'
        with manifest_path.open('x', encoding='utf-8') as output:
            output.write(_json({'entries': manifest.entries}) + '\n')
            _sync_file(output)
        _sync_dir(staging)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        staging.rename(destination)
        _sync_dir(parent)
    return manifest


@dataclass(frozen=True)
class MarkerManifest:
    original_dir: Path
    marker_dir: Path
    daemon_sha256: str


def _copy_marker_tree(source: Path, target: Path, *, writable_levels: int) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f'expected a marker source directory: {source}')
    target.mkdir(mode=0o700)
    for entry in source.iterdir():
        destination = target / entry.name
        if entry.is_symlink():
            raise ValueError(f'unexpected symlink in marker source: {entry}')
        if entry.is_dir():
            _copy_marker_tree(entry, destination, writable_levels=writable_levels - 1)
        elif entry.is_file():
            shutil.copyfile(entry, destination)
            destination.chmod(0o400)
            with destination.open('rb') as copied:
                _sync_file(copied)
        else:
            raise ValueError(f'expected a regular marker source file: {entry}')
    target.chmod(0o700 if writable_levels > 0 else 0o500)
    _sync_dir(target)


def place_marker(root_path: str | Path) -> MarkerManifest:
    """Place the moved-layout marker for a caller-held legacy root."""
    root = Path(root_path)
    engine = root / '.engine'
    original = root / '.engine.migrated-to-v3'
    if original.exists() or original.is_symlink():
        raise FileExistsError(original)
    if engine.is_symlink() or not engine.is_dir():
        raise ValueError(f'expected a legacy engine directory: {engine}')

    with tempfile.TemporaryDirectory(prefix='.marker-', dir=root) as staging:
        # A finalized Darwin directory can move to its sibling at mode 0500.
        marker = Path(staging)
        _copy_marker_tree(engine / 'seats', marker / 'seats', writable_levels=0)
        _copy_marker_tree(engine / 'drafts', marker / 'drafts', writable_levels=2)
        daemon = marker / 'daemon.json'
        daemon.write_bytes(MARKER_DAEMON_JSON)
        daemon.chmod(0o400)
        with daemon.open('rb') as written:
            _sync_file(written)
        marker.chmod(0o500)
        _sync_dir(marker)

        engine.rename(original)
        try:
            marker.rename(engine)
        except OSError:
            original.rename(engine)
            _sync_dir(root)
            raise
        _sync_dir(root)

    return MarkerManifest(original, engine, hashlib.sha256(MARKER_DAEMON_JSON).hexdigest())


@dataclass(frozen=True)
class MarkerTarget:
    store_id: str
    job_id: str
    job_dir: str | Path
    root_index: int
    root_uuid: str
    root_path: str | Path


_ENGINE = '.engine'
_PARKED = '.engine.migrated-to-v3'


def _absolute_marker_path(value):
    path = Path(value)
    if path.anchor != '/' or '..' in path.parts or path == Path('/'):
        raise ValueError('expected an absolute marker path')
    return path


@contextmanager
def _open_marker_directory(path):
    with ExitStack() as stack:
        parent = stack.enter_context(_open_preimage_entry('/', directory=True))
        parents = []
        for name in path.parts[1:]:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            directory = stack.enter_context(_open_preimage_entry(name, parent=parent, directory=True))
            seen = os.fstat(directory)
            if (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
                raise ValueError(f'marker directory changed: {name}')
            parents.append((parent, name, directory))
            parent = directory
        yield parent
        for parent, name, directory in reversed(parents):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            seen = os.fstat(directory)
            if (current.st_dev, current.st_ino) != (seen.st_dev, seen.st_ino):
                raise ValueError(f'marker directory changed: {name}')


def _marker_inventory(parent, name, relative, entries):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    directory = stat.S_ISDIR(info.st_mode)
    if not directory and not stat.S_ISREG(info.st_mode):
        raise ValueError(f'unsupported marker entry: {relative}')
    row = dict(path=relative.as_posix(), kind='directory' if directory else 'file',
               mode=stat.S_IMODE(info.st_mode))
    with _open_preimage_entry(name, parent=parent, directory=directory) as fd:
        _verify_preimage_entry(name, parent, info, os.fstat(fd))
        if directory:
            for child in os.listdir(fd):
                _marker_inventory(fd, child, relative / child, entries)
        else:
            with os.fdopen(fd, 'rb', closefd=False) as source:
                row.update(size=info.st_size, sha256=hashlib.file_digest(source, 'sha256').hexdigest())
        _verify_preimage_entry(name, parent, info, os.fstat(fd))
    entries.append(row)
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _marker_tree(parent, name):
    entries = []
    identity = _marker_inventory(parent, name, Path('.'), entries)
    if entries[-1]['kind'] != 'directory':
        raise ValueError(f'expected a marker directory: {name}')
    return sorted(entries, key=lambda row: row['path']), identity


def _read_marker_tree(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    return _marker_tree(parent, name)


def _observe_marker(root):
    with _open_marker_directory(root) as fd:
        original = os.fstat(fd)
        engine, engine_identity = _read_marker_tree(fd, _ENGINE)
        parked, parked_identity = _read_marker_tree(fd, _PARKED)
        if _preimage_signature(original) != _preimage_signature(os.fstat(fd)):
            raise ValueError('marker root changed during observation')
    return dict(engine=engine, parked=parked, engine_identity=engine_identity,
                parked_identity=parked_identity,
                root_identity={'dev': original.st_dev, 'ino': original.st_ino})


def _marker_states(preimage, marker):
    return (
        {'engine': preimage['inventory'], 'parked': None},
        {'engine': marker, 'parked': preimage['inventory']},
    )


def _classify_marker(observed, preimage, marker):
    before, after = _marker_states(preimage, marker)
    state = {key: observed[key] for key in ('engine', 'parked')}
    if observed['root_identity'] == preimage['root_identity']:
        if state == before and observed['engine_identity'] == preimage['original_identity']:
            return 'preimage'
        if state == after and observed['parked_identity'] == preimage['original_identity']:
            return 'marked'
    return 'other'


def root_state(root_path, preimage, marker) -> str:
    """Classify the two complete marker states using read-only evidence."""
    try:
        observed = _observe_marker(_absolute_marker_path(root_path))
        return _classify_marker(observed, preimage, marker)
    except (OSError, ValueError, TypeError, KeyError):
        return 'other'


def _marker_binding(target):
    if not isinstance(target, MarkerTarget) or type(target.root_index) is not int or target.root_index < 0:
        raise ValueError('invalid marker target')
    for value in (target.store_id, target.job_id, target.root_uuid):
        if str(uuid.UUID(value)) != value:
            raise ValueError('invalid marker target identity')
    job = _absolute_marker_path(target.job_dir)
    if job.name != target.job_id:
        raise ValueError('marker job directory does not match job ID')
    return dict(store_id=target.store_id, job_id=target.job_id, job_dir=str(job),
                root_index=target.root_index, root_uuid=target.root_uuid,
                root_path=str(_absolute_marker_path(target.root_path)))


def _journal_main(journal):
    databases = journal._conn.execute('PRAGMA database_list')
    try:
        return next((row[2] for row in databases if row[1] == 'main'), None)
    finally:
        databases.close()


def _marker_job_paths(journal, binding, source=None):
    job = Path(binding['job_dir'])
    main = _journal_main(journal)
    if source is None:
        if main != str(job / 'job.sqlite3'):
            raise ValueError('marker journal does not belong to the bound job')
    else:
        snapshot, unmark, work, known_ids = source._source_custody
        if snapshot.job_dir != str(job) or _journal_main(source) != str(work / 'job.sqlite3'):
            raise ValueError('source work copy does not belong to the bound job')
        if journal is not source:
            info = Path(main).lstat()
            if (main != str(unmark / 'job.sqlite3') or not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) in known_ids):
                raise ValueError('unmark journal aliases source or is outside its job')
    return job / 'originals' / str(binding['root_index']), Path(binding['root_path'])


def _marker_preimage(path):
    with _open_marker_directory(path) as fd:
        before = os.fstat(fd)
        if set(os.listdir(fd)) != {'manifest.json', 'payload'}:
            raise ValueError('unexpected entries in published marker preimage')
        info = os.stat('manifest.json', dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('marker preimage manifest is not a regular file')
        with _open_preimage_entry('manifest.json', parent=fd) as manifest_fd:
            _verify_preimage_entry('manifest.json', fd, info, os.fstat(manifest_fd))
            with os.fdopen(manifest_fd, 'rb', closefd=False) as manifest:
                raw = manifest.read()
            inventory, _ = _marker_tree(fd, 'payload')
            _verify_preimage_entry('manifest.json', fd, info, os.fstat(manifest_fd))
        if _preimage_signature(before) != _preimage_signature(os.fstat(fd)):
            raise ValueError('marker preimage directory changed during verification')
    if raw != (_json({'entries': inventory}) + '\n').encode('utf-8'):
        raise ValueError('marker preimage manifest differs from payload')
    return inventory, hashlib.sha256(raw).hexdigest()


def _expected_marker(inventory):
    marker = [{'path': '.', 'kind': 'directory', 'mode': 0o500}]
    copied_dirs = {row['path'] for row in inventory
                   if row['kind'] == 'directory' and row['path'] in ('seats', 'drafts')}
    if copied_dirs != {'seats', 'drafts'}:
        raise ValueError('marker preimage lacks seats or drafts')
    for row in inventory:
        parts = Path(row['path']).parts
        if parts and parts[0] in copied_dirs:
            mode = 0o400
            if row['kind'] == 'directory':
                mode = 0o700 if parts[0] == 'drafts' and len(parts) <= 2 else 0o500
            marker.append({**row, 'mode': mode})
    marker.append(dict(path='daemon.json', kind='file', mode=0o400, size=len(MARKER_DAEMON_JSON),
                       sha256=hashlib.sha256(MARKER_DAEMON_JSON).hexdigest()))
    return sorted(marker, key=lambda row: row['path'])


_MARKER_SEQUENCE = ('marker-open', 'marker-fill', 'marker-seal', 'marker-aside', 'marker-place',
                    'inverse-unplace', 'inverse-restore', 'dispose-tomb', 'dispose-clear')
_MARKER_WIRE_VERSION = 2
_MISSING_RECEIPT = object()


@dataclass(frozen=True)
class MarkerProgress:
    phase: Literal['none', 'forward', 'marked', 'inverse', 'restored']
    attempt: int
    pending: Intent | None
    next_step: str | None


@dataclass
class _MarkerHistory:
    progress: MarkerProgress
    authority: dict
    nonce: str | None
    build_identity: dict | None
    dispose_slot: str | None
    inventory: list
    last: Intent | None = None


def _marker_identity(info):
    return {'dev': info.st_dev, 'ino': info.st_ino}


def _marker_unknown(before, after, observed):
    return StepStateUnknown(before, after, observed)


def _marker_slots(binding, attempt, nonce):
    suffix = f"{binding['job_id']}-{binding['root_index']}"
    attempt_suffix = f'{suffix}-{attempt}-{nonce}'
    return {
        'engine': _ENGINE, 'parked': _PARKED,
        'build': f'.marker-build-{attempt_suffix}', 'stage': f'.marker-stage-{suffix}',
        'sibling': f'.marker-inverse-{suffix}',
        'discard': f'.marker-discard-{attempt_suffix}',
        'inverse_discard': f'.marker-discard-{suffix}-inverse',
    }


def _marker_states_for(step, authority, identity, disposing):
    def slot(kind, owner):
        return {'kind': kind, 'identity': owner}

    original = slot('preimage', authority['original_identity'])
    marker = slot('marker', identity)
    empty = slot('empty', identity)
    owned = slot('owned', identity)
    chosen = slot('new-empty', None)
    initial = dict(engine=original, parked=None, build=None, stage=None, sibling=None,
                   discard=None, inverse_discard=None)
    staged = {**initial, 'stage': marker}
    aside = {**staged, 'engine': None, 'parked': original}
    placed = {**aside, 'engine': marker, 'stage': None}
    unplaced = {**placed, 'engine': None, 'sibling': marker}
    restored = {**unplaced, 'engine': original, 'parked': None}
    if step == 'marker-open':
        return initial, {**initial, 'build': chosen}
    if step == 'marker-fill':
        return {**initial, 'build': empty}, {**initial, 'build': marker}
    if step == 'marker-seal':
        return {**initial, 'build': marker}, staged
    if step == 'marker-aside':
        return staged, aside
    if step == 'marker-place':
        return aside, placed
    if step == 'inverse-unplace':
        return placed, unplaced
    if step == 'inverse-restore':
        return unplaced, restored
    if disposing == 'build':
        baseline, contents = initial, owned
    elif disposing == 'sibling':
        baseline, contents = {**restored, 'sibling': None}, marker
    else:
        raise ValueError('missing disposal binding')
    discard_slot = 'inverse_discard' if disposing == 'sibling' else 'discard'
    discard = {**baseline, discard_slot: slot('marker-subset', identity)
               if disposing == 'sibling' else owned}
    if step == 'dispose-tomb':
        return {**baseline, disposing: contents}, {**baseline, discard_slot: contents}
    if step == 'dispose-clear':
        return discard, baseline
    raise ValueError(f'unknown marker step: {step}')


def _marker_rows(journal, binding):
    labels = ','.join('?' for _ in _MARKER_SEQUENCE)
    cursor = journal._conn.execute(
        f'SELECT i.id,i.step,i.target,i.before_state,i.after_state,r.observed '
        f'FROM intents i LEFT JOIN receipts r ON r.intent_id=i.id '
        f'WHERE i.step IN ({labels}) ORDER BY i.id', _MARKER_SEQUENCE,
    )
    try:
        for ident, step, raw, before, after, receipt in cursor:
            target = json.loads(raw)
            if any(target[k] == binding[k] for k in ('root_index', 'root_uuid', 'root_path')):
                if any(target[k] != value for k, value in binding.items()):
                    raise ValueError('marker row does not match its bound root or job')
                yield Intent(ident, step, target, json.loads(before), json.loads(after)), (
                    _MISSING_RECEIPT if receipt is None else json.loads(receipt)
                )
    finally:
        cursor.close()


def _marker_target_for(authority, binding, attempt, nonce, identity, disposing):
    return dict(**authority, attempt=attempt, nonce=nonce, build_identity=identity,
                dispose_slot=disposing, slots=_marker_slots(binding, attempt, nonce))


def _read_marker_history(journal, target, *, source=None):
    try:
        binding = _marker_binding(target)
        preimage, root = _marker_job_paths(journal, binding, source)
        rows = list(_marker_rows(journal, binding))
        if source is not None and source is not journal:
            if any(intent.step not in ('inverse-unplace', 'inverse-restore', 'dispose-tomb',
                                       'dispose-clear') for intent, _ in rows):
                raise ValueError('unmark journal contains a forward marker step')
            rows = list(_marker_rows(source, binding)) + rows
        if not rows:
            return _MarkerHistory(MarkerProgress('none', 1, None, 'marker-open'), binding,
                                  None, None, None, [])
        start = rows[0][0].target
        with _open_marker_directory(root) as root_fd:
            root_identity = _marker_identity(os.fstat(root_fd))
        inventory, manifest_hash = _marker_preimage(preimage)
        if source is not None and manifest_hash != start['preimage_manifest_sha256']:
            raise _ChangedMarkerPreimage(start['preimage_manifest_sha256'], manifest_hash, str(preimage))
        original_identity = start['original_identity']
        if (set(original_identity) != {'dev', 'ino'} or
                any(type(n) is not int or n < 0 for n in original_identity.values())):
            raise ValueError('invalid original root identity')
        authority = dict(**binding, version=_MARKER_WIRE_VERSION, preimage_path=str(preimage),
                         preimage_manifest_sha256=manifest_hash, root_identity=root_identity,
                         original_identity=original_identity)
        step = 'marker-open'
        phase = 'forward'
        attempt, nonce, identity, disposing, pending = 1, None, None, None, None
        for intent, receipt in rows:
            if pending is not None or intent.step != step:
                raise ValueError('marker journal contains duplicate or out-of-order steps')
            if step == 'marker-open':
                nonce = intent.target['nonce']
                if (not isinstance(nonce, str) or len(nonce) != 32 or
                        any(c not in '0123456789abcdef' for c in nonce)):
                    raise ValueError('marker open is missing its bound nonce')
            expected = _marker_target_for(authority, binding, attempt, nonce, identity, disposing)
            if intent.target != expected:
                raise ValueError('marker target differs from its bound journal or attempt')
            before, after = _marker_states_for(step, authority, identity, disposing)
            if (intent.before, intent.after) != (before, after):
                raise ValueError('marker step states differ from its exact journal encoding')
            if step == 'inverse-unplace':
                phase = 'inverse'
            if receipt is _MISSING_RECEIPT:
                pending = intent
                continue
            if step == 'marker-open':
                if set(receipt) != {'build_identity'}:
                    raise ValueError('invalid marker-open identity receipt')
                identity = receipt['build_identity']
                if (set(identity) != {'dev', 'ino'} or
                        any(type(n) is not int or n < 0 for n in identity.values())):
                    raise ValueError('invalid build directory identity')
                step = 'marker-fill'
            elif step == 'marker-fill' and receipt == {'abandoned': True}:
                disposing, step = 'build', 'dispose-tomb'
            else:
                if receipt != after:
                    raise ValueError('marker receipt is not its after state')
                if step == 'marker-place':
                    phase, step = 'marked', 'inverse-unplace'
                elif step == 'inverse-restore':
                    disposing, step = 'sibling', 'dispose-tomb'
                elif step == 'dispose-clear':
                    if disposing == 'build':
                        attempt, nonce, identity, disposing = attempt + 1, None, None, None
                        step = 'marker-open'
                    else:
                        phase, step = 'restored', None
                else:
                    step = _MARKER_SEQUENCE[_MARKER_SEQUENCE.index(step) + 1]
        next_step = None if pending or phase in ('marked', 'restored') else step
        return _MarkerHistory(MarkerProgress(phase, attempt, pending, next_step), authority,
                              nonce, identity, disposing, inventory, rows[-1][0])
    except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error) as error:
        raise _marker_unknown('bound marker history', 'valid next step',
                              {'binding_error': str(error)}) from error


def marker_progress(journal, target) -> MarkerProgress:
    """Read the current attempt and transition without changing files or journal."""
    return _read_marker_history(journal, target).progress


def _marker_match_inventory(kind, actual, inventory):
    if kind == 'preimage':
        return actual == inventory
    expected = _expected_marker(inventory)
    if kind == 'marker':
        return actual == expected
    if kind in ('empty', 'new-empty'):
        return actual == [{'path': '.', 'kind': 'directory', 'mode': 0o700}]
    if kind == 'marker-subset':
        by_path = {row['path']: row for row in expected}
        return all(row == by_path.get(row['path']) or (
            row['kind'] == 'directory' and row['mode'] == 0o700
            and {**row, 'mode': 0o500} == by_path.get(row['path'])
        ) for row in actual)
    raise ValueError(f'unknown marker inventory class: {kind}')


def _marker_check(root_fd, target, state, inventory):
    """Read only the exact named slots, without following directory aliases."""
    seen = {}
    matches = True
    if _marker_identity(os.fstat(root_fd)) != target['root_identity']:
        return False, {'root_identity': _marker_identity(os.fstat(root_fd))}
    for slot, name in target['slots'].items():
        expected = state[slot]
        try:
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            seen[slot] = None
            matches = matches and expected is None
            continue
        except OSError as error:
            seen[slot] = {'observation_error': str(error)}
            matches = False
            continue
        identity = _marker_identity(info)
        seen[slot] = {'identity': identity, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid}
        if expected is None or not stat.S_ISDIR(info.st_mode):
            matches = False
            continue
        kind = expected['kind']
        if kind == 'new-empty':
            if info.st_uid != os.geteuid():
                matches = False
                continue
        elif identity != expected['identity']:
            matches = False
            continue
        if kind == 'owned':
            continue
        try:
            actual, verified = _marker_tree(root_fd, name)
        except OSError as error:
            seen[slot]['observation_error'] = str(error)
            matches = False
            continue
        seen[slot]['inventory'] = actual
        if verified != identity or not _marker_match_inventory(kind, actual, inventory):
            matches = False
    return matches, seen


def _write_marker_step(journal, target, history, inverse, preimage, *, source=None):
    progress = history.progress
    if progress.pending is not None:
        raise ValueError('marker step already pending for this root')
    if inverse:
        step = 'inverse-unplace' if progress.phase == 'marked' else progress.next_step
        if progress.phase not in ('marked', 'inverse') or not step:
            raise ValueError('inverse step is out of order')
    else:
        step = progress.next_step
        if progress.phase not in ('none', 'forward') or not step:
            raise ValueError('marker step is out of order')
    binding = _marker_binding(target)
    path, root = _marker_job_paths(journal, binding, source)
    authority, inventory = history.authority, history.inventory
    if progress.phase == 'none':
        inventory, digest = _marker_preimage(path)
        if preimage is None or inventory != list(preimage.entries):
            raise ValueError('published marker preimage differs from captured preimage')
        observed = _observe_marker(root)
        authority = dict(**binding, version=_MARKER_WIRE_VERSION, preimage_path=str(path),
                         preimage_manifest_sha256=digest, root_identity=observed['root_identity'],
                         original_identity=observed['engine_identity'])
    elif preimage is not None and inventory != list(preimage.entries):
        raise ValueError('marker preimage differs from bound history')

    nonce = secrets.token_hex(16) if step == 'marker-open' else history.nonce
    encoded = _marker_target_for(authority, binding, progress.attempt, nonce,
                                 history.build_identity, history.dispose_slot)
    before, after = _marker_states_for(step, authority, history.build_identity, history.dispose_slot)
    with _open_marker_directory(root) as root_fd:
        correct, observed = _marker_check(root_fd, encoded, before, inventory)
        if not correct:
            raise _marker_unknown(before, after, observed)
    ident = journal.intent(step, encoded, before, after)
    return Intent(ident, step, encoded, before, after)


def write_marker_intent(journal: Journal, target: MarkerTarget, preimage: PreimageManifest | None) -> Intent:
    """Commit exactly the next forward step authorized by the root history."""
    return _write_marker_step(journal, target, _read_marker_history(journal, target), False, preimage)


def write_inverse_intent(journal: Journal, target: MarkerTarget,
                         preimage: PreimageManifest | None) -> Intent:
    """Commit exactly the next inverse step authorized by the root history."""
    return _write_marker_step(journal, target, _read_marker_history(journal, target), True, preimage)


def _fill_marker(engine, build):
    _copy_marker_tree(engine / 'seats', build / 'seats', writable_levels=0)
    _copy_marker_tree(engine / 'drafts', build / 'drafts', writable_levels=2)
    daemon = build / 'daemon.json'
    with daemon.open('xb') as file:
        file.write(MARKER_DAEMON_JSON)
        os.fchmod(file.fileno(), 0o400)
        _sync_file(file)
    build.chmod(0o500)
    _sync_dir(build)


def _sync_marker_tree(path):
    for directory, _, files in os.walk(path, topdown=False):
        parent = Path(directory)
        for name in files:
            with (parent / name).open('rb') as source:
                _sync_file(source)
        _sync_dir(parent)


def _clear_owned_marker(parent, name):
    with _open_preimage_entry(name, parent=parent, directory=True) as fd:
        os.fchmod(fd, 0o700)
        for child in sorted(os.listdir(fd)):
            info = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _clear_owned_marker(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
        os.fsync(fd)
    os.rmdir(name, dir_fd=parent)


def _marker_effect(root, root_fd, intent):
    slots = intent.target['slots']
    step = intent.step
    discard = 'inverse_discard' if intent.target['dispose_slot'] == 'sibling' else 'discard'
    moves = {'marker-seal': ('build', 'stage'), 'marker-aside': ('engine', 'parked'),
             'marker-place': ('stage', 'engine'), 'inverse-unplace': ('engine', 'sibling'),
             'inverse-restore': ('parked', 'engine'),
             'dispose-tomb': (intent.target['dispose_slot'], discard)}
    if step == 'marker-open':
        os.mkdir(slots['build'], 0o700, dir_fd=root_fd)
    elif step == 'marker-fill':
        _fill_marker(root / slots['engine'], root / slots['build'])
    elif step == 'dispose-clear':
        _clear_owned_marker(root_fd, slots[discard])
    else:
        src, dst = moves[step]
        os.rename(slots[src], slots[dst], src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _resume_marker_step(journal, intent, target, inverse, *, source=None):
    history = _read_marker_history(journal, target, source=source)
    progress = history.progress
    if progress.pending != intent or progress.phase != ('inverse' if inverse else 'forward'):
        raise _marker_unknown(intent.before, intent.after, 'not the bound pending marker step')
    _, root = _marker_job_paths(journal, _marker_binding(target), source)
    try:
        with _open_marker_directory(root) as root_fd:
            is_after, after_seen = _marker_check(root_fd, intent.target, intent.after, history.inventory)
            if not is_after:
                is_before, before_seen = _marker_check(root_fd, intent.target, intent.before, history.inventory)
                if not is_before and intent.step == 'marker-fill':
                    partial = {**intent.before, 'build': {
                        'kind': 'owned', 'identity': history.build_identity,
                    }}
                    owned_partial, _ = _marker_check(root_fd, intent.target, partial, history.inventory)
                    if owned_partial:
                        journal.receipt(intent.id, {'abandoned': True})
                        return 'receipt'
                if not is_before:
                    raise _marker_unknown(intent.before, intent.after,
                                          {'before': before_seen, 'after': after_seen})
                _marker_effect(root, root_fd, intent)
            elif intent.step == 'marker-fill':
                _sync_marker_tree(root / intent.target['slots']['build'])
            _sync_dir(root)
            correct, observed = _marker_check(root_fd, intent.target, intent.after, history.inventory)
            if not correct:
                raise _marker_unknown(intent.before, intent.after, observed)
            if is_after and observed != after_seen:
                raise _marker_unknown(intent.before, intent.after, observed)
        receipt = ({'build_identity': observed['build']['identity']} if intent.step == 'marker-open'
                   else intent.after)
    except (ValueError, TypeError, KeyError) as error:
        raise _marker_unknown(intent.before, intent.after, {'observation_error': str(error)}) from error
    journal.receipt(intent.id, receipt)
    return 'receipt' if is_after else 'marker'


def resume_marker(journal: Journal, intent: Intent, target: MarkerTarget) -> str:
    """Resume one exact forward or discarded-build step."""
    return _resume_marker_step(journal, intent, target, False)


def resume_inverse(journal: Journal, intent: Intent, target: MarkerTarget) -> str:
    """Resume one exact inverse step."""
    return _resume_marker_step(journal, intent, target, True)


def advance_marker(journal, target, preimage: PreimageManifest | None) -> str:
    """Drive the bound forward root to its final durable marker receipt."""
    status = 'receipt'
    while (progress := marker_progress(journal, target)).phase != 'marked':
        intent = progress.pending or write_marker_intent(journal, target, preimage)
        resume_marker(journal, intent, target)
        status = 'marker'
    return status


def advance_inverse(journal, target) -> str:
    """Drive the bound inverse root through disposal to its final receipt."""
    status = 'receipt'
    while (progress := marker_progress(journal, target)).phase != 'restored':
        intent = progress.pending or write_inverse_intent(journal, target, None)
        resume_inverse(journal, intent, target)
        status = 'marker'
    return status


class _ChangedMarkerPreimage(StepStateUnknown):
    pass


def source_marker_phase(source: Journal, target: MarkerTarget) -> str:
    """Classify only the immutable orphaned history, before acquiring scopes."""
    progress = _read_marker_history(source, target, source=source).progress
    if progress.phase == 'none':
        return 'none'
    return 'marked' if progress.phase == 'marked' and progress.pending is None else 'unfinished'


def _adopted_history(source, journal, target):
    if journal is source:
        raise _marker_unknown('separate unmark journal', 'no alias', str(target.job_dir))
    if source_marker_phase(source, target) != 'marked':
        raise _marker_unknown('marked source history', 'adopted inverse', str(target.root_path))
    return _read_marker_history(journal, target, source=source)


def adopted_progress(source: Journal, journal: Journal, target: MarkerTarget) -> MarkerProgress:
    return _adopted_history(source, journal, target).progress


def adopted_root_check(source: Journal, journal: Journal, target: MarkerTarget) -> str:
    """Read the whole bound state, including absent sibling and disposal slots."""
    try:
        history = _adopted_history(source, journal, target)
    except _ChangedMarkerPreimage:
        return 'other'
    pending = history.progress.pending
    last = history.last
    if pending is not None:
        states = (pending.after, pending.before)
        answer = 'at-step'
    else:
        states = (last.after,)
        phase = history.progress.phase
        if phase == 'marked':
            answer = 'marked'
        elif phase == 'restored':
            answer = 'preimage'
        else:
            answer = 'at-step'
    try:
        with _open_marker_directory(Path(target.root_path)) as root_fd:
            if any(_marker_check(root_fd, last.target, state, history.inventory)[0]
                   for state in states):
                return answer
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 'other'


def advance_adopted_inverse(source: Journal, journal: Journal, target: MarkerTarget) -> str:
    status = 'receipt'
    while True:
        history = _adopted_history(source, journal, target)
        if history.progress.phase == 'restored':
            return status
        intent = history.progress.pending or _write_marker_step(
            journal, target, history, True, None, source=source)
        _resume_marker_step(journal, intent, target, True, source=source)
        status = 'marker'
