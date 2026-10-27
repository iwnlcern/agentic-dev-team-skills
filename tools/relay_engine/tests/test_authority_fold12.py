"""E30: foreign control veto covers selection-relevant relays."""
from unittest import mock
from relay_engine import authority, rules
from relay_engine.tests import test_authority_fold10 as foreign_fixture
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5

class TestAuthorityFold12(TestAuthorityFold5):
    git = foreign_fixture.TestAuthorityFold10.git
    foreign_case = foreign_fixture.TestAuthorityFold10.foreign_case
    assert_case = foreign_fixture.TestAuthorityFold10.assert_case

    def test_pdc_inline_nul_unrelated_allows(self):
        self.assert_case([], unrelated='SUBJECT: unrelated historical probe\n\nExample `hostile\0input` stays immutable.\n')

    def test_body_only_doc_id_refuses(self):
        # Historical name: R181 now allows this noncandidate carrier.
        self.assert_case([], unrelated='PHASE: SITREP\n\nThis discusses d1.\f')

    def test_two_phase_mentions_refuse(self):
        # Historical name: R181 now allows this noncandidate carrier.
        self.assert_case([], unrelated='PHASE: SITREP\n\nAn example PHASE: elsewhere.\f')

    def test_split_doc_id_refuses(self):
        # Historical name: R181 now allows this noncandidate carrier.
        self.assert_case([], unrelated='PHASE: SITREP\n\nDiscuss d\x011.')

    def test_ascii_case_doc_id_refuses(self):
        # Historical name: R181 now allows this noncandidate carrier.
        self.assert_case([], unrelated='PHASE: SITREP\n\nDiscuss D1.\f')

    def test_older_approving_review_refuses(self):
        older=foreign_fixture.REVIEW+'\nprose\f tail'
        self.assert_case(['authority.lineage-incomplete'], unrelated={'DESIGN-REVIEW-implementer-20260801-103000.md':older})

    def test_newer_contaminated_review_skipped_still_refuses(self):
        newer=foreign_fixture.REVIEW.replace('PHASE: DESIGN-REVIEW','PHASE: DESIGN-RE\x01VIEW')
        with self.foreign_case(unrelated={'DESIGN-REVIEW-implementer-20260801-120000.md':newer}) as (f,g,_,declaration):
            origin,review,population=rules.xroot_verify(f.root.path,declaration)
            self.assertEqual(review.name,'DESIGN-REVIEW-implementer-20260801-110000.md')
            self.assertEqual(f.check(g)['reasons'],['authority.lineage-incomplete'])

    def test_relevant_non_ff_controls_refuse(self):
        # Historical name: R181 now allows this noncandidate carrier.
        for c in ['\v','\0','\x7f','\x85','\u2028']:
            with self.subTest(control=repr(c)):
                self.assert_case([],unrelated='PHASE: SITREP\n\nDiscuss d1.'+c)

    def test_selected_candidates_are_in_scanned_set(self):
        older=foreign_fixture.REVIEW
        unrelated={'DESIGN-REVIEW-implementer-20260801-103000.md':older,
                   'SITREP-planner-20260801-120000.md':'PHASE: SITREP\n\nOther design.'}
        candidates=[];scanned=[]
        target=rules.xroot_target_ok;control=authority._control_refusal
        def matching(relay,key,expected):
            answer=target(relay,key,expected)
            if key=='DESIGN_DOC_ID' and answer:candidates.append(relay.text)
            return answer
        def checking(text):
            scanned.append(text)
            return control(text)
        with self.foreign_case(unrelated=unrelated) as (f,g,_,_):
            with mock.patch.object(rules,'xroot_target_ok',side_effect=matching), mock.patch.object(authority,'_control_refusal',side_effect=checking):
                self.assertEqual(f.check(g)['reasons'],[])
        self.assertGreaterEqual(len(candidates),3)
        self.assertTrue(all(text in scanned for text in candidates))

    def test_shared_fixture_candidates_are_selection_relevant(self):
        import tempfile
        from pathlib import Path
        import xrootfixgen
        matched=[]
        target=rules.xroot_target_ok
        def matching(relay,key,expected):
            answer=target(relay,key,expected)
            if key=='DESIGN_DOC_ID' and answer:
                matched.append(relay.name)
                self.assertTrue(authority._foreign_selection_relevant(relay.text,expected),relay.name)
            return answer
        with tempfile.TemporaryDirectory(prefix='e30-candidates-') as td:
            cases=xrootfixgen.materialize(Path(td))
            self.assertEqual(len(cases),130)
            with mock.patch.object(rules,'xroot_target_ok',side_effect=matching):
                for label,root,exit_code,errors in cases:
                    with self.subTest(case=label):
                        result=rules.lint_relay_root(root)
                        self.assertEqual(sorted(result.errors),sorted(errors))
        self.assertGreater(len(matched),0)
