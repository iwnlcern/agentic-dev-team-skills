"""Fixture-qualified Store migration through manifest, quiet and durable copy."""

from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
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

from relay_engine import store_import, store_layout, store_steps, submit
from relay_engine.store_evidence import _sync_dir, _sync_file


_LEDGER = '.engine/ledger.db'
_LEDGER_FILES = (_LEDGER, _LEDGER + '-wal', _LEDGER + '-shm')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# Exact moved-layout marker from the Platform h-prime contract.
_MOVED_MARKER_SHA256 = '4a9c34859cf78db4a00dfcdbf152dc08650613784507b1b9eea6a9fc024632f0'
_QUIET_SECONDS = 60
_CHUNK_SIZE = 1024 * 1024
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
        invalid = 'copy-input-invalid'
        if phase != 'copy' or data['job_id'] != job_id:
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
