"""One independent M0 selector per builder, including a read of its scratch copy."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import api_client, queries
from relay_engine.tests import relay_reads_fixtures as f


class TestFixtureBuild(unittest.TestCase):
    @contextmanager
    def copied(self, builder, *, relay_count, role):
        with tempfile.TemporaryDirectory(prefix='m0-') as tmp:
            base = Path(tmp).resolve()
            with patch.object(tempfile, 'tempdir', str(base)):
                fix = builder(str(base / 'store'))
            reader = f'{role}-0'
            with closing(sqlite3.connect(f.scratch_copy_path(fix.root).as_uri() + '?mode=rw',
                                         uri=True)) as conn:
                conn.execute('PRAGMA foreign_keys=ON').close()
                self.assertEqual(conn.execute('SELECT id, name FROM projects').fetchall(),
                                 [(fix.project_id, f.PROJECT_NAME)])
                self.assertEqual(conn.execute('SELECT count(*) FROM relays').fetchone(), (relay_count,))
                self.assertEqual(conn.execute(
                    'SELECT count(*) FROM recipients r LEFT JOIN seats s ON s.id=r.seat_id'
                    ' LEFT JOIN team_instances t ON t.id=s.team_instance_id'
                    " WHERE coalesce(r.special, '') != 'operator' AND"
                    ' (t.project_id IS NULL OR t.project_id != ?)', (fix.project_id,)
                ).fetchone(), (0,))
                key = api_client.read_key_file(fix.reader_key)
                self.assertEqual(conn.execute(
                    'SELECT session.seat_id, occupant.key_id FROM seat_sessions session'
                    ' JOIN seat_occupants occupant ON occupant.id=session.occupant_id'
                    ' WHERE session.id=? AND session.ended_at IS NULL',
                    (fix.reader_session_id,),
                ).fetchone(), (reader, key.key_id))
                relay_id = fix.relay_ids[0]
                result = queries.read(conn, relay_id, reader, 1_700_000_010_000)
                self.assertIsNotNone(result)
                body_bytes, header_bytes = conn.execute(
                    'SELECT body_bytes, header_bytes FROM relays WHERE id=?', (relay_id,)
                ).fetchone()
                self.assertEqual(len(result.body), body_bytes)
                self.assertGreater(header_bytes, 0)
                yield conn, fix, result, header_bytes
        self.assertFalse(base.exists())

    def _imported(self, builder, size):
        with self.copied(builder, relay_count=1, role='planner') as (conn, fix, result, header_bytes):
            self.assertEqual(len(fix.relay_ids), 1)
            self.assertEqual(conn.execute(
                'SELECT count(*), count(DISTINCT name), min(length(name)), max(length(name))'
                ' FROM team_instances WHERE project_id=? AND level=?', (fix.project_id, 'pair')
            ).fetchone(), (2_048, 2_048, 64, 64))
            self.assertEqual(conn.execute(
                'SELECT role, count(*) FROM seats GROUP BY role ORDER BY role'
            ).fetchall(), [('implementer', 2_048), ('planner', 2_048)])
            self.assertEqual(result.envelope['to'], [f'planner-{i}' for i in range(2_048)])
            self.assertEqual(result.envelope['cc'], [f'implementer-{i}' for i in range(2_048)])
            self.assertEqual(result.envelope['sender'], 'operator')
            self.assertTrue(result.envelope['imported'])
            self.assertEqual(result.envelope['subject'], '\u0001' * 16_384)
            self.assertEqual(len(result.body), size)
            historical = ('KIND: SITREP\nFROM: operator\nTO: ' + ', '.join(result.envelope['to'])
                          + '\nCC: ' + ', '.join(result.envelope['cc'])
                          + '\nSUBJECT: ' + result.envelope['subject'] + '\n\n')
            self.assertEqual(header_bytes, len(historical.encode('utf-8')))

    def test_import_bound_store(self):
        self._imported(f.import_bound_store, 8_388_608)

    def test_import_over_body_store(self):
        self._imported(f.import_over_body_store, 8_388_609)

    def test_live_request_store(self):
        with self.copied(f.live_request_store, relay_count=256, role='implementer') as (
                conn, fix, result, header_bytes):
            self.assertEqual(header_bytes, 8_192)
            self.assertEqual(len(result.body), 65_536)
            self.assertFalse(result.envelope['imported'])
            self.assertEqual((result.envelope['kind'], result.envelope['needs']), ('REVIEW', 'review'))
            self.assertEqual(result.envelope['to'], ['implementer-0'])
            self.assertEqual(result.envelope['cc'], ['operator'])
            self.assertTrue(result.refs)
            self.assertEqual(result.findings, ())
            self.assertEqual(len(result.unopened), 255)
            self.assertEqual({handle for _, handle in result.unopened}, set(range(1, 256)))
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM recipients WHERE seat_id=? AND role=? AND opened_at IS NULL',
                ('implementer-0', 'to')
            ).fetchone(), (255,))

    def test_live_reply_store(self):
        with self.copied(f.live_reply_store, relay_count=2, role='planner') as (
                _, _, result, header_bytes):
            self.assertEqual(header_bytes, 8_192)
            self.assertEqual(len(result.body), 65_536)
            self.assertEqual((result.envelope['kind'], result.envelope['needs'],
                              result.envelope['verdict']), ('REVIEW', 'none', 'must-revise'))
            self.assertFalse(result.envelope['imported'])
            self.assertTrue(result.refs)
            self.assertEqual(len(result.findings), 200)
            self.assertTrue(all(item.severity == 'advisory' for item in result.findings))
            self.assertEqual(result.unopened, [])

    def test_frame_page_store(self):
        with self.copied(f.frame_page_store, relay_count=500, role='planner') as (
                conn, fix, result, header_bytes):
            self.assertEqual(len(set(fix.relay_ids)), 500)
            self.assertEqual(result.body, b'')
            self.assertEqual(result.envelope['subject'], '\u0800' * 16_384)
            self.assertTrue(result.envelope['imported'])
            self.assertEqual(result.envelope['to'], ['planner-0'])
            historical = ('KIND: SITREP\nFROM: operator\nTO: planner-0\nSUBJECT: '
                          + result.envelope['subject'] + '\n\n').encode('utf-8')
            self.assertEqual(header_bytes, len(historical))
            self.assertEqual(conn.execute(
                'SELECT count(*), sum(length(CAST(subject AS BLOB))), max(body_bytes),'
                ' min(header_bytes), max(header_bytes) FROM relays '
                'WHERE subject=? AND sender_special=? AND submission_key IS NULL',
                ('\u0800' * 16_384, 'operator'),
            ).fetchone(), (500, 24_576_000, 0, header_bytes, header_bytes))
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM recipients WHERE seat_id=? AND role=?',
                ('planner-0', 'to'),
            ).fetchone(), (500,))
