"""Machine catalogue identity, classification and pre-release refusal."""
from pathlib import Path
import hashlib
import inspect
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock
from uuid import UUID

from relay_engine import catalogue, errors, store_layout, submit


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class TestCatalogue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'

    def opened(self, root=None, **kwargs):
        conn = store_layout.open_catalogue(root or self.root, **kwargs)
        self.addCleanup(conn.close)
        return conn

    def identity(self, conn):
        return conn.execute('SELECT store_id, kind, created_at FROM catalogue_identity').fetchall()

    def snapshot(self, conn):
        return '\n'.join(conn.iterdump())

    def test_init_empty_builds_and_identifies(self):
        conn = self.opened()
        rows = self.identity(conn)
        self.assertEqual(len(rows), 1)
        identity, kind, created = rows[0]
        self.assertEqual(str(UUID(identity)), identity)
        self.assertEqual(UUID(identity).version, 7)
        self.assertEqual(kind, 'isolated')
        self.assertIsInstance(created, int)
        self.assertEqual(self.identity(self.opened()), rows)
        production = Path(self.tmp.name) / 'production'
        rows = self.identity(self.opened(production, kind='production'))
        self.assertEqual(rows[0][1], 'production')
        self.assertEqual(self.identity(self.opened(production)), rows)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone(), ('ok',))

    def test_catalogue_file_is_machine_sqlite3(self):
        self.opened()
        entries = {path.name for path in self.root.iterdir()}
        self.assertIn('machine.sqlite3', entries)
        self.assertNotIn('catalogue.sqlite3', entries)
        self.assertLessEqual(entries, {'machine.sqlite3', 'machine.sqlite3-wal',
                                      'machine.sqlite3-shm', 'machine.sqlite3-journal'})

    def test_bound_call_never_resolves_default(self):
        with mock.patch.object(store_layout, 'production_root', side_effect=AssertionError) as resolver:
            for index, kwargs in enumerate([{}, {'kind': 'isolated'}]):
                root = Path(self.tmp.name) / str(index)
                rows = self.identity(self.opened(root, **kwargs))
                self.assertEqual(rows[0][1], 'isolated')
                self.assertEqual(self.identity(self.opened(root, **kwargs)), rows)
            resolver.assert_not_called()
        # Control: run a classifying variant of the opener itself.
        source = inspect.getsource(store_layout._open_catalogue)
        self.assertIn("kind or 'isolated'", source)
        source = source.replace("kind or 'isolated'",
                                "kind or ('production' if root == production_root() else 'isolated')")
        with mock.patch.object(store_layout, 'production_root', side_effect=AssertionError('resolved')):
            namespace = dict(vars(store_layout))
            exec(compile(source, '<classifying-opener>', 'exec'), namespace)
            with self.assertRaisesRegex(AssertionError, 'resolved'):
                namespace['_open_catalogue'](Path(self.tmp.name) / 'classifying', None)

    def test_default_never_creates_production(self):
        with mock.patch.object(store_layout, 'production_root', return_value=self.root) as resolver:
            self.assertEqual(self.identity(self.opened(str(self.root)))[0][1], 'isolated')
            resolver.assert_not_called()

    def test_explicit_other_kind_refuses(self):
        for initial, other in [('isolated', 'production'), ('production', 'isolated')]:
            with self.subTest(initial=initial):
                root = Path(self.tmp.name) / initial
                conn = self.opened(root, kind=initial)
                before = self.snapshot(conn)
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root, kind=other)
                self.assertEqual(caught.exception.cause, 'wrong-kind')
                self.assertEqual(self.snapshot(conn), before)

    def test_init_refuses_other_digest(self):
        for mutation in ["UPDATE schema_version SET ddl_sha256 = 'old'",
                         'CREATE TABLE extra(value TEXT) STRICT',
                         'ALTER TABLE project_routes ADD COLUMN extra TEXT']:
            with self.subTest(mutation=mutation):
                root = Path(self.tmp.name) / str(len(mutation))
                conn = self.opened(root)
                conn.execute(mutation)
                if conn.in_transaction:
                    conn.execute('COMMIT')
                before = self.snapshot(conn)
                with self.assertRaises(errors.EngineError) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')
                self.assertEqual(self.snapshot(conn), before)

    def test_missing_catalogue_with_projects_refuses(self):
        # M12: a lost catalogue must not silently become a new empty store.
        for fixture in ['absent', 'empty', 'no-tables']:
            with self.subTest(fixture=fixture):
                root = Path(self.tmp.name) / fixture
                ledger = store_layout.open_ledger(root, P, create=True)
                ledger.close()
                path = root / 'machine.sqlite3'
                if fixture == 'empty':
                    path.touch()
                elif fixture == 'no-tables':
                    conn = sqlite3.connect(path)
                    conn.execute('PRAGMA user_version=1').close()
                    conn.close()
                before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
                entries = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.cause, 'missing-catalogue')
                after = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
                self.assertEqual(after, before)
                self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('*')), entries)
        # An empty projects directory still permits a genuinely new store.
        root = Path(self.tmp.name) / 'empty-projects'
        (root / 'projects').mkdir(parents=True)
        self.assertEqual(self.identity(self.opened(root))[0][1], 'isolated')

    def test_creation_is_one_transaction(self):
        # M13: a commit between schema and identity strands a half-created store.
        with mock.patch.object(submit, 'new_uuid7', side_effect=RuntimeError('before identity')):
            with self.assertRaisesRegex(RuntimeError, 'before identity'):
                self.opened(kind='production')
        probe = sqlite3.connect(self.root / 'machine.sqlite3')
        try:
            self.assertEqual(probe.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        finally:
            probe.close()
        trace = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            return conn

        with mock.patch.object(sqlite3, 'connect', side_effect=traced):
            conn = self.opened(kind='production')
        self.assertEqual(self.identity(conn)[0][1], 'production')
        self.assertEqual([sql for sql in trace if sql in ('BEGIN IMMEDIATE', 'COMMIT')],
                         ['BEGIN IMMEDIATE', 'COMMIT'])

    def test_tables_without_identity_refuse(self):
        for kwargs in [{}, {'kind': 'production'}]:
            with self.subTest(kwargs=kwargs):
                root = Path(self.tmp.name) / str(len(kwargs))
                conn = self.opened(root)
                conn.execute('DELETE FROM catalogue_identity').close()
                conn.execute('COMMIT').close()
                conn.close()
                path = root / 'machine.sqlite3'
                before = (path.read_bytes(), path.stat().st_mtime_ns,
                          sorted(p.name for p in root.iterdir()))
                with self.assertRaises(store_layout.LayoutFault) as caught:
                    self.opened(root, **kwargs)
                self.assertEqual(caught.exception.cause, 'no-identity')
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns,
                                  sorted(p.name for p in root.iterdir())), before)

    def test_pragmas_precede_first_write(self):
        # The first durable schema/identity write must already have full sync.
        connect = sqlite3.connect
        trace = []

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(trace.append)
            return conn

        with mock.patch.object(sqlite3, 'connect', side_effect=traced):
            self.opened()
        first_create = next(i for i, sql in enumerate(trace) if sql.startswith('CREATE '))
        for pragma in ['PRAGMA journal_mode=WAL', 'PRAGMA synchronous=FULL']:
            self.assertLess(trace.index(pragma), first_create)

    def test_sqlite_prefix_is_literal(self):
        # M14: '_' as a wildcard hides all four added object types.
        for statement in ['CREATE TABLE sqliteXextra (value TEXT)',
                          'CREATE INDEX sqlite1 ON project_routes (created_at)',
                          'CREATE VIEW sqlitev AS SELECT * FROM project_routes',
                          'CREATE TRIGGER sqlitet AFTER INSERT ON project_routes BEGIN SELECT 1; END']:
            with self.subTest(statement=statement):
                root = Path(self.tmp.name) / statement.split()[1]
                conn = self.opened(root)
                conn.execute(statement).close()
                with self.assertRaises(errors.EngineError) as caught:
                    self.opened(root)
                self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')

    def test_ddl_is_pinned(self):
        # Pin the actual stored definitions independently of catalogue._TABLES.
        conn = self.opened()
        definitions = conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name").fetchall()
        digest = hashlib.sha256('\n'.join(sql for (sql,) in definitions).encode()).hexdigest()
        self.assertEqual(digest, 'ddcdd6c8b0e39f7f5d012d2dcf02f807b9ad0f8140ca3a44ad992d143ab165c7')

    def test_constraints_hold(self):
        conn = self.opened()
        statements = [
            ("INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, 'invalid', 'other', 1)", (P,)),
            ("INSERT INTO catalogue_identity VALUES (2, 'other', 'isolated', 1)", ()),
            ("INSERT INTO key_routes VALUES (?, ?, 1)", (Q, P)),
        ]
        for sql, parameters in statements:
            with self.subTest(sql=sql):
                conn.execute('BEGIN IMMEDIATE').close()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(sql, parameters).close()
                conn.execute('ROLLBACK').close()

    def test_created_root_is_0700(self):
        previous = os.umask(0)
        try:
            self.opened()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_owner_map_covers_every_table(self):
        conn = self.opened()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(set(catalogue.TABLE_OWNERS), names)
        self.assertEqual(set(catalogue.TABLE_OWNERS.values()), {'store', 'console'})
        self.assertEqual(conn.execute('SELECT version, ddl_sha256 FROM schema_version').fetchone(),
                         ('1', catalogue.ddl_digest()))

    def test_name_is_unique(self):
        conn = self.opened()
        conn.execute("INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, 'same', 'creating', 1)", (P,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO project_routes (project_id, name, state, created_at) VALUES (?, 'same', 'creating', 1)", (Q,))
        conn.execute('ROLLBACK')

    def test_catalogue_opens_wal(self):
        conn = self.opened()
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertEqual(conn.execute('PRAGMA synchronous').fetchone(), (2,))
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone(), (1,))
        for pragma in ['fullfsync', 'checkpoint_fullfsync']:
            self.assertEqual(conn.execute('PRAGMA ' + pragma).fetchone(),
                             (1 if sys.platform == 'darwin' else 0,))
        with mock.patch.object(store_layout, '_row', return_value=('delete',)):
            with self.assertRaises(store_layout.LayoutFault) as caught:
                self.opened()
        self.assertEqual(caught.exception.cause, 'not-wal')


QUEUE_TABLES = frozenset({
    'queue_items', 'queue_conditions', 'queue_events', 'queue_answers',
    'queue_receipts', 'queue_notifications', 'queue_highwaters',
    'queue_notifier_health', 'queue_store_item_sources', 'queue_ingest_skips',
})
NO_PROJECT_NOTICES = (
    'notifier_unhealthy', 'worker_stalled', 'bridge_refused', 'panel_key_enrolled',
    'engine_updated', 'engine_update_failed', 'instruction_carried',
    'seat_record_missing', 'seat_ambiguous', 'backup_missed',
    'engine_stopped', 'engine_started', 'engine_was_down', 'approval_seal_backup_included',
)
OTHER_NOTICES = (
    'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',
    'engine_restart_requested', 'doorbell_off', 'operator_act_carried', 'answer_faulted',
    'settlement_finished', 'settlement_undone', 'project_stopped', 'project_started',
    'merge_target_changed', 'approval_seal_unusable',
)


class TestQueueCatalogue(unittest.TestCase):
    setUp = TestCatalogue.setUp
    opened = TestCatalogue.opened
    def item(self, conn, item_id, **changes):
        values = dict(item_id=item_id, kind='question', subkind=None, project_id=P,
                      raiser='console_queue', revision=1, state='pending', texts='{}',
                      pins='{}', condition_key=item_id, raised_position=1, raised_at_ms=0)
        values.update(changes)
        columns = ', '.join(values)
        conn.execute(f"INSERT INTO queue_items ({columns}) VALUES ({', '.join('?' for _ in values)})",
                     tuple(values.values())).close()

    def condition(self, conn, item_id, *, kind='question', subkind=None, project=P, key='key'):
        self.item(conn, item_id, kind=kind, subkind=subkind, project_id=project)
        conn.execute('INSERT INTO queue_conditions '
                     '(item_id,kind,subkind,project_id,condition_key,raiser,raised_at_ms) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (item_id, kind, subkind, project, key, 'console_queue', 0)).close()

    def test_console_tables_and_owners(self):
        conn = self.opened()
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(QUEUE_TABLES <= tables)
        self.assertEqual({n for n, owner in catalogue.TABLE_OWNERS.items() if owner == 'console'},
                         QUEUE_TABLES)
        self.assertEqual(len(catalogue.TABLE_OWNERS), 18)
        for name in QUEUE_TABLES:
            self.assertEqual(conn.execute('SELECT strict FROM pragma_table_list WHERE name=?',
                                          (name,)).fetchone(), (1,))

    def test_open_condition_unique_with_null_subkind(self):
        conn = self.opened()
        self.condition(conn, 'a')
        with self.assertRaises(sqlite3.IntegrityError):
            self.condition(conn, 'b')
        conn.execute("UPDATE queue_conditions SET closed_at_ms=1 WHERE item_id='a'").close()
        conn.execute("INSERT INTO queue_conditions (item_id,kind,project_id,condition_key,raiser,raised_at_ms) "
                     "VALUES ('b','question',?,'key','console_queue',0)", (P,)).close()

    def test_one_open_condition_per_key(self):
        conn = self.opened()
        self.condition(conn, 'a', kind='notice', subkind='worker_stalled', project=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.condition(conn, 'b', kind='notice', subkind='worker_stalled', project=None)
        self.condition(conn, 'c', kind='notice', subkind='worker_stalled', project=None, key='other')

    def test_item_constraints(self):
        conn = self.opened()
        invalid = [dict(kind='other'), dict(state='other'), dict(kind='notice', subkind='other'),
                   dict(kind='notice'), dict(subkind='doorbell_off'), dict(revision=0),
                   dict(project_id=None)]
        invalid.extend(dict(kind='notice', subkind=s, project_id=P) for s in NO_PROJECT_NOTICES)
        invalid.extend(dict(kind='notice', subkind=s, project_id=None) for s in OTHER_NOTICES)
        invalid.extend(dict(kind=k, project_id=None) for k in
                       ('cross_project_permission', 'merge_go_ahead', 'verification_held'))
        for i, changes in enumerate(invalid):
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.item(conn, 'invalid-' + str(i), **changes)
        self.item(conn, 'source', source_relay_id='relay')
        with self.assertRaises(sqlite3.IntegrityError):
            self.item(conn, 'duplicate', source_relay_id='relay')
        self.item(conn, 'retired', state='retired')
        self.item(conn, 'faulted', state='faulted')
        for s in NO_PROJECT_NOTICES:
            self.item(conn, 'valid-' + s, kind='notice', subkind=s, project_id=None)
        for s in OTHER_NOTICES:
            self.item(conn, 'valid-' + s, kind='notice', subkind=s)
        with self.assertRaises(sqlite3.IntegrityError):
            self.item(conn, 'wrong-answer-to', kind='notice', subkind='doorbell_off', answer_to='a.planner')
        with self.assertRaises(sqlite3.IntegrityError):
            self.item(conn, 'wrong-source', kind='notice', subkind='doorbell_off', source_handle=1)

    def test_answer_row_has_answered_at(self):
        conn = self.opened()
        self.item(conn, 'a')
        for value in (None, -1):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO queue_answers '
                             '(item_id,answer_id,answer,answered_at_ms,apply_key,project_id,phase) '
                             "VALUES ('a','answer','yes',?,'apply',?,'recorded')", (value, P)).close()
        conn.execute('INSERT INTO queue_answers '
                     '(item_id,answer_id,answer,answered_at_ms,apply_key,project_id,phase) '
                     "VALUES ('a','answer','yes',0,'apply',?,'recorded')", (P,)).close()

    def test_open_condition_unique_with_null_project(self):
        conn = self.opened()
        self.condition(conn, 'a', kind='notice', subkind='notifier_unhealthy', project=None, key='notifier')
        with self.assertRaises(sqlite3.IntegrityError):
            self.condition(conn, 'b', kind='notice', subkind='notifier_unhealthy', project=None, key='notifier')
        conn.execute("UPDATE queue_conditions SET closed_at_ms=1 WHERE item_id='a'").close()
        conn.execute("INSERT INTO queue_conditions (item_id,kind,subkind,condition_key,raiser,raised_at_ms) "
                     "VALUES ('b','notice','notifier_unhealthy','notifier','console_queue',0)").close()
        self.condition(conn, 'c', kind='notice', subkind='notifier_unhealthy', project=None, key='notify_worker')

    def test_notifier_health_one_row(self):
        conn = self.opened()
        conn.execute('INSERT INTO queue_notifier_health VALUES (1,0,0)').close()
        for row in [(2,0,0), (1,-1,0)]:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT OR REPLACE INTO queue_notifier_health VALUES (?,?,?)', row).close()

    def test_declared_entries(self):
        conn = self.opened()
        names = QUEUE_TABLES | {'queue_conditions_open_key', 'queue_highwaters_key'}
        entries = {r[0]: r[1] for r in conn.execute('SELECT name,sql FROM sqlite_master')}
        for name in names:
            self.assertIn(name, entries)
            self.assertIn(entries[name], catalogue._TABLES)
        self.assertEqual(conn.execute('SELECT ddl_sha256 FROM schema_version').fetchone(),
                         (hashlib.sha256('\n'.join(catalogue._TABLES).encode()).hexdigest(),))

    def test_anchor_check_constraints(self):
        conn = self.opened()
        bad = [(P,s,0,'anchor') for s in ('question','lifecycle')]
        bad += [(P,s,3,None) for s in ('question','lifecycle')]
        bad += [(None,s,3,'anchor') for s in ('stop_events','store_items')]
        bad += [(None,'lifecycle_log',3,None)]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                conn.execute('INSERT INTO queue_highwaters (project_id,source,position,anchor_id) VALUES (?,?,?,?)', row).close()
        good = [(P,'question',0,None),(Q,'question',3,'question-id'),
                (P,'lifecycle',0,None),(Q,'lifecycle',3,'a'*32),
                (None,'stop_events',3,None),(None,'store_items',3,None),
                (None,'lifecycle_log',3,'line-id')]
        for row in good:
            conn.execute('INSERT INTO queue_highwaters (project_id,source,position,anchor_id) VALUES (?,?,?,?)', row).close()

    def test_store_level_cursor_unique(self):
        conn = self.opened()
        sql = 'INSERT INTO queue_highwaters (project_id,source,position) VALUES (?,?,0)'
        conn.execute(sql, (None,'lifecycle_log')).close()
        for row in [(None,'lifecycle_log'), (P,'lifecycle_log'), (None,'question')]:
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql, row).close()
        for source in ('stop_events','store_items'):
            conn.execute(sql, (None,source)).close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, (None,'store_items')).close()
        # r14's composite primary key permits two NULL-project rows.
        mutant = sqlite3.connect(':memory:')
        self.addCleanup(mutant.close)
        mutant.execute('CREATE TABLE queue_highwaters '
                       '(project_id TEXT, source TEXT, position INTEGER, PRIMARY KEY(project_id,source))')
        mutant.execute(sql, (None,'lifecycle_log'))
        mutant.execute(sql, (None,'lifecycle_log'))
        self.assertEqual(mutant.execute('SELECT count(*) FROM queue_highwaters').fetchone(), (2,))

    def test_store_item_sources_and_ingest_skips(self):
        from relay_engine.tests.test_queue_package import immutable_record_violations
        conn = self.opened()
        self.item(conn, 'a')
        conn.execute("INSERT INTO queue_store_item_sources VALUES (1,'a')").close()
        conn.execute("INSERT INTO queue_store_item_sources VALUES (2,'a')").close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO queue_store_item_sources VALUES (1,'a')").close()
        sql = 'INSERT INTO queue_ingest_skips VALUES (?,?,?,?)'
        for row in [('bad','1','reason',0),('store_items','','reason',0),
                    ('store_items','1','',0),('store_items','1','reason',-1)]:
            with self.subTest(row=row), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql, row).close()
        for source in ('store_items','lifecycle_log'):
            conn.execute(sql, (source,'1','reason',0)).close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, ('store_items','1','again',1)).close()
        package = Path(__file__).parents[1] / 'operator_queue'
        self.assertEqual(immutable_record_violations(package), [])
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / 'probe.py'
            for table in ('queue_store_item_sources','queue_ingest_skips'):
                for operation in ('UPDATE', 'DELETE FROM'):
                    probe.write_text(f'conn.execute("{operation} {table} SET x=1")')
                    self.assertTrue(immutable_record_violations(Path(tmp)))

    def test_receipt_scope_unique(self):
        conn = self.opened()
        sql = 'INSERT INTO queue_receipts VALUES (?,?,?,?,?,?,?,?)'
        conn.execute(sql, ('a','operator','operator','dismiss','key','a'*64,'{}',0)).close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, ('b','operator','operator','dismiss','key','a'*64,'{}',0)).close()
        conn.execute(sql, ('b','operator','operator','open','key','a'*64,'{}',0)).close()
        for digest in ('A'*64, 'a'*63, 'g'*64):
            with self.subTest(digest=digest), self.assertRaises(sqlite3.IntegrityError):
                conn.execute(sql, ('bad','service','s','open',digest,digest,'{}',0)).close()

    def test_required_columns_refuse_null(self):
        conn = self.opened()
        self.item(conn, 'parent')
        rows = {
            'queue_items': dict(item_id='control', kind='question', project_id=P,
                                raiser='console_queue', revision=1, state='pending',
                                texts='{}', pins='{}', condition_key='key',
                                raised_position=1, raised_at_ms=0),
            'queue_conditions': dict(item_id='parent', kind='question', project_id=P,
                                     condition_key='key', raiser='console_queue', raised_at_ms=0),
            'queue_events': dict(position=1, item_id='parent', revision=1, state='pending',
                                 event_kind='raised', principal_kind='service',
                                 principal_id='console_queue', at_ms=0),
            'queue_answers': dict(item_id='parent', answer_id='answer', answer='yes',
                                  answered_at_ms=0, apply_key='apply', project_id=P,
                                  phase='recorded'),
            'queue_receipts': dict(receipt_id='receipt', principal_kind='operator',
                                   principal_id='operator', operation='dismiss',
                                   operation_key='key', request_digest='a' * 64,
                                   result='{}', committed_at_ms=0),
            'queue_notifications': dict(item_id='parent', revision=1, state='pending',
                                        next_attempt_at_ms=0),
        }
        required = {
            'queue_items': ('kind', 'state'),
            'queue_conditions': ('kind', 'condition_key', 'raiser', 'raised_at_ms'),
            'queue_events': ('revision', 'state', 'event_kind', 'principal_kind',
                             'principal_id', 'at_ms'),
            'queue_answers': ('answer_id', 'phase'),
            'queue_receipts': ('principal_kind', 'principal_id', 'operation',
                               'operation_key', 'request_digest', 'committed_at_ms'),
            'queue_notifications': ('state',),
        }
        self.assertEqual(sum(map(len, required.values())), 21)

        def insert(table, values):
            conn.execute(f"INSERT INTO {table} ({', '.join(values)}) "
                         f"VALUES ({', '.join('?' for _ in values)})",
                         tuple(values.values())).close()

        for table, columns in required.items():
            # Each NULL probe starts from a row proved valid against all other constraints.
            conn.execute('SAVEPOINT control').close()
            insert(table, rows[table])
            conn.execute('ROLLBACK TO control').close()
            conn.execute('RELEASE control').close()
            for column in columns:
                with self.subTest(table=table, column=column):
                    conn.execute('SAVEPOINT probe').close()
                    try:
                        with self.assertRaises(sqlite3.IntegrityError) as caught:
                            insert(table, dict(rows[table], **{column: None}))
                        self.assertEqual(str(caught.exception),
                                         f'NOT NULL constraint failed: {table}.{column}')
                    finally:
                        conn.execute('ROLLBACK TO probe').close()
                        conn.execute('RELEASE probe').close()
        with self.subTest(table='queue_notifications', column='pending due time'):
            conn.execute('SAVEPOINT due').close()
            try:
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    insert('queue_notifications', dict(rows['queue_notifications'],
                                                       next_attempt_at_ms=None))
                self.assertEqual(str(caught.exception),
                                 "CHECK constraint failed: state <> 'pending' OR next_attempt_at_ms IS NOT NULL")
            finally:
                conn.execute('ROLLBACK TO due').close()
                conn.execute('RELEASE due').close()
        insert('queue_notifications', dict(rows['queue_notifications'],
                                           state='retired', next_attempt_at_ms=None))

    def test_older_catalogue_refuses(self):
        root = Path(self.tmp.name) / 'older'
        root.mkdir()
        conn = sqlite3.connect(root / 'machine.sqlite3')
        try:
            for statement in D689B0A_TABLES:
                conn.execute(statement).close()
            conn.execute('INSERT INTO schema_version VALUES (1,?,?)',
                         ('1',hashlib.sha256('\n'.join(D689B0A_TABLES).encode()).hexdigest())).close()
            conn.execute('INSERT INTO catalogue_identity VALUES (1,?,?,0)', (P,'isolated')).close()
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(errors.EngineError) as caught:
            self.opened(root)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')


