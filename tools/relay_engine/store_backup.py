"""Consistent, evidence-pinned backups inside the caller's declared turn."""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time

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


def _rows(conn, sql, args=()):
    cursor = conn.execute(sql, args)
    try:
        return cursor.fetchall()
    finally:
        cursor.close()


def present_ledgers(catalogue_conn, root) -> tuple[str, ...]:
    """Presence includes creations whose effect committed before activation."""
    return tuple(sorted(route.project_id
                        for route in store_layout.project_routes(catalogue_conn)
                        if store_layout.ledger_path(root, route.project_id).exists()))


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


def take(conns: Mapping[str, sqlite3.Connection], root, reason: str, now_ms: int, *,
         then_hold: tuple[str, str, Sequence[str]] | None = None) -> BackupManifest:
    """Copy while every declared source is locked; publish before taking holds."""
    started = time.monotonic()
    root = Path(root)
    cat = conns['catalogue']
    present = present_ledgers(cat, root)
    declared = tuple(sorted(key for key in conns if key != 'catalogue'))
    if present != declared:
        raise BackupRefused('present-set-changed', {'present': present, 'declared': declared})
    hold = store_layout.any_hold(cat)
    if hold is not None:
        raise BackupRefused('maintenance-held', hold)
    states = _route_states(cat)
    for project in present:
        if states[project][0] == 'creating' and states[project][1] is not None:
            raise BackupRefused('owned-creation-pending', project)
    if reason not in _REASONS or type(now_ms) is not int or now_ms < 0:
        raise ValueError('invalid backup reason or time')
    if then_hold is not None and not cat.in_transaction:
        raise ValueError('backup holds require the caller transaction')
    try:
        stamp = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        directory = root / 'backups' / (stamp + '-' + reason)
        store_evidence._mkdir(directory.parent)
        directory.mkdir(mode=0o700)
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
        _write_synced(directory / 'manifest.json', encoded)
    except Exception as exc:
        raise BackupRefused('copy-failed', str(exc)) from exc
    if then_hold is not None:
        job, kind, scopes = then_hold
        store_layout.take_holds(cat, job, kind, scopes, now_ms)
    return BackupManifest(directory, store_id, reason, now_ms, files, held_seconds)


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
