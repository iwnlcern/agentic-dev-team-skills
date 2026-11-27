"""Project settings through the real Platform component and Store writer."""

from contextlib import closing
from dataclasses import replace
from itertools import count
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import api_protocol as protocol, api_service, api_wire, api_writer
from relay_engine import queries, store_layout
from relay_engine.api_principals import OperatorPrincipal
from relay_engine.tests.test_api_service import P, admitted_store_fixture, seed_service_store

Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
MISSING = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
THRESHOLD = 'doorbell_off_minutes'
INTERVAL = 'delivery_interval_seconds'
GET = 'project.settings.get'
SET = 'project.settings.set'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='settings-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'store'
        self.tick = count(1_700_000_000_000)
        self.keys = {}

        def seed(root):
            seed_service_store(root, self.keys, self.tick)
            with closing(store_layout.open_catalogue(root)) as conn, conn:
                store_layout.reserve_project(conn, Q, 'not-served', 1)
                store_layout.activate_project(conn, Q)

        receipt = admitted_store_fixture(self.root, P, project_name='project', seed=seed).receipt
        self.service = api_service.ApiService(str(self.root), P,
                                              clock=lambda: next(self.tick), admission=receipt)
        self.addCleanup(self.service.stop)
        self.service.start()
        self.writer = self.service.writer
        self.page = OperatorPrincipal('page')

    def dispatch(self, op, args, *, principal=None, binding=None):
        args = {key: str(value) if type(value) is int else value for key, value in args.items()}
        registry, runtime, ports = self.service.registry, self.service.runtime, self.service.operation_ports
        connected = protocol.dispatch_api(protocol.initial_connection_state(), api_wire.ApiRequest(
            '1', 'protocol.connect', {'supported': {'min': '3.0', 'max': '3.0'}}),
            runtime, registry, ports)
        self.assertIn('result', connected.response, connected.response)
        reply = protocol.dispatch_api(replace(connected.state, binding=binding),
                                      api_wire.ApiRequest('2', op, args),
                                      runtime, registry, ports, principal=principal)
        self.assertFalse(reply.close, reply.response)
        return reply

    def get(self, project=P):
        return self.dispatch(GET, {'project_id': project}).response

    def set_values(self, revision, *, project=P, principal=None, **values):
        return self.dispatch(SET, {'project_id': project, 'expected_revision': revision, **values},
                             principal=self.page if principal is None else principal).response

    def record(self, reply, threshold, interval, revision, *, project=P):
        self.assertIn('result', reply, reply)
        self.assertEqual(reply['result'], {'project_id': project, THRESHOLD: str(threshold),
                                           INTERVAL: str(interval), 'revision': str(revision)})

    def refused(self, response, reason, code=None, engine=None):
        self.assertIn('error', response, response)
        error = response['error']
        details = {'kind': reason}
        if engine is not None:
            details['engine_code'] = engine
        self.assertEqual(error['cause'], details)
        if code is not None:
            self.assertEqual(error['code'], code)
        return error

    def history(self):
        return self.writer.read(P, lambda conn: queries.settings_history(conn, P, 20))

    def engine_write(self, values):
        with closing(store_layout.open_ledger(self.root, P)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE').close()
            for key, value in values:
                queries.set_project_setting_step(conn, P, key, value, principal='operator',
                                                 now_ms=next(self.tick))

    def test_get_defaults_and_no_write(self):
        allowed = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_TRANSACTION}
        seen, changes = [], []
        real = self.writer.read

        def read(project, callback):
            def checked(conn):
                def authorize(action, *detail):
                    if action not in allowed:
                        seen.append((action, detail))
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                conn.set_authorizer(authorize)
                value = callback(conn)
                changes.append(conn.total_changes)
                return value
            return real(project, checked)

        with patch.object(self.writer, 'read', side_effect=read):
            self.record(self.get(), 15, 300, 0)
        self.assertEqual(changes, [0])
        self.assertEqual(seen, [])
        self.assertEqual(self.history(), [])

    def test_get_one_snapshot(self):
        real = self.writer.read
        case = self

        class InterleavedRead:
            def __init__(self, conn, writes):
                self.conn = conn
                self.writes = writes
                self.raced = False

            def execute(self, query, *args):
                if 'FROM settings_history' in query:
                    case.assertFalse(self.raced)
                    self.raced = True
                    case.engine_write(self.writes)
                return self.conn.execute(query, *args)

        for old, writes, new in (
                ((15, 300, 0), ((THRESHOLD, 16),), (16, 300, 1)),
                ((16, 300, 1), ((THRESHOLD, 17), (INTERVAL, 360)), (17, 360, 3))):
            with self.subTest(writes=writes):
                snapshots = []

                def read(project, callback):
                    def wrap(conn):
                        proxy = InterleavedRead(conn, writes)
                        snapshots.append(proxy)
                        return callback(proxy)
                    return real(project, wrap)

                with patch.object(self.writer, 'read', side_effect=read):
                    self.record(self.get(), *old)
                self.assertEqual([proxy.raced for proxy in snapshots], [True])
                self.record(self.get(), *new)

    def test_set_one_key(self):
        self.record(self.set_values(0, **{THRESHOLD: 16}), 16, 300, 1)
        self.assertEqual([(r.key, r.new, r.by_principal) for r in self.history()],
                         [(THRESHOLD, 16, 'operator')])
        self.record(self.set_values(1, principal=OperatorPrincipal('terminal'), **{INTERVAL: 310}),
                    16, 310, 2)
        self.assertEqual([(r.key, r.new, r.by_principal) for r in self.history()],
                         [(INTERVAL, 310, 'operator'), (THRESHOLD, 16, 'operator')])

    def test_set_both_keys(self):
        self.record(self.set_values(0, **{THRESHOLD: 16, INTERVAL: 350}), 16, 350, 2)
        self.assertEqual([(r.revision, r.key, r.new) for r in self.history()],
                         [(2, INTERVAL, 350), (1, THRESHOLD, 16)])

    def test_set_order(self):
        from relay_engine.api_settings import _order

        self.engine_write(((INTERVAL, 600),))
        self.record(self.set_values(1, **{THRESHOLD: 6, INTERVAL: 300}), 6, 300, 3)
        self.assertEqual([r.key for r in self.history()[:2]], [THRESHOLD, INTERVAL])
        self.record(self.set_values(3, **{THRESHOLD: 15, INTERVAL: 600}), 15, 600, 5)
        self.assertEqual([r.key for r in self.history()[:2]], [INTERVAL, THRESHOLD])
        self.assertEqual(_order({THRESHOLD: 15, INTERVAL: 600}, {THRESHOLD: 6, INTERVAL: 300}),
                         (INTERVAL, THRESHOLD))
        self.assertEqual(_order({THRESHOLD: 6, INTERVAL: 300}, {THRESHOLD: 15, INTERVAL: 600}),
                         (THRESHOLD, INTERVAL))

    def test_set_all_or_nothing(self):
        self.refused(self.set_values(0, **{THRESHOLD: 0, INTERVAL: 240}), 'command',
                     'E_API_ADMISSION', 'E-SETTING-VALUE')
        self.record(self.get(), 15, 300, 0)
        self.assertEqual(self.history(), [])

    def test_set_equal_values(self):
        self.engine_write(((THRESHOLD, 16),))
        history = self.history()
        for values in ({THRESHOLD: 16}, {THRESHOLD: 16, INTERVAL: 300}):
            self.record(self.set_values(0, **values), 16, 300, 1)
        self.assertEqual(self.history(), history)

    def test_set_stale_revision(self):
        self.engine_write(((THRESHOLD, 16),))
        history = self.history()
        for values in ({THRESHOLD: 17}, {THRESHOLD: 16, INTERVAL: 320}):
            error = self.refused(self.set_values(0, **values), 'settings_revision_stale',
                                 'E_API_CONFLICT')
            self.assertEqual(error['remedy'], 'reload_settings')
        self.assertEqual(self.history(), history)

    def test_set_uncertain_outcome(self):
        real = self.writer.submit

        def lose_answer(project, job):
            self.assertTrue(real(project, job).commit)
            return api_writer.JobOutcome(False, None, True)

        with patch.object(self.writer, 'submit', side_effect=lose_answer):
            self.refused(self.set_values(0, **{THRESHOLD: 16}), 'handler_fault', 'E_API_INTERNAL')
        self.record(self.set_values(0, **{THRESHOLD: 16}), 16, 300, 1)
        self.assertEqual(len(self.history()), 1)
        with patch.object(self.writer, 'submit', side_effect=lose_answer):
            self.refused(self.set_values(1, **{THRESHOLD: 17}), 'handler_fault', 'E_API_INTERNAL')
        self.record(self.set_values(2, **{THRESHOLD: 18}), 18, 300, 3)
        history = self.history()
        self.refused(self.set_values(1, **{THRESHOLD: 17}), 'settings_revision_stale',
                     'E_API_CONFLICT')
        self.assertEqual(self.history(), history)

    def test_engine_refusals(self):
        self.refused(self.set_values(0, **{INTERVAL: 601}), 'command',
                     'E_API_ADMISSION', 'E-SETTING-VALUE')
        self.assertEqual(self.history(), [])
        self.engine_write(((THRESHOLD, 6),))
        history = self.history()
        self.refused(self.set_values(1, **{INTERVAL: 301}), 'policy',
                     'E_API_ADMISSION', 'E-DELIVERY-INTERVAL-MARGIN')
        self.assertEqual(self.history(), history)
        with closing(store_layout.open_ledger(self.root, P)) as conn, conn:
            conn.executemany('INSERT INTO project_settings (project_id,key,value) VALUES (?,?,?) '
                             'ON CONFLICT(project_id,key) DO UPDATE SET value=excluded.value',
                             [(P, THRESHOLD, '5'), (P, INTERVAL, '600')]).close()
        self.refused(self.set_values(1, **{THRESHOLD: 6, INTERVAL: 300}), 'policy',
                     'E_API_ADMISSION', 'E-DELIVERY-INTERVAL-MARGIN')
        self.record(self.get(), 5, 600, 1)
        self.assertEqual(self.history(), history)

    def test_refused_before_handler(self):
        self.refused(self.set_values(0, jev_act=True), 'invalid_args', 'E_API_PROTOCOL')
        signed = self.dispatch('session.sign-in', self.keys['implementer'] | {
            'host': 'codex', 'host_session_id': 'settings-fixture', 'operation_key': 'settings-fixture'})
        self.assertIn('result', signed.response, signed.response)
        self.assertIsNotNone(signed.state.binding)
        with patch.object(self.writer, 'submit', wraps=self.writer.submit) as submit:
            refused = self.dispatch(SET, {'project_id': P, 'expected_revision': 0, THRESHOLD: 16},
                                    binding=signed.state.binding).response
            self.refused(refused, 'not_signed_in', 'E_API_AUTH')
            self.refused(self.set_values(0), 'invalid_args', 'E_API_PROTOCOL')
            submit.assert_not_called()
        self.assertEqual(self.history(), [])

    def test_unknown_project(self):
        for project, reason, code in ((MISSING, 'project_unknown', 'E_API_NOT_FOUND'),
                                      (P.upper(), 'project_unknown', 'E_API_NOT_FOUND'),
                                      (Q, 'runtime_not_ready', 'E_API_UNAVAILABLE')):
            self.refused(self.get(project), reason, code)
            self.refused(self.set_values(0, project=project, **{THRESHOLD: 16}), reason, code)

    def test_creating_route_not_ready(self):
        with closing(store_layout.open_catalogue(self.root)) as conn, conn:
            store_layout.reserve_project(conn, MISSING, 'creating-project', next(self.tick))
        self.refused(self.get(MISSING), 'runtime_not_ready', 'E_API_UNAVAILABLE')
        self.refused(self.set_values(0, project=MISSING, **{THRESHOLD: 16}),
                     'runtime_not_ready', 'E_API_UNAVAILABLE')

    def test_settings_through_platform_component(self):
        self.record(self.get(), 15, 300, 0)
        self.record(self.set_values(0, **{THRESHOLD: 16}), 16, 300, 1)
        self.assertEqual(self.writer.read(P, lambda conn: queries.project_settings(conn, P)).values[
            THRESHOLD], 16)
        self.test_unknown_project()


class SettingsBoundaryTests(unittest.TestCase):
    def test_settings_import_boundary(self):
        result = subprocess.run([sys.executable, '-c', """
import sys
from relay_engine import api_settings, api_platform, api_schema
blocked = {'sqlite3', 'relay_engine.queries', 'relay_engine.store_layout'}
assert blocked.isdisjoint(sys.modules), blocked.intersection(sys.modules)
built = api_platform.build_platform()
artifact = api_schema.export_schema(built.registry, '3.0')
assert b'project.settings.get' in artifact.canonical_bytes
assert blocked.isdisjoint(sys.modules), blocked.intersection(sys.modules)
"""], capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