# Historical DDL copied from committed d689b0a, not from the current schema.
D689B0A_TABLES = ("CREATE TABLE catalogue_identity (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    store_id TEXT NOT NULL UNIQUE,\n    kind TEXT NOT NULL CHECK (kind IN ('production', 'isolated')),\n    created_at INTEGER NOT NULL\n) STRICT", "CREATE TABLE project_routes (\n    project_id TEXT NOT NULL PRIMARY KEY,\n    name TEXT NOT NULL UNIQUE,\n    state TEXT NOT NULL CHECK (state IN ('creating', 'active')),\n    created_at INTEGER NOT NULL\n) STRICT", 'CREATE TABLE key_routes (\n    key_id TEXT NOT NULL PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    created_at INTEGER NOT NULL\n) STRICT', 'CREATE TABLE schema_version (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    version TEXT NOT NULL,\n    ddl_sha256 TEXT NOT NULL\n) STRICT')





# Historical batch-5 definitions copied byte-for-byte from committed b1096c25.
B1096C25_TABLES = (
    "CREATE TABLE catalogue_identity (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    store_id TEXT NOT NULL UNIQUE,\n    kind TEXT NOT NULL CHECK (kind IN ('production', 'isolated')),\n    created_at INTEGER NOT NULL\n) STRICT",
    "CREATE TABLE project_routes (\n    project_id TEXT NOT NULL PRIMARY KEY,\n    name TEXT NOT NULL UNIQUE,\n    state TEXT NOT NULL CHECK (state IN ('creating', 'active')),\n    created_at INTEGER NOT NULL,\n    held_by_job TEXT NULL\n) STRICT",
    "CREATE TABLE maintenance_holds (\n    scope TEXT PRIMARY KEY,\n    job_id TEXT NOT NULL,\n    kind TEXT NOT NULL CHECK (kind IN ('cutover','undo','unmark')),\n    since INTEGER NOT NULL\n) STRICT",
    'CREATE TABLE key_routes (\n    key_id TEXT NOT NULL PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    created_at INTEGER NOT NULL\n) STRICT',
    'CREATE TABLE schema_version (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    version TEXT NOT NULL,\n    ddl_sha256 TEXT NOT NULL\n) STRICT',
    "CREATE TABLE engine_stops (\n    row_id INTEGER PRIMARY KEY,\n    scope TEXT NOT NULL CHECK (scope IN ('store', 'project')),\n    project_id TEXT REFERENCES project_routes (project_id),\n    stopped_at TEXT NOT NULL,\n    stopped_by TEXT NOT NULL,\n    ended_at TEXT,\n    ended_by TEXT,\n    CHECK ((scope = 'store') = (project_id IS NULL)),\n    CHECK ((ended_at IS NULL) = (ended_by IS NULL))\n) STRICT",
    "CREATE UNIQUE INDEX engine_stops_one_open\nON engine_stops (scope, ifnull(project_id, '')) WHERE ended_at IS NULL",
    "CREATE TABLE engine_stop_events (\n    event_id INTEGER PRIMARY KEY AUTOINCREMENT,\n    row_id INTEGER NOT NULL REFERENCES engine_stops (row_id),\n    kind TEXT NOT NULL CHECK (kind IN ('stop', 'start')),\n    at TEXT NOT NULL,\n    by TEXT NOT NULL\n) STRICT",
    "CREATE TRIGGER engine_stops_guard_update BEFORE UPDATE ON engine_stops\nWHEN NOT (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL AND NEW.ended_by IS NOT NULL\n          AND NEW.row_id IS OLD.row_id AND NEW.scope IS OLD.scope\n          AND NEW.project_id IS OLD.project_id AND NEW.stopped_at IS OLD.stopped_at\n          AND NEW.stopped_by IS OLD.stopped_by)\nBEGIN SELECT RAISE(ABORT, 'engine_stops is immutable except its first end'); END",
    "CREATE TRIGGER engine_stops_guard_delete BEFORE DELETE ON engine_stops\nBEGIN SELECT RAISE(ABORT, 'engine_stops cannot be deleted'); END",
    "CREATE TRIGGER engine_stop_events_guard_update BEFORE UPDATE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be updated'); END",
    "CREATE TRIGGER engine_stop_events_guard_delete BEFORE DELETE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be deleted'); END",
    "CREATE TABLE checkout_routes (\n    checkout_id TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    repository_id TEXT NOT NULL,\n    git_common_dir TEXT NOT NULL,\n    worktree_path TEXT NOT NULL,\n    dev INTEGER NOT NULL,\n    ino INTEGER NOT NULL,\n    created_at INTEGER NOT NULL,\n    ended_at INTEGER,\n    end_reason TEXT,\n    ledger_synced_at INTEGER,\n    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),\n    CHECK (end_reason IN ('matched', 'unmatched'))\n) STRICT",
    'CREATE UNIQUE INDEX checkout_routes_one_open\nON checkout_routes (dev, ino) WHERE ended_at IS NULL',
    "CREATE TABLE queue_items (\n    item_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK (kind IN ('question', 'cross_project_permission', 'merge_go_ahead', 'verification_held', 'notice')),\n    subkind TEXT CHECK ((subkind IS NULL) = (kind <> 'notice') AND (subkind IS NULL OR subkind IN (\n        'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',\n        'engine_restart_requested', 'engine_updated', 'engine_update_failed',\n        'doorbell_off', 'notifier_unhealthy', 'operator_act_carried', 'worker_stalled',\n        'bridge_refused', 'panel_key_enrolled', 'answer_faulted', 'instruction_carried',\n        'seat_record_missing', 'seat_ambiguous'))),\n    project_id TEXT CHECK ((project_id IS NULL) = (\n        subkind IS 'notifier_unhealthy' OR subkind IS 'worker_stalled' OR\n        subkind IS 'bridge_refused' OR subkind IS 'panel_key_enrolled' OR\n        subkind IS 'engine_updated' OR subkind IS 'engine_update_failed' OR\n        subkind IS 'instruction_carried' OR subkind IS 'seat_record_missing' OR\n        subkind IS 'seat_ambiguous')),\n    raiser TEXT NOT NULL,\n    asking_seat_id TEXT,\n    actor_address TEXT,\n    fields TEXT,\n    answer_to TEXT CHECK (answer_to IS NULL OR kind = 'question'),\n    source_handle INTEGER CHECK (source_handle IS NULL OR kind = 'question'),\n    source_relay_id TEXT UNIQUE,\n    revision INTEGER NOT NULL CHECK (revision >= 1),\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    texts TEXT NOT NULL,\n    popup_text TEXT,\n    choices TEXT,\n    choices_reason TEXT,\n    pins TEXT NOT NULL,\n    condition_key TEXT NOT NULL,\n    raised_position INTEGER NOT NULL,\n    raised_at_ms INTEGER NOT NULL\n) STRICT",
    'CREATE TABLE queue_conditions (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    kind TEXT NOT NULL,\n    subkind TEXT,\n    project_id TEXT,\n    condition_key TEXT NOT NULL,\n    raiser TEXT NOT NULL,\n    raised_at_ms INTEGER NOT NULL,\n    clear_requested INTEGER NOT NULL DEFAULT 0 CHECK (clear_requested IN (0, 1)),\n    closed_at_ms INTEGER,\n    closed_position INTEGER\n) STRICT',
    "CREATE UNIQUE INDEX queue_conditions_open_key\nON queue_conditions (kind, COALESCE(subkind, ''), COALESCE(project_id, ''), condition_key)\nWHERE closed_at_ms IS NULL",
    "CREATE TABLE queue_events (\n    position INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id),\n    revision INTEGER NOT NULL,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    event_kind TEXT NOT NULL CHECK (event_kind IN ('raised', 'answered', 'applied', 'superseded', 'refused', 'faulted', 'withdrawn', 'opened', 'dismissed', 'dismissed_all', 'cleared', 'clear_requested', 'condition_closed', 'retired')),\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    receipt_id TEXT,\n    detail TEXT,\n    at_ms INTEGER NOT NULL\n) STRICT",
    "CREATE TABLE queue_answers (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    answer_id TEXT NOT NULL UNIQUE,\n    answer TEXT NOT NULL,\n    answered_at_ms INTEGER NOT NULL CHECK (answered_at_ms >= 0),\n    apply_key TEXT UNIQUE NOT NULL,\n    draft BLOB,\n    call_args TEXT,\n    project_id TEXT NOT NULL,\n    phase TEXT NOT NULL CHECK (phase IN ('recorded', 'applied', 'confirmed', 'faulted')),\n    last_reason TEXT,\n    outcome TEXT\n) STRICT",
    "CREATE TABLE queue_receipts (\n    receipt_id TEXT PRIMARY KEY,\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    operation TEXT NOT NULL,\n    operation_key TEXT NOT NULL,\n    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'),\n    result TEXT NOT NULL,\n    committed_at_ms INTEGER NOT NULL,\n    UNIQUE (principal_kind, principal_id, operation, operation_key)\n) STRICT",
    "CREATE TABLE queue_notifications (\n    item_id TEXT REFERENCES queue_items (item_id),\n    revision INTEGER,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'retired')),\n    attempts INTEGER NOT NULL DEFAULT 0,\n    last_error_class TEXT,\n    next_attempt_at_ms INTEGER,\n    PRIMARY KEY (item_id, revision),\n    CHECK (state <> 'pending' OR next_attempt_at_ms IS NOT NULL)\n) STRICT",
    "CREATE TABLE queue_highwaters (\n    project_id TEXT,\n    source TEXT NOT NULL CHECK (source IN ('question', 'lifecycle', 'lifecycle_log', 'stop_events', 'store_items')),\n    position INTEGER NOT NULL,\n    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),\n    anchor_id TEXT,\n    CHECK ((project_id IS NULL) = (source IN ('lifecycle_log', 'stop_events', 'store_items'))),\n    CHECK (source NOT IN ('question', 'lifecycle', 'lifecycle_log') OR (position = 0) = (anchor_id IS NULL)),\n    CHECK (source IN ('question', 'lifecycle', 'lifecycle_log') OR anchor_id IS NULL)\n) STRICT",
    "CREATE UNIQUE INDEX queue_highwaters_key\nON queue_highwaters (COALESCE(project_id, ''), source)",
    'CREATE TABLE queue_notifier_health (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),\n    updated_at_ms INTEGER NOT NULL\n) STRICT',
    'CREATE TABLE queue_store_item_sources (\n    raise_row_id INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id)\n) STRICT',
    "CREATE TABLE queue_ingest_skips (\n    source TEXT NOT NULL CHECK (source IN ('store_items', 'lifecycle_log')),\n    row_ref TEXT NOT NULL CHECK (length(row_ref) >= 1),\n    reason TEXT NOT NULL CHECK (length(reason) >= 1),\n    skipped_at_ms INTEGER NOT NULL CHECK (skipped_at_ms >= 0),\n    UNIQUE (source, row_ref)\n) STRICT",
)

