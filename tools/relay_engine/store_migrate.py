"""Fixture-qualified Store migration from manifest through cutover."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, is_dataclass, replace
import errno
import fcntl
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time

from relay_engine import identity_import, imports, store_backup, store_import, store_layout, store_steps, submit
from relay_engine.store_evidence import _sync_dir, _sync_file


_LEDGER = '.engine/ledger.db'
_LEDGER_FILES = (_LEDGER, _LEDGER + '-wal', _LEDGER + '-shm')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Exact moved-layout marker from the Platform h-prime contract.
_MOVED_MARKER_SHA256 = '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0'
_QUIET_SECONDS = 60
_CHUNK_SIZE = 1024 * 1024
_IMPORT_BATCH_SIZE = 256
_COPY_FORMAT = 1
_RENDERED = 'rendered'
_RECEIPT = 'rendered-receipt.json'
_COPY_MANIFEST = 'manifest.json'
_DIGEST = re.compile(r'[0-9a-f]{64}')



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


class CutoverStarted(ValueError):
    """A validated migration cannot be rebuilt after cutover starts."""


class HoldMissing(ValueError):
    """A migration operation lacks the maintenance hold it requires."""


class NotQuiet(ValueError):
    """A legacy daemon is up, or a named source entry moved."""


class LegacySourceLink(ValueError):
    """A selected legacy path includes a symbolic link."""


class CopiedSourceError(ValueError):
    """Committed copy data is incompatible or failed verification."""

    def __init__(self, reason, job_id, root_index, path, detail):
        self.reason = reason
        self.job_id = job_id
        self.root_index = root_index
        self.path = path
        super().__init__(f'{reason}: migration {job_id}, root {root_index}, {path}: {detail}')


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
def _job(root, job_id, *, read_check=None):
    # The directory inode serializes phase/retry operations without another
    # writable job record. A reader of an existing reservation uses its journal.
    path = _job_path(root, job_id)
    fd = os.open(path, _DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if read_check is not None:
            # A writable WAL connection can checkpoint a crashed writer on
            # close, even when this operation issued no SQL writes.
            uri = (path / 'job.sqlite3').absolute().as_uri() + '?mode=ro'
            try:
                wal_has_content = (path / 'job.sqlite3-wal').stat().st_size != 0
            except FileNotFoundError:
                wal_has_content = False
            if not wal_has_content:
                # Under this job's exclusive lock an empty/missing WAL is
                # stable, and an immutable read creates no SQLite side files.
                uri += '&immutable=1'
            with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as conn:
                read_check(store_steps.Journal(conn))
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


def _read_file(parent, name, display, previous, output=None, *, observe_mtime=None):
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
        if observe_mtime is not None:
            observe_mtime(opened.st_mtime_ns)
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


def _walk(fd, root, relative, records, observe, *, directories=None):
    before = os.fstat(fd)
    for name in sorted(os.listdir(fd)):
        rel = relative / name
        path = root / rel
        entry = _entry(fd, name, path)
        if stat.S_ISDIR(entry.st_mode):
            if directories is not None and rel.as_posix() not in directories:
                raise ValueError(f'unexpected copied directory: {path}')
            with _descriptor(name, _DIRECTORY, parent=fd, display=path) as child:
                if _signature(os.fstat(child)) != _signature(entry):
                    raise NotQuiet(f'legacy source moved: {path}')
                _walk(child, root, rel, records, observe, directories=directories)
                _match_entry(fd, name, path, entry)
        elif stat.S_ISREG(entry.st_mode):
            records[rel.as_posix()] = observe(rel, fd, name, path, entry)
        else:
            raise NotQuiet(f'unsupported legacy source entry: {path}')
    if _signature(os.fstat(fd)) != _signature(before):
        raise NotQuiet(f'legacy directory moved: {root / relative}')


def _fingerprint(root, mtimes, *, rendered=None, selected=()):
    records = {}

    def observe(relative, parent, name, path, entry):
        key = relative.as_posix()

        def capture_mtime(value):
            mtimes[key] = value

        if key in selected:
            output_path = rendered / relative
            with _private_file(output_path) as output:
                checked = _read_file(parent, name, path, entry, output, observe_mtime=capture_mtime)
                _sync_file(output)
                return checked
        return _read_file(parent, name, path, entry, observe_mtime=capture_mtime)

    with _legacy_dir(root) as fd:
        _walk(fd, root, Path(), records, observe)
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
                times = [{} for _ in paths]
                fingerprints = [_fingerprint(path, mtimes) for path, mtimes in zip(paths, times, strict=True)]
                return fingerprints, times

            first, first_times = sample()
            time.sleep(_QUIET_SECONDS)
            _bound_turn(turns, data)
            second, second_times = sample()
            for path, before, after in zip(paths, first, second, strict=True):
                _match_fingerprint(path, before, after)
            _bound_turn(turns, data, lambda: journal.checkpoint(
                'quiet', {**data, 'fingerprints': second,
                          'fingerprint_mtimes': [first_times, second_times]},
            ))
        elif phase not in ('quiet', 'copy'):
            raise ValueError(f'migration {job_id} cannot enter quiet from {phase}')
        return QuietResult(job_id)


def _canonical_bytes(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode() + b'\n'


def _read_at(parent, path, output):
    return _read_file(parent, path.name, path, _entry(parent, path.name, path), output)


def _copy_ledger(path, dest, expected):
    engine = path / '.engine'
    with _legacy_dir(engine) as parent:
        before = os.fstat(parent)
        for relative in _LEDGER_FILES:
            if relative not in expected:
                continue
            file = path / relative
            with _private_file(dest / file.name) as output:
                if _read_at(parent, file, output) != expected[relative]:
                    raise NotQuiet(f'legacy source moved: {file}')
                _sync_file(output)
        if _signature(os.fstat(parent)) != _signature(before):
            raise NotQuiet(f'legacy directory moved: {engine}')


def _verified_ledger(parent, folder, item, expected):
    with tempfile.TemporaryDirectory(prefix='copied-ledger-') as temporary:
        duplicate = Path(temporary)
        for relative in _LEDGER_FILES:
            if relative not in expected:
                continue
            path = folder / Path(relative).name
            with _private_file(duplicate / path.name) as output:
                if _read_at(parent, path, output) != expected[relative]:
                    raise ValueError(f'copied bytes failed verification: {path}')
        ledger = store_import._read_ledger(duplicate)
    if any(ledger[0][key] != item[key] for key in ('root_uuid', 'run_id')):
        raise ValueError(f'copied ledger identity differs from fixed job input: {folder / "ledger.db"}')
    return ledger


def _canonical_path(value):
    if not isinstance(value, str) or not value or '\x00' in value:
        return False
    path = Path(value)
    return (not path.is_absolute() and path.as_posix() == value and value != '.'
            and '..' not in path.parts)


def _quiet_evidence(data, index):
    records = data['fingerprints'][index]
    first, second = data['fingerprint_mtimes']
    before, after = first[index], second[index]
    if (not isinstance(records, dict) or _LEDGER not in records
            or not isinstance(before, dict) or not isinstance(after, dict)
            or before.keys() != records.keys() or after.keys() != records.keys()):
        raise ValueError('quiet file and mtime records differ')
    for name, record in records.items():
        if (not _canonical_path(name) or not isinstance(record, dict)
                or record.keys() != {'size', 'sha256'} or type(record['size']) is not int
                or record['size'] < 0 or not isinstance(record['sha256'], str)
                or _DIGEST.fullmatch(record['sha256']) is None
                or type(before[name]) is not int or type(after[name]) is not int):
            raise ValueError(f'invalid quiet file or mtime record: {name}')
    return records, before, after


def _render_receipt(item, ledger, expected, before, after, root):
    selected, defects = {}, {}
    previous_seq = None
    identity = {key: item[key] for key in ('root_uuid', 'run_id')}
    for row in ledger[1]:
        seq, literal = row['seq'], row['rendered_path']
        if type(seq) is not int or (previous_seq is not None and seq <= previous_seq):
            raise ValueError('invalid or duplicate copied ledger sequence')
        previous_seq = seq
        if not _canonical_path(literal) or literal in _LEDGER_FILES:
            reason = 'unsafe rendered path in ledger'
        elif literal not in expected:
            reason = 'rendered path missing from source manifest'
        else:
            if before[literal] != after[literal]:
                raise NotQuiet(f'legacy source mtime changed: {root / literal}')
            selected[literal] = {**expected[literal], 'mtime_ns': after[literal]}
            continue
        defects[seq] = {**identity, 'seq': seq, 'path': literal, 'reason': reason}
    receipt = {**identity, 'version': _COPY_FORMAT, 'defects': list(defects.values()),
               'files': [{'path': name, **selected[name]} for name in sorted(selected)]}
    return receipt, selected, defects


def _render_dirs(selected):
    return {parent.as_posix() for name in selected for parent in Path(name).parents}


def _write_metadata(path, payload):
    with _private_file(path) as output:
        output.write(payload)
        _sync_file(output)


def _copy_root(path, dest, item, records, before, after):
    _copy_ledger(path, dest, records)
    with _legacy_dir(dest) as parent:
        ledger = _verified_ledger(parent, dest, item, records)
    receipt, selected, _ = _render_receipt(item, ledger, records, before, after, path)
    rendered = dest / _RENDERED
    directories = sorted(_render_dirs(selected) | {'.'}, key=lambda name: (len(Path(name).parts), name))
    for name in directories:
        (rendered / name).mkdir(mode=0o700)
    captured_times = {}
    actual = _fingerprint(path, captured_times, rendered=rendered, selected=selected)
    _match_fingerprint(path, records, actual)
    for name in selected:
        if captured_times[name] != after[name]:
            raise NotQuiet(f'legacy source mtime changed: {path / name}')
    for name in reversed(directories):
        _sync_dir(rendered / name)
    _write_metadata(dest / _COPY_MANIFEST, _canonical_bytes(actual))
    receipt_bytes = _canonical_bytes(receipt)
    _write_metadata(dest / _RECEIPT, receipt_bytes)
    _sync_dir(dest)
    return hashlib.sha256(receipt_bytes).hexdigest()


def copy(root, turns: store_steps.TurnRunner, job_id: str) -> None:
    """Publish an independent verified copy, then record its durable checkpoint."""
    with _job(store_layout.resolve_root(root), job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        if phase == 'copy':
            return
        if phase != 'quiet':
            raise ValueError(f'migration {job_id} has no quiet checkpoint')
        published = job / 'source'
        with tempfile.TemporaryDirectory(prefix='.source-', dir=job) as staging:
            candidate = Path(staging) / 'source'
            candidate.mkdir(mode=0o700)
            digests = []
            for index, item in enumerate(data['roots']):
                dest = candidate / str(index)
                dest.mkdir(mode=0o700)
                digests.append(_copy_root(Path(item['path']), dest, item, *_quiet_evidence(data, index)))
            _sync_dir(candidate)

            def finish():
                # A published tree without this checkpoint is provisional.
                # Revalidate the source before replacing it on any retry.
                if published.exists():
                    shutil.rmtree(published)
                os.replace(candidate, published)
                _sync_dir(job)
                journal.checkpoint('copy', {**data, 'copy_format': _COPY_FORMAT, 'rendered_receipts': digests})
            _bound_turn(turns, data, finish)


def _contents(parent, file):
    with io.BytesIO() as output:
        record = _read_at(parent, file, output)
        return record, output.getvalue()


def _read_rendered(root, selected):
    payloads = {}

    def observe(relative, parent, name, path, entry):
        key = relative.as_posix()
        if key not in selected:
            raise ValueError(f'unexpected copied render: {path}')
        with io.BytesIO() as output:
            record = _read_file(parent, name, path, entry, output)
            payloads[key] = output.getvalue()
        if any(selected[key][field] != value for field, value in record.items()):
            raise ValueError(f'copied render bytes failed verification: {path}')
        return record

    with _legacy_dir(root) as parent:
        _walk(parent, root, Path(), {}, observe, directories=_render_dirs(selected))
    if missing := selected.keys() - payloads.keys():
        raise ValueError(f'missing copied render: {root / min(missing)}')
    return payloads


def _read_copy_root(folder, data, index):
    item = data['roots'][index]
    records, before, after = _quiet_evidence(data, index)
    with _legacy_dir(folder) as parent:
        snapshot = os.fstat(parent)
        names = {Path(name).name for name in _LEDGER_FILES if name in records}
        names.update((_RENDERED, _RECEIPT, _COPY_MANIFEST))
        if extras := set(os.listdir(parent)) ^ names:
            raise ValueError(f'missing or unexpected committed copy inputs: {sorted(extras)}')
        _, manifest_bytes = _contents(parent, folder / _COPY_MANIFEST)
        if manifest_bytes != _canonical_bytes(records):
            raise ValueError(f'copy manifest differs from checkpoint: {folder / _COPY_MANIFEST}')
        receipt_record, receipt_bytes = _contents(parent, folder / _RECEIPT)
        if receipt_record['sha256'] != data['rendered_receipts'][index]:
            raise ValueError(f'copy receipt digest differs from checkpoint: {folder / _RECEIPT}')
        ledger = _verified_ledger(parent, folder, item, records)
        receipt, selected, defects = _render_receipt(item, ledger, records, before, after, folder)
        if receipt_bytes != _canonical_bytes(receipt):
            raise ValueError(f'copy receipt contradicts verified ledger or quiet evidence: {folder / _RECEIPT}')
        payloads = _read_rendered(folder / _RENDERED, selected)
        if _signature(os.fstat(parent)) != _signature(snapshot):
            raise ValueError(f'committed copy directory changed during verification: {folder}')

    def rendered(row):
        if defect := defects.get(row['seq']):
            return None, None, defect['reason']
        path = row['rendered_path']
        ns = selected[path]['mtime_ns']
        seconds = ns // 1_000_000_000 if ns >= 0 else -(-ns // 1_000_000_000)
        return payloads[path], seconds, None

    manifest = {name: (record['size'], record['sha256']) for name, record in records.items()}
    return store_import._source_root(folder, ledger, manifest, manifest_bytes, rendered)


def read_copied_sources(root, turns: store_steps.TurnRunner, job_id: str) -> tuple[store_import.SourceRoot, ...]:
    """Read and verify the fixed, committed source copies in job-manifest order."""
    with _job(store_layout.resolve_root(root), job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        return _read_committed_sources(job, phase, data, job_id)


def _read_committed_sources(job, phase, data, job_id, *, prepared_ok=False):
    invalid = 'copy-input-invalid'
    valid_phase = phase == 'copy' or (prepared_ok and phase in ('import', 'check'))
    if not valid_phase or data['job_id'] != job_id:
        raise CopiedSourceError(invalid, job_id, 0, 'checkpoint', 'expected this job in copy phase')
    if 'copy_format' not in data and 'rendered_receipts' not in data:
        raise CopiedSourceError('copy-format-incompatible', job_id, 0, _RECEIPT,
                                'original rendered mtime evidence is absent')
    version = data.get('copy_format')
    if type(version) is not int or version != _COPY_FORMAT:
        raise CopiedSourceError(invalid, job_id, 0, _RECEIPT, 'invalid copy format')
    try:
        roots = data['roots']
        count = len(roots)
        first, second = data['fingerprint_mtimes']
        if (not isinstance(roots, list) or not count or any(
            not isinstance(records, list) or len(records) != count for records in
            (data['fingerprints'], first, second, data['rendered_receipts'])
        )):
            raise ValueError('source root, fingerprint and receipt counts differ')
        with _legacy_dir(job / 'source') as parent:
            if set(os.listdir(parent)) != {str(index) for index in range(count)}:
                raise ValueError('committed source root roster differs')
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise CopiedSourceError(invalid, job_id, 0, _RECEIPT, str(exc)) from exc
    sources = []
    for index in range(count):
        try:
            sources.append(_read_copy_root(job / 'source' / str(index), data, index))
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
            raise CopiedSourceError(invalid, job_id, index, _RECEIPT, str(exc)) from exc
    return tuple(sources)


def _write_import_batches(conn, data, relays, identities):
    project_id = data['project_id']
    marked_at_ms = data['marked_at_ms']
    seats = {seat.id: seat for seat in identities.seats}
    instances = {instance.id: instance.name for instance in identities.instances}
    seat_ids = {
        instances[seat.team_instance_id] + '.' + seat.role: seat.id
        for seat in identities.seats
    }
    seat_ids['operator'] = 'operator'
    relay_ids = []
    for offset in range(0, max(1, len(relays)), _IMPORT_BATCH_SIZE):
        with conn:
            store_layout._pragma(conn, 'BEGIN IMMEDIATE')
            if offset == 0:
                identity_import.import_identities_step(
                    conn, project_id=project_id, project_name=data['project_name'],
                    created_at=min((relay.created_at for relay in relays), default=marked_at_ms),
                    instances=identities.instances, seats=identities.seats,
                    top_seat_id=identities.top_seat_id,
                )
            for relay in relays[offset:offset + _IMPORT_BATCH_SIZE]:
                sender = seat_ids[relay.sender]
                seat = seats.get(sender)
                imported = imports.ImportedRelay(
                    created_at=relay.created_at, kind=relay.kind, verdict=relay.verdict,
                    subject=relay.subject, sender=sender,
                    sender_role=None if seat is None else seat.role,
                    team_instance_id=None if seat is None else seat.team_instance_id,
                    to=tuple(seat_ids[party] for party in relay.to),
                    cc=tuple(seat_ids[party] for party in relay.cc),
                    parent_id=None if relay.parent is None else relay_ids[relay.parent],
                    parent_spelling=relay.parent_spelling, body=relay.body,
                    header_bytes=len(relay.header_bytes), header_sha256=relay.header_sha256,
                    answers=tuple(relay_ids[position] for position in relay.answers),
                )
                relay_id = imports.import_relay_step(
                    conn, project_id, imported, marked_at_ms=marked_at_ms,
                )
                conn.executemany(
                    'INSERT INTO imported_relays '
                    '(relay_id,root_uuid,seq,stamp,zone,utc_offset_s,rendered_path,origin,'
                    'headers_json,source_sha256,original_phase,foreign_to_json,delivery_marked_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    [(relay_id, item.root_uuid, item.seq, item.stamp, item.zone, item.offset,
                      item.rendered_path, item.origin, _canonical_bytes(item.headers).decode('utf-8'),
                      item.file_sha256, item.original_phase,
                      _canonical_bytes(item.foreign).decode('utf-8') if item.foreign else None,
                      marked_at_ms) for item in relay.provenance],
                ).close()
                relay_ids.append(relay_id)


def _discard_staging(staging):
    if os.path.lexists(staging):
        shutil.rmtree(staging)
        _sync_dir(staging.parent)


def _prepared_receipt(staging, ledger):
    with _legacy_dir(staging) as parent:
        if set(os.listdir(parent)) != {ledger.name}:
            raise ValueError('prepared import staging contains unexpected files')
        with _descriptor(ledger.name, _READ, parent=parent, display=ledger) as file_fd:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('prepared import ledger is not a regular file')
            with os.fdopen(file_fd, 'rb', closefd=False) as file:
                digest = hashlib.file_digest(file, 'sha256').hexdigest()
            if _signature(os.fstat(file_fd)) != _signature(info):
                raise ValueError('prepared import ledger changed during verification')
        return {'size': info.st_size, 'sha256': digest}


def import_staging(root, turns: store_steps.TurnRunner, job_id: str) -> store_import.Report:
    """Build a private prepared ledger from the job's verified committed copies."""
    with _job(store_layout.resolve_root(root), job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        sources = _read_committed_sources(job, phase, data, job_id, prepared_ok=True)
        return _import_staging(job, journal, turns, phase, data, sources)


def _import_staging(job, journal, turns, phase, data, sources):
    staging = job / 'staging'
    ledger = staging / 'ledger.sqlite3'
    fixed = _fixed_import_inputs(data)
    attempt = data.get('import_attempt')
    if attempt is None:
        if phase in ('import', 'check'):
            raise ValueError('prepared import checkpoint has no fixed attempt inputs')
        decisions = json.loads(_canonical_bytes(data.get('decisions', {})))
        attempt = {**fixed, 'decisions': decisions}
        data = {**data, 'import_attempt': attempt}
        _bound_turn(turns, data, lambda: journal.checkpoint('copy', data))
    elif attempt != {**fixed, 'decisions': attempt['decisions']}:
        raise ValueError('fixed import attempt inputs differ from the job checkpoint')

    planned = store_import.plan_relays(sources)
    identities = store_import.plan_identities(sources, attempt['decisions'])

    def report(plan):
        return store_import.Report(
            plan, identities, store_id=attempt['store_id'], project_id=attempt['project_id'],
        )

    if phase in ('import', 'check'):
        if _prepared_receipt(staging, ledger) != data.get('prepared'):
            raise ValueError('prepared import ledger differs from the import checkpoint')
        return report(planned)

    _discard_staging(staging)
    staging.mkdir(mode=0o700)
    try:
        with closing(submit.open_store(ledger)) as conn:
            store_layout._configure(conn)
            if store_layout._row(conn, 'PRAGMA foreign_keys') != (1,):
                raise store_layout.LayoutFault('foreign-keys-off')
            try:
                _write_import_batches(conn, attempt, planned.relays, identities)
            except identity_import.IdentityImportRefused as exc:
                refusal = store_import.Listed(exc.reason, str(exc.item))
            except ValueError as exc:
                refusal = store_import.Listed('import refused', str(exc))
            else:
                refusal = None
        if refusal is not None:
            _discard_staging(staging)
            return report(replace(planned, listed=planned.listed + (refusal,)))
        prepared = store_steps.prepare_database(ledger)
        _sync_dir(staging)
        _sync_dir(job)
    except BaseException:
        _discard_staging(staging)
        raise

    # After checkpoint publication begins, a retry must inspect the journal
    # before discarding any output. A failure can follow a durable commit.
    receipt = {'size': prepared.size, 'sha256': prepared.sha256}
    _bound_turn(turns, data, lambda: journal.checkpoint('import', {**data, 'prepared': receipt}))
    return report(planned)



def _fixed_import_inputs(data):
    return {key: data[key] for key in (
        'job_id', 'store_id', 'project_id', 'project_name', 'marked_at_ms', 'rendered_receipts',
    )}


def _publish_report(job, report):
    folder = job / 'report'
    folder.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.check-', dir=folder) as temporary:
        candidate = Path(temporary) / 'migration.txt'
        _write_metadata(candidate, report.render().encode('utf-8'))
        os.replace(candidate, folder / candidate.name)
    _sync_dir(folder)
    _sync_dir(job)


def decide(root, turns: store_steps.TurnRunner, job_id: str,
           decisions: Mapping[str, str]) -> store_import.Report:
    """Recheck original fingerprints, then rebuild from the fixed copied inputs."""
    def check_before_write(journal):
        _, data = journal.last_checkpoint()
        _bound_turn(turns, data)
        row = store_layout._row(
            journal._conn, "SELECT 1 FROM intents WHERE step LIKE 'cutover-%' OR step='move' "
            "OR step LIKE 'marker-%' LIMIT 1"
        )
        if row is not None:
            raise CutoverStarted(f'cutover already started for migration {job_id}')

    with _job(store_layout.resolve_root(root), job_id, read_check=check_before_write) as (job, journal):
        phase, data = journal.last_checkpoint()
        sources = _read_committed_sources(job, phase, data, job_id, prepared_ok=True)
        for item, expected in zip(data['roots'], data['fingerprints'], strict=True):
            path = Path(item['path'])
            try:
                actual = _fingerprint(path, {})
            except LegacySourceLink as error:
                raise NotQuiet(str(error)) from error
            _match_fingerprint(path, expected, actual)

        choices = json.loads(_canonical_bytes(dict(decisions)))
        data = {**data, 'decisions': choices,
                'import_attempt': {**_fixed_import_inputs(data), 'decisions': choices}}
        data.pop('prepared', None)
        _bound_turn(turns, data, lambda: journal.checkpoint('copy', data))
        report = _import_staging(job, journal, turns, 'copy', data, sources)

        def finish():
            _publish_report(job, report)
            if not report.unresolved():
                _, prepared = journal.last_checkpoint()
                journal.checkpoint('check', prepared)

        _bound_turn(turns, data, finish)
        return report


def _cutover_log(journal, step, target=None):
    cursor = journal._conn.execute(
        'SELECT i.id,i.target,i.before_state,i.after_state,r.observed '
        'FROM intents i LEFT JOIN receipts r ON r.intent_id=i.id WHERE i.step=? ORDER BY i.id',
        (step,),
    )
    try:
        found = [(store_steps.Intent(row[0], step, *(json.loads(value) for value in row[1:4])),
                  row[4] is not None) for row in cursor]
    finally:
        cursor.close()
    if target is not None:
        found = [entry for entry in found if target(entry[0].target)]
    if len(found) > 1:
        raise ValueError(f'duplicate cutover journal step: {step}')
    return found[0] if found else (None, False)


def _cutover_intent(journal, step, target, before, after):
    return store_steps.Intent(journal.intent(step, target, before, after), step, target, before, after)


def _cutover_holders(conn, scopes):
    return {scope: list(holder) for scope in scopes if
            (holder := store_layout._row(
                conn, 'SELECT job_id FROM maintenance_holds WHERE scope=?', (scope,),
            )) is not None}


def _cutover_route(conn, name):
    row = store_layout._row(
        conn, 'SELECT project_id,state,held_by_job FROM project_routes WHERE name=?', (name,),
    )
    return None if row is None else list(row)


def _cutover_before_reserve(conn, data, root):
    name, project, prior = (data[k] for k in ('project_name', 'project_id', 'prior_job'))
    observed = _cutover_route(conn, name)
    if data['mode'] == 'takeover':
        if observed != [project, 'creating', prior] or os.path.lexists(
                store_layout.ledger_path(root, project)):
            raise store_layout.ReservationChanged(f'reservation owner, state or ledger changed for {name!r}')
    elif observed is not None:
        raise store_layout.NameTaken(name, observed[0])


def _cutover_marker(data, job, journal, index, item, expected):
    target = store_steps.MarkerTarget(
        data['store_id'], data['job_id'], job, index, item['root_uuid'], item['path'],
    )
    progress = store_steps.marker_progress(journal, target)
    if progress.phase == 'marked':
        return
    preimage = None
    if progress.phase == 'none':
        path = Path(item['path'])
        try:
            _match_fingerprint(path, expected, _fingerprint(path, {}))
        except LegacySourceLink as error:
            raise NotQuiet(str(error)) from error
        original = job / 'originals' / str(index)
        if os.path.lexists(original):
            inventory, _ = store_steps._marker_preimage(original)
            preimage = store_steps.PreimageManifest(tuple(inventory))
        else:
            preimage = store_steps.capture_preimage(path, original)
    store_steps.advance_marker(journal, target, preimage)


def cutover(root, turns: store_steps.TurnRunner, job_id: str, now_ms: int) -> None:
    """Back up, reserve, move, mark, activate and release a validated migration."""
    root = store_layout.resolve_root(root)
    with _job(root, job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        name, project = data['project_name'], data['project_id']
        scopes = sorted([store_layout.project_scope(project)] + [
            store_layout.root_scope(item['root_uuid']) for item in data['roots']
        ])
        held = {scope: [job_id] for scope in scopes}
        creating = [project, 'creating', job_id]
        active = [project, 'active', job_id]
        route_before = [project, 'creating', data['prior_job']] if data['mode'] == 'takeover' else None
        ledger = store_layout.ledger_path(root, project)

        def check(conn, *, require_hold=False):
            if _identity(conn) != data['store_id']:
                raise JobOrphaned(f'Store changed for migration {job_id}')
            if require_hold and _cutover_holders(conn, scopes) != held:
                raise HoldMissing(f'cutover holds missing for migration {job_id}')

        def turn(access, action, *, require_hold=True):
            def body(conns):
                cat = conns['catalogue']
                check(cat, require_hold=require_hold)
                return action(cat)
            return turns.run(store_steps.Declared(access, {}), body)

        def route_is(cat, expected):
            observed = _cutover_route(cat, name)
            if observed != expected:
                raise store_steps.StepStateUnknown(expected, expected, observed)

        def raise_refused(intent):
            holder = intent.target['holder']
            if holder is None:
                raise store_layout.ReservationChanged(intent.target['reason'])
            raise store_layout.NameTaken(name, holder)

        def finish_refused(intent, done):
            if not done:
                def release_refused(cat):
                    observed = _cutover_holders(cat, scopes)
                    # Only inspect or release this job's scopes. A later job
                    # can already hold them after the refusal committed.
                    own = {scope: value for scope, value in observed.items() if value == [job_id]}
                    if own == held:
                        store_layout.release_holds(cat, job_id)
                    elif own:
                        raise store_steps.StepStateUnknown(held, {}, own)
                turn('written', release_refused, require_hold=False)
                journal.receipt(intent.id, intent.after)
            reserve_intent, reserve_done = _cutover_log(journal, 'cutover-reserve')
            if reserve_intent is not None and not reserve_done:
                journal.receipt(reserve_intent.id, {'refused': intent.id})
            raise_refused(intent)

        first_holder = turn('read', lambda cat: store_layout.name_holder(cat, name),
                            require_hold=False)
        if phase == 'cutover':
            return
        if phase != 'check':
            raise ValueError(f'cutover requires a validated check checkpoint, not {phase}')

        refused, refusal_done = _cutover_log(journal, 'cutover-refuse')
        if refused is not None:
            finish_refused(refused, refusal_done)
        release, released = _cutover_log(journal, 'cutover-release')
        if release is None:
            backup, backed_up = _cutover_log(journal, 'cutover-backup')
            if not backed_up:
                def before_backup(conns):
                    nonlocal backup
                    cat = conns['catalogue']
                    check(cat)
                    _cutover_before_reserve(cat, data, root)
                    observed = _cutover_holders(cat, scopes)
                    if observed:
                        scope, holder = next(iter(observed.items()))
                        raise store_layout.HoldConflict(holder[0], scope)
                    if backup is None:
                        backup = _cutover_intent(journal, 'cutover-backup', job_id, {}, held)

                observed = turn('read', lambda cat: _cutover_holders(cat, scopes), require_hold=False)
                if backup is None or observed != backup.after:
                    if backup is not None and any(value == [job_id] for value in observed.values()):
                        raise store_steps.StepStateUnknown(backup.before, backup.after, observed)
                    if first_holder is not None and first_holder != project:
                        raise store_layout.NameTaken(name, first_holder)
                    last = store_backup.last_complete(root, 'before-move')
                    stamp = now_ms if last is None else max(now_ms, last + 1)
                    try:
                        store_backup.backup(turns, root, 'before-move', stamp,
                                           then_hold=(job_id, 'cutover', scopes), before=before_backup)
                    except store_backup.BackupRefused as error:
                        if error.reason == 'creation-pending':
                            holder = turn('read', lambda cat: store_layout.name_holder(cat, name),
                                          require_hold=False)
                            if holder is not None and holder == error.detail and holder != project:
                                raise store_layout.NameTaken(name, holder) from error
                        raise
                turn('read', lambda _cat: None)
                journal.receipt(backup.id, backup.after)

            reserve, reserved = _cutover_log(journal, 'cutover-reserve')
            if not reserved:
                def reserve_route(cat):
                    nonlocal reserve, refused
                    observed = _cutover_route(cat, name)
                    if reserve is not None and observed == reserve.after:
                        return
                    try:
                        _cutover_before_reserve(cat, data, root)
                        if reserve is None:
                            reserve = _cutover_intent(journal, 'cutover-reserve', project,
                                                      route_before, creating)
                        if data['mode'] == 'takeover':
                            store_layout.take_over_reservation(cat, project, data['prior_job'],
                                                               job_id, os.path.lexists(ledger))
                        else:
                            store_layout.reserve_project(cat, project, name, now_ms, held_by_job=job_id)
                    except (store_layout.NameTaken, store_layout.ReservationChanged) as error:
                        refused = _cutover_intent(
                            journal, 'cutover-refuse',
                            {'name': name, 'holder': getattr(error, 'holder_project_id', None),
                             'reason': str(error)}, held, {},
                        )
                        store_layout.release_holds(cat, job_id)

                turn('written', reserve_route)
                if refused is not None:
                    journal.receipt(refused.id, refused.after)
                    finish_refused(refused, True)
                journal.receipt(reserve.id, reserve.after)

            move, moved = _cutover_log(journal, 'move')
            if not moved:
                turn('read', lambda cat: route_is(cat, creating))
                staging = job / 'staging' / 'ledger.sqlite3'
                prepared = store_steps.Prepared(**data['prepared'])
                if move is None:
                    store_steps.move_database(journal, prepared, staging, ledger)
                else:
                    before, after = store_steps._states(prepared)
                    if (move.target, move.before, move.after) != (
                            {'src': str(staging), 'dst': str(ledger)}, before, after):
                        raise store_steps.StepStateUnknown(before, after, move)
                    store_steps.resume_move(journal, move)

            activate, activated = _cutover_log(journal, 'cutover-activate')
            if activate is None:
                for index, (item, expected) in enumerate(
                        zip(data['roots'], data['fingerprints'], strict=True)):
                    turn('read', lambda cat: route_is(cat, creating))
                    _cutover_marker(data, job, journal, index, item, expected)

            if not activated:
                def activate_route(cat):
                    nonlocal activate
                    if activate is not None and _cutover_route(cat, name) == activate.after:
                        return
                    route_is(cat, creating)
                    if activate is None:
                        activate = _cutover_intent(journal, 'cutover-activate', project, creating, active)
                    store_layout.activate_project(cat, project)
                turn('written', activate_route)
                journal.receipt(activate.id, activate.after)

        if not released:
            def release_span(cat):
                nonlocal release
                route_is(cat, active)
                observed = _cutover_holders(cat, scopes)
                if release is not None and not any(value == [job_id] for value in observed.values()):
                    return
                check(cat, require_hold=True)
                if release is None:
                    release = _cutover_intent(journal, 'cutover-release', job_id, held, {})
                store_layout.release_holds(cat, job_id)
            turn('written', release_span, require_hold=False)
            journal.receipt(release.id, release.after)
        turn('read', lambda cat: route_is(cat, active), require_hold=False)
        journal.checkpoint('cutover', data)


@dataclass(frozen=True)
class UndoResult:
    job_id: str
    project_id: str
    restored_roots: tuple[Path, ...]


class UndoRefused(Exception):
    """Undo cannot discard committed v3 work or a live queue action."""

    def __init__(self, reason, items):
        self.reason = reason
        self.items = items
        super().__init__(reason)


def first_v3_write(ledger_conn) -> str | None:
    """Name the first kind of work that makes this ledger forward-only."""
    for kind, sql in (
        ('relay', 'SELECT 1 FROM relays r WHERE NOT EXISTS '
                  '(SELECT 1 FROM imported_relays i WHERE i.relay_id=r.id) LIMIT 1'),
        ('slip', 'SELECT 1 FROM slips LIMIT 1'),
        ('claim', 'SELECT 1 FROM verification_requests LIMIT 1'),
    ):
        if store_layout._row(ledger_conn, sql) is not None:
            return kind
    return None


def undo(root, turns: store_steps.TurnRunner, job_id: str, *,
         queue_step: Callable[..., object], blocked_error: type[Exception], now_ms: int) -> UndoResult:
    """Fence and return a cutover to legacy before the first v3 write."""
    root = store_layout.resolve_root(root)
    with _job(root, job_id) as (job, journal):
        phase, data = journal.last_checkpoint()
        if phase not in ('cutover', 'undo'):
            raise ValueError(f'undo requires a completed cutover, not {phase}')
        project, name = data['project_id'], data['project_name']
        paths = tuple(Path(item['path']) for item in data['roots'])
        result = UndoResult(job_id, project, paths)
        _bound_turn(turns, data)
        if phase == 'undo':
            return result

        scopes = sorted([store_layout.project_scope(project)] + [
            store_layout.root_scope(item['root_uuid']) for item in data['roots']
        ])
        held = frozenset(scopes)
        active = [project, 'active', job_id]
        creating = [project, 'creating', job_id]
        before = {'route': active, 'holds': []}
        after = {'route': creating, 'holds': scopes}

        def checked_cat(conns):
            cat = conns['catalogue']
            if _identity(cat) != data['store_id']:
                raise JobOrphaned(f'Store changed for migration {job_id}')
            return cat

        def observe(cat):
            return {'route': _cutover_route(cat, name),
                    'holds': sorted(store_layout.holds_of(cat, job_id))}

        def step(access, action, *, require_hold=True):
            def body(conns):
                cat = checked_cat(conns)
                if require_hold and (observed := observe(cat)) != after:
                    raise store_steps.StepStateUnknown(after, after, observed)
                return action(cat)
            return turns.run(store_steps.Declared(access, {}), body)

        # A refusal has no catalogue effect. Its intent carries the full
        # observation, so a crash before the receipt cannot lose that record.
        for undone_refusal in journal.pending():
            if undone_refusal.step == 'undo-refuse':
                journal.receipt(undone_refusal.id, undone_refusal.target)
        route_intent, routed = _cutover_log(journal, 'undo-route')
        if route_intent is not None and (route_intent.target, route_intent.before, route_intent.after) != (
                project, before, after):
            raise store_steps.StepStateUnknown(before, after, route_intent)
        if not routed:
            observed = step('read', observe, require_hold=False)
            if route_intent is None or observed != after:
                if observed != before:
                    raise store_steps.StepStateUnknown(before, after, observed)

                def route_and_queue(conns):
                    nonlocal route_intent
                    cat = checked_cat(conns)
                    current = observe(cat)
                    if current != before:
                        raise store_steps.StepStateUnknown(before, after, current)
                    if route_intent is None:
                        route_intent = _cutover_intent(journal, 'undo-route', project, before, after)
                    if (kind := first_v3_write(conns[project])) is not None:
                        raise UndoRefused('first-v3-write', (kind,))
                    try:
                        queue_step(cat, project, job_id, now_ms=now_ms)
                    except blocked_error as error:
                        raise UndoRefused('queue-blocked', error.items) from error
                    store_layout.take_holds(cat, job_id, 'undo', scopes, now_ms)
                    store_layout.return_to_creating(cat, project, job_id)

                try:
                    turns.run(store_steps.Declared('written', {project: 'read'}), route_and_queue,
                              fence_project=project)
                except UndoRefused as error:
                    items = json.loads(json.dumps(error.items, default=_undo_item_data))
                    refusal = {'route_intent': route_intent.id, 'reason': error.reason, 'items': items}
                    ident = journal.intent('undo-refuse', refusal, before, before)
                    journal.receipt(ident, refusal)
                    raise
            journal.receipt(route_intent.id, after)

        ledger = store_layout.ledger_path(root, project)
        undone = job / 'undone' / 'ledger.sqlite3'
        prepare, prepared_done = _cutover_log(journal, 'undo-prepare')
        if prepare is not None and (prepare.target, prepare.before, prepare.after) != (
                str(ledger), None, 'prepared'):
            raise ValueError('undo prepare journal does not match the ledger')
        if not prepared_done:
            def prepare_ledger(_cat):
                nonlocal prepare
                if prepare is None:
                    prepare = _cutover_intent(journal, 'undo-prepare', str(ledger), None, 'prepared')
                prepared = store_steps.prepare_database(ledger)
                journal.receipt(prepare.id, {'size': prepared.size, 'sha256': prepared.sha256})
            step('read', prepare_ledger)
        prepared = store_steps.Prepared(**json.loads(store_layout._row(
            journal._conn, 'SELECT observed FROM receipts WHERE intent_id=?', (prepare.id,))[0]))
        move, moved = _cutover_log(journal, 'move', lambda target: target.get('dst') == str(undone))
        if not moved:
            def move_ledger(_cat):
                if move is None:
                    store_steps.move_database(journal, prepared, ledger, undone)
                else:
                    src, dst = store_steps._states(prepared)
                    if (move.target, move.before, move.after) != (
                            {'src': str(ledger), 'dst': str(undone)}, src, dst):
                        raise store_steps.StepStateUnknown(src, dst, move)
                    store_steps.resume_move(journal, move)
            step('read', move_ledger)

        release, released = _cutover_log(journal, 'undo-release')
        if release is None:
            for index, (item, expected) in enumerate(zip(data['roots'], data['fingerprints'], strict=True)):
                step('read', lambda _cat: _undo_root(data, job, journal, index, item, expected))

        released_state = {'route': creating, 'holds': []}
        if release is not None and (release.target, release.before, release.after) != (
                job_id, after, released_state):
            raise store_steps.StepStateUnknown(after, released_state, release)
        if not released:
            def release_holds(cat):
                nonlocal release
                observed = observe(cat)
                if release is not None and observed == released_state:
                    return
                if observed != after:
                    raise store_steps.StepStateUnknown(after, released_state, observed)
                if release is None:
                    release = _cutover_intent(journal, 'undo-release', job_id, after, released_state)
                if store_layout.release_holds(cat, job_id) != len(held):
                    raise HoldMissing(f'undo holds missing for migration {job_id}')
            step('written', release_holds, require_hold=False)
            journal.receipt(release.id, release.after)
        observed = step('read', observe, require_hold=False)
        if observed != released_state:
            raise store_steps.StepStateUnknown(after, released_state, observed)
        journal.checkpoint('undo', data)
        return result


def _undo_item_data(item):
    # The queue's blocking item is a dataclass behind the injected seam.
    return asdict(item) if is_dataclass(item) else str(item)


def _undo_root(data, job, journal, index, item, expected):
    path = Path(item['path'])
    target = store_steps.MarkerTarget(
        data['store_id'], data['job_id'], job, index, item['root_uuid'], item['path'],
    )
    store_steps.advance_inverse(journal, target)
    try:
        _match_fingerprint(path, expected, _fingerprint(path, {}))
    except LegacySourceLink as error:
        raise NotQuiet(str(error)) from error


@dataclass(frozen=True)
class UnmarkConfirmation:
    job_id: str
    project_name: str
    store_id: str
    route_state: str
    owner_job: str | None


@dataclass(frozen=True)
class UnmarkResult:
    lapsed: bool
    store_id: str | None = None
    route_state: str | None = None
    owner_job: str | None = None
    detail: str | None = None


def _unmark_route(cat, confirmation):
    route = _cutover_route(cat, confirmation.project_name)
    return (_identity(cat), 'not-found' if route is None else route[1],
            None if route is None else route[2])


def _unmark_source_manifest(source, orphan):
    _, data = source.last_checkpoint()
    targets = tuple(store_steps.MarkerTarget(
        data['store_id'], data['job_id'], orphan, index, item['root_uuid'], item['path'],
    ) for index, item in enumerate(data['roots']))
    bindings = {target.root_index: store_steps._marker_binding(target) for target in targets}
    labels = ','.join('?' for _ in store_steps._MARKER_SEQUENCE)
    rows = source._conn.execute(
        f'SELECT target FROM intents WHERE step IN ({labels}) ORDER BY id',
        store_steps._MARKER_SEQUENCE)
    try:
        for raw, in rows:
            encoded = json.loads(raw)
            binding = bindings.get(encoded['root_index'])
            if binding is None or any(encoded.get(key) != value for key, value in binding.items()):
                raise store_steps.StepStateUnknown('manifest-bound marker history', 'valid root',
                                                   encoded.get('root_path', str(orphan)))
    finally:
        rows.close()
    return data, targets


def unmark_after_restore(root, turns: store_steps.TurnRunner, confirmation: UnmarkConfirmation,
                         unmark_job_id: str, now_ms: int) -> UnmarkResult:
    """Restore only the confirmed orphan's marked roots, via a separate inverse journal."""
    if not isinstance(confirmation, UnmarkConfirmation):
        return UnmarkResult(True, detail='unmarking needs operator confirmation')
    root = store_layout.resolve_root(root)
    old_job = store_layout._canonical_job(confirmation.job_id)
    unmark_id = store_layout._canonical_job(unmark_job_id)
    expected = (confirmation.store_id, confirmation.route_state, confirmation.owner_job)

    def lapsed(observed, detail):
        return UnmarkResult(True, *observed, detail)

    def route_now(conns):
        return _unmark_route(conns['catalogue'], confirmation)

    def allowed(observed):
        return observed == expected and (observed[1:] == ('not-found', None)
                                         or observed[1:] == ('creating', unmark_id))

    observed = turns.run(store_steps.Declared('read', {}), route_now)
    if not allowed(observed):
        return lapsed(observed, 'the confirmed Store or route has changed')
    orphan = root / 'migrations' / old_job
    unmark = root / 'migrations' / unmark_id
    if old_job == unmark_id or any(p.is_symlink() for p in (orphan.parent, orphan, unmark)):
        return lapsed(observed, 'unmark journal must be separate from its source')
    unmark = store_steps.migration_dir(root, unmark_id)
    db = unmark / 'job.sqlite3'
    if os.path.lexists(db):
        meta = db.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise store_steps.StepStateUnknown('separate unmark journal', 'ordinary file', str(db))
    with closing(store_steps.open_journal(unmark, create=True)) as journal:
        recorded = {}
        with closing(journal._conn.execute('SELECT phase,data FROM checkpoints ORDER BY id')) as rows:
            for label, raw in rows:
                if label in recorded:
                    raise ValueError(f'duplicate unmark checkpoint: {label}')
                recorded[label] = json.loads(raw)
        if recorded and 'unmark-confirmation' not in recorded:
            raise ValueError('journal is not an unmark job')
        if 'unmark-confirmation' not in recorded:
            journal.checkpoint('unmark-confirmation', vars(confirmation))
        elif recorded['unmark-confirmation'] != vars(confirmation):
            return lapsed(observed, 'unmark job already binds a different confirmation')
        if 'unmark-refused' in recorded:
            return lapsed(observed, recorded['unmark-refused'])

        def bind_source(source):
            data, targets = _unmark_source_manifest(source, orphan)
            if (data['job_id'] != old_job or data['project_name'] != confirmation.project_name
                    or data['store_id'] == confirmation.store_id):
                raise ValueError('confirmation does not name an orphaned migration for this project')
            return data, targets

        if 'unmark-source' not in recorded:
            try:
                snapshot = store_steps.snapshot_source(orphan, unmark)
                source = store_steps.open_source(snapshot, unmark)
                try:
                    data, targets = bind_source(source)
                    marked = []
                    for target in targets:
                        try:
                            phase = store_steps.source_marker_phase(source, target)
                        except store_steps.StepStateUnknown as error:
                            raise ValueError(f'{target.root_path}: {error}') from error
                        if phase == 'unfinished':
                            raise ValueError(f'unfinished forward work: {target.root_path}')
                        if phase == 'marked':
                            marked.append(target.root_index)
                    if not marked:
                        raise ValueError('no marked roots to restore')
                finally:
                    store_steps.close_source(source)
            except (OSError, TypeError, ValueError, KeyError, store_steps.StepStateUnknown) as error:
                journal.checkpoint('unmark-refused', str(error))
                return lapsed(observed, str(error))
            record = dict(snapshot=vars(snapshot), targets=marked)
            journal.checkpoint('unmark-source', record)
        else:
            record = recorded['unmark-source']
            snapshot = store_steps.SourceSnapshot(**record['snapshot'])
        marked = record['targets']
        if not marked or list(marked) != sorted(set(marked)):
            raise ValueError('invalid pinned unmark target set')

        @contextmanager
        def bound_source():
            source = store_steps.open_source(snapshot, unmark)
            try:
                data, targets = bind_source(source)
                yield source, data, tuple(targets[index] for index in marked)
            finally:
                store_steps.close_source(source)

        with bound_source() as (_, data, targets):
            scopes = sorted({store_layout.project_scope(data['project_id']),
                             *(store_layout.root_scope(target.root_uuid) for target in targets)})
        required = frozenset(scopes)
        holds_intent, holds_done = _cutover_log(journal, 'unmark-holds')
        release_intent, release_done = _cutover_log(journal, 'unmark-release')
        if holds_intent is not None and (holds_intent.target, holds_intent.before, holds_intent.after) != (
                unmark_id, [], scopes):
            raise ValueError('unmark hold intent does not bind the pinned scopes')
        if release_intent is not None and (
                release_intent.target, release_intent.before, release_intent.after) != (
                unmark_id, scopes, []):
            raise ValueError('unmark release intent does not bind the pinned scopes')

        def checked(conns, *, require_holds=True):
            cat = conns['catalogue']
            current = _unmark_route(cat, confirmation)
            if not allowed(current):
                return cat, lapsed(current, 'the confirmed Store or route has changed')
            if require_holds and store_layout.holds_of(cat, unmark_id) != required:
                raise HoldMissing(f'unmark holds missing for migration {unmark_id}')
            return cat, None

        def release(refusal=False):
            nonlocal release_intent

            def turn(conns):
                nonlocal release_intent
                cat, stale = checked(conns, require_holds=False)
                if stale is not None:
                    return stale
                held = store_layout.holds_of(cat, unmark_id)
                if release_intent is not None and not held:
                    return None
                if held != required:
                    raise HoldMissing(f'unmark holds missing for migration {unmark_id}')
                if release_intent is None:
                    release_intent = _cutover_intent(journal, 'unmark-release', unmark_id, scopes, [])
                if store_layout.release_holds(cat, unmark_id) != len(required):
                    raise HoldMissing(f'unmark holds missing for migration {unmark_id}')
                return None
            stale = turns.run(store_steps.Declared('written', {}), turn)
            if stale is not None:
                return stale
            journal.receipt(release_intent.id, [])
            if refusal:
                return lapsed(observed, refusal)
            return UnmarkResult(False, *observed)

        if release_done:
            refusal = recorded.get('unmark-validation-refused')
            return lapsed(observed, refusal) if refusal else UnmarkResult(False, *observed)
        if release_intent is not None or 'unmark-validation-refused' in recorded:
            return release(recorded.get('unmark-validation-refused', False))

        if not holds_done:
            hold_id = holds_intent.id if holds_intent is not None else None

            def acquire(conns):
                nonlocal hold_id
                cat, stale = checked(conns, require_holds=False)
                if stale is not None:
                    return stale
                held = store_layout.holds_of(cat, unmark_id)
                if hold_id is not None and held == required:
                    return None
                if held:
                    raise HoldMissing(f'unmark holds differ for migration {unmark_id}')
                if hold_id is None:
                    hold_id = journal.intent('unmark-holds', unmark_id, [], scopes)
                store_layout.take_holds(cat, unmark_id, 'unmark', scopes, now_ms)
                return None

            try:
                stale = turns.run(store_steps.Declared('written', {}), acquire)
            except store_layout.HoldConflict as error:
                journal.checkpoint('unmark-refused', str(error))
                return lapsed(observed, str(error))
            if stale is not None:
                return stale
            journal.receipt(hold_id, scopes)

        def validate(conns):
            _, stale = checked(conns)
            if stale is not None:
                return stale
            with bound_source() as (source, _, targets):
                for target in targets:
                    if store_steps.adopted_root_check(source, journal, target) == 'other':
                        raise store_steps.StepStateUnknown('bound marker or inverse', 'exact preimage',
                                                           str(target.root_path))
            return None

        try:
            stale = turns.run(store_steps.Declared('read', {}), validate)
        except store_steps.StepStateUnknown as error:
            inverse = store_layout._row(journal._conn,
                                       "SELECT 1 FROM intents WHERE step='inverse-unplace' LIMIT 1")
            if inverse is None:
                journal.checkpoint('unmark-validation-refused', str(error))
                release(str(error))
            raise
        if stale is not None:
            return stale

        for index in range(len(marked)):
            def advance(conns):
                _, stale = checked(conns)
                if stale is not None:
                    return stale
                with bound_source() as (source, _, targets):
                    store_steps.advance_adopted_inverse(source, journal, targets[index])
                return None
            stale = turns.run(store_steps.Declared('read', {}), advance)
            if stale is not None:
                return stale
        return release()
