"""E26 exact verdicts and E27 ledger-selected amendment control scan."""
from contextlib import contextmanager

from relay_engine.tests import test_authority as base_fixture
from relay_engine.tests import test_authority_fold7 as header_fixture
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5


class TestAuthorityFold9(TestAuthorityFold5):
    grant_case = header_fixture.TestAuthorityFold7.grant_case
    review_case = header_fixture.TestAuthorityFold7.review_case
    base = base_fixture.TestAuthority.base

    @contextmanager
    def isolated(self):
        fixture = TestAuthorityFold9()
        fixture.setUp()
        try:
            yield fixture
        finally:
            fixture.tearDown()

    def amendment_pair(self, kind='PLAN', carrier_content='', review_content='',
                       verdict='approve', reviewed=True):
        self.base(design=True)
        stem = 'PL-base-erratum-1' if kind == 'PLAN' else 'DD-base-erratum-1'
        pin = self.artifact('plans' if kind == 'PLAN' else 'designs', stem)
        lock = 'PLAN_LOCK_ID: PL' if kind == 'PLAN' else 'DESIGN_DOC_ID: DD'
        carrier = self.add(kind, ident='amendment', extra=lock+'\n'+pin,
                           content=carrier_content)
        review = None
        if reviewed:
            review = self.add(kind+'-REVIEW', 'p.implementer', 'p.planner', 'amendment-review',
                              extra='PARENT_DISPATCH_ID: amendment\n'+pin+'\nVERDICT: '+verdict,
                              content=review_content)
        return carrier, review, pin, stem

    def test_exact_merge_values_and_reason_sets(self):
        values = ['approved? no', 'approved only after B6 merges', 'approved, but hold',
                  'approved - not yet', 'approved (withdrawn)', 'granted conditionally',
                  'approved.', 'APPROVED.']
        for value in values:
            with self.subTest(value=value):
                self.grant_case('MERGE_AUTHORIZATION: '+value, content='', action='merge',
                                reasons=['authority.denied', 'authority.shape-absent'])
        self.grant_case('MERGE_AUTHORIZATION: approved? no', action='merge', reasons=['authority.denied'])
        self.grant_case('VERDICT: merge ready', content='', action='merge',
                        reasons=['authority.denied', 'authority.shape-absent'])
        self.grant_case('MERGE_AUTHORIZATION: denied\nVERDICT: approved', content='', action='merge',
                        reasons=['authority.denied', 'authority.verdict-conflict'])
        for value in ['Granted\t', '\tapproved  ', 'approved', 'granted']:
            self.grant_case('MERGE_AUTHORIZATION: '+value, content='', action='merge')

    def test_exact_grant_and_review_words(self):
        for value in ['APPROVED.', 'merge ready', 'approved\tcomment', 'approved comment', 'ａpproved']:
            self.grant_case('VERDICT: '+value, reasons=['authority.denied'])
        for value in ['approved.', 'approve with minors', 'approved\tcomment', 'approved comment']:
            self.review_case(value, expected=['authority.lineage-incomplete'])
        for word in ['approve', 'approved', 'granted']:
            for padding in ['', ' ', '\t']:
                self.grant_case('VERDICT: '+padding+word+padding)
                if word != 'granted':
                    self.review_case(padding+word.capitalize()+padding)
        self.review_case('granted', expected=['authority.lineage-incomplete'])

    def test_carrier_control_keeps_artifact_discrimination(self):
        for kind in ['PLAN', 'DESIGN']:
            for content in ['', 'prose\f tail']:
                with self.subTest(kind=kind, content=content), self.isolated() as f:
                    _, _, _, stem = f.amendment_pair(kind, carrier_content=content)
                    expected = ['authority.lineage-incomplete'] if content else []
                    self.assertEqual(f.check()['reasons'], expected)
                    f.files[stem].write_bytes(b'changed')
                    self.assertEqual(f.check()['reasons'], ['authority.artifact-changed']+expected)

    def test_matching_and_obsolete_control_reviews_refuse(self):
        for kind in ['PLAN', 'DESIGN']:
            for later_clean in [False, True]:
                with self.subTest(kind=kind, later_clean=later_clean), self.isolated() as f:
                    _, _, pin, _ = f.amendment_pair(kind, review_content='prose\f tail')
                    if later_clean:
                        f.add(kind+'-REVIEW', 'p.implementer', 'p.planner', 'later-clean',
                              extra='PARENT_DISPATCH_ID: amendment\n'+pin+'\nVERDICT: approve', content='')
                    self.assertEqual(f.check()['reasons'], ['authority.lineage-incomplete'])

    def test_scan_does_not_depend_on_final_review_eligibility(self):
        for state in ['negative', 'superseded', 'engine-authored', 'header-conflicting']:
            with self.subTest(state=state), self.isolated() as f:
                _, review, _, _ = f.amendment_pair(
                    review_content='prose\f tail'+('\nFROM: other.implementer' if state=='header-conflicting' else ''),
                    verdict='must-revise' if state=='negative' else 'approve')
                if state == 'superseded':
                    f.ledger.execute("INSERT INTO supersession_edges VALUES(1,?,?,'local',1)", (review.seq,f.grant.seq))
                if state == 'engine-authored':
                    f.ledger.execute('INSERT INTO routed_notices VALUES(?,?)', (f.grant.seq,review.seq))
                self.assertEqual(f.check()['reasons'], ['authority.lineage-incomplete'])
        with self.isolated() as f:
            f.amendment_pair(carrier_content='prose\f tail', reviewed=False)
            self.assertEqual(f.check()['reasons'], ['authority.lineage-incomplete'])

    def test_scan_filters_only_ledger_phase_sender_and_sequence(self):
        for phase, sender, extra, expected in [
                ('PLAN', 'p.planner', 'PLAN_LOCK_ID: unrelated', ['authority.lineage-incomplete']),
                ('PLAN-REVIEW', 'p.implementer', 'PARENT_DISPATCH_ID: unrelated', ['authority.lineage-incomplete']),
                ('PLAN', 'other.planner', '', []),
                ('PLAN-REVIEW', 'other.implementer', '', []),
                ('SITREP', 'p.planner', '', [])]:
            with self.subTest(phase=phase,sender=sender), self.isolated() as f:
                f.base(design=True)
                f.add(phase,sender,extra=extra,content='prose\f tail')
                self.assertEqual(f.check()['reasons'], expected)
        with self.isolated() as f:
            f.add('PLAN', 'p.planner', content='earlier\f row')
            f.add('PLAN-REVIEW', 'p.implementer', content='earlier\f row')
            f.base(design=True)
            self.assertEqual(f.check()['reasons'], [])

    def test_scan_starts_at_origin_even_without_a_lock(self):
        for phase, sender in [('PLAN', 'p.planner'), ('PLAN-REVIEW', 'p.implementer')]:
            with self.subTest(phase=phase), self.isolated() as f:
                pin=f.artifact('plans','P')
                f.add('PLAN',ident='origin',extra=pin,content='')
                f.add('PLAN-REVIEW','p.implementer','p.planner','clean-review',
                      extra='PARENT_DISPATCH_ID: origin\n'+pin+'\nVERDICT: approve',content='')
                f.add(phase,sender,content='prose\f tail')
                grant=f.add(extra='PARENT_DISPATCH_ID: clean-review')
                self.assertEqual(f.check(grant)['reasons'], ['authority.lineage-incomplete'])

    def test_scan_uses_persisted_envelope_even_when_body_disagrees(self):
        for old, new in [(b'PHASE: PLAN\n', b'PHASE: SITREP\n'),
                         (b'FROM: p.planner\n', b'FROM: other.planner\n')]:
            with self.subTest(field=old), self.isolated() as f:
                f.base(design=True)
                row=f.add('PLAN','p.planner',content='prose\f tail')
                raw=bytes(f.ledger.execute('SELECT body FROM relays WHERE seq=?',(row.seq,)).fetchone()[0])
                self.assertIn(old,raw)
                # Imported/stale text cannot replace the persisted admission envelope.
                f.ledger.execute('UPDATE relays SET body=? WHERE seq=?',(raw.replace(old,new,1),row.seq))
                self.assertEqual(f.check()['reasons'], ['authority.lineage-incomplete'])

    def test_direct_issuer_is_outside_amendment_scan(self):
        for content in ['', 'prose\f tail']:
            with self.subTest(content=content), self.isolated() as f:
                _, _, _, stem=f.amendment_pair(carrier_content=content)
                grant=f.add('MERGE-GATE','operator',extra='PARENT_DISPATCH_ID: plan',content='DISPATCH MERGE')
                self.assertEqual(f.check(grant,action='merge')['reasons'], [])
                f.files[stem].write_bytes(b'changed')
                self.assertEqual(f.check(grant,action='merge')['reasons'], ['authority.artifact-changed'])