class TestPreMaintenanceCatalogue(unittest.TestCase):
    def test_digest_changes_refuse_old_catalogue(self):
        # Each historical catalogue is refused without converting its bytes.
        for revision, definitions in (('38501b22', H_C_TABLES),
                                      ('b1096c25', B1096C25_TABLES),
                                      ('20ef0ccf', H20EF0CCF_TABLES)):
            with self.subTest(revision=revision):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    conn = sqlite3.connect(root / 'machine.sqlite3')
                    try:
                        for statement in definitions:
                            conn.execute(statement).close()
                        conn.execute('INSERT INTO schema_version VALUES (1,?,?)',
                                     ('1', hashlib.sha256('\n'.join(definitions).encode()).hexdigest())).close()
                        conn.execute('INSERT INTO catalogue_identity VALUES (1,?,?,0)', (P, 'isolated')).close()
                        conn.commit()
                    finally:
                        conn.close()
                    before = (root / 'machine.sqlite3').read_bytes()
                    opened = None
                    caught = None
                    try:
                        opened = store_layout.open_catalogue(root)
                    except errors.EngineError as error:
                        caught = error
                    finally:
                        if opened is not None:
                            opened.close()
                    self.assertIsNotNone(caught)
                    self.assertEqual(caught.code, 'E-SCHEMA-MISMATCH')
                    self.assertEqual((root / 'machine.sqlite3').read_bytes(), before)


