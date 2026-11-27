"""Authority's sending-ledger cross-project permission records."""
from contextlib import closing
import sqlite3

from relay_engine import errors, schema, strings
from relay_engine.tests import test_schema


SHA = 'a' * 64
TABLES = ('cross_project_allows', 'cross_project_decisions', 'cross_project_outcomes')
CODE = 'E-AUTHORITY-CROSS-PROJECT-DENIED'


class TestPermissionTables(test_schema.Store):
    def test_tables_strict_owned_and_checked(self):
        conn = self.connect()
        self.relay(conn, 'relay', 1, body_sha=SHA)
        with closing(conn.execute('PRAGMA table_list')) as cursor:
            strict = {row[1]: row[5] for row in cursor}
        for table in TABLES:
            with self.subTest(table=table):
                self.assertEqual(strict.get(table), 1)
                self.assertEqual(schema.TABLE_OWNERS.get(table), 'authority')

        permits = ('q', 1, 10, 'operator', None, None)
        decisions = ('relay', 'q', SHA, 'once', 10, 'operator')
        outcomes = ('key', 'answer', SHA, 'applied', None, 'receipt', 10)
        for table, row, bad in (
            (TABLES[0], permits, ((0, ''), (1, 0), (3, 'seat'),
                                  (4, 11), (5, 'operator'), (5, 'seat'))),
            (TABLES[1], decisions, ((0, 'missing'), (2, '0'), (2, 'A' * 64),
                                    (3, 'always'), (5, 'seat'))),
            (TABLES[2], outcomes, ((0, ''), (0, 'é' * 129), (0, 'k\x00y'), (1, 'unknown'),
                                   (2, 'g' * 64), (3, 'refused'), (3, 'stale'),
                                   (4, 'source_restored'))),
        ):
            sql = 'INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in row) + ')'
            for field, value in bad:
                with self.subTest(table=table, field=field, value=value):
                    invalid = tuple(value if i == field else old for i, old in enumerate(row))
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql, invalid).close()
            conn.execute(sql, row).close()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO cross_project_outcomes VALUES (?,?,?,?,?,?,?)',
                         ('another-key', *outcomes[1:])).close()
        for table in TABLES[1:]:
            for sql in ('UPDATE %s SET rowid = rowid', 'DELETE FROM %s'):
                with self.subTest(table=table, sql=sql):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, 'insert-only'):
                        conn.execute(sql % table).close()

    def test_denied_code_catalogue(self):
        error = errors.error_for(CODE)
        self.assertEqual(error.cause, 'the operator did not allow this relay to reach the other project')
        self.assertEqual(error.remedy, 'ask the operator, or send it to a seat of your own project')
        self.assertEqual(errors.explain_for(CODE)[0],
                         'a relay to another project waits until the operator answers it, and a deny ends that relay only')
        self.assertEqual(errors.ERRORS[CODE].cls, 'command')
        self.assertIn(CODE, strings._CODE_VALUES)
