"""R1 locators require lineage-authorized digests, regardless of spelling."""
import hashlib
from relay_engine import ledger
from relay_engine.tests import test_authority as h
from relay_engine.tests.test_ledger import LedgerFixture


class TestAuthorityFold3(LedgerFixture):
    add = h.TestAuthority.add
    artifact = h.TestAuthority.artifact
    check = h.TestAuthority.check
    denied = h.TestAuthority.denied

    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger, 'r')
        self.number = 0
        self.files = {}

    def pair(self, declarations, review_declarations=''):
        ident=f'p-{self.number}'
        self.add('PLAN',ident=ident,extra=declarations,content='')
        self.add('PLAN-REVIEW','p.implementer','p.planner',ident+'-review',
                 'PARENT_DISPATCH_ID: '+ident+'\nVERDICT: approve'+('\n'+review_declarations if review_declarations else ''), '')
        return self.add(extra='PARENT_DISPATCH_ID: '+ident+'-review')

    def test_R1_four_missing_digest_forms_refuse_pair_and_direct(self):
        digest=hashlib.sha256(b'original').hexdigest()
        for field,directory in [('PLAN','plans'),('DESIGN','designs')]:
            self.artifact(directory,'missing')
            for suffix in ('',f'\nNote: {field}_SHA256 is {digest}',
                           f'\n  {field}_SHA256: {digest}',f'\n{field.lower()}_sha256: {digest}'):
                for direct in (False,True):
                    with self.subTest(field=field,suffix=suffix,direct=direct):
                        decl=f'{field}_ARTIFACT: missing'+suffix
                        grant=self.add(sender='operator',extra=decl) if direct else self.pair(decl)
                        for data in (b'original',b'changed'):
                            self.files['missing'].write_bytes(data)
                            self.assertEqual(self.check(grant)['reasons'],['authority.could-not-evaluate'])

    def test_R1_ancestor_digest_supplies_locator_across_field_and_path_forms(self):
        for field,directory in [('PLAN','plans'),('DESIGN','designs')]:
            for ancestor_path in (False,True):
                decl=self.artifact(directory,'inherited')
                digest=hashlib.sha256(b'original').hexdigest()
                if ancestor_path:decl=f'{field}_LOCK_ID: {directory}/inherited.md @ sha256 {digest}'
                ident=f'ancestor-{self.number}'
                self.add('PLAN',ident=ident,extra=decl,content='')
                for use_path in (False,True):
                    locator=f'{field}_LOCK_ID: {directory}/inherited.md' if use_path else f'{field}_ARTIFACT: inherited'
                    grant=self.add(sender='operator',extra=f'PARENT_DISPATCH_ID: {ident}\n'+locator)
                    self.files['inherited'].write_bytes(b'original')
                    self.assertTrue(self.check(grant)['allowed'],self.check(grant))
                    self.files['inherited'].write_bytes(b'changed')
                    self.assertEqual(self.check(grant)['reasons'],['authority.artifact-changed'])

    def test_R1_review_supplies_pair_plan_locator_digest(self):
        for field,directory in [('PLAN','plans'),('DESIGN','designs')]:
            decl=self.artifact(directory,'reviewed')
            grant=self.pair(f'{field}_ARTIFACT: reviewed',decl)
            self.assertTrue(self.check(grant)['allowed'])
            self.files['reviewed'].write_bytes(b'changed')
            self.denied('artifact-changed',grant)

    def test_R1_newest_digest_wins_and_locator_only_does_not_erase_it(self):
        old=self.artifact('plans','latest',b'old')
        self.add('PLAN',ident='old',extra=old,content='')
        new=self.artifact('plans','latest',b'new')
        self.add('PLAN-REVIEW','p.implementer','p.planner','new',
                 'PARENT_DISPATCH_ID: old\n'+new+'\nVERDICT: approve', '')
        grant=self.add(sender='operator',extra='PARENT_DISPATCH_ID: new\nPLAN_ARTIFACT: latest\nPLAN_LOCK_ID: plans/latest.md')
        self.assertTrue(self.check(grant)['allowed'],self.check(grant))
        self.files['latest'].write_bytes(b'old')
        self.denied('artifact-changed',grant)

    def test_R1_path_only_without_lineage_digest_refuses(self):
        for field,directory in [('PLAN','plans'),('DESIGN','designs')]:
            for direct in (False,True):
                decl=f'{field}_LOCK_ID: {directory}/unbound.md'
                grant=self.add(sender='operator',extra=decl) if direct else self.pair('',decl)
                self.assertEqual(self.check(grant)['reasons'],['authority.could-not-evaluate'])