# Historical DDL copied from committed H_C=38501b22, independent of current entries.
H_C_TABLES = ("CREATE TABLE catalogue_identity (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    store_id TEXT NOT NULL UNIQUE,\n    kind TEXT NOT NULL CHECK (kind IN ('production', 'isolated')),\n    created_at INTEGER NOT NULL\n) STRICT", "CREATE TABLE project_routes (\n    project_id TEXT NOT NULL PRIMARY KEY,\n    name TEXT NOT NULL UNIQUE,\n    state TEXT NOT NULL CHECK (state IN ('creating', 'active')),\n    created_at INTEGER NOT NULL\n) STRICT", 'CREATE TABLE key_routes (\n    key_id TEXT NOT NULL PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    created_at INTEGER NOT NULL\n) STRICT', 'CREATE TABLE schema_version (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    version TEXT NOT NULL,\n    ddl_sha256 TEXT NOT NULL\n) STRICT', "CREATE TABLE engine_stops (\n    row_id INTEGER PRIMARY KEY,\n    scope TEXT NOT NULL CHECK (scope IN ('store', 'project')),\n    project_id TEXT REFERENCES project_routes (project_id),\n    stopped_at TEXT NOT NULL,\n    stopped_by TEXT NOT NULL,\n    ended_at TEXT,\n    ended_by TEXT,\n    CHECK ((scope = 'store') = (project_id IS NULL)),\n    CHECK ((ended_at IS NULL) = (ended_by IS NULL))\n) STRICT", "CREATE UNIQUE INDEX engine_stops_one_open\nON engine_stops (scope, ifnull(project_id, '')) WHERE ended_at IS NULL", "CREATE TABLE engine_stop_events (\n    event_id INTEGER PRIMARY KEY AUTOINCREMENT,\n    row_id INTEGER NOT NULL REFERENCES engine_stops (row_id),\n    kind TEXT NOT NULL CHECK (kind IN ('stop', 'start')),\n    at TEXT NOT NULL,\n    by TEXT NOT NULL\n) STRICT", "CREATE TRIGGER engine_stops_guard_update BEFORE UPDATE ON engine_stops\nWHEN NOT (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL AND NEW.ended_by IS NOT NULL\n          AND NEW.row_id IS OLD.row_id AND NEW.scope IS OLD.scope\n          AND NEW.project_id IS OLD.project_id AND NEW.stopped_at IS OLD.stopped_at\n          AND NEW.stopped_by IS OLD.stopped_by)\nBEGIN SELECT RAISE(ABORT, 'engine_stops is immutable except its first end'); END", "CREATE TRIGGER engine_stops_guard_delete BEFORE DELETE ON engine_stops\nBEGIN SELECT RAISE(ABORT, 'engine_stops cannot be deleted'); END", "CREATE TRIGGER engine_stop_events_guard_update BEFORE UPDATE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be updated'); END", "CREATE TRIGGER engine_stop_events_guard_delete BEFORE DELETE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be deleted'); END", "CREATE TABLE checkout_routes (\n    checkout_id TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    repository_id TEXT NOT NULL,\n    git_common_dir TEXT NOT NULL,\n    worktree_path TEXT NOT NULL,\n    dev INTEGER NOT NULL,\n    ino INTEGER NOT NULL,\n    created_at INTEGER NOT NULL,\n    ended_at INTEGER,\n    end_reason TEXT,\n    ledger_synced_at INTEGER,\n    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),\n    CHECK (end_reason IN ('matched', 'unmatched'))\n) STRICT", 'CREATE UNIQUE INDEX checkout_routes_one_open\nON checkout_routes (dev, ino) WHERE ended_at IS NULL', "CREATE TABLE queue_items (\n    item_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK (kind IN ('question', 'cross_project_permission', 'merge_go_ahead', 'verification_held', 'notice')),\n    subkind TEXT CHECK ((subkind IS NULL) = (kind <> 'notice') AND (subkind IS NULL OR subkind IN (\n        'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',\n        'engine_restart_requested', 'engine_updated', 'engine_update_failed',\n        'doorbell_off', 'notifier_unhealthy', 'operator_act_carried', 'worker_stalled',\n        'bridge_refused', 'panel_key_enrolled', 'answer_faulted', 'instruction_carried',\n        'seat_record_missing', 'seat_ambiguous'))),\n    project_id TEXT CHECK ((project_id IS NULL) = (\n        subkind IS 'notifier_unhealthy' OR subkind IS 'worker_stalled' OR\n        subkind IS 'bridge_refused' OR subkind IS 'panel_key_enrolled' OR\n        subkind IS 'engine_updated' OR subkind IS 'engine_update_failed' OR\n        subkind IS 'instruction_carried' OR subkind IS 'seat_record_missing' OR\n        subkind IS 'seat_ambiguous')),\n    raiser TEXT NOT NULL,\n    asking_seat_id TEXT,\n    actor_address TEXT,\n    fields TEXT,\n    answer_to TEXT CHECK (answer_to IS NULL OR kind = 'question'),\n    source_handle INTEGER CHECK (source_handle IS NULL OR kind = 'question'),\n    source_relay_id TEXT UNIQUE,\n    revision INTEGER NOT NULL CHECK (revision >= 1),\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    texts TEXT NOT NULL,\n    popup_text TEXT,\n    choices TEXT,\n    choices_reason TEXT,\n    pins TEXT NOT NULL,\n    condition_key TEXT NOT NULL,\n    raised_position INTEGER NOT NULL,\n    raised_at_ms INTEGER NOT NULL\n) STRICT", 'CREATE TABLE queue_conditions (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    kind TEXT NOT NULL,\n    subkind TEXT,\n    project_id TEXT,\n    condition_key TEXT NOT NULL,\n    raiser TEXT NOT NULL,\n    raised_at_ms INTEGER NOT NULL,\n    clear_requested INTEGER NOT NULL DEFAULT 0 CHECK (clear_requested IN (0, 1)),\n    closed_at_ms INTEGER,\n    closed_position INTEGER\n) STRICT', "CREATE UNIQUE INDEX queue_conditions_open_key\nON queue_conditions (kind, COALESCE(subkind, ''), COALESCE(project_id, ''), condition_key)\nWHERE closed_at_ms IS NULL", "CREATE TABLE queue_events (\n    position INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id),\n    revision INTEGER NOT NULL,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    event_kind TEXT NOT NULL CHECK (event_kind IN ('raised', 'answered', 'applied', 'superseded', 'refused', 'faulted', 'withdrawn', 'opened', 'dismissed', 'dismissed_all', 'cleared', 'clear_requested', 'condition_closed', 'retired')),\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    receipt_id TEXT,\n    detail TEXT,\n    at_ms INTEGER NOT NULL\n) STRICT", "CREATE TABLE queue_answers (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    answer_id TEXT NOT NULL UNIQUE,\n    answer TEXT NOT NULL,\n    answered_at_ms INTEGER NOT NULL CHECK (answered_at_ms >= 0),\n    apply_key TEXT UNIQUE NOT NULL,\n    draft BLOB,\n    call_args TEXT,\n    project_id TEXT NOT NULL,\n    phase TEXT NOT NULL CHECK (phase IN ('recorded', 'applied', 'confirmed', 'faulted')),\n    last_reason TEXT,\n    outcome TEXT\n) STRICT", "CREATE TABLE queue_receipts (\n    receipt_id TEXT PRIMARY KEY,\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    operation TEXT NOT NULL,\n    operation_key TEXT NOT NULL,\n    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'),\n    result TEXT NOT NULL,\n    committed_at_ms INTEGER NOT NULL,\n    UNIQUE (principal_kind, principal_id, operation, operation_key)\n) STRICT", "CREATE TABLE queue_notifications (\n    item_id TEXT REFERENCES queue_items (item_id),\n    revision INTEGER,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'retired')),\n    attempts INTEGER NOT NULL DEFAULT 0,\n    last_error_class TEXT,\n    next_attempt_at_ms INTEGER,\n    PRIMARY KEY (item_id, revision),\n    CHECK (state <> 'pending' OR next_attempt_at_ms IS NOT NULL)\n) STRICT", "CREATE TABLE queue_highwaters (\n    project_id TEXT,\n    source TEXT NOT NULL CHECK (source IN ('question', 'lifecycle', 'lifecycle_log', 'stop_events', 'store_items')),\n    position INTEGER NOT NULL,\n    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),\n    anchor_id TEXT,\n    CHECK ((project_id IS NULL) = (source IN ('lifecycle_log', 'stop_events', 'store_items'))),\n    CHECK (source NOT IN ('question', 'lifecycle', 'lifecycle_log') OR (position = 0) = (anchor_id IS NULL)),\n    CHECK (source IN ('question', 'lifecycle', 'lifecycle_log') OR anchor_id IS NULL)\n) STRICT", "CREATE UNIQUE INDEX queue_highwaters_key\nON queue_highwaters (COALESCE(project_id, ''), source)", 'CREATE TABLE queue_notifier_health (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),\n    updated_at_ms INTEGER NOT NULL\n) STRICT', 'CREATE TABLE queue_store_item_sources (\n    raise_row_id INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id)\n) STRICT', "CREATE TABLE queue_ingest_skips (\n    source TEXT NOT NULL CHECK (source IN ('store_items', 'lifecycle_log')),\n    row_ref TEXT NOT NULL CHECK (length(row_ref) >= 1),\n    reason TEXT NOT NULL CHECK (length(reason) >= 1),\n    skipped_at_ms INTEGER NOT NULL CHECK (skipped_at_ms >= 0),\n    UNIQUE (source, row_ref)\n) STRICT")

