"""Consistent, evidence-pinned backups inside the caller's declared turn."""
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
from pathlib import Path
import os
import stat
import shutil
import sqlite3
import time
import uuid

from relay_engine import store_evidence, store_layout, store_steps, version


_REASONS = ('daily', 'before-move', 'before-update')
_DAY_MS = 86400000


class BackupRefused(Exception):
    def __init__(self, reason, detail):
        self.reason, self.detail = reason, detail
        super().__init__(f'backup refused: {reason}: {detail}')


@dataclass(frozen=True)
class BackupSettings:
    daily_keep: int = 7
    event_keep_days: int = 30

    def __post_init__(self):
        if (type(self.daily_keep) is not int or self.daily_keep < 1
                or type(self.event_keep_days) is not int or self.event_keep_days < 0):
            raise ValueError('invalid backup retention')


@dataclass(frozen=True)
class BackupManifest:
    path: Path
    store_id: str
    reason: str
    now_ms: int
    files: Mapping[str, dict]
    held_seconds: float
    remains: tuple[str, ...]


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def present_ledgers(catalogue_conn, root) -> tuple[str, ...]:
    """Presence includes creations whose effect committed before activation."""
    return _present_routes(catalogue_conn, root)[1]


def _present_routes(catalogue_conn, root):
    routes = store_layout.project_routes(catalogue_conn)
    present = []
    for route in routes:
        try:
            os.stat(store_layout.ledger_path(root, route.project_id))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupRefused('copy-failed', route.project_id) from exc
        else:
            present.append(route.project_id)
    return routes, tuple(sorted(present))


def _route_states(conn):
    return {project: (state, owner) for project, state, owner in
            _rows(conn, 'SELECT project_id,state,held_by_job FROM project_routes')}


def backup(turns: store_steps.TurnRunner, root, reason: str, now_ms: int, *,
           then_hold: tuple[str, str, Sequence[str]] | None = None,
           before: Callable[[Mapping[str, sqlite3.Connection]], None] | None = None
           ) -> BackupManifest:
    for attempt in range(2):
        present, states = turns.run(store_steps.Declared('read', {}), lambda c:
            (present_ledgers(c['catalogue'], root), _route_states(c['catalogue'])))
        declared = store_steps.Declared('written' if then_hold else 'read',
                                       {p: 'read' for p in present})
        def body(conns):
            if before is not None:
                before(conns)
            return take(conns, root, reason, now_ms, then_hold=then_hold)
        try:
            return turns.run(declared, body)
        except store_layout.LayoutFault as original:
            if original.cause != 'no-project-row':
                raise
            creating = [p for p in present if states[p][0] == 'creating']
            for project in store_layout.declared_order(False, creating):
                try:
                    turns.run(store_steps.Declared(None, {project: 'read'}), lambda c: None)
                except store_layout.LayoutFault as probe:
                    if probe.cause == 'no-project-row':
                        raise BackupRefused('creation-pending', project) from original
            raise
        except BackupRefused as exc:
            if exc.reason != 'present-set-changed' or attempt:
                raise
    raise AssertionError('bounded retry exhausted')


