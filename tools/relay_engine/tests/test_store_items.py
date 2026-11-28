"""Operator items use the caller's transaction and exact occurrence identity."""
import sqlite3
import unittest

from relay_engine import catalogue, store_steps


class TestStoreItems(unittest.TestCase):
    def test_write_raise_and_end(self):
        try:
            from relay_engine import store_items
        except ImportError as error:
            self.fail(f'operator-item API missing: {error}')

        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA foreign_keys=ON').close()
        catalogue.init_catalogue(conn)
        conn.commit()

        project = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        key = dict(kind='report_awaiting_confirmation', project_id=project, subject='job-a')
        row = store_items.write_raise(conn, **key, fields={'z': 'last', 'a': 'first'}, at_ms=20)
        self.assertEqual(conn.execute(
            'SELECT id, ends, at, fields FROM store_operator_items'
        ).fetchall(), [(row, None, 20, '{"a":"first","z":"last"}')])
        end = store_items.write_end(conn, raise_id=row, fields={'ok': 'yes'}, at_ms=22)
        self.assertEqual(conn.execute(
            'SELECT kind, project_id, subject, ends, at, fields FROM store_operator_items WHERE id=?',
            (end,),
        ).fetchone(), (key['kind'], project, key['subject'], row, 22, '{"ok":"yes"}'))
        self.assertEqual(store_items.find_raise(conn, **key),
                         store_items.StoreItem(row, key['kind'], project, key['subject'],
                                               {'a': 'first', 'z': 'last'}, end))
        self.assertIsNone(store_items.find_raise(conn, **dict(key, project_id=None)))
        for invalid_id in (end, end + 100):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                store_items.write_end(conn, raise_id=invalid_id, fields={}, at_ms=23)
        conn.rollback()
        self.assertEqual(conn.execute('SELECT id FROM store_operator_items').fetchall(), [])

        hold_key = dict(kind='project_held_new_folder', project_id=project, subject='hold-a')
        first = store_items.write_raise(conn, **hold_key, fields={'path': 'first'}, at_ms=24)
        first_end = store_items.write_end(conn, raise_id=first, fields={}, at_ms=25)
        second = store_items.write_raise(conn, **hold_key, fields={'path': 'second'}, at_ms=26)
        read_first = store_items.read_item(conn, first)
        read_second = store_items.read_item(conn, second)
        self.assertEqual((read_first.id, read_first.end_id, read_first.fields),
                         (first, first_end, {'path': 'first'}))
        self.assertEqual((read_second.id, read_second.end_id, read_second.fields),
                         (second, None, {'path': 'second'}))
        self.assertIsNone(store_items.read_item(conn, second + 100))
        with self.assertRaises(ValueError):
            store_items.find_raise(conn, **hold_key)
        with self.assertRaises(ValueError):
            store_items.ensure_raise(conn, **hold_key, fields={}, at_ms=27)

        replay_key = dict(kind='restore_command_started', project_id=None, subject='job-b')
        saved = store_items.ensure_raise(conn, **replay_key, fields={'b': '2', 'a': '1'}, at_ms=28)
        self.assertEqual(store_items.ensure_raise(
            conn, **replay_key, fields={'a': '1', 'b': '2'}, at_ms=29), saved)
        self.assertEqual(conn.execute(
            'SELECT id, at FROM store_operator_items WHERE kind=?', (replay_key['kind'],)
        ).fetchall(), [(saved, 28)])
        with self.assertRaises(store_steps.FenceBreach) as caught:
            store_items.ensure_raise(conn, **replay_key, fields={'a': 'changed'}, at_ms=30)
        self.assertIn(str(saved), str(caught.exception))
        self.assertEqual(store_items.read_item(conn, saved).fields, {'a': '1', 'b': '2'})
