"""M0 fixture builders for the relay read and inbox boundary tests."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
import base64
import hashlib
import itertools
import os
from pathlib import Path
import sqlite3
import threading

from relay_engine import api_client, api_keys, api_service, imports, store_layout
from relay_engine.tests.test_api_service import admitted_store_fixture


PROJECT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
PROJECT_NAME = 'p' * 32
IMPORT_BYTES = 8_388_608
BODY_BYTES = 65_536
HEADER_BYTES = 8_192
IMPORT_SUBJECT = '\u0001' * 16_384
PAGE_SUBJECT = '\u0800' * 16_384


@dataclass(frozen=True)
class ReadFixture:
    root: str
    project_id: str
    relay_ids: tuple[str, ...]
    reader_key: str
    reader_session_id: str


def _team(index):
    return f't{index:063d}'


def _seat(index, role):
    return f'{role}-{index}'


def _private_file(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(value)


def _import(conn, tick, subject, to, cc=(), body=b''):
    historical = ('KIND: SITREP\nFROM: operator\nTO: ' + ', '.join(to)
                  + ('\nCC: ' + ', '.join(cc) if cc else '')
                  + '\nSUBJECT: ' + subject + '\n\n').encode('utf-8')
    relay = imports.ImportedRelay(
        created_at=tick, kind='SITREP', verdict=None, subject=subject, sender='operator',
        sender_role=None, team_instance_id=None, to=to, cc=cc, parent_id=None,
        parent_spelling=None, body=body, header_bytes=len(historical),
        header_sha256=hashlib.sha256(historical).hexdigest(),
    )
    return imports.import_relay_step(conn, PROJECT, relay, marked_at_ms=tick)


def scratch_copy_path(root):
    location = Path(root)
    return location.with_name(location.name + '.copy.sqlite')


def _copy_held_ledger(root):
    with (closing(store_layout.open_ledger_readonly(root, PROJECT)) as source,
          closing(sqlite3.connect(scratch_copy_path(root))) as target):
        source.backup(target)


@contextmanager
def _serve(root, *, teams=1, seed_history=None):
    location = Path(root).resolve()
    key_paths = {}
    imported_ids = []
    tick = itertools.count(1_700_000_000_000)

    def seed(path):
        with closing(store_layout.open_ledger(path, PROJECT)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE').close()
            conn.executemany(
                'INSERT INTO team_instances (id, project_id, name, level, opened_at, opened_by)'
                " VALUES (?, ?, ?, 'pair', 1, 'operator')",
                ((f'team-{i}', PROJECT, _team(i)) for i in range(teams)),
            ).close()
            conn.executemany(
                'INSERT INTO seats (id, team_instance_id, role, state)'
                " VALUES (?, ?, ?, 'vacant')",
                ((_seat(i, role), f'team-{i}', role) for i in range(teams)
                 for role in ('planner', 'implementer')),
            ).close()
            if seed_history is not None:
                imported_ids.extend(seed_history(conn, tick))
            for role in ('planner', 'implementer'):
                key_id, secret = api_keys.issue_occupant_key(conn, _seat(0, role), next(tick))
                path = location.with_name(location.name + '.' + role + '.key')
                _private_file(path, f'adt-seat-key-v1 {key_id} {secret}\n'.encode('ascii'))
                key_paths[role] = path

    admission = admitted_store_fixture(location, PROJECT, project_name=PROJECT_NAME, seed=seed)
    service = api_service.ApiService(str(location), PROJECT, clock=lambda: next(tick),
                                     admission=admission.receipt)
    try:
        ready = api_client.read_ready_file(service.start())
        acceptor = threading.Thread(target=service.serve_forever, name='m0-fixture-acceptor')
        acceptor.start()
        try:
            yield location, ready, key_paths, imported_ids
            _copy_held_ledger(location)
        finally:
            service.stop()
            acceptor.join(5)
            if acceptor.is_alive():
                raise RuntimeError('fixture acceptor did not stop')
    finally:
        service.stop()
    if service._retired_admission is None:
        raise RuntimeError('fixture service did not return an idle receipt')
    _private_file(location.with_name(location.name + '.idle-receipt'),
                  store_layout._encode_idle_receipt(*service._retired_admission))


def _result(reply):
    if isinstance(reply, api_client.ApiErrorValue):
        raise RuntimeError(f'fixture API refusal {reply.code}: {dict(reply.cause)}')
    return reply


@contextmanager
def _session(ready, role, key_path):
    with closing(api_client.ApiClient.open(ready)) as client:
        key = api_client.read_key_file(str(key_path))
        result = _result(client.sign_in(key, 'codex', 'm0-' + role, 'm0-sign-in-' + role))
        yield client, result['session_id']


def _submit(client, content, key):
    return _result(client.call('relay.submit', {
        'draft_base64': base64.b64encode(content).decode('ascii'),
        'submission_key': key,
    }))


def _draft(kind, to, needs, *, parent=None, cc=None, verdict=None, capped=False):
    lines = [f'KIND: {kind}', f'TO: {to}']
    if cc is not None:
        lines.append(f'CC: {cc}')
    lines.extend(['BUNDLE: m0' if parent is None else f'PARENT: #{parent}', f'NEEDS: {needs}'])
    if verdict is not None:
        lines.append(f'VERDICT: {verdict}')
    findings = [f'FINDING: advisory note{i}\n' for i in range(200)] if capped and verdict else []
    prefix = ''.join(line + '\n' for line in lines)
    subject_length = 400 if capped else 1
    refs = []
    if capped:
        fixed = len(prefix) + len('SUBJECT: \n\n') + sum(map(len, findings))
        room = HEADER_BYTES - fixed - subject_length
        ref_size = len('REF: #1\n')
        subject_length -= (-room) % ref_size
        refs = ['REF: #1\n'] * ((HEADER_BYTES - fixed - subject_length) // ref_size)
    header = (prefix + 'SUBJECT: ' + 's' * subject_length + '\n'
              + ''.join(refs + findings) + '\n').encode('ascii')
    if capped and len(header) != HEADER_BYTES:
        raise AssertionError(f'fixture header has {len(header)} bytes')
    return header + (b'b' * (BODY_BYTES - 1) + b'\n' if capped else b'fixture\n')


def _make_fixture(location, ids, key_paths, role, session_id):
    return ReadFixture(str(location), PROJECT, tuple(ids), str(key_paths[role]), session_id)


def _import_store(root, body_size):
    def seed(conn, tick):
        to = tuple(_seat(i, 'planner') for i in range(2_048))
        cc = tuple(_seat(i, 'implementer') for i in range(2_048))
        yield _import(conn, next(tick), IMPORT_SUBJECT, to, cc, b'b' * body_size)

    with _serve(root, teams=2_048, seed_history=seed) as (location, ready, keys, ids):
        with _session(ready, 'planner', keys['planner']) as (_, session_id):
            return _make_fixture(location, ids, keys, 'planner', session_id)


def import_bound_store(root: str) -> ReadFixture:
    return _import_store(root, IMPORT_BYTES)


def import_over_body_store(root: str) -> ReadFixture:
    return _import_store(root, IMPORT_BYTES + 1)


def live_request_store(root: str) -> ReadFixture:
    with _serve(root) as (location, ready, keys, _):
        reader = _team(0) + '.implementer'
        with (_session(ready, 'planner', keys['planner']) as (writer, _),
              _session(ready, 'implementer', keys['implementer']) as (_, session_id)):
            parent = None
            for index in range(255):
                parent = _submit(writer, _draft('SITREP', reader, 'none', parent=parent),
                                 f'ancestor-{index}')['handle']
            relay = _submit(writer, _draft('REVIEW', reader, 'review', parent=parent,
                                           cc='operator', capped=True), 'review-request')
            return _make_fixture(location, (relay['relay_id'],), keys, 'implementer', session_id)


def live_reply_store(root: str) -> ReadFixture:
    with _serve(root) as (location, ready, keys, _):
        with (_session(ready, 'planner', keys['planner']) as (planner, session_id),
              _session(ready, 'implementer', keys['implementer']) as (implementer, _)):
            request = _submit(planner, _draft('REVIEW', _team(0) + '.implementer', 'review'),
                              'review-request')
            reply = _submit(implementer, _draft('REVIEW', _team(0) + '.planner', 'none',
                                               parent=request['handle'], verdict='must-revise',
                                               capped=True), 'review-reply')
            return _make_fixture(location, (reply['relay_id'],), keys, 'planner', session_id)


def frame_page_store(root: str) -> ReadFixture:
    def seed(conn, tick):
        for _ in range(500):
            yield _import(conn, next(tick), PAGE_SUBJECT, (_seat(0, 'planner'),))

    with _serve(root, seed_history=seed) as (location, ready, keys, ids):
        with _session(ready, 'planner', keys['planner']) as (_, session_id):
            return _make_fixture(location, ids, keys, 'planner', session_id)
