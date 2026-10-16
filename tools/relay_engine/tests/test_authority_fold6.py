"""Original shape controls with body-verdict expectations amended by E23."""
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5


class TestAuthorityFold6(TestAuthorityFold5):
    def assert_case(self, content, reasons, extra=''):
        for action,phase,token in [('implement','IMPL','DISPATCH IMPL'),('merge','MERGE-GATE','DISPATCH MERGE')]:
            with self.subTest(action=action,content=content):
                grant=self.add(phase,'operator',extra=extra,content=content.replace('TOKEN',token))
                result=self.check(grant,action=action)
                self.assertEqual(result['reasons'],sorted(reasons),result)
                self.assertEqual(result['allowed'],not reasons,result)

    def test_rule1_original_isolation(self):
        for text in ['TOKEN','\nTOKEN\n','\nTOKEN','\nTOKEN \t\n']:
            self.assert_case(text,[])
        for text in ['text\nTOKEN','TOKEN\ntext',' TOKEN','TOKEN x','```\nx\n```\nTOKEN','```\n\nTOKEN\n\n```']:
            self.assert_case(text,['authority.shape-absent'])

    def test_rule2_openers_anywhere(self):
        for opener in ['<!--','<?','<![','<!X','<SCRIPT>','<pre ','<style\t','<textarea>']:
            self.assert_case('TOKEN\n\ntext `'+opener+'`',['authority.shape-absent'])
            self.assert_case('TOKEN\n\n```\n'+opener+'\n```',['authority.shape-absent'])
        self.assert_case('TOKEN\n\n<scripted>',['authority.shape-absent'])
        self.assert_case('TOKEN\n\n<textarea',['authority.shape-absent'])
        self.assert_case('prose <!--\n```\n-->\n\nTOKEN\n\n```',['authority.shape-absent'])

    def test_rule4_body_negative_forms_are_inert(self):
        for text in ['VERDICT: denied','**VERDICT:** denied','- VERDICT: denied','1. VERDICT: denied','## VERDICT: denied','~~VERDICT:~~ denied','[VERDICT](/u): denied','VERD&#73;CT: denied','VERD\\ICT: denied','VERD\u200bICT: denied','  - - VERDICT: denied','| VERDICT | denied |','&lt;VERDICT: denied&gt;','VERDICT: .approved','VERDICT::approved','VERDICT:','VERDICT: approved&#10;','VERDICT: yeſ','**VERDICT:** .approved','**VERDICT:** **','\\`VERDICT: denied`','`VERDICT: denied']:
            self.assert_case('TOKEN\n\n'+text,[])
        self.assert_case('TOKEN\n\n<!-- VERDICT: denied -->',['authority.shape-absent'])
        for text in ['VERDICT: <denied>approved','VERDICT: approved | VERDICT: denied','**VERDICT:** approved\nVERDICT: denied']:
            self.assert_case('TOKEN\n\n'+text,[])

    def test_rule4_positive_and_inert_controls(self):
        for text in ['VERDICT: approved','**VERDICT:** approved','__VERDICT:__ approved','~~VERDICT:~~ approved','VERDICT: merge ready','VERDICT: **approved**','VERDICT: approved.','VERDICT: YES','VERDICT: approved\nordinary next line','`VERDICT: denied`']:
            self.assert_case('TOKEN\n\n'+text,[])
        self.assert_case('TOKEN\n\n``VERDICT: denied``',[])
        self.assert_case('TOKEN\n\n`x\nVERDICT: denied\nx`',[])

    def test_rule3_merge_only_header(self):
        for extra,content,reasons in [
            ('MERGE_AUTHORIZATION: approved','',[]),
            ('','MERGE_AUTHORIZATION: approved',['authority.shape-absent']),
            ('','**MERGE_AUTHORIZATION:** approved',['authority.shape-absent']),
            ('MERGE_AUTHORIZATION: approved','VERDICT: denied',[]),
            ('MERGE_AUTHORIZATION: approved','<!-- VERDICT: denied -->',['authority.shape-absent'])]:
            g=self.add('MERGE-GATE','operator',extra=extra,content=content)
            self.assertEqual(self.check(g,action='merge')['reasons'],reasons)

    def test_rule3_pair_reviews_raw_header(self):
        pin=self.artifact('plans','P');self.add('PLAN',ident='plan',extra=pin,content='')
        for header,content,allowed in [
            ('VERDICT: approve','',True),('', 'VERDICT: approve',False),
            ('VERDICT: approve','<!-- VERDICT: must-revise -->',False),
            ('VERDICT: approve','&lt;VERDICT: must-revise&gt;',True),
            ('VERDICT: approve','VERDICT: <must-revise>approve',True),
            ('VERDICT: approve','## VERDICT: denied',True),
            ('VERDICT: approve','VERDICT: approved&#10;',True),
            ('VERDICT: approve','VERDICT: yeſ',True),
            ('VERDICT: APPROVE','VERDICT: YES',True),
            ('VERDICT: approve','VERDICT: approved\nordinary next line',True),
            ('VERDICT: approve','`VERDICT: denied`',True),
            ('VERDICT: approve','```\nVERDICT: denied\n```',True),
            ('VERDICT: approve','<div>',False),
            ('VERDICT: approve','```\n<div>\n```',False)]:
            with self.subTest(header=header,content=content):
                ident=f'review-{self.number}'
                self.add('PLAN-REVIEW','p.implementer','p.planner',ident,'PARENT_DISPATCH_ID: plan\n'+pin+('\n'+header if header else ''),content)
                g=self.add(extra='PARENT_DISPATCH_ID: '+ident)
                self.assertEqual(self.check(g)['reasons'],[] if allowed else ['authority.lineage-incomplete'])

    def test_errata5_to7_refusals(self):
        for fence in ['```', '~~~']:
            self.assert_case(f'- {fence}\n  {fence}\n\n{fence}\n\nTOKEN\n\n{fence}', ['authority.shape-absent'])
            self.assert_case('TOKEN\n\n'+fence+'\nexample\n'+fence, ['authority.shape-absent'])
            self.assert_case('TOKEN\n\nmid '+fence+' run', ['authority.shape-absent'])
            self.assert_case('TOKEN\n\n'+fence+'\nVERDICT: denied\n'+fence, ['authority.shape-absent'])
        self.assert_case('TOKEN\n\n<b>VERDICT:</b> denied', ['authority.shape-absent'])
        self.assert_case('<div>\n```\n\nTOKEN\n\n```', ['authority.shape-absent'])
        self.assert_case('<div>\n```\n\nTOKEN\n\n<p>VERDICT: denied</p>\n```', ['authority.shape-absent'])
        g=self.add('MERGE-GATE','operator',extra='MERGE_AUTHORIZATION: approved',content='```\nexample\n```')
        self.assertEqual(self.check(g,action='merge')['reasons'],[])
