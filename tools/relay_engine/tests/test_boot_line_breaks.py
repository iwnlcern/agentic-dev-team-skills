"""Registration inputs cannot create split-line boot headers."""
from pathlib import Path
import unittest
from unittest import mock

from relay_engine import commission, envelope, errors, jcs, ledger, seats
from relay_engine.tests import test_ledger as fixtures


def record(path='parent/dispatch.md'):
    return jcs.frame_record(dict(v=1, parent_root_uuid='12345678-1234-1234-1234-123456789012',
                                commissioning_path=path, dispatch_content_digest='a' * 64,
                                child_run_id='r'))


class TestBootLineBreaks(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.LedgerFixture()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.initialize()
        ledger.establish_run_identity(self.f.ledger, 'r')

    def test_dispatch_ref_with_any_line_break_is_refused_without_a_key(self):
        for char in sorted(envelope.LINE_BREAKS):
            with self.subTest(char=repr(char)):
                before = self.f.ledger.execute('SELECT * FROM seat_events').fetchall()
                with self.assertRaises(errors.EngineError) as caught:
                    seats.register(self.f.ledger, self.f.root, 'p.planner', 'Planner', 'before' + char + 'after')
                self.assertEqual(caught.exception.code, 'E-ENVELOPE')
                self.assertEqual(list(Path(self.f.root.path, '.engine/seats/p.planner').glob('*.key')), [])
                self.assertEqual(self.f.ledger.execute('SELECT * FROM seat_events').fetchall(), before)

    def test_clean_dispatch_ref_registers(self):
        result = seats.register(self.f.ledger, self.f.root, 'p.planner', 'Planner', 'clean-boot')
        self.assertTrue(Path(self.f.root.path, result['key_path']).is_file())
        self.assertTrue(Path(self.f.root.path, result['boot_relay']).is_file())

    def test_commissioning_path_line_breaks_refused_at_both_entries(self):
        for path in ('a\u2028b/x.md', 'a\x85b/x.md', 'a\u2029b/x.md', 'a\nb/x.md', 'a/../x.md'):
            for entry in ('adopt', 'startup'):
                with self.subTest(path=path, entry=entry):
                    before = self.f.ledger.execute('SELECT * FROM runs').fetchall()
                    with mock.patch.object(commission, 'establish_run_identity', wraps=ledger.establish_run_identity) as establish:
                        with self.assertRaisesRegex(ValueError, '^commissioning path mismatch$'):
                            if entry == 'adopt':
                                commission.adopt(self.f.ledger, record(path))
                            else:
                                commission.startup_run_identity(dict(ledger=self.f.ledger, run_id='r',
                                                                    commissioning_record=record(path)))
                        if path != 'a/../x.md':
                            establish.assert_not_called()
                    self.assertEqual(self.f.ledger.execute('SELECT * FROM runs').fetchall(), before)

    def test_boot_backstop_names_the_field_and_removes_the_key(self):
        self.f.ledger.execute('INSERT INTO runs(run_id,commissioned_by) VALUES(?,?)', ('r', 'old\u2028record'))
        before = self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
        with self.assertRaises(errors.EngineError) as caught:
            seats.register(self.f.ledger, self.f.root, 'p.planner', 'Planner', 'boot')
        self.assertEqual(caught.exception.code, 'E-DRAFT-SHAPE')
        self.assertEqual(caught.exception.cause_key, 'error-draft-shape-boot-field-cause')
        self.assertIn('COMMISSIONED_BY', caught.exception.cause)
        self.assertEqual(list(Path(self.f.root.path, '.engine/seats/p.planner').glob('*.key')), [])
        self.assertEqual(self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
        self.assertEqual(self.f.ledger.execute('SELECT * FROM seat_events').fetchall(), [])

    def test_valid_boot_inputs_build_no_shape(self):
        for number, commissioned in enumerate((None, 'valid')):
            if commissioned:
                commission.adopt(self.f.ledger, record())
            raw = seats._boot_bytes('p.planner', 'Planner', 'r', 'boot', 'occupant',
                                    self.f.ledger.execute('SELECT commissioned_by FROM runs').fetchone()[0]
                                    if commissioned else None)
            self.assertIsNone(envelope.draft_shape(raw.decode()))

    def test_boot_backstop_names_any_line_the_record_splices(self):
        for record_value, field in (('a\n\nSUPERSEDES: x', 'SUPERSEDES'),
                                    ('a\nFOO: b\u2028c', 'FOO'),
                                    ('a\nX1: b\u2028c', 'X1')):
            with self.subTest(field=field):
                self.f.ledger.execute('DELETE FROM runs')
                self.f.ledger.execute('INSERT INTO runs(run_id,commissioned_by) VALUES(?,?)',
                                      ('r', record_value))
                before = self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                with self.assertRaises(errors.EngineError) as caught:
                    seats.register(self.f.ledger, self.f.root, 'p.planner', 'Planner', 'boot')
                self.assertEqual((caught.exception.code, caught.exception.cause_key),
                                 ('E-DRAFT-SHAPE', 'error-draft-shape-boot-field-cause'))
                self.assertIn(field, caught.exception.cause)
                self.assertEqual(list(Path(self.f.root.path, '.engine/seats/p.planner').glob('*.key')), [])
                self.assertEqual(self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
                self.assertEqual(self.f.ledger.execute('SELECT * FROM seat_events').fetchall(), [])

    def test_boot_backstop_refuses_any_stored_line_break(self):
        for value in ('a\nSUPERSEDES: x', 'a\rCYCLE_REOPEN: x', 'a\n\nX1: y'):
            with self.subTest(value=value):
                # Each case owns its ledger: an erroneously admitted boot must
                # not turn the next case into a seat-occupied refusal.
                f = fixtures.LedgerFixture()
                f.setUp()
                try:
                    f.initialize()
                    ledger.establish_run_identity(f.ledger, 'r')
                    f.ledger.execute('INSERT INTO runs(run_id,commissioned_by) VALUES(?,?)', ('r', value))
                    before = f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                    events = f.ledger.execute('SELECT * FROM seat_events').fetchall()
                    with self.assertRaises(errors.EngineError) as caught:
                        seats.register(f.ledger, f.root, 'p.planner', 'Planner', 'boot')
                    self.assertEqual((caught.exception.code, caught.exception.cause_key),
                                     ('E-DRAFT-SHAPE', 'error-draft-shape-boot-field-cause'))
                    self.assertIn('COMMISSIONED_BY', caught.exception.cause)
                    self.assertEqual(list(Path(f.root.path, '.engine/seats/p.planner').glob('*.key')), [])
                    self.assertEqual(f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
                    self.assertEqual(f.ledger.execute('SELECT * FROM seat_events').fetchall(), events)
                finally:
                    f.tearDown()

    def test_boot_backstop_names_a_reference_showing_a_key(self):
        # Scan refusal must happen before a boot relay or seat event is written.
        for ref, stored, field in (
                ('note SUPERSEDES: x', None, 'DISPATCH_ID'),
                ('SUPERSEDES：x', None, 'DISPATCH_ID'),
                ('a&colon;b CYCLE_REOPEN&#58; c', None, 'DISPATCH_ID'),
                ('v297-b2-impl-6', 'see SUPERSEDES: x', 'COMMISSIONED_BY')):
            with self.subTest(ref=ref, stored=stored):
                f = fixtures.LedgerFixture()
                f.setUp()
                try:
                    f.initialize()
                    ledger.establish_run_identity(f.ledger, 'r')
                    if stored:
                        f.ledger.execute('INSERT INTO runs(run_id,commissioned_by) VALUES(?,?)', ('r', stored))
                    before = f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                    events = f.ledger.execute('SELECT * FROM seat_events').fetchall()
                    with self.assertRaises(errors.EngineError) as caught:
                        seats.register(f.ledger, f.root, 'p.planner', 'Planner', ref)
                    self.assertEqual(caught.exception.code, 'E-DRAFT-SHAPE')
                    self.assertEqual(caught.exception.cause_key, 'error-draft-shape-boot-field-cause')
                    self.assertIn(field, caught.exception.cause)
                    self.assertEqual(caught.exception.remedy, "the line is built from the registration's inputs and the root's stored commissioning record; re-register with a dispatch reference on one line that shows no cycle or supersession key with a colon, or ask the operator to re-commission the root with a commissioning path on one line that shows none")
                    self.assertEqual(list(Path(f.root.path, '.engine/seats/p.planner').glob('*.key')), [])
                    self.assertEqual(f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
                    self.assertEqual(f.ledger.execute('SELECT * FROM seat_events').fetchall(), events)
                finally:
                    f.tearDown()
        result = seats.register(self.f.ledger, self.f.root, 'p.planner', 'Planner', 'v297-b2-impl-6')
        self.assertTrue(Path(self.f.root.path, result['boot_relay']).is_file())
