"""Additive judgment storage preserves old writers and transactional rollback."""
import ast
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import types
import unittest

from relay_engine import daemon, ledger, rules
from relay_engine.tests.test_ledger import LedgerFixture, BODY, WALL
from relay_engine.tests.test_identity_matrix import RunningDaemon

NEW_TABLES = {'admission_judgments', 'admission_findings', 'roster', 'routed_notices'}
BASE = 'cbc8f071cd593af4c48994ec30b0775b3c919ad6'


def base_module(name):
    try:
        source = subprocess.check_output(['git', 'show', BASE + ':tools/relay_engine/' + name + '.py'], text=True)
    except subprocess.CalledProcessError as exc:
        raise AssertionError('base oracle requires commit ' + BASE + ' in repository history') from exc
    module = types.ModuleType('v297_test_base_' + name)
    module.__file__ = str(Path(ledger.__file__).with_name(name + '.py'))
    sys.modules[module.__name__] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


class TestJudgmentStore(LedgerFixture):
    @classmethod
    def setUpClass(cls):
        cls.old = base_module('ledger')
        cls.old_daemon = base_module('daemon')

    def require_store(self):
        self.assertTrue(hasattr(ledger, 'ensure_additive_schema'))
        self.assertTrue(hasattr(ledger, 'record_judgment'))
        self.assertTrue(hasattr(ledger, 'judgment_for'))

    def tables(self):
        return {row[0] for row in self.ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def test_fresh_ledger_has_the_four_new_tables(self):
        self.initialize()
        self.assertTrue(NEW_TABLES <= self.tables())

    def test_existing_2_9_6_ledger_gains_tables_on_start_and_keeps_rows(self):
        self.require_store()
        self.ledger = self.old.init_schema(self.engine_fd, self.root.path)
        original = self.old.admit(self.ledger, self.root, self.envelope, BODY, 'old', clock=lambda: WALL)
        before = self.ledger.execute('SELECT * FROM relays').fetchall()
        self.assertFalse(NEW_TABLES & self.tables())
        self.ledger.close()
        self.ledger = None
        service = RunningDaemon(self.root.path, {'kit': '2.9.0', 'fp': 'a' * 64, 'install': '/judgment-upgrade'}).start()
        service.stop()
        self.ledger = ledger.open_ledger(self.engine_fd)
        self.assertTrue(NEW_TABLES <= self.tables())
        self.assertEqual(self.ledger.execute('SELECT * FROM relays WHERE seq=?', (original.seq,)).fetchall(), before)
        self.assertEqual(ledger.judgment_for(self.ledger, original.seq), ('none', ()))
        self.assertEqual(self.ledger.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone(), ('1',))

    def test_ensure_is_idempotent(self):
        self.require_store()
        self.initialize()
        ledger.ensure_additive_schema(self.ledger)
        ledger.ensure_additive_schema(self.ledger)
        self.assertTrue(NEW_TABLES <= self.tables())

    def test_judgment_and_findings_commit_or_roll_back_with_the_relay(self):
        self.require_store()
        self.initialize()
        finding = rules.Finding('lineage.dispatch', 'error', 'placeholder', ('older.md',), {'edge': 'parent'}, 'missing parent')
        def fault(point):
            if point == 'after-relay-insert':
                seq, path = self.ledger.execute('SELECT seq,rendered_path FROM relays').fetchone()
                ledger.record_judgment(self.ledger, seq, 'judged', 'b' * 64, (finding,))
                status, stored = ledger.judgment_for(self.ledger, seq)
                self.assertEqual(status, 'judged')
                self.assertEqual(stored, (rules.Finding(finding.id, finding.lint_severity, path, finding.operands, finding.params, finding.message),))
                raise ledger.InjectedFault(point)
        with self.assertRaises(ledger.InjectedFault):
            self.submit(fault=fault, clock=lambda: WALL)
        for table in ('relays', 'admission_judgments', 'admission_findings'):
            self.assertEqual(self.ledger.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        admitted = self.submit(clock=lambda: WALL)
        with ledger._transaction(self.ledger):
            ledger.record_judgment(self.ledger, admitted.seq, 'judged', 'b' * 64, (finding,))
        self.assertEqual(ledger.judgment_for(self.ledger, admitted.seq)[1][0].message, 'missing parent')
        with ledger._transaction(self.ledger), self.assertRaises(sqlite3.IntegrityError):
            ledger.record_judgment(self.ledger, admitted.seq, 'judged', 'b' * 64, ())

    def test_row_appended_without_judgment_reads_none(self):
        self.require_store()
        self.initialize()
        result = self.old.admit(self.ledger, self.root, self.envelope, BODY, 'old-writer', clock=lambda: WALL)
        self.assertEqual(ledger.judgment_for(self.ledger, result.seq), ('none', ()))

    def test_base_reader_ignores_new_tables(self):
        self.require_store()
        self.initialize()
        admitted = self.submit(clock=lambda: WALL)
        with ledger._transaction(self.ledger):
            ledger.record_judgment(self.ledger, admitted.seq, 'judged', 'c' * 64, ())
        self.ledger.close()
        self.ledger = None
        service = RunningDaemon(self.root.path, {'kit': '2.9.0', 'fp': 'a' * 64, 'install': '/judgment-reader'}).start()
        service.stop()
        context = self.old_daemon.read_lint_context(self.root.path)
        self.assertIsNotNone(context)
        self.assertEqual(context['entries'], [{'path': admitted.rendered_path, 'body_sha256': __import__('hashlib').sha256(BODY).hexdigest(), 'origin': 'daemon'}])

    def test_new_tables_are_append_only(self):
        self.require_store()
        for name in ('ledger.py', 'roster.py'):
            path = Path(ledger.__file__).with_name(name)
            if not path.exists():
                continue  # Roster is created in Task 5; the guard then includes it.
            source = path.read_text().upper()
            for table in NEW_TABLES:
                for operation in ('UPDATE ', 'DELETE FROM '):
                    self.assertNotIn(operation + table.upper(), source)

    def test_record_requires_transaction_and_failed_has_no_findings(self):
        self.require_store()
        self.initialize()
        admitted = self.submit(clock=lambda: WALL)
        with self.assertRaises(ValueError):
            ledger.record_judgment(self.ledger, admitted.seq, 'judged', 'c' * 64, ())
        with ledger._transaction(self.ledger):
            ledger.record_judgment(self.ledger, admitted.seq, 'failed', 'c' * 64, ())
        self.assertEqual(ledger.judgment_for(self.ledger, admitted.seq), ('failed', ()))
