"""Fixture-qualified Store migration through manifest, quiet and durable copy."""

from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

from relay_engine import store_layout, store_steps, submit
from relay_engine.store_evidence import _sync_dir, _sync_file


_LEDGER = '.engine/ledger.db'
_LEDGER_FILES = (_LEDGER, _LEDGER + '-wal', _LEDGER + '-shm')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Exact moved-layout marker from the Platform h-prime contract.
_MOVED_MARKER_SHA256 = '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0'
_QUIET_SECONDS = 60
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class MigrationManifest:
    mode: str
    project_name: str
    roots: Sequence[Path]


@dataclass(frozen=True)
class QuietResult:
    job_id: str


class JobOrphaned(ValueError):
    """The job's Store identity no longer matches the catalogue."""


class HoldMissing(ValueError):
    """A migration operation lacks the maintenance hold it requires."""


class NotQuiet(ValueError):
    """A legacy daemon is up, or a named source entry moved."""


class LegacySourceLink(ValueError):
    """A selected legacy path includes a symbolic link."""


def _identity(conn):
    return store_layout._row(conn, 'SELECT store_id FROM catalogue_identity WHERE id=1')[0]


def _bound_turn(turns, data, action=lambda: None):
    def checked(conns):
        if _identity(conns['catalogue']) != data['store_id']:
            raise JobOrphaned(f"Store changed for migration {data['job_id']}")
        return action()
    return turns.run(store_steps.Declared('read', {}), checked)


def _job_path(root, job_id):
    return Path(root) / 'migrations' / store_layout._canonical_job(job_id)


