"""Shape refusals precede admission on every supported writer path."""
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from relay_engine import authority, client, daemon, envelope, errors, jcs, ledger, migrate, reconcile, supersede
from relay_engine.tests import test_admission_judging as admission
from relay_engine.tests import test_ledger as fixtures
from relay_engine.tests import test_migrate as migration
from relay_engine.tests import test_supersede as adoption
from relay_engine.tests.test_draft_shape import CHARS


class TestDraftShapeAdmission(unittest.TestCase):
    def setUp(self):
        self.f = admission.TestAdmissionJudging()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def shaped(self):
        for ch in CHARS:
            yield admission.body('SITREP', content='before' + ch + 'after')
        for key in ('SUPERSEDES', 'CYCLE_TERMINUS', 'CYCLE_REOPEN'):
            yield admission.body('SITREP', content=key + ': withdrawn')
        yield admission.body('MERGE-GATE', 'operator', content='DISPATCH MERGE\n\nVERDICT: merge-blocked')

    def assert_refused(self, raw, call):
        shape = envelope.draft_shape(raw.decode())
        self.assertIsNotNone(shape)
        expected = envelope.shape_error(shape)
        before = self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
        with self.assertRaises(errors.EngineError) as caught:
            call()
        self.assertEqual((caught.exception.code, caught.exception.cause_key,
                          caught.exception.cause, caught.exception.remedy),
                         ('E-DRAFT-SHAPE', expected.cause_key, expected.cause, expected.remedy))
        self.assertEqual(self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)

    def test_client_refuses_before_any_request(self):
        path = Path(self.f.root.path, '.engine/drafts/p.planner/draft.md')
        path.parent.mkdir(parents=True)
        for raw in self.shaped():
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with mock.patch.object(client, 'request', wraps=client.request) as request:
                    self.assert_refused(raw, lambda: client.submit(
                        self.f.root.path, '.engine/drafts/p.planner/draft.md',
                        '.engine/seats/p.planner/occ.key'))
                    request.assert_not_called()

    def test_handler_refuses_a_client_that_skips_the_check(self):
        for raw in self.shaped():
            with self.subTest(raw=raw):
                args = self.f.args(raw)
                self.assert_refused(raw, lambda: daemon._submit_handler(self.f.ledger, self.f.root, args))

    def test_handler_admits_clean_crlf_and_bare_cr_drafts(self):
        for number, ending in enumerate((b'\n', b'\r\n', b'\r')):
            raw = admission.body('SITREP', ident=f'clean-{number}').replace(b'\n', ending)
            result = self.f.handler(raw, sid=f'clean-{number}')
            self.assertEqual(self.f.ledger.execute('SELECT body FROM relays WHERE rendered_path=?',
                                                  (result['path'],)).fetchone()[0], raw)

    def test_late_shapes_are_refused_at_admission(self):
        for raw in list(self.shaped())[len(CHARS):]:
            with self.subTest(raw=raw):
                self.assert_refused(raw, lambda: self.f.handler(raw))

    def test_fence_pair_at_admission(self):
        grant = self.f.handler(admission.body('MERGE-GATE', 'operator', ident='fence-cycle',
                                              content='DISPATCH MERGE'), sid='grant')
        self.assertTrue(authority.check(self.f.ledger, self.f.root, grant['path'],
                                        'p.implementer', 'merge')['allowed'])
        raw = admission.body('SITREP', 'operator', ident='fence-cycle', content='')
        raw = raw.replace(b'SUBJECT: test candidate\n',
                          b'SUBJECT: test candidate\n```\nCYCLE_TERMINUS: withdrawn\n')
        self.assert_refused(raw, lambda: self.f.handler(raw, sid='open', edge=grant['path']))
        closed = admission.body('SITREP', 'operator', ident='fence-cycle',
                                content='```\nCYCLE_TERMINUS: withdrawn\n```').replace(b'FINAL_GIT_STATUS_SHORT: clean\n\n', b'FINAL_GIT_STATUS_SHORT: clean\n')
        self.f.handler(closed, sid='closed', edge=grant['path'])
        self.assertTrue(authority.check(self.f.ledger, self.f.root, grant['path'],
                                        'p.implementer', 'merge')['allowed'])

    def test_same_line_shapes_are_refused_as_the_late_shape(self):
        for raw in (admission.body('SITREP', content='SUPERSEDES: x\u2028'),
                    admission.body('SITREP', content='SUPERSEDES: x\x85'),
                    admission.body('MERGE-GATE', 'operator', content='DISPATCH MERGE\n\nVERDICT: no\u2029')):
            with self.subTest(raw=raw):
                self.assertTrue(envelope.draft_shape(raw.decode()).kind.startswith('late-'))
                self.assert_refused(raw, lambda: self.f.handler(raw))

    def test_adopt_ruling_refuses_a_shaped_body(self):
        parent = adoption.TestAdoptedSupersession()
        parent.setUp()
        self.addCleanup(parent.tearDown)
        child = fixtures.LedgerFixture()
        child.setUp()
        self.addCleanup(child.tearDown)
        child.initialize()
        ledger.establish_run_identity(child.ledger, 'child', base64.b64decode(parent.record_result['record_b64']))
        target = adoption.relay('child', 'target', 'child.planner', 'child.implementer')
        ledger.admit(child.ledger, child.root, envelope.parse_draft(target), target.encode(), None,
                     origin='hand', stamp='20260808-010000', rendered_path='child/target.md')
        clean_bundle = base64.b64decode(parent.bundle_result['bundle_b64'])
        fields, _ = jcs.parse_record(clean_bundle)
        clean = base64.b64decode(fields['ruling_body_b64']).decode()
        for tail in [f'before{ch}after' for ch in CHARS] + ['SUPERSEDES: second.md']:
            raw = (clean + '\n' + tail + '\n').encode()
            changed = dict(fields, ruling_body_b64=base64.b64encode(raw).decode(),
                           ruling_content_digest=hashlib.sha256(raw).hexdigest())
            expected = envelope.shape_error(envelope.draft_shape(raw.decode()))
            with self.subTest(tail=tail):
                before = child.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                with self.assertRaises(errors.EngineError) as caught:
                    supersede.adopt_ruling(child.ledger, child.root, jcs.frame_record(changed))
                self.assertEqual((caught.exception.code, caught.exception.cause_key, caught.exception.cause),
                                 ('E-DRAFT-SHAPE', expected.cause_key, expected.cause))
                self.assertEqual(child.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
        self.assertTrue(supersede.adopt_ruling(child.ledger, child.root, clean_bundle)['applied'])

    def test_reconcile_lists_a_shaped_hand_file_as_malformed(self):
        lane = Path(self.f.root.path, 'hand'); lane.mkdir()
        good = lane / 'SITREP-pair-planner-20261026-010000.md'
        bad = lane / 'SITREP-pair-planner-20261026-010001.md'
        good.write_bytes(admission.body('SITREP', ident='good'))
        bad.write_bytes(admission.body('SITREP', ident='bad', content='before\x85after'))
        result = reconcile.reconcile(self.f.ledger, self.f.root)
        self.assertEqual(result['malformed'], ['hand/' + bad.name])
        self.assertEqual(result['ingested'], ['hand/' + good.name])

    def test_migrate_paths_list_a_shaped_file_as_malformed(self):
        f = migration.TestMigrate()
        f.setUp(); self.addCleanup(f.tearDown)
        bad = 'legacy/PLAN-planner-20260808-010001.md'
        Path(f.root.path, bad).write_text(migration.DRAFT.replace('migration-fixture', 'bad') + 'before\x85after')
        result = migrate.check(f.ledger, f.root)
        receipt = json.loads(f.root.open_read(result['receipt']))
        self.assertEqual(result['census']['malformed'], [bad])
        self.assertEqual(receipt['census']['malformed'], [bad])
        cutover = migrate.cutover(f.ledger, f.root, result['receipt'])
        self.assertEqual(cutover['imported'], [f.relay_path])

    def test_shape_is_checked_before_parse_draft(self):
        raw = admission.body('SITREP').replace(b'ROLE: Pair Planner',
                                              'ROLE: a\u2028b'.encode())
        with self.assertRaises(errors.EngineError) as parse_error:
            envelope.parse_draft(raw.decode())
        self.assertEqual(parse_error.exception.code, 'E-HEADER')
        path = Path(self.f.root.path, '.engine/drafts/p.planner/order.md')
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        with mock.patch.object(client, 'request', wraps=client.request) as request:
            self.assert_refused(raw, lambda: client.submit(
                self.f.root.path, '.engine/drafts/p.planner/order.md',
                '.engine/seats/p.planner/occ.key'))
            request.assert_not_called()
        # Build valid request metadata from a clean body, then replace just the
        # bytes and supplied envelope to exercise a check-skipping client.
        args = self.f.args(admission.body('SITREP'))
        args['body_b64'] = base64.b64encode(raw).decode()
        args['envelope'] = {}
        self.assert_refused(raw, lambda: daemon._submit_handler(self.f.ledger, self.f.root, args))
        parent = adoption.TestAdoptedSupersession()
        parent.setUp()
        self.addCleanup(parent.tearDown)
        child = fixtures.LedgerFixture()
        child.setUp()
        self.addCleanup(child.tearDown)
        child.initialize()
        ledger.establish_run_identity(child.ledger, 'child', base64.b64decode(parent.record_result['record_b64']))
        fields, _ = jcs.parse_record(base64.b64decode(parent.bundle_result['bundle_b64']))
        clean = base64.b64decode(fields['ruling_body_b64']).decode()
        lines = clean.splitlines(keepends=True)
        index = next(i for i,line in enumerate(lines) if line.startswith('ROLE:'))
        lines[index] = 'ROLE: a\u2028b\n'
        raw = ''.join(lines).encode()
        with self.assertRaises(errors.EngineError) as parse_error:
            envelope.parse_draft(raw.decode())
        self.assertEqual(parse_error.exception.code, 'E-HEADER')
        changed = dict(fields, ruling_body_b64=base64.b64encode(raw).decode(),
                       ruling_content_digest=hashlib.sha256(raw).hexdigest())
        before = child.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
        with self.assertRaises(errors.EngineError) as caught:
            supersede.adopt_ruling(child.ledger, child.root, jcs.frame_record(changed))
        self.assertEqual(caught.exception.code, 'E-DRAFT-SHAPE')
        self.assertIn('U+2028', caught.exception.cause)
        self.assertEqual(child.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)

    def test_shown_keys_are_refused_at_admission(self):
        path = Path(self.f.root.path, '.engine/drafts/p.planner/shown.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = (
            (admission.body('SITREP', content='**SUPERSEDES:** x'), 'late-control'),
            (admission.body('MERGE-GATE', 'operator', content='DISPATCH MERGE\n\n(PLAN_REVIEW_VERDICT: approve, at x)'), 'late-verdict'),
            (admission.body('SITREP').replace(b'SUBJECT: test candidate', b'SUBJECT: s see SUPERSEDES: x'), 'late-control-in-block'))
        for raw, variant in cases:
            with self.subTest(variant=variant):
                path.write_bytes(raw)
                before = self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                with mock.patch.object(client, 'request', wraps=client.request) as request:
                    with self.assertRaises(errors.EngineError) as caught:
                        client.submit(self.f.root.path, '.engine/drafts/p.planner/shown.md', '.engine/seats/p.planner/occ.key')
                    request.assert_not_called()
                self.assertEqual(caught.exception.code, 'E-DRAFT-SHAPE')
                self.assertEqual(caught.exception.cause_key, 'error-draft-shape-'+variant+'-cause')
                with self.assertRaises(errors.EngineError) as caught:
                    daemon._submit_handler(self.f.ledger, self.f.root, self.f.args(raw))
                self.assertEqual(caught.exception.code, 'E-DRAFT-SHAPE')
                self.assertEqual(caught.exception.cause_key, 'error-draft-shape-'+variant+'-cause')
                line = next(i for i, text in enumerate(raw.decode().splitlines(), 1) if ('SUPERSEDES' in text or '(PLAN_REVIEW_VERDICT' in text))
                self.assertIn('line '+str(line)+':', caught.exception.cause)
                self.assertEqual(self.f.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
        for i, content in enumerate(('`SUPERSEDES: x`', 'Supersedes: x')):
            raw = admission.body('SITREP', ident='shown-clean-'+str(i), content=content)
            result = self.f.handler(raw, sid='shown-clean-'+str(i))
            self.assertEqual(self.f.ledger.execute('SELECT body FROM relays WHERE rendered_path=?', (result['path'],)).fetchone()[0], raw)
