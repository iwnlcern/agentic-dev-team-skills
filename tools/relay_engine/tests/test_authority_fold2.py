"""Fold-two regressions: each refusal has a valid control."""
import subprocess
import unittest
from unittest import mock
from relay_engine import authority, ledger
from relay_engine.tests import test_authority as h
from relay_engine.tests.test_ledger import LedgerFixture


class TestAuthorityFold2(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger, 'r')
        self.number = 0
        self.files = {}
    add = h.TestAuthority.add
    artifact = h.TestAuthority.artifact
    base = h.TestAuthority.base
    amendment = h.TestAuthority.amendment
    review_amend = h.TestAuthority.review_amend
    check = h.TestAuthority.check
    denied = h.TestAuthority.denied

    def test_F1_equal_backtick_runs_hide_tokens(self):
        for action,phase,token in [('implement','IMPL','DISPATCH IMPL'),('merge','MERGE-GATE','DISPATCH MERGE')]:
            for n in (1,2,3,4):
                for content in ('`'*n+token+'`'*n, '`'*n+'\n'+token+'\n'+'`'*n):
                    with self.subTest(action=action,content=content):
                        self.denied('shape-absent',self.add(phase,'operator',content=content),action=action)
            # Removing a code span must not synthesize a bare dispatch token.
            self.denied('shape-absent',self.add(phase,'operator',content='`` ` ``'+token),action=action)
            self.assertTrue(self.check(self.add(phase,'operator',content=token),action=action)['allowed'])

    def test_F2_direct_ancestors_bind_bytes_but_not_state(self):
        self.base()
        self.ledger.execute("INSERT INTO cycle_events(seq,dispatch_id,event,cause_seq) VALUES(1,'plan','close',?)",(self.plan.seq,))
        self.ledger.execute("INSERT INTO supersession_edges(seq,target_seq,ruling_seq,source,applied) VALUES(1,?,?, 'local',1)",(self.plan.seq,self.grant.seq))
        direct=self.add(sender='operator',extra='PARENT_DISPATCH_ID: review')
        self.assertTrue(self.check(direct)['allowed'])
        self.files['PL-base'].write_bytes(b'changed')
        self.assertEqual(self.check(direct)['reasons'],['authority.artifact-changed'])
        for parent in ('none','unavailable — no parent','missing-parent'):
            self.assertTrue(self.check(self.add(sender='operator',extra='PARENT_DISPATCH_ID: '+parent))['allowed'])

    def test_F3_eligible_erratum_redeclaration_allows(self):
        self.base();_,_,decl=self.amendment()
        grant=self.add(extra='PARENT_DISPATCH_ID: review\n'+decl)
        self.assertTrue(self.check(grant)['allowed'],self.check(grant))
        self.files['PL-base-erratum-1'].write_bytes(b'changed')
        self.denied('artifact-changed',grant)

    def test_F4_present_scope_requires_all_in(self):
        self.base()
        for value in ('Deviation-Present','deviation-present — note','partial','unknown',''):
            with self.subTest(value=value):
                self.denied('issuer-not-permitted',self.add(extra='PARENT_DISPATCH_ID: review\nSCOPE_DIFF_RESULT: '+value))
        for value in ('all-in','All-In. — note'):
            self.assertTrue(self.check(self.add(extra='PARENT_DISPATCH_ID: review\nSCOPE_DIFF_RESULT: '+value))['allowed'])

    def test_F5_locator_only_requires_lineage_digest(self):
        for value in ('PL-v295-b3-20260911','PL-v295-b3-20260911-erratum-1'):
            g=self.add('MERGE-GATE','operator',extra='PLAN_ARTIFACT: '+value,content='DISPATCH MERGE')
            self.assertEqual(self.check(g,action='merge')['reasons'],['authority.could-not-evaluate'])
        self.denied('could-not-evaluate',self.add(sender='operator',extra='PLAN_SHA256: '+'a'*64))

    def test_F6_incomplete_lineage_does_not_invent_artifact_change(self):
        decl=self.artifact('plans','PL-base')
        grant=self.add(extra='PARENT_DISPATCH_ID: missing\n'+decl)
        self.assertEqual(self.check(grant)['reasons'],['authority.lineage-incomplete'])

    def test_S1_fenced_header_quotes_are_inert(self):
        for fence in ('```','~~~','````'):
            g=self.add(sender='operator',content=f'{fence}\nFROM: other.implementer\n{fence}\nDISPATCH IMPL')
            self.assertTrue(self.check(g)['allowed'],self.check(g))
        self.denied('header-conflict',self.add(sender='operator',content='DISPATCH IMPL\nFROM: other.implementer'))
        self.denied('header-conflict',self.add(sender='operator',content='DISPATCH IMPL\nprose\u2028FROM: other.implementer'))


class TestFold2Oracles(unittest.TestCase):
    def test_S3_module_docstrings_are_preserved(self):
        from relay_engine.tests import test_finding_tags, test_history_audit
        for module in (test_finding_tags,test_history_audit):
            self.assertIsNotNone(module.__doc__,module.__name__)

    def test_base_commit_requirement_is_in_failure_message(self):
        from relay_engine.tests.test_judgment_store import base_module
        with mock.patch('subprocess.check_output',side_effect=subprocess.CalledProcessError(128,['git','show'])):
            try:
                base_module('rules')
            except Exception as exc:
                self.assertIsInstance(exc, AssertionError)
                self.assertRegex(str(exc), 'requires.*cbc8f07')
            else:
                self.fail('missing base commit must fail explicitly')