if __name__ == '__main__':
    unittest.main()


# Historical DDL copied from the granted base 20ef0ccf.
H20EF0CCF_TABLES = ("CREATE TABLE catalogue_identity (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    store_id TEXT NOT NULL UNIQUE,\n    kind TEXT NOT NULL CHECK (kind IN ('production', 'isolated')),\n    created_at INTEGER NOT NULL\n) STRICT", "CREATE TABLE project_routes (\n    project_id TEXT NOT NULL PRIMARY KEY,\n    name TEXT NOT NULL UNIQUE,\n    state TEXT NOT NULL CHECK (state IN ('creating', 'active')),\n    created_at INTEGER NOT NULL,\n    held_by_job TEXT NULL\n) STRICT", "CREATE TABLE maintenance_holds (\n    scope TEXT PRIMARY KEY,\n    job_id TEXT NOT NULL,\n    kind TEXT NOT NULL CHECK (kind IN ('cutover','undo','unmark')),\n    since INTEGER NOT NULL\n) STRICT", 'CREATE TABLE key_routes (\n    key_id TEXT NOT NULL PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    created_at INTEGER NOT NULL\n) STRICT', 'CREATE TABLE schema_version (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    version TEXT NOT NULL,\n    ddl_sha256 TEXT NOT NULL\n) STRICT', "CREATE TABLE engine_stops (\n    row_id INTEGER PRIMARY KEY,\n    scope TEXT NOT NULL CHECK (scope IN ('store', 'project')),\n    project_id TEXT REFERENCES project_routes (project_id),\n    stopped_at TEXT NOT NULL,\n    stopped_by TEXT NOT NULL,\n    ended_at TEXT,\n    ended_by TEXT,\n    CHECK ((scope = 'store') = (project_id IS NULL)),\n    CHECK ((ended_at IS NULL) = (ended_by IS NULL))\n) STRICT", "CREATE UNIQUE INDEX engine_stops_one_open\nON engine_stops (scope, ifnull(project_id, '')) WHERE ended_at IS NULL", "CREATE TABLE engine_stop_events (\n    event_id INTEGER PRIMARY KEY AUTOINCREMENT,\n    row_id INTEGER NOT NULL REFERENCES engine_stops (row_id),\n    kind TEXT NOT NULL CHECK (kind IN ('stop', 'start')),\n    at TEXT NOT NULL,\n    by TEXT NOT NULL\n) STRICT", "CREATE TRIGGER engine_stops_guard_update BEFORE UPDATE ON engine_stops\nWHEN NOT (OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL AND NEW.ended_by IS NOT NULL\n          AND NEW.row_id IS OLD.row_id AND NEW.scope IS OLD.scope\n          AND NEW.project_id IS OLD.project_id AND NEW.stopped_at IS OLD.stopped_at\n          AND NEW.stopped_by IS OLD.stopped_by)\nBEGIN SELECT RAISE(ABORT, 'engine_stops is immutable except its first end'); END", "CREATE TRIGGER engine_stops_guard_delete BEFORE DELETE ON engine_stops\nBEGIN SELECT RAISE(ABORT, 'engine_stops cannot be deleted'); END", "CREATE TRIGGER engine_stop_events_guard_update BEFORE UPDATE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be updated'); END", "CREATE TRIGGER engine_stop_events_guard_delete BEFORE DELETE ON engine_stop_events\nBEGIN SELECT RAISE(ABORT, 'engine_stop_events cannot be deleted'); END", "CREATE TABLE checkout_routes (\n    checkout_id TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL REFERENCES project_routes (project_id),\n    repository_id TEXT NOT NULL,\n    git_common_dir TEXT NOT NULL,\n    worktree_path TEXT NOT NULL,\n    dev INTEGER NOT NULL,\n    ino INTEGER NOT NULL,\n    created_at INTEGER NOT NULL,\n    ended_at INTEGER,\n    end_reason TEXT,\n    ledger_synced_at INTEGER,\n    CHECK ((ended_at IS NULL) = (end_reason IS NULL)),\n    CHECK (end_reason IN ('matched', 'unmatched'))\n) STRICT", 'CREATE UNIQUE INDEX checkout_routes_one_open\nON checkout_routes (dev, ino) WHERE ended_at IS NULL', "CREATE TABLE queue_items (\n    item_id TEXT PRIMARY KEY,\n    kind TEXT NOT NULL CHECK (kind IN ('question', 'cross_project_permission', 'merge_go_ahead', 'verification_held', 'notice')),\n    subkind TEXT CHECK ((subkind IS NULL) = (kind <> 'notice') AND (subkind IS NULL OR subkind IN (\n        'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',\n        'engine_restart_requested', 'engine_updated', 'engine_update_failed',\n        'doorbell_off', 'notifier_unhealthy', 'operator_act_carried', 'worker_stalled',\n        'bridge_refused', 'panel_key_enrolled', 'answer_faulted', 'instruction_carried',\n        'seat_record_missing', 'seat_ambiguous', 'backup_missed'))),\n    project_id TEXT CHECK ((project_id IS NULL) = (\n        subkind IS 'notifier_unhealthy' OR subkind IS 'worker_stalled' OR\n        subkind IS 'bridge_refused' OR subkind IS 'panel_key_enrolled' OR\n        subkind IS 'engine_updated' OR subkind IS 'engine_update_failed' OR\n        subkind IS 'instruction_carried' OR subkind IS 'seat_record_missing' OR\n        subkind IS 'seat_ambiguous' OR subkind IS 'backup_missed')),\n    raiser TEXT NOT NULL,\n    asking_seat_id TEXT,\n    actor_address TEXT,\n    fields TEXT,\n    answer_to TEXT CHECK (answer_to IS NULL OR kind = 'question'),\n    source_handle INTEGER CHECK (source_handle IS NULL OR kind = 'question'),\n    source_relay_id TEXT UNIQUE,\n    revision INTEGER NOT NULL CHECK (revision >= 1),\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    texts TEXT NOT NULL,\n    popup_text TEXT,\n    choices TEXT,\n    choices_reason TEXT,\n    pins TEXT NOT NULL,\n    condition_key TEXT NOT NULL,\n    raised_position INTEGER NOT NULL,\n    raised_at_ms INTEGER NOT NULL\n) STRICT", 'CREATE TABLE queue_conditions (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    kind TEXT NOT NULL,\n    subkind TEXT,\n    project_id TEXT,\n    condition_key TEXT NOT NULL,\n    raiser TEXT NOT NULL,\n    raised_at_ms INTEGER NOT NULL,\n    clear_requested INTEGER NOT NULL DEFAULT 0 CHECK (clear_requested IN (0, 1)),\n    closed_at_ms INTEGER,\n    closed_position INTEGER\n) STRICT', "CREATE UNIQUE INDEX queue_conditions_open_key\nON queue_conditions (kind, COALESCE(subkind, ''), COALESCE(project_id, ''), condition_key)\nWHERE closed_at_ms IS NULL", "CREATE TABLE queue_events (\n    position INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id),\n    revision INTEGER NOT NULL,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted', 'dismissed', 'withdrawn', 'cleared', 'retired')),\n    event_kind TEXT NOT NULL CHECK (event_kind IN ('raised', 'answered', 'applied', 'superseded', 'refused', 'faulted', 'withdrawn', 'opened', 'dismissed', 'dismissed_all', 'cleared', 'clear_requested', 'condition_closed', 'retired')),\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    receipt_id TEXT,\n    detail TEXT,\n    at_ms INTEGER NOT NULL\n) STRICT", "CREATE TABLE queue_answers (\n    item_id TEXT PRIMARY KEY REFERENCES queue_items (item_id),\n    answer_id TEXT NOT NULL UNIQUE,\n    answer TEXT NOT NULL,\n    answered_at_ms INTEGER NOT NULL CHECK (answered_at_ms >= 0),\n    apply_key TEXT UNIQUE NOT NULL,\n    draft BLOB,\n    call_args TEXT,\n    project_id TEXT NOT NULL,\n    phase TEXT NOT NULL CHECK (phase IN ('recorded', 'applied', 'confirmed', 'faulted')),\n    last_reason TEXT,\n    outcome TEXT\n) STRICT", "CREATE TABLE queue_receipts (\n    receipt_id TEXT PRIMARY KEY,\n    principal_kind TEXT NOT NULL,\n    principal_id TEXT NOT NULL,\n    operation TEXT NOT NULL,\n    operation_key TEXT NOT NULL,\n    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'),\n    result TEXT NOT NULL,\n    committed_at_ms INTEGER NOT NULL,\n    UNIQUE (principal_kind, principal_id, operation, operation_key)\n) STRICT", "CREATE TABLE queue_notifications (\n    item_id TEXT REFERENCES queue_items (item_id),\n    revision INTEGER,\n    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'retired')),\n    attempts INTEGER NOT NULL DEFAULT 0,\n    last_error_class TEXT,\n    next_attempt_at_ms INTEGER,\n    PRIMARY KEY (item_id, revision),\n    CHECK (state <> 'pending' OR next_attempt_at_ms IS NOT NULL)\n) STRICT", "CREATE TABLE queue_highwaters (\n    project_id TEXT,\n    source TEXT NOT NULL CHECK (source IN ('question', 'lifecycle', 'lifecycle_log', 'stop_events', 'store_items')),\n    position INTEGER NOT NULL,\n    epoch INTEGER NOT NULL DEFAULT 0 CHECK (epoch >= 0),\n    anchor_id TEXT,\n    CHECK ((project_id IS NULL) = (source IN ('lifecycle_log', 'stop_events', 'store_items'))),\n    CHECK (source NOT IN ('question', 'lifecycle', 'lifecycle_log') OR (position = 0) = (anchor_id IS NULL)),\n    CHECK (source IN ('question', 'lifecycle', 'lifecycle_log') OR anchor_id IS NULL)\n) STRICT", "CREATE UNIQUE INDEX queue_highwaters_key\nON queue_highwaters (COALESCE(project_id, ''), source)", 'CREATE TABLE queue_notifier_health (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),\n    updated_at_ms INTEGER NOT NULL\n) STRICT', 'CREATE TABLE queue_store_item_sources (\n    raise_row_id INTEGER PRIMARY KEY,\n    item_id TEXT NOT NULL REFERENCES queue_items (item_id)\n) STRICT', "CREATE TABLE queue_ingest_skips (\n    source TEXT NOT NULL CHECK (source IN ('store_items', 'lifecycle_log')),\n    row_ref TEXT NOT NULL CHECK (length(row_ref) >= 1),\n    reason TEXT NOT NULL CHECK (length(reason) >= 1),\n    skipped_at_ms INTEGER NOT NULL CHECK (skipped_at_ms >= 0),\n    UNIQUE (source, row_ref)\n) STRICT")