def _file_record(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        while block := file.read(1024 * 1024):
            digest.update(block)
    return {'size': path.stat().st_size, 'sha256': digest.hexdigest()}


def _write_synced(path, data):
    with path.open('xb') as file:
        file.write(data)
        store_evidence._sync_file(file)
    store_evidence._sync_dir(path.parent)


def _copy(source, destination):
    # Only this auxiliary read connection performs the online backup.
    dest = sqlite3.connect(destination)
    try:
        source.backup(dest)
    finally:
        dest.close()
    with destination.open('rb') as file:
        store_evidence._sync_file(file)
    store_evidence._sync_dir(destination.parent)
    record = _file_record(destination)
    record['schema_version'], record['ddl_sha256'] = _rows(
        source, 'SELECT version,ddl_sha256 FROM schema_version WHERE id=1')[0]
    return record


def _remove_abandoned(directory):
    remains = []
    for path in sorted(directory.iterdir()):
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError:
            remains.append(path.name)
    store_evidence._sync_dir(directory)
    return tuple(remains)


def take(conns: Mapping[str, sqlite3.Connection], root, reason: str, now_ms: int, *,
         then_hold: tuple[str, str, Sequence[str]] | None = None) -> BackupManifest:
    """Copy while every declared source is locked; publish before taking holds."""
    started = time.monotonic()
    root = Path(root)
    cat = conns['catalogue']
    routes, present = _present_routes(cat, root)
    declared = tuple(sorted(key for key in conns if key != 'catalogue'))
    if present != declared:
        raise BackupRefused('present-set-changed', {'present': present, 'declared': declared})
    hold = store_layout.any_hold(cat)
    if hold is not None:
        raise BackupRefused('maintenance-held', hold)
    for route in routes:
        if route.state == 'active' and route.project_id not in present:
            raise BackupRefused('active-ledger-missing', route.project_id)
    states = _route_states(cat)
    for project in present:
        if states[project][0] == 'creating' and states[project][1] is not None:
            raise BackupRefused('owned-creation-pending', project)
    if reason not in _REASONS or type(now_ms) is not int or now_ms < 0:
        raise ValueError('invalid backup reason or time')
    if then_hold is not None and not cat.in_transaction:
        raise ValueError('backup holds require the caller transaction')
    created = published = False
    try:
        stamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        destination = root / 'backups' / (stamp + '-' + reason)
        store_evidence._mkdir(destination.parent)
        partial = destination.parent / '.partial'
        try:
            partial.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            store_evidence._sync_dir(partial.parent)
        if not stat.S_ISDIR(os.lstat(partial).st_mode):
            raise OSError('backup staging path is not a directory')
        remains = _remove_abandoned(partial)
        directory = partial / destination.name
        directory.mkdir(mode=0o700)
        created = True
        store_evidence._sync_dir(directory.parent)
        files = {}
        source = store_layout.open_catalogue_readonly(root)
        try:
            files['machine.sqlite3'] = _copy(source, directory / 'machine.sqlite3')
        finally:
            source.close()
        for project in present:
            relative = store_layout.ledger_path(root, project).relative_to(root)
            target = directory / relative
            store_evidence._mkdir(target.parent)
            source = store_layout.open_ledger_readonly(root, project)
            try:
                files[relative.as_posix()] = _copy(source, target)
            finally:
                source.close()
            for sha, codec in _rows(conns[project], 'SELECT sha256,codec FROM evidence_objects ORDER BY sha256'):
                live = store_evidence._object_path(root, project, sha, codec)
                relative = live.relative_to(root)
                target = directory / relative
                store_evidence._mkdir(target.parent)
                target.hardlink_to(live)
                with target.open('rb') as file:
                    store_evidence._sync_file(file)
                store_evidence._sync_dir(target.parent)
                files[relative.as_posix()] = _file_record(target)
        store_id = _rows(cat, 'SELECT store_id FROM catalogue_identity WHERE id=1')[0][0]
        held_seconds = time.monotonic() - started
        document = {'store_id': store_id, 'reason': reason, 'now_ms': now_ms,
                    'kit_build': {'version': version.KIT_VERSION,
                                  'fingerprint': version.fingerprint(Path(__file__).parent.parent)},
                    'files': files, 'held_seconds': held_seconds}
        encoded = (json.dumps(document, sort_keys=True, separators=(',', ':')) + '\n').encode()
        _write_synced(directory / 'manifest.sha256',
                      (hashlib.sha256(encoded).hexdigest() + '\n').encode())
        temporary = directory / 'manifest.tmp'
        _write_synced(temporary, encoded)
        temporary.rename(directory / 'manifest.json')
        store_evidence._sync_dir(directory)
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError('backup destination already exists')
        os.rename(directory, destination)
        published = True
        store_evidence._sync_dir(destination.parent)
        store_evidence._sync_dir(partial)
    except Exception as exc:
        if created and not published:
            try:
                shutil.rmtree(directory)
                store_evidence._sync_dir(directory.parent)
            except OSError as cleanup:
                exc.add_note(f'backup directory cleanup failed: {cleanup}')
        raise BackupRefused('copy-failed', str(exc)) from exc
    if then_hold is not None:
        job, kind, scopes = then_hold
        store_layout.take_holds(cat, job, kind, scopes, now_ms)
    return BackupManifest(destination, store_id, reason, now_ms, files, held_seconds, remains)


def _complete(root):
    directory = Path(root) / 'backups'
    if not directory.is_dir():
        return []
    complete = []
    for path in directory.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            encoded = (path / 'manifest.json').read_bytes()
            if (path / 'manifest.sha256').read_text().strip() != hashlib.sha256(encoded).hexdigest():
                continue
            value = json.loads(encoded)
            if (value['reason'] not in _REASONS or type(value['now_ms']) is not int
                    or not isinstance(value['files'], dict)):
                continue
            complete.append((value['now_ms'], path.name, value['reason'], path))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(complete)


def last_complete(root, reason: str) -> int | None:
    times = [stamp for stamp, _, why, _ in _complete(root) if why == reason]
    return max(times) if times else None


def prune(root, settings: BackupSettings, now_ms: int) -> list[str]:
    complete = _complete(root)
    if not complete:
        return []
    newest = complete[-1][0]
    dailies = [row for row in complete if row[2] == 'daily']
    keep_daily = {row[1] for row in dailies[-settings.daily_keep:]}
    removed = []
    for stamp, name, reason, path in complete:
        expired = (name not in keep_daily if reason == 'daily'
                   else stamp < now_ms - settings.event_keep_days * _DAY_MS)
        if expired and stamp < newest:
            shutil.rmtree(path)
            store_evidence._sync_dir(path.parent)
            removed.append(name)
    return removed


JOBS_LOCK_WAIT_S = 10
STOP_NOT_KEPT_LINE = "Project {name}'s stop was not kept: {name} is not in the restored backup."
NOTICES_LINE = "Stops and starts after the backup's moment are kept, but their notices are not in the restored queue."
UNMARK_OFFER_LINE = ("Project {name} is offline. The restored backup is from before {name} moved, but "
                     "{name}'s old folders are still marked as moved. Restore {name}'s old folders so 2.9.7 "
                     "can use them again? Work done in v3 after the backup is not in the restored backup.")
LEDGER_CLASSES = ('cancel', 'creating-no-row')
HELD_LINE = 'Restoring cancels every go-ahead and approval in project {name} and holds {name} until you end the hold at the terminal.'
BEING_CREATED_LINE = 'Project {name} was still being created at the backup\'s moment; the restore finishes creating it, and it is not held.'
RESTORE_LIMITS = (
    'A restore always cancels every go-ahead and approval, even when nothing was lost.',
    'Only a restore through the restore command is caught. A ledger file put back by hand, or an old copy a disk returns, is not noticed, and nothing is cancelled.',
    'Objections and waivers come back as they stood at the backup moment. An objection filed after it is gone until its reviewer files it again.',
    'During the hold no agent gets a go-ahead, and you end the hold at the terminal.',
    'Re-approval means each planner asks again for its plan or design to be reviewed, the implementer approves it, and its reviewer approves the new request.',
    'Nothing on the page shows that a project is held. You see it in the restore report at the terminal, and an agent whose go-ahead was refused is told the restore holds it.',
    'Branches tied to cancelled go-aheads stay with the same agent and bundle.',
)


class RestoreRefused(Exception):
    def __init__(self, reason, detail):
        self.reason, self.detail = reason, detail
        super().__init__(f'restore refused: {reason}: {detail}')


@dataclass(frozen=True)
class RestoreLoss:
    project_id: str
    project_name: str
    newest_relay: str | None
    newest_receipt: int | None
    readable: bool

    def line(self):
        if not self.readable:
            return f'Project {self.project_name}: writes after the backup could not be read.'
        if self.newest_relay is None and self.newest_receipt is None:
            return f'Project {self.project_name}: no later relay or receipt was found.'
        relay = self.newest_relay or 'none'
        receipt = 'none' if self.newest_receipt is None else str(self.newest_receipt)
        return f'Project {self.project_name}: newest relay lost: {relay}, newest receipt lost: {receipt}.'


@dataclass(frozen=True)
class MovedJob:
    job_id: str | None
    project_name: str | None
    marked_roots: tuple[str, ...]
    state: str

    def line(self):
        if self.state == 'unavailable':
            job = '' if self.job_id is None else f' for job {self.job_id}'
            return f'The move state{job} is unavailable.'
        roots = ', '.join(self.marked_roots)
        return (f'Project {self.project_name}, job {self.job_id}: these old folders are still marked '
                f'as moved: {roots}.')


@dataclass(frozen=True)
class RestoreReport:
    new_store_id: str
    losses: tuple[RestoreLoss, ...]
    moved_jobs: list[MovedJob]
    held: tuple[str, ...]
    being_created: tuple[str, ...]
    mode: str = 'readable'
    stops_not_kept: tuple[str, ...] = ()

    def lines(self) -> tuple[str, ...]:
        lines = []
        offers = set()
        for loss in self.losses:
            lines.append(loss.line())
            for index, job in enumerate(self.moved_jobs):
                if index not in offers and job.state == 'journal' and job.project_name == loss.project_name:
                    lines.append(UNMARK_OFFER_LINE.format(name=job.project_name))
                    offers.add(index)
            if loss.project_id in self.held:
                lines.append(HELD_LINE.format(name=loss.project_name))
            if loss.project_id in self.being_created:
                lines.append(BEING_CREATED_LINE.format(name=loss.project_name))
        for index, job in enumerate(self.moved_jobs):
            lines.append(job.line())
            if job.state == 'journal' and index not in offers:
                lines.append(UNMARK_OFFER_LINE.format(name=job.project_name))
        lines.extend(STOP_NOT_KEPT_LINE.format(name=name) for name in self.stops_not_kept)
        if self.mode == 'readable':
            lines.append(NOTICES_LINE)
        return tuple(lines) + RESTORE_LIMITS


def pending_creations(catalogue_conn) -> list[str]:
    return [project for project, in _rows(
        catalogue_conn, "SELECT project_id FROM project_routes WHERE state='creating'"
        " AND held_by_job IS NULL ORDER BY name")]


class JobsBusy(Exception):
    pass


class JobsLockError(Exception):
    def __init__(self, errno_name: str):
        self.errno_name = errno_name
        super().__init__(errno_name)


@contextmanager
def jobs_lock(root, *, wait_s: float = JOBS_LOCK_WAIT_S):
    path = Path(root) / 'jobs.lock'
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise JobsLockError(errno.errorcode.get(exc.errno, str(exc.errno))) from exc
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EINTR):
                    raise JobsLockError(errno.errorcode.get(exc.errno, str(exc.errno))) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JobsBusy() from exc
                time.sleep(min(0.05, remaining))
        yield
    finally:
        os.close(fd)


