"""Authority entry uses real admission and leaves the caller's transaction intact."""
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
from unittest import mock

from relay_engine import authority, slips
from relay_engine.tests import test_authority as legacy_tests
from relay_engine.tests.test_slips import SlipStore
from relay_engine.tests.test_claims import SECOND
from relay_engine.tests.test_submit import IMPL


class TestAuthorityV3(SlipStore):
    def entry(self, seat=IMPL, branch='work'):
        return authority.act_entry(self.conn, seat.seat_id,
                                   slips.Effect('commit', 'repo-1', branch), self.clock)

    def test_kit_task9_answers(self):
        _, _, issued = self.build()
        sid = self.slip(issued)[0]
        self.conn.execute('BEGIN IMMEDIATE')
        allowed = self.entry()
        self.assertEqual((allowed.outcome, allowed.code, allowed.slip_id, allowed.revision),
                         ('allow', None, sid, 1))
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        missing = self.entry(SECOND, branch='missing')
        self.assertEqual((missing.outcome, missing.code), ('deny', 'authority.slip-missing'))
        self.assertTrue(missing.reason)
        self.assertTrue(missing.unblocker)
        self.conn.commit()
        slips.revoke(self.conn, sid, 'operator', 'stop', self.clock)
        self.conn.execute('BEGIN IMMEDIATE')
        revoked = self.entry()
        self.assertEqual((revoked.outcome, revoked.code, revoked.slip_id),
                         ('deny', 'authority.slip-invalid', sid))
        self.assertIn('revoked', revoked.reason)
        self.conn.commit()

    def test_working_copy_digest(self):
        digest = authority.working_copy_digest
        self.assertEqual(digest(str(self.plan)), hashlib.sha256(self.plan.read_bytes()).hexdigest())
        self.assertIsNone(digest(str(self.plan.with_name('missing'))))
        with mock.patch.object(os, 'open', side_effect=PermissionError('unreadable working copy')):
            self.assertIsNone(digest(str(self.plan)))
        self.plan.write_bytes(b'changed working copy\n')
        self.assertEqual(digest(str(self.plan)), hashlib.sha256(b'changed working copy\n').hexdigest())
        # F6: only a regular file is a working copy; nothing else is followed or read.
        folder = self.plan.with_name('folder'); folder.mkdir()
        self.assertIsNone(digest(str(folder)))
        link = self.plan.with_name('link.md'); link.symlink_to(self.plan)
        self.assertIsNone(digest(str(link)))
        pipe = self.plan.with_name('pipe'); os.mkfifo(pipe)
        results = []
        reader = threading.Thread(target=lambda: results.append(digest(str(pipe))), daemon=True)
        reader.start(); reader.join(5)
        blocked = reader.is_alive()
        if blocked:
            try:  # a writer's open releases the blocked reader, so the thread ends
                os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            reader.join(5)
        self.assertFalse(blocked, 'working_copy_digest blocked on a FIFO')
        self.assertEqual(results, [None])
        # Erratum 4 r2 R3: the Git algorithms, against fixed `git hash-object --no-filters`
        # values (SHA-1 from a default repository, SHA-256 from one made with
        # --object-format=sha256); no Git runs here.
        self.plan.write_bytes(b'plan X\n')
        empty = self.plan.with_name('empty'); empty.write_bytes(b'')
        expected = {(str(self.plan), 'git-sha1'): 'af9994ac749e94382d55b9f7807071468f65dbc4',
                    (str(self.plan), 'git-sha256'): '780764d7815dbb5562d62e834f0830644482c141fefbea62b208ef3fe28f60a1',
                    (str(empty), 'git-sha1'): 'e69de29bb2d1d6434b8b29ae775ad8c2e48c5391',
                    (str(empty), 'git-sha256'): '473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813',
                    (str(self.plan), 'sha256'): hashlib.sha256(b'plan X\n').hexdigest()}
        try:
            found = {key: digest(*key) for key in expected}
        except TypeError as error:
            self.fail('working_copy_digest takes no algorithm: %s' % error)
        self.assertEqual(found, expected)
        self.assertIsNone(digest(str(link), 'git-sha1'))
        for algorithm in ('md5', 'git-sha512', '', None):
            with self.subTest(algorithm=algorithm), self.assertRaises(ValueError):
                digest(str(self.plan), algorithm)

    def test_act_entry_callee_ends_transaction(self):
        # F4: a callee that ends the caller's transaction leaves no savepoint; the entry answers held.
        self.build()
        for mode in ('allow', 'raise'):
            with self.subTest(mode=mode):
                def ending(conn, *args, **kwargs):
                    conn.commit()
                    if mode == 'raise':
                        raise RuntimeError('fault after the transaction ended')
                    return slips.Answer('allow', None, None, None, None, None)
                self.conn.execute('BEGIN IMMEDIATE')
                with mock.patch.object(slips, 'act', side_effect=ending):
                    try:
                        answer = self.entry(branch=mode)
                    except sqlite3.Error as error:
                        self.fail('act_entry raised %r' % error)
                self.assertEqual((answer.outcome, answer.code), ('held', 'authority.check-unavailable'))
                self.assertTrue(answer.reason)
                self.assertTrue(answer.unblocker)
                self.assertFalse(self.conn.in_transaction)

    def test_act_entry_failed_cleanup_escapes(self):
        # Erratum 4 r3 R5: a cleanup that fails in an open transaction escapes act_entry, never a
        # held answer, and carries nothing of the first fault.  A callee that ends the caller's
        # transaction and begins another leaves no savepoint to roll back to.
        self.build()
        for mode in ('raise', 'allow'):
            with self.subTest(mode=mode):
                def reopening(conn, *args, **kwargs):
                    conn.commit()
                    conn.execute('BEGIN IMMEDIATE')
                    if mode == 'raise':
                        raise RuntimeError('callee fault text')
                    return slips.Answer('allow', None, None, None, None, None)
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    with mock.patch.object(slips, 'act', side_effect=reopening):
                        with self.assertRaises(sqlite3.OperationalError) as caught:
                            self.entry(branch=mode)
                    self.assertIn('authority_act', str(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)
                    self.assertTrue(self.conn.in_transaction)
                finally:
                    self.conn.rollback()
        # Root 133408 point 2: a callee that closes the connection makes the open-transaction
        # read raise, and that error escapes the same way; nothing uses the connection after.
        def closing(conn, *args, **kwargs):
            conn.close()
            raise RuntimeError('callee fault text')
        self.conn.execute('BEGIN IMMEDIATE')
        with mock.patch.object(slips, 'act', side_effect=closing):
            with self.assertRaises(sqlite3.ProgrammingError) as caught:
                self.entry(branch='closed')
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_act_entry_requires_open_transaction(self):
        before = self.conn.total_changes
        with mock.patch.object(slips, 'act') as act:
            with self.assertRaises(ValueError):
                self.entry()
            act.assert_not_called()
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
        closed = sqlite3.connect(':memory:')
        closed.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            authority.act_entry(closed, IMPL.seat_id, slips.Effect('commit', 'repo-1', 'work'), self.clock)

    def test_act_entry_leaves_commit_to_caller(self):
        self.build()
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 1)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 1)

    def test_act_entry_fault_rolls_back_own_writes(self):
        self.build()
        real = slips.act
        for mode in ('raise', 'held'):
            with self.subTest(mode=mode):
                self.conn.execute('BEGIN IMMEDIATE')
                try:
                    marker = 'marker-' + mode
                    self.conn.execute('INSERT INTO repositories (id, project_id, name) VALUES (?, ?, ?)',
                                      (marker, 'p', marker))
                    witnessed = []
                    def after_real(*args, **kwargs):
                        answer = real(*args, **kwargs)
                        count = self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0]
                        witnessed.append((answer.outcome, count, kwargs['digest'] is authority.working_copy_digest))
                        if mode == 'raise':
                            raise RuntimeError('fault after real binding')
                        return slips.Answer('held', 'authority.check-unavailable', 'injected fault', 'operator', None, None)
                    with mock.patch.object(slips, 'act', side_effect=after_real):
                        answer = self.entry(branch=mode)
                    self.assertEqual(witnessed, [('allow', 1, True)])
                    self.assertEqual((answer.outcome, answer.code), ('held', 'authority.check-unavailable'))
                    self.assertTrue(answer.reason)
                    self.assertTrue(answer.unblocker)
                    self.assertTrue(self.conn.in_transaction)
                    self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
                    self.conn.commit()
                    self.assertEqual(self.conn.execute('SELECT count(*) FROM branch_bindings').fetchone()[0], 0)
                    self.assertEqual(self.conn.execute('SELECT id FROM repositories WHERE id = ?', (marker,)).fetchone(),
                                     (marker,))
                finally:
                    self.conn.rollback()

    def test_act_entry_reads_no_bodies(self):
        self.build()
        real = slips.act
        def guarded(*args, **kwargs):
            with self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute('SELECT body FROM bodies').fetchall()
            return real(*args, **kwargs)
        self.conn.execute('BEGIN IMMEDIATE')
        with mock.patch.object(slips, 'act', side_effect=guarded):
            self.assertEqual(self.entry().outcome, 'allow')
        self.assertTrue(self.conn.in_transaction)
        self.conn.commit()
        # The caller's access is restored when the entry's read guard exits.
        self.assertTrue(self.conn.execute('SELECT body FROM bodies LIMIT 1').fetchone())

    def test_legacy_reader_unchanged(self):
        fixture = legacy_tests.TestAuthority('test_valid_grant_unchanged_artifact_allows')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.base()
        self.assertTrue(fixture.check()['allowed'])
        root = Path(fixture.root.path)
        self.assertEqual(authority.read_artifact(root, 'plans/PL-base.md'), b'original')
        fixture.files['PL-base'].write_bytes(b'edited')
        self.assertIn('authority.artifact-changed', fixture.check()['reasons'])
        self.assertEqual(authority.read_artifact(root, 'plans/PL-base.md'), b'edited')
        fixture.files['PL-base'].unlink()
        with self.assertRaises(FileNotFoundError):
            authority.read_artifact(root, 'plans/PL-base.md')
