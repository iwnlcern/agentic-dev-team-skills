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