def _canonical_restore(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode()


def _restore_checkpoints(conn):
    checkpoints = {}
    for phase, raw in _rows(conn, 'SELECT phase,data FROM checkpoints ORDER BY id'):
        if phase in checkpoints and phase != 'restore-abandoned':
            raise ValueError(f'duplicate restore checkpoint: {phase}')
        checkpoints[phase] = json.loads(raw)
    return checkpoints


def _probe_restore(root, job_path):
    db = job_path / 'job.sqlite3'
    _restore_parents(db, root, missing=True)
    if not os.path.lexists(db):
        return {}, None
    _ordinary_file(db, root)
    with closing(_offline_read(db)) as conn:
        return _restore_checkpoints(conn), _rows(conn, 'SELECT phase FROM checkpoints ORDER BY id DESC LIMIT 1')


def _abandoned(job_id, checkpoints, last):
    start = checkpoints.get('restore-start')
    terminal = checkpoints.get('restore-abandoned')
    return (last == [('restore-abandoned',)] and isinstance(terminal, dict)
            and isinstance(start, dict)
            and terminal.get('job_id') == job_id
            and terminal.get('start_sha256') == hashlib.sha256(_canonical_restore(start)).hexdigest())


def open_restores(root) -> tuple[str, ...]:
    root = Path(root)
    recovery = root / 'recovery'
    if not os.path.lexists(recovery):
        return ()
    _restore_parents(recovery / 'unused', root)
    opened = []
    for folder in sorted(recovery.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        checkpoints, last = _probe_restore(root, folder)
        if 'restore-start' not in checkpoints:
            continue
        start = checkpoints['restore-start']
        if (start.get('job_id') != folder.name
                or (last != [('restore-done',)] and not _abandoned(folder.name, checkpoints, last))):
            opened.append(folder.name)
    return tuple(opened)


def _open_undo(root, store_id):
    migrations = root / 'migrations'
    if not migrations.is_dir() or migrations.is_symlink():
        return None
    for folder in sorted(migrations.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            store_layout._canonical_job(folder.name)
        except ValueError:
            continue
        db = folder / 'job.sqlite3'
        if not os.path.lexists(db):
            continue
        _ordinary_file(db, root)
        with closing(_offline_read(db)) as conn:
            route, routed = _journal_record_view(conn, 'undo-route', lambda _: True)
            if route is None:
                continue
            records = _rows(conn, 'SELECT data FROM checkpoints ORDER BY id DESC LIMIT 1')
            if not records or json.loads(records[0][0]).get('store_id') != store_id:
                continue
            if routed is not None:
                _, released = _journal_record_view(conn, 'undo-release', lambda _: True)
                if released is None:
                    return folder.name
            else:
                tail = _rows(conn, 'SELECT i.step,i.target,r.observed FROM intents i LEFT JOIN receipts r'
                             ' ON i.id=r.intent_id ORDER BY i.id DESC LIMIT 1')
                step, target, receipt = tail[0]
                if (step != 'undo-refuse' or receipt is None or target != receipt
                        or json.loads(receipt).get('route_intent') != route.id):
                    return folder.name
    return None


_RESTORE_CATALOGUE = ('machine.sqlite3', 'machine.sqlite3-wal', 'machine.sqlite3-shm')
_STOP_OBJECTS = frozenset({'engine_stops', 'engine_stop_events', 'engine_stops_one_open',
                           'engine_stops_guard_update', 'engine_stops_guard_delete',
                           'engine_stop_events_guard_update', 'engine_stop_events_guard_delete'})


def _restore_file_stat(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise store_steps.FenceBreach('ordinary file', 'ordinary file', str(path))
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _restore_observation(root, job_path):
    from relay_engine import catalogue, errors

    partial, observed = job_path / 'observed.partial', job_path / 'observed'
    for path in (partial, observed, *job_path.glob('observe-work-*')):
        if os.path.lexists(path):
            _restore_parents(path / 'unused', job_path)
            shutil.rmtree(path)
    store_evidence._mkdir(partial)
    stats = {name: _restore_file_stat(root / name) for name in _RESTORE_CATALOGUE}
    copies, lost_members = [], []
    for index, name in enumerate(_RESTORE_CATALOGUE):
        before = stats[name]
        if before is None:
            continue
        source = root / name
        target = partial / name
        if index < 2:
            with source.open('rb') as input_file, target.open('xb') as output:
                shutil.copyfileobj(input_file, output)
                os.fchmod(output.fileno(), 0o400)
                store_evidence._sync_file(output)
            record = _file_record(target)
            copies.append(dict(name=name, **record))
        else:
            record = _file_record(source)
        lost_members.append(dict(name=name, dev=before[0], ino=before[1], **record))
    for name, before in stats.items():
        if _restore_file_stat(root / name) != before:
            raise store_steps.FenceBreach(before, before, str(root / name))
    store_evidence._sync_dir(partial)
    os.rename(partial, observed)
    store_evidence._sync_dir(job_path)

    if stats[_RESTORE_CATALOGUE[0]] is None:
        return dict(mode='lost', observed=copies, observation_error='live catalogue is missing',
                    lost_members=lost_members)

    def work_copy():
        work = job_path / ('observe-work-' + uuid.uuid4().hex)
        store_evidence._mkdir(work)
        try:
            for row in copies:
                target = work / row['name']
                shutil.copyfile(observed / row['name'], target)
                target.chmod(0o600)
        except BaseException:
            shutil.rmtree(work)
            raise
        return work

    work = work_copy()
    try:
        try:
            catalogue_file = work / _RESTORE_CATALOGUE[0]
            with closing(sqlite3.connect(catalogue_file.as_uri() + '?mode=rw', uri=True)) as cat:
                catalogue._require_matching(cat)
                (store_id,), = _rows(cat, 'SELECT store_id FROM catalogue_identity WHERE id=1')
        except (sqlite3.Error, errors.EngineError, ValueError) as exc:
            return dict(mode='lost', observed=copies, observation_error=str(exc),
                        lost_members=lost_members)
    finally:
        shutil.rmtree(work)
    work = work_copy()
    try:
        catalogue_file = work / _RESTORE_CATALOGUE[0]
        with closing(sqlite3.connect(catalogue_file.as_uri() + '?mode=rw', uri=True)) as cat:
            holds = _rows(cat, 'SELECT job_id FROM maintenance_holds ORDER BY job_id LIMIT 1')
            if holds:
                raise RestoreRefused('job-open', holds[0][0])
            payload = dict(
                stops=_rows(cat, 'SELECT * FROM engine_stops ORDER BY row_id'),
                events=_rows(cat, 'SELECT * FROM engine_stop_events ORDER BY event_id'),
                sequence=_stop_sequence(cat),
                cursor=_rows(cat, "SELECT * FROM queue_highwaters WHERE project_id IS NULL AND source='stop_events'"),
                routes=[dict(project_id=route.project_id, name=route.name, state=route.state)
                        for route in store_layout.project_routes(cat)],
            )
    finally:
        shutil.rmtree(work)
    if (undo := _open_undo(root, store_id)) is not None:
        raise RestoreRefused('job-open', undo)
    work = work_copy()
    try:
        prepared = store_steps.prepare_database(work / _RESTORE_CATALOGUE[0])
    finally:
        shutil.rmtree(work)
    return dict(mode='readable', observed=copies, live_store_id=store_id, payload=payload,
                payload_sha256=hashlib.sha256(_canonical_restore(payload)).hexdigest(),
                prepared=vars(prepared))


def _restore_verify_observation(job_path, start):
    observed = job_path / 'observed'
    try:
        _restore_parents(observed / 'unused', job_path)
    except (FileNotFoundError, ValueError) as exc:
        raise store_steps.FenceBreach('observation directory', 'observation directory', str(observed)) from exc
    expected_names = {record['name'] for record in start['observed']}
    for path in sorted(observed.iterdir()):
        if path.name not in expected_names:
            raise store_steps.FenceBreach('only pinned members', 'only pinned members', str(path))
    for record in start['observed']:
        path = observed / record['name']
        expected = {key: record[key] for key in ('size', 'sha256')}
        if _restore_file_stat(path) is None or _file_record(path) != expected:
            raise store_steps.FenceBreach(expected, expected, str(path))
    if start['mode'] == 'readable':
        digest = hashlib.sha256(_canonical_restore(start['payload'])).hexdigest()
        if digest != start['payload_sha256']:
            raise store_steps.FenceBreach(start['payload_sha256'], start['payload_sha256'], 'payload')


def _restore_fence_live(source, start, *, allow_prepared):
    states = {}
    for name in _RESTORE_CATALOGUE:
        path = source.parent / name
        if _restore_file_stat(path) is not None:
            states[name] = _file_record(path)
    expected = {row['name']: {key: row[key] for key in ('size', 'sha256')} for row in start['observed']}
    if allow_prepared and states == {source.name: start['prepared']}:
        return True
    if states == expected:
        return False
    if allow_prepared and states.get(source.name) == start['prepared']:
        expected = {source.name: start['prepared']}
    name = next(name for name in _RESTORE_CATALOGUE if states.get(name) != expected.get(name))
    raise store_steps.FenceBreach(expected, start['prepared'], str(source.parent / name))


def _stop_sequence(conn):
    row = _rows(conn, "SELECT seq FROM sqlite_sequence WHERE name='engine_stop_events'")
    return row[0][0] if row else 0


def _restore_carry(journal, staged, payload):
    from relay_engine import catalogue, errors

    intent, done = _journal_record(journal, 'restore-carry', lambda target: target == 'machine.sqlite3')
    if done is not None:
        return
    ident = (journal.intent('restore-carry', 'machine.sqlite3', 'backup-stops', 'live-stops')
             if intent is None else intent.id)
    with closing(store_layout.open_catalogue(staged)) as cat, cat:
        cat.execute('BEGIN IMMEDIATE').close()
        sequence = max(_stop_sequence(cat), payload['sequence'])
        keep = {route.project_id for route in store_layout.project_routes(cat)}
        rows = [row for row in payload['stops'] if row[2] is None or row[2] in keep]
        stop_ids = {row[0] for row in rows}
        cat.execute('DROP TABLE engine_stop_events').close()
        cat.execute('DROP TABLE engine_stops').close()
        for statement in catalogue._TABLES:
            tokens = statement.split()
            object_name = tokens[3] if tokens[1] == 'UNIQUE' else tokens[2]
            if object_name in _STOP_OBJECTS:
                cat.execute(statement).close()
        cat.executemany('INSERT INTO engine_stops VALUES (?,?,?,?,?,?,?)', rows).close()
        cat.executemany('INSERT INTO engine_stop_events VALUES (?,?,?,?,?)',
                        [row for row in payload['events'] if row[1] in stop_ids]).close()
        cat.execute("DELETE FROM sqlite_sequence WHERE name='engine_stop_events'").close()
        cat.execute("INSERT INTO sqlite_sequence VALUES('engine_stop_events',?)", (sequence,)).close()
        cat.execute("DELETE FROM queue_highwaters WHERE project_id IS NULL AND source='stop_events'").close()
        cat.executemany('INSERT INTO queue_highwaters VALUES (?,?,?,?,?)', payload['cursor']).close()
        try:
            catalogue._require_matching(cat)
        except errors.EngineError as exc:
            if exc.code != 'E-SCHEMA-MISMATCH':
                raise
            raise store_steps.FenceBreach('matching catalogue schema', 'matching catalogue schema',
                                          exc.code) from exc
        failures = _rows(cat, 'PRAGMA foreign_key_check')
        if failures:
            raise store_steps.FenceBreach('foreign keys valid', 'foreign keys valid', failures)
    journal.receipt(ident, 'live-stops')


def _restore_excluded(staged, payload):
    with closing(_offline_read(store_layout.catalogue_path(staged))) as cat:
        restored = {route.project_id for route in store_layout.project_routes(cat)}
    absent = {row[2] for row in payload['stops'] if row[2] is not None and row[2] not in restored}
    return [row['name'] for row in payload['routes'] if row['project_id'] in absent]


def _restore_lost_members(journal, root, job_path, members):
    lost = job_path / 'lost'
    _restore_parents(lost / 'unused', job_path, missing=True)
    store_evidence._mkdir(lost)
    for member in members:
        name = member['name']
        source, target = root / name, lost / name
        binding = dict(member, src=str(source), dst=str(target))
        intent, done = _journal_record(journal, 'restore-lost-move', lambda value: value.get('name') == name)
        if intent is not None and intent.target != binding:
            raise store_steps.FenceBreach(binding, binding, intent.target)
        if done is not None:
            continue
        ident = (journal.intent('restore-lost-move', binding, 'at-source', 'at-destination')
                 if intent is None else intent.id)
        at_source, at_target = _restore_file_stat(source), _restore_file_stat(target)
        expected = (member['dev'], member['ino'], member['size'])
        digest = {key: member[key] for key in ('size', 'sha256')}
        if at_source is not None and at_target is None and at_source[:3] == expected and _file_record(source) == digest:
            if os.path.lexists(target):
                raise store_steps.FenceBreach('destination absent', 'destination absent', str(target))
            os.rename(source, target)
        elif (at_source is not None or at_target is None or at_target[:3] != expected
              or _file_record(target) != digest):
            raise store_steps.FenceBreach('bound lost member at source', 'bound lost member at destination', name)
        store_evidence._sync_dir(lost)
        store_evidence._sync_dir(root)
        journal.receipt(ident, 'at-destination')


def _restore_record_lost_stop(journal, root, stopped_by):
    intent, done = _journal_record(journal, 'restore-lost-stop', lambda value: value == 'store')
    if done is not None:
        return
    ident = (journal.intent('restore-lost-stop', 'store', 'unobserved', {'stopped_by': stopped_by})
             if intent is None else intent.id)
    by = stopped_by if intent is None else intent.after['stopped_by']
    with closing(store_layout.open_catalogue(root)) as cat, cat:
        cat.execute('BEGIN IMMEDIATE').close()
        if not _rows(cat, "SELECT row_id FROM engine_stops WHERE scope='store' AND ended_at IS NULL"):
            store_layout.record_stop(cat, 'store', None, by)
    journal.receipt(ident, 'stopped')


def _restore_member(name):
    parts = name.split('/')
    if name == 'machine.sqlite3':
        return Path(name)
    if (len(parts) < 3 or parts[0] != 'projects'
            or any(not part or part in ('.', '..') for part in parts)):
        raise ValueError('unexpected backup member')
    store_layout._canonical_id(parts[1])
    if parts[2:] != ['ledger.sqlite3'] and not (len(parts) >= 5 and parts[2:4] == ['evidence', 'objects']):
        raise ValueError('unexpected project backup member')
    return Path(*parts)


def _restore_parents(path, root, *, missing=False):
    for folder in reversed(path.relative_to(root).parents):
        try:
            mode = os.lstat(root / folder).st_mode
        except FileNotFoundError:
            if missing:
                return
            raise
        if not stat.S_ISDIR(mode):
            raise ValueError('restore path is not an ordinary directory')


def _ordinary_file(path, parent):
    _restore_parents(path, parent)
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError('restore member is not an ordinary file')


def _verified_backup(directory):
    try:
        manifest, checksum = directory / 'manifest.json', directory / 'manifest.sha256'
        _ordinary_file(manifest, directory)
        _ordinary_file(checksum, directory)
        raw = manifest.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if checksum.read_text().strip() != digest:
            raise ValueError('manifest digest differs')
        value = json.loads(raw)
        if (value['reason'] not in _REASONS or type(value['now_ms']) is not int
                or not isinstance(value['files'], dict) or 'machine.sqlite3' not in value['files']):
            raise ValueError('incomplete manifest')
        store_layout._canonical_id(value['store_id'])
        for name, expected in value['files'].items():
            member = directory / _restore_member(name)
            _ordinary_file(member, directory)
            if _file_record(member) != {key: expected[key] for key in ('size', 'sha256')}:
                raise ValueError(f'backup member differs: {name}')
        return digest, value
    except (OSError, TypeError, ValueError, KeyError, AttributeError) as exc:
        raise RestoreRefused('backup-mismatch', str(exc)) from exc


def _restore_stage(directory, staged, manifest, store_id, now_ms):
    if staged.exists():
        # Without the staged receipt no other step could have used these files.
        shutil.rmtree(staged)
    for name in sorted(manifest['files']):
        relative = _restore_member(name)
        destination = staged / relative
        store_evidence._mkdir(destination.parent)
        with (directory / relative).open('rb') as source, destination.open('xb') as target:
            shutil.copyfileobj(source, target)
            store_evidence._sync_file(target)
        store_evidence._sync_dir(destination.parent)
        expected = manifest['files'][name]
        if _file_record(destination) != {key: expected[key] for key in ('size', 'sha256')}:
            raise RestoreRefused('backup-mismatch', f'backup member changed during copy: {name}')
    with closing(store_layout.open_catalogue(staged)) as cat, cat:
        if _rows(cat, 'SELECT store_id FROM catalogue_identity') != [(manifest['store_id'],)]:
            raise RestoreRefused('backup-mismatch', 'catalogue identity differs from the manifest')
        cat.execute('UPDATE catalogue_identity SET store_id=?,created_at=? WHERE id=1',
                    (store_id, now_ms)).close()


def _offline_read(path):
    """Read stopped-store bytes without asking for a running writer."""
    wal = Path(str(path) + '-wal')
    shm = Path(str(path) + '-shm')
    if wal.exists() and not shm.exists():
        raise store_layout.LayoutFault('no-writer')
    suffix = '?mode=ro' if wal.exists() else '?mode=ro&immutable=1'
    return sqlite3.connect(path.as_uri() + suffix, uri=True)


def _restore_classes(staged):
    with closing(_offline_read(store_layout.catalogue_path(staged))) as cat:
        routes = store_layout.project_routes(cat)
        states = _route_states(cat)
    files = {path.parent.name for path in (staged / 'projects').glob('*/ledger.sqlite3')}
    classes = []
    for project in sorted(files - states.keys()):
        raise RestoreRefused('unclassed-ledger', (project, 'no-route'))
    for route in routes:
        project = route.project_id
        state, holder = states[project]
        if project not in files:
            if state == 'active':
                raise RestoreRefused('unclassed-ledger', (project, 'missing'))
            continue
        if state == 'creating' and holder is not None:
            raise RestoreRefused('unclassed-ledger', (project, 'creating-held'))
        try:
            conn = store_layout.open_ledger(staged, project)
        except store_layout.LayoutFault as exc:
            if state != 'creating' or exc.cause != 'no-project-row':
                raise RestoreRefused('unclassed-ledger', (project, exc.cause)) from exc
            conn = store_layout.open_ledger(staged, project, create=True)
            with closing(conn):
                if any(_rows(conn, f'SELECT 1 FROM {table} LIMIT 1')
                       for table in ('relays', 'recipients', 'slips', 'seats')):
                    raise RestoreRefused('unclassed-ledger', (project, 'data-without-project'))
            classes.append([project, 'creating-no-row'])
        else:
            conn.close()
            classes.append([project, 'cancel'])
    return classes


def _journal_record(journal, step, matches):
    return _journal_record_view(journal._conn, step, matches)


def _journal_record_view(conn, step, matches):
    rows = _rows(conn, 'SELECT i.id,i.target,i.before_state,i.after_state,r.observed'
                 ' FROM intents i LEFT JOIN receipts r ON r.intent_id=i.id WHERE i.step=?',
                 (step,))
    found = []
    for row_id, raw_target, before, after, receipt in rows:
        target = json.loads(raw_target)
        if matches(target):
            found.append((store_steps.Intent(row_id, step, target, json.loads(before), json.loads(after)),
                          None if receipt is None else json.loads(receipt)))
    if len(found) > 1:
        raise ValueError(f'duplicate restore journal step: {step}')
    return found[0] if found else (None, None)


def _cancel_staged(journal, staged, classes, job_id, now_ms):
    from relay_engine import restore_cancel

    for project, kind in classes:
        if kind != 'cancel':
            continue
        intent, done = _journal_record(journal, 'restore-cancel', lambda target: target == project)
        if done is not None:
            continue
        ident = (journal.intent('restore-cancel', project, 'not-cancelled', 'cancelled')
                 if intent is None else intent.id)
        with closing(store_layout.open_ledger(staged, project)) as ledger:
            result = restore_cancel.cancel_all(ledger, project, job_id, now_ms)
        journal.receipt(ident, {'cut_handle': result.cut_handle, 'already': result.already})


def _loss(root, route, cutoff, live):
    if not live:
        return RestoreLoss(route.project_id, route.name, None, None, False)
    try:
        with closing(_offline_read(store_layout.ledger_path(root, route.project_id))) as ledger:
            relay = _rows(ledger, 'SELECT id FROM relays WHERE created_at>? ORDER BY handle DESC LIMIT 1',
                          (cutoff,))
            receipt = _rows(ledger, 'SELECT max(committed_at) FROM mutation_receipts WHERE committed_at>?',
                            (cutoff,))
    except (sqlite3.Error, OSError, store_layout.LayoutFault):
        return RestoreLoss(route.project_id, route.name, None, None, False)
    return RestoreLoss(route.project_id, route.name, relay[0][0] if relay else None, receipt[0][0], True)


def _moved_job(path, old_id, restored):
    job_id = store_layout._canonical_job(path.name)
    db = path / 'job.sqlite3'
    _ordinary_file(db, path)
    with closing(_offline_read(db)) as conn:
        # Unrelated or pre-backup jobs do not become current just because
        # their folders still exist. The retained marker intents name roots.
        checkpoint = _rows(conn, 'SELECT data FROM checkpoints ORDER BY id DESC LIMIT 1')
        if not checkpoint:
            return None
        data = json.loads(checkpoint[0][0])
        if data.get('store_id') != old_id or restored.get(data.get('project_id')) == 'active':
            return None
        markers = _rows(conn, "SELECT i.target FROM intents i JOIN receipts r ON r.intent_id=i.id"
                        " WHERE i.step='marker-place' ORDER BY i.id")
        if not markers:
            return None
        undone = _rows(conn, "SELECT i.target FROM intents i JOIN receipts r ON r.intent_id=i.id"
                       " WHERE i.step='inverse-restore'")
        unmarked = {json.loads(row[0])['root_path'] for row in undone}
        bound = {item['path'] for item in data['roots']}
        marked = []
        for raw, in markers:
            target = json.loads(raw)
            if target['store_id'] != old_id or target['job_id'] != job_id or target['root_path'] not in bound:
                raise ValueError('migration marker is not bound to the retained job')
            if target['root_path'] not in unmarked:
                marked.append(target['root_path'])
        # A backup cannot be taken during a cutover span. A marker in
        # this store whose project is absent or inert in the backup
        # therefore belongs to a cutover newer than the backup's route.
        return MovedJob(job_id, data['project_name'], tuple(dict.fromkeys(marked)), 'journal') if marked else None


def _restore_report(root, staged, manifest, store_id, classes, start):
    with closing(_offline_read(store_layout.catalogue_path(staged))) as cat:
        routes = store_layout.project_routes(cat)
        pending = pending_creations(cat)
    restored = {route.project_id: route.state for route in routes}
    by_project = {route.project_id: route for route in routes}
    live = start['mode'] == 'readable'
    old_id = start.get('live_store_id', manifest['store_id'])
    if live:
        for row in start['payload']['routes']:
            project = row['project_id']
            by_project.setdefault(project, store_layout.ProjectRoute(
                project, row['name'], row['state'], store_layout.ledger_path(root, project)))
    losses = [_loss(root, route, manifest['now_ms'], live)
              for route in sorted(by_project.values(), key=lambda item: item.name)]
    moved = []
    migrations = root / 'migrations'
    if migrations.is_dir() and not migrations.is_symlink():
        for path in sorted(migrations.iterdir()):
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                store_layout._canonical_job(path.name)
            except ValueError:
                continue
            try:
                record = _moved_job(path, old_id, restored)
            except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, AttributeError,
                    store_layout.LayoutFault):
                record = MovedJob(path.name, None, (), 'unavailable')
            if record is not None:
                moved.append(record)
    if not moved and manifest['reason'] == 'before-move' and not live:
        moved.append(MovedJob(None, None, (), 'unavailable'))
    held = [project for project, kind in classes if kind == 'cancel']
    return dict(new_store_id=store_id, losses=[vars(loss) for loss in losses],
                moved_jobs=[vars(item) for item in moved], held=held,
                being_created=[project for project in pending if project not in held])


def _restore_current_files(root, *, include_catalogue=True):
    members = []
    if include_catalogue:
        members.append('machine.sqlite3')
    projects = root / 'projects'
    if projects.is_dir() and not projects.is_symlink():
        for project in sorted(projects.iterdir()):
            if project.is_symlink() or not project.is_dir():
                continue
            if os.path.lexists(project / 'ledger.sqlite3'):
                members.append((project / 'ledger.sqlite3').relative_to(root).as_posix())
            evidence = project / 'evidence' / 'objects'
            if evidence.is_dir() and not evidence.is_symlink():
                for path in sorted(evidence.rglob('*')):
                    if path.is_symlink() or path.is_file():
                        members.append(path.relative_to(root).as_posix())
    for name in members:
        if name != 'machine.sqlite3':
            _ordinary_file(root / _restore_member(name), root)
    return members


def _require_empty_catalogue_slots(root):
    for slot in _RESTORE_CATALOGUE:
        if os.path.lexists(root / slot):
            raise store_steps.FenceBreach('absent slot', 'absent slot', str(root / slot))


def _restore_move(journal, root, source, target, database, *, live_start=None, lost_install=False):
    binding = {'src': str(source), 'dst': str(target)}
    intent, moved = _journal_record(journal, 'move', lambda value: value == binding)
    if moved is not None:
        return
    _restore_parents(source, root)
    _restore_parents(target, root, missing=True)
    if intent is not None:
        if lost_install and os.path.lexists(source):
            _require_empty_catalogue_slots(root)
        store_steps.resume_move(journal, intent)
        return

    prep, proof = _journal_record(journal, 'restore-prepare', lambda value: value == binding)
    if proof is None:
        already_prepared = (live_start is not None
                            and _restore_fence_live(source, live_start, allow_prepared=prep is not None))
        ident = journal.intent('restore-prepare', binding, 'unprepared', 'prepared') if prep is None else prep.id
        _ordinary_file(source, root)
        if already_prepared:
            prepared = store_steps.Prepared(**live_start['prepared'])
        elif database:
            prepared = store_steps.prepare_database(source)
            if live_start is not None and vars(prepared) != live_start['prepared']:
                raise store_steps.FenceBreach(live_start['prepared'], live_start['prepared'], str(source))
        else:
            with source.open('rb') as file:
                store_evidence._sync_file(file)
            prepared = store_steps.Prepared(**_file_record(source))
        journal.receipt(ident, vars(prepared))
    else:
        prepared = store_steps.Prepared(**proof)
    if lost_install:
        _require_empty_catalogue_slots(root)
    store_steps.move_database(journal, prepared, source, target)


def restore(root, backup_dir, recovery_job_id: str, *, serving_stopped: bool,
            confirm: bool, now_ms: int, stopped_by: str = 'restore') -> RestoreReport:
    """Restore through the journaled swap; reconciliation and serving stay outside."""
    from relay_engine import submit

    if serving_stopped is not True:
        raise ValueError('restore requires serving to be stopped')
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError('restore requires Unix epoch milliseconds')
    root = store_layout.resolve_root(root)
    job_id = store_layout._canonical_job(recovery_job_id)
    directory = Path(backup_dir).absolute()
    digest, manifest = _verified_backup(directory)
    job_path = root / 'recovery' / job_id
    pending, _ = _probe_restore(root, job_path)
    if 'restore-start' not in pending:
        try:
            with jobs_lock(root):
                pending, _ = _probe_restore(root, job_path)
                if 'restore-start' not in pending:
                    other = next((job for job in open_restores(root) if job != job_id), None)
                    if other is not None:
                        raise RestoreRefused('job-open', other)
                    job_path = store_steps.recovery_dir(root, job_id)
                    with closing(store_steps.open_journal(job_path, create=True)) as journal:
                        observation = _restore_observation(root, job_path)
                        journal.checkpoint('restore-start', dict(
                            backup=str(directory), sha256=digest, store_id=submit.new_uuid7(now_ms),
                            now_ms=now_ms, job_id=job_id, **observation))
        except JobsBusy as exc:
            raise RestoreRefused('jobs-busy', None) from exc
        except JobsLockError as exc:
            raise RestoreRefused('jobs-lock-error', exc.errno_name) from exc

    pending, last = _probe_restore(root, job_path)
    start = pending['restore-start']
    if start.get('job_id') != job_id or _abandoned(job_id, pending, last):
        raise RestoreRefused('job-open', job_id)
    if (start['backup'], start['sha256']) != (str(directory), digest):
        raise RestoreRefused('backup-mismatch', 'recovery job already binds a different backup')
    _restore_verify_observation(job_path, start)
    readable = start['mode'] == 'readable'
    staged, replaced = job_path / 'store', job_path / 'replaced'
    with closing(store_steps.open_journal(job_path)) as journal:
        checkpoints = _restore_checkpoints(journal._conn)

        def checkpoint(phase, data):
            journal.checkpoint(phase, data)
            checkpoints[phase] = data

        if 'restore-staged' not in checkpoints:
            _restore_stage(directory, staged, manifest, start['store_id'], start['now_ms'])
            checkpoint('restore-staged', True)
        if 'restore-classes' not in checkpoints:
            checkpoint('restore-classes', _restore_classes(staged))
        classes = checkpoints['restore-classes']
        if 'restore-report' not in checkpoints:
            _cancel_staged(journal, staged, classes, job_id, start['now_ms'])
            if readable:
                _restore_carry(journal, staged, start['payload'])
                if 'restore-excluded' not in checkpoints:
                    checkpoint('restore-excluded', _restore_excluded(staged, start['payload']))
            checkpoint('restore-report', _restore_report(root, staged, manifest, start['store_id'], classes, start))
        report = checkpoints['restore-report']
        if confirm and 'restore-done' not in checkpoints:
            if 'restore-swapped' not in checkpoints:
                if 'restore-swap' not in checkpoints:
                    checkpoint('restore-swap', _restore_current_files(root, include_catalogue=readable))
                if not readable:
                    _restore_lost_members(journal, root, job_path, start['lost_members'])
                for name in checkpoints['restore-swap']:
                    relative = _restore_member(name)
                    _restore_move(journal, root, root / relative, replaced / relative,
                                  len(relative.parts) in (1, 3),
                                  live_start=start if name == 'machine.sqlite3' else None)
                for name in sorted(manifest['files'], key=lambda value: (value == 'machine.sqlite3', value)):
                    relative = _restore_member(name)
                    source, target = staged / relative, root / relative
                    _restore_move(journal, root, source, target, len(relative.parts) in (1, 3),
                                  lost_install=not readable and name == 'machine.sqlite3')
                checkpoint('restore-swapped', True)
            if not readable:
                _restore_record_lost_stop(journal, root, stopped_by)
            checkpoint('restore-done', True)
        return RestoreReport(report['new_store_id'], tuple(RestoreLoss(**row) for row in report['losses']),
                             [MovedJob(row['job_id'], row['project_name'], tuple(row['marked_roots']), row['state'])
                              for row in report['moved_jobs']], tuple(report['held']), tuple(report['being_created']),
                             start['mode'], tuple(checkpoints.get('restore-excluded', ())))