@contextmanager
def _job(root, job_id):
    # The directory inode serializes phase/retry operations without another
    # writable job record. A reader of an existing reservation uses its journal.
    path = _job_path(root, job_id)
    fd = os.open(path, _DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with closing(store_steps.open_journal(path)) as journal:
            yield path, journal
    finally:
        os.close(fd)


def _eligible(conn, root, manifest):
    store_id = _identity(conn)
    name = manifest.project_name
    row = store_layout._row(
        conn, 'SELECT project_id, state, held_by_job FROM project_routes WHERE name=?', (name,),
    )
    if manifest.mode == 'new-project':
        if row is not None:
            raise store_layout.NameTaken(name, row[0])
        return store_id, None, None
    if row is None or row[1] != 'creating' or row[2] is None:
        raise ValueError(f'no inert migration reservation for {name!r}')

    project_id, _, prior_job = row
    scope = store_layout.project_scope(project_id)
    hold = store_layout._row(conn, 'SELECT job_id FROM maintenance_holds WHERE scope=?', (scope,))
    if hold is not None:
        raise store_layout.HoldConflict(hold[0], scope)
    if os.path.lexists(store_layout.ledger_path(root, project_id)):
        raise store_layout.ReservationChanged(f'project ledger already exists for {name!r}')

    # Earlier phases repeat the manifest's Store ID in each checkpoint. An
    # undo completion is inert even when its own receipt only names the project.
    try:
        with closing(store_steps.open_journal(_job_path(root, prior_job))) as journal:
            checkpoint = journal.last_checkpoint()
    except sqlite3.OperationalError as error:
        raise ValueError(f'cannot verify previous migration {prior_job}') from error
    if checkpoint is None or (
            checkpoint[0] != 'undo'
            and checkpoint[1].get('store_id') in (None, store_id)):
        raise ValueError(f'prior migration {prior_job} is still in progress or unverifiable')
    return store_id, project_id, prior_job


@contextmanager
def _descriptor(name, flags, *, parent=None, display=None):
    display = Path(name) if display is None else display
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if error.errno == errno.ELOOP or (info is not None and stat.S_ISLNK(info.st_mode)):
                raise LegacySourceLink(f'legacy source link: {display}') from error
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            raise NotQuiet(f'legacy source moved: {display}') from error
        raise
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _legacy_dir(path):
    # Walk every selected component via a parent descriptor. No pathname
    # resolution may traverse a symlink even if a component is swapped.
    with _descriptor(path.anchor, _DIRECTORY) as fd:
        yield from _descend(fd, Path(path.anchor), path.parts[1:])


def _descend(parent, prefix, names):
    if not names:
        yield parent
        return
    name, *rest = names
    path = prefix / name
    with _descriptor(name, _DIRECTORY, parent=parent, display=path) as fd:
        yield from _descend(fd, path, rest)
        _match_entry(parent, name, path, os.fstat(fd), content=False)


def _entry(parent, name, display):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError as error:
        raise NotQuiet(f'legacy source moved: {display}') from error
    if stat.S_ISLNK(info.st_mode):
        raise LegacySourceLink(f'legacy source link: {display}')
    return info


def _signature(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _match_entry(parent, name, display, previous, *, content=True):
    current = _entry(parent, name, display)
    unchanged = _signature(current) == _signature(previous) if content else os.path.samestat(current, previous)
    if not unchanged:
        raise NotQuiet(f'legacy source moved: {display}')


def _read_file(parent, name, display, previous, output=None):
    digest = hashlib.sha256()
    size = 0
    with _descriptor(name, _READ, parent=parent, display=display) as fd:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != _signature(previous):
            raise NotQuiet(f'legacy source moved: {display}')
        while chunk := os.read(fd, _CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            if output is not None:
                output.write(chunk)
        if size != opened.st_size or _signature(os.fstat(fd)) != _signature(opened):
            raise NotQuiet(f'legacy source moved: {display}')
        _match_entry(parent, name, display, opened)
    return {'size': size, 'sha256': digest.hexdigest()}


def _private_file(path):
    def open_exclusive(name, flags):
        return os.open(name, flags | os.O_NOFOLLOW, 0o600)

    return open(path, 'xb', opener=open_exclusive)


def _legacy_meta(path):
    with tempfile.TemporaryDirectory(prefix='adt-migration-meta-') as temporary:
        copied = Path(temporary) / 'ledger.db'
        with _legacy_dir(path) as root_fd, _descriptor(
                '.engine', _DIRECTORY, parent=root_fd, display=path / '.engine') as engine:
            engine_info = os.fstat(engine)
            daemon = path / '.engine' / 'daemon.json'
            try:
                marker = _entry(engine, daemon.name, daemon)
            except NotQuiet:
                marker = None
            if marker is not None:
                digest = _read_file(engine, daemon.name, daemon, marker)['sha256']
                if digest == _MOVED_MARKER_SHA256:
                    raise ValueError(f'legacy root already carries the moved-layout marker: {path}')
            for relative in _LEDGER_FILES:
                f = path / relative
                try:
                    previous = _entry(engine, f.name, f)
                except NotQuiet:
                    if relative == _LEDGER:
                        raise
                    continue
                with _private_file(copied.with_name(f.name)) as output:
                    _read_file(engine, f.name, f, previous, output)
            _match_entry(root_fd, '.engine', path / '.engine', engine_info)
        with closing(sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True)) as conn:
            with closing(conn.execute("SELECT key,value FROM meta WHERE key IN ('root_uuid','run_id')")) as rows:
                meta = dict(rows)
    if set(meta) != {'root_uuid', 'run_id'} or not meta['run_id']:
        raise ValueError(f'missing legacy migration identity: {path}')
    store_layout._canonical_id(meta['root_uuid'])
    return {'path': str(path), **meta}


def start(root, turns: store_steps.TurnRunner, manifest: MigrationManifest, now_ms: int) -> str:
    """Record the selected roots and the current Store's identity."""
    if manifest.mode not in ('new-project', 'takeover'):
        raise ValueError(f'unknown migration mode: {manifest.mode!r}')
    name = manifest.project_name
    if not isinstance(name, str) or not name.strip() or not name.isprintable():
        raise ValueError('migration needs a nonempty project name')
    paths = tuple(Path(path).absolute() for path in manifest.roots)
    if not paths or len(set(paths)) != len(paths) or any('..' in path.parts for path in paths):
        raise ValueError('migration needs unique lexical legacy roots')
    # Migration owns only its Store's job directories; a source cannot overlap
    # the directory where we will create or publish one of those jobs.
    jobs = store_layout.resolve_root(root) / 'migrations'
    if any(jobs.is_relative_to(path) or path.is_relative_to(jobs) for path in paths):
        raise ValueError('legacy root overlaps the migration job area')

    def check(conns):
        return _eligible(conns['catalogue'], root, manifest)

    declaration = store_steps.Declared('read', {})
    eligibility = turns.run(declaration, check)
    store_id, project_id, prior_job = eligibility
    sources = [_legacy_meta(path) for path in paths]
    if len({source['root_uuid'] for source in sources}) != len(sources):
        raise ValueError('duplicate legacy root identity')
    job_id = submit.new_uuid7(now_ms)
    data = {
        'job_id': job_id,
        'store_id': store_id,
        'project_id': project_id if project_id is not None else submit.new_uuid7(now_ms, last=job_id),
        'project_name': name,
        'mode': manifest.mode,
        'prior_job': prior_job,
        'marked_at_ms': now_ms,
        'roots': sources,
    }

    def record(conns):
        if check(conns) != eligibility:
            raise store_layout.ReservationChanged(f'migration eligibility changed for {name!r}')
        job = store_steps.migration_dir(root, job_id)
        with closing(store_steps.open_journal(job, create=True)) as journal:
            journal.checkpoint('manifest', data)
    turns.run(declaration, record)
    return job_id


def _walk(fd, root, relative, records, destination):
    before = os.fstat(fd)
    for name in sorted(os.listdir(fd)):
        rel = relative / name
        path = root / rel
        entry = _entry(fd, name, path)
        if stat.S_ISDIR(entry.st_mode):
            with _descriptor(name, _DIRECTORY, parent=fd, display=path) as child:
                if _signature(os.fstat(child)) != _signature(entry):
                    raise NotQuiet(f'legacy source moved: {path}')
                _walk(child, root, rel, records, destination)
                _match_entry(fd, name, path, entry)
        elif stat.S_ISREG(entry.st_mode):
            key = rel.as_posix()
            if destination is not None and key in _LEDGER_FILES:
                with _private_file(destination / name) as output:
                    records[key] = _read_file(fd, name, path, entry, output)
                    _sync_file(output)
            else:
                records[key] = _read_file(fd, name, path, entry)
        else:
            raise NotQuiet(f'unsupported legacy source entry: {path}')
    if _signature(os.fstat(fd)) != _signature(before):
        raise NotQuiet(f'legacy directory moved: {root / relative}')


def _fingerprint(root, destination=None):
    records = {}
    with _legacy_dir(root) as fd:
        _walk(fd, root, Path(), records, destination)
    if _LEDGER not in records:
        raise NotQuiet(f'legacy source moved: {root / _LEDGER}')
    return records


def _match_fingerprint(root, expected, actual):
    for name in sorted(expected.keys() | actual.keys()):
        if expected.get(name) != actual.get(name):
            raise NotQuiet(f'legacy source moved: {root / name}')


def quiet(root, turns: store_steps.TurnRunner, job_id: str,
          status_reader: Callable[[Path], bool]) -> QuietResult:
    """Check the injected daemon status and bracket 60 seconds of source state."""
    with _job(root, job_id) as (_, journal):
        phase, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        if phase == 'manifest':
            paths = [Path(item['path']) for item in data['roots']]

            def sample():
                for path in paths:
                    if status_reader(path) is not False:
                        raise NotQuiet(f'legacy daemon not confirmed down: {path}')
                return [_fingerprint(path) for path in paths]

            first = sample()
            time.sleep(_QUIET_SECONDS)
            _bound_turn(turns, data)
            second = sample()
            for path, before, after in zip(paths, first, second, strict=True):
                _match_fingerprint(path, before, after)
            _bound_turn(turns, data, lambda: journal.checkpoint(
                'quiet', {**data, 'fingerprints': second},
            ))
        elif phase not in ('quiet', 'copy'):
            raise ValueError(f'migration {job_id} cannot enter quiet from {phase}')
        return QuietResult(job_id)


def _verify_copy(destination, expected):
    for relative in _LEDGER_FILES:
        if relative not in expected:
            continue
        file = destination / Path(relative).name
        with file.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            size = os.fstat(stream.fileno()).st_size
        if {'size': size, 'sha256': digest} != expected[relative]:
            raise OSError(f'copied bytes failed verification: {file}')


def copy(root, turns: store_steps.TurnRunner, job_id: str) -> None:
    """Publish an independent verified copy, then record its durable checkpoint."""
    with _job(root, job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        if phase == 'copy':
            return
        if phase != 'quiet':
            raise ValueError(f'migration {job_id} has no quiet checkpoint')
        published = job / 'source'
        with tempfile.TemporaryDirectory(prefix='.source-', dir=job) as staging:
            staging = Path(staging)
            candidate = staging / 'source'
            candidate.mkdir(mode=0o700)
            for index, (item, expected) in enumerate(zip(data['roots'], data['fingerprints'], strict=True)):
                path = Path(item['path'])
                dest = candidate / str(index)
                dest.mkdir(mode=0o700)
                actual = _fingerprint(path, dest)
                _match_fingerprint(path, expected, actual)
                _verify_copy(dest, expected)
                with _private_file(dest / 'manifest.json') as output:
                    output.write(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode() + b'\n')
                    _sync_file(output)
                _sync_dir(dest)
            _sync_dir(candidate)

            def finish():
                # A published tree without this checkpoint is provisional.
                # Revalidate the source before replacing it on any retry.
                if published.exists():
                    shutil.rmtree(published)
                os.replace(candidate, published)
                _sync_dir(job)
                journal.checkpoint('copy', data)
            _bound_turn(turns, data, finish)
