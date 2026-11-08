"""Per-project paths and opening: catches ambient-home, creation and WAL regressions."""
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from relay_engine import errors, schema, store_layout, submit


P = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
Q = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
K = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'


class TestStoreLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'

    def opened(self, *, project=P, create=True):
        conn = store_layout.open_ledger(self.root, project, create=create)
        self.addCleanup(conn.close)
        return conn

    def project(self, conn, project=P):
        conn.execute('INSERT INTO projects(id, name, created_at) VALUES (?, ?, 1)',
                     (project, 'example'))
        conn.execute('COMMIT')

    def test_production_root_uses_password_home(self):
        # Break: trusting the caller's HOME instead of the password database.
        previous = os.environ.get('HOME')
        try:
            os.environ['HOME'] = '/ambient-home-must-not-win'
            with mock.patch('pwd.getpwuid', return_value=SimpleNamespace(pw_dir='/password-home')):
                self.assertEqual(store_layout.production_root(), Path('/password-home/.adt'))
        finally:
            if previous is None:
                os.environ.pop('HOME', None)
            else:
                os.environ['HOME'] = previous

    def test_paths_refuse_noncanonical_ids(self):
        # Break: allowing an id to escape its root or alias another spelling.
        invalid = [P.upper(), P[:-1], '..', '../' + P, P + '/child', '', 1]
        for value in invalid:
            with self.subTest(value=value):
                for call in [lambda: store_layout.ledger_path(self.root, value),
                             lambda: store_layout.evidence_dir(self.root, value),
                             lambda: store_layout.key_path(self.root, P, value, K),
                             lambda: store_layout.key_path(self.root, P, Q, value),
                             lambda: store_layout.jev_log_dir(self.root, value)]:
                    with self.assertRaises(ValueError):
                        call()
        self.assertEqual(store_layout.ledger_path(self.root, P),
                         self.root / 'projects' / P / 'ledger.sqlite3')
        self.assertEqual(store_layout.evidence_dir(self.root, P),
                         self.root / 'projects' / P / 'evidence')
        self.assertEqual(store_layout.key_path(self.root, P, Q, K),
                         self.root / 'projects' / P / 'keys' / Q / (K + '.key'))
        self.assertFalse(self.root.exists())

    def test_jev_paths_unchanged(self):
        self.assertEqual(store_layout.jev_key_path(self.root), self.root / 'jev' / 'key')
        self.assertEqual(store_layout.jev_log_dir(self.root, P),
                         self.root / 'projects' / P / 'jev')

    def test_open_missing_ledger_creates_nothing(self):
        # M1: without the mode=rw probe open_store silently initializes a file.
        path = self.root / 'projects' / P / 'ledger.sqlite3'
        path.parent.mkdir(parents=True)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.cause, 'missing')
        self.assertFalse(path.exists())
        control = submit.open_store(path)
        control.close()
        self.assertTrue(path.exists())

    def test_open_sets_and_asserts_wal(self):
        # M2: issuing the pragma without checking its answer accepts delete mode.
        conn = self.opened()
        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('wal',))
        self.assertEqual(conn.execute('PRAGMA synchronous').fetchone(), (2,))
        self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone(), (1,))

        class Answer:
            def fetchone(self):
                return ('delete',)

            def close(self):
                pass

        class RefusingWal:
            def execute(self, sql):
                return Answer()

            def close(self):
                pass

        with mock.patch.object(submit, 'open_store', return_value=RefusingWal()):
            with self.assertRaises(store_layout.LayoutFault) as caught:
                self.opened()
        self.assertEqual(caught.exception.cause, 'not-wal')
        control = sqlite3.connect(Path(self.tmp.name) / 'delete.sqlite3')
        self.addCleanup(control.close)
        control.execute('BEGIN IMMEDIATE')
        self.assertEqual(control.execute('PRAGMA journal_mode=WAL').fetchone(), ('delete',))
        control.execute('ROLLBACK')

    def test_open_darwin_full_syncs(self):
        conn = self.opened()
        want = 1 if sys.platform == 'darwin' else 0
        self.assertEqual(conn.execute('PRAGMA fullfsync').fetchone(), (want,))
        self.assertEqual(conn.execute('PRAGMA checkpoint_fullfsync').fetchone(), (want,))

    def test_open_refuses_wrong_project(self):
        conn = self.opened()
        self.project(conn, Q)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.cause, 'wrong-project')
        # A second fresh ledger may be provisioned, but may not be served empty.
        self.opened(project=Q)
        with self.assertRaises(store_layout.LayoutFault) as caught:
            self.opened(project=Q, create=False)
        self.assertEqual(caught.exception.cause, 'no-project-row')

    def test_open_refuses_old_digest(self):
        conn = self.opened()
        self.project(conn)
        conn.execute("UPDATE schema_version SET ddl_sha256 = ?", ('f' * 64,))
        conn.execute('COMMIT')
        with self.assertRaises(errors.EngineError) as caught:
            self.opened(create=False)
        self.assertEqual(caught.exception.code, 'E-SCHEMA-MISMATCH')

    def test_created_dirs_are_0700(self):
        old = os.umask(0)
        try:
            self.opened()
        finally:
            os.umask(old)
        for path in [self.root, self.root / 'projects', self.root / 'projects' / P]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)


if __name__ == '__main__':
    unittest.main()
