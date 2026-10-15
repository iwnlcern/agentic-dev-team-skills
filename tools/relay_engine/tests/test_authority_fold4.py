"""N4 direct edges bind openers; pair edges bind reviewed planner revisions."""
from relay_engine import cycles, ledger
from relay_engine.tests import test_authority as h
from relay_engine.tests.test_ledger import LedgerFixture


class TestAuthorityFold4(LedgerFixture):
    add=h.TestAuthority.add
    artifact=h.TestAuthority.artifact
    check=h.TestAuthority.check
    denied=h.TestAuthority.denied

    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger,'r')
        self.number=0
        self.files={}

    def test_N4_direct_parent_reuse_cannot_replace_pin_at_any_depth(self):
        for deep in (False,True):
            ident=f'plan-{deep}'
            pin_a=self.artifact('plans',ident,b'A')
            plan=self.add('PLAN',ident=ident,extra=pin_a,content='',prechecks=(cycles.precheck,))
            parent=ident
            if deep:
                parent='review-deep'
                self.add('PLAN-REVIEW','p.implementer','p.planner',parent,
                         'PARENT_DISPATCH_ID: '+ident+'\nVERDICT: approve', '')
            control=self.add(sender='operator',extra='PARENT_DISPATCH_ID: '+parent)
            self.assertTrue(self.check(control)['allowed'])
            pin_b=self.artifact('plans',ident,b'B')
            self.add('SITREP','p.implementer','p.planner',ident,pin_b,'',
                     admits_against=plan.rendered_path,prechecks=(cycles.precheck,))
            grant=self.add(sender='operator',extra='PARENT_DISPATCH_ID: '+parent)
            self.assertEqual(self.check(grant)['reasons'],['authority.artifact-changed'])
            self.files[ident].write_bytes(b'A')
            self.assertTrue(self.check(grant)['allowed'])
            self.assertTrue(self.check(control)['allowed'])

    def test_N4_pair_same_sender_plan_reuse_cannot_replace_pin(self):
        pin_a=self.artifact('plans','P',b'A')
        plan=self.add('PLAN',ident='plan',extra=pin_a,content='')
        pin_b=self.artifact('plans','P',b'B')
        self.add('PLAN',ident='plan',extra=pin_b,content='',
                 admits_against=plan.rendered_path,prechecks=(cycles.precheck,))
        self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                 'PARENT_DISPATCH_ID: plan\n'+pin_a+'\nVERDICT: approve', '')
        grant=self.add(extra='PARENT_DISPATCH_ID: review')
        self.assertEqual(self.check(grant)['reasons'],['authority.artifact-changed'])
        self.files['P'].write_bytes(b'A')
        self.assertTrue(self.check(grant)['allowed'])

    def test_N4_pair_same_sender_review_reuse_cannot_install_unknown_pin(self):
        pin_a=self.artifact('plans','P',b'A')
        self.add('PLAN',ident='plan',extra=pin_a,content='')
        review=self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                        'PARENT_DISPATCH_ID: plan\n'+pin_a+'\nVERDICT: approve', '')
        control=self.add(extra='PARENT_DISPATCH_ID: review')
        self.assertTrue(self.check(control)['allowed'])
        pin_b=self.artifact('plans','P',b'B')
        self.add('PLAN',ident='other-plan',extra=pin_b,content='')
        self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                 'PARENT_DISPATCH_ID: plan\n'+pin_b+'\nVERDICT: approve', '',
                 admits_against=review.rendered_path,prechecks=(cycles.precheck,))
        grant=self.add(extra='PARENT_DISPATCH_ID: review')
        self.assertEqual(self.check(grant)['reasons'],['authority.lineage-incomplete'])
        self.files['P'].write_bytes(b'A')
        self.assertEqual(self.check(grant)['reasons'],
                         ['authority.artifact-changed','authority.lineage-incomplete'])
        self.assertTrue(self.check(control)['allowed'])

    def test_N4_shared_design_revision_uses_latest_review_and_matching_digest(self):
        pin_a=self.artifact('designs','D',b'A')
        self.add('DESIGN',ident='design',extra='DESIGN_DOC_ID: DD\n'+pin_a,content='')
        self.add('DESIGN-REVIEW','p.implementer','p.planner','design',
                 'PARENT_DISPATCH_ID: design\nDESIGN_DOC_ID: DD\n'+pin_a+'\nVERDICT: must-revise', '')
        pin_b=self.artifact('designs','D',b'B')
        self.add('DESIGN',ident='design',extra='DESIGN_DOC_ID: DD\n'+pin_b,content='')
        self.add('DESIGN-REVIEW','p.implementer','p.planner','design',
                 'PARENT_DISPATCH_ID: design\nDESIGN_DOC_ID: DD\n'+pin_b+'\nVERDICT: approve', '')
        # A later revision is not the one the effective review approved.
        pin_c=self.artifact('designs','D',b'C')
        self.add('DESIGN',ident='design',extra='DESIGN_DOC_ID: DD\n'+pin_c,content='')
        self.files['D'].write_bytes(b'B')
        plan_pin=self.artifact('plans','P')
        self.add('PLAN',ident='plan',extra='PARENT_DISPATCH_ID: design\nDESIGN_LOCK_ID: DD\n'+plan_pin,content='')
        self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                 'PARENT_DISPATCH_ID: plan\n'+plan_pin+'\nVERDICT: approve', '')
        grant=self.add(extra='PARENT_DISPATCH_ID: review')
        self.assertTrue(self.check(grant)['allowed'],self.check(grant))
        self.files['D'].write_bytes(b'C')
        self.assertEqual(self.check(grant)['reasons'],['authority.artifact-changed'])

    def test_N4_review_without_digest_uses_latest_planner_pin(self):
        self.add('PLAN',ident='plan',extra=self.artifact('plans','P',b'A'),content='')
        self.add('PLAN',ident='plan',extra=self.artifact('plans','P',b'B'),content='')
        self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                 'PARENT_DISPATCH_ID: plan'+'\nVERDICT: approve', '')
        grant=self.add(extra='PARENT_DISPATCH_ID: review')
        self.assertTrue(self.check(grant)['allowed'])
        self.files['P'].write_bytes(b'A')
        self.assertEqual(self.check(grant)['reasons'],['authority.artifact-changed'])

    def test_N4_adopted_row_is_not_dispatch_opener(self):
        pin_b=self.artifact('plans','P',b'B')
        self.add('PLAN',ident='plan',extra=pin_b,content='',origin='adopted',
                 stamp='20260808-110000',rendered_path='old/PLAN-pair-planner-20260808-110000.md')
        pin_a=self.artifact('plans','P',b'A')
        self.add('PLAN',ident='plan',extra=pin_a,content='')
        grant=self.add(sender='operator',extra='PARENT_DISPATCH_ID: plan')
        self.assertTrue(self.check(grant)['allowed'])
        self.files['P'].write_bytes(b'B')
        self.denied('artifact-changed',grant)
