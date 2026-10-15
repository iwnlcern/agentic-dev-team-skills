"""Erratum 10 r2: header-only verdicts and independent header shape veto."""
from unittest import mock

from relay_engine import rules
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
from relay_engine.tests.test_judging import body


class TestAuthorityFold7(TestAuthorityFold5):
    def add(self, *args, extra='', **kwargs):
        subject = next((line for line in extra.splitlines() if line.startswith('SUBJECT:')), None)
        if subject is None:
            return super().add(*args, extra=extra, **kwargs)
        extra = '\n'.join(line for line in extra.splitlines() if not line.startswith('SUBJECT:'))
        def with_subject(*a, **k):
            raw = body(*a, **k)
            old = next(line for line in raw.splitlines() if line.startswith(b'SUBJECT:'))
            return raw.replace(old, subject.encode(), 1)
        with mock.patch('relay_engine.tests.test_authority_fold5.body', side_effect=with_subject):
            return super().add(*args, extra=extra, **kwargs)

    def grant_case(self, extra='', content='TOKEN', reasons=(), action='implement'):
        phase, token = ('IMPL', 'DISPATCH IMPL') if action == 'implement' else ('MERGE-GATE', 'DISPATCH MERGE')
        sender = 'operator'
        if action == 'implement':
            pin = self.artifact('plans', 'P')
            plan = f'plan-{self.number}'
            self.add('PLAN', ident=plan, extra=pin, content='')
            review = f'review-{self.number}'
            self.add('PLAN-REVIEW', 'p.implementer', 'p.planner', review,
                     extra='PARENT_DISPATCH_ID: '+plan+'\n'+pin+'\nVERDICT: approve', content='')
            extra = 'PARENT_DISPATCH_ID: '+review+('\n'+extra if extra else '')
            sender = 'p.planner'
        grant = self.add(phase, sender, extra=extra, content=content.replace('TOKEN', token))
        result = self.check(grant, action=action)
        self.assertEqual(result['reasons'], sorted(reasons), result)
        self.assertEqual(result['allowed'], not reasons, result)
        return grant

    def test_header_values_and_complete_reason_sets(self):
        for action in ['implement', 'merge']:
            for key in ['VERDICT', 'HUMAN_MERGE_AUTHORIZATION', 'CUSTOM_MERGE_APPROVED']:
                with self.subTest(action=action, key=key):
                    self.grant_case(key+': denied', reasons=['authority.denied'], action=action)
        for value in ['.approved', '~~approved~~ denied', '**approved**', '_approved', '&#97;pproved', 'yeſ', 'approved&#10;']:
            with self.subTest(value=value):
                self.grant_case('MERGE_AUTHORIZATION: '+value, reasons=['authority.denied'] + (['authority.verdict-conflict'] if value in ['yeſ', 'approved&#10;'] else []), action='merge')
        self.grant_case('VERDICT: `approved`', reasons=['authority.denied'])
        self.grant_case('MERGE_AUTHORIZATION: approved<x>', reasons=['authority.denied','authority.shape-absent','authority.verdict-conflict'], action='merge')
        self.grant_case('MERGE_AUTHORIZATION: approved~~', reasons=['authority.denied','authority.verdict-conflict'], action='merge')
        self.grant_case('VERDICT: <denied>', reasons=['authority.denied','authority.shape-absent'])
        self.grant_case('VERDICT: approved <note>', reasons=['authority.shape-absent'])
        self.grant_case('SUBJECT: compare <tag> values', reasons=['authority.shape-absent'])
        self.grant_case('MERGE_AUTHORIZATION: denied\nVERDICT: approved', reasons=['authority.denied','authority.verdict-conflict'], action='merge')
        for value in ['approve', 'APPROVED.', 'granted', 'YES', 'true', 'complete', 'merge-ready', 'merge ready']:
            self.grant_case('VERDICT: '+value)

    def test_body_verdicts_do_not_participate(self):
        for action in ['implement','merge']:
            self.grant_case(content='TOKEN\n\nVERDICT: denied', action=action)
            self.grant_case('VERDICT: approved', 'TOKEN\n\nVERDICT: denied', action=action)
            self.grant_case(content='TOKEN\n\nVERDICT: <denied>approved', action=action)

    def test_duplicate_headers_keep_independent_reasons(self):
        # Admission rejects duplicates; imported historical rows may contain them.
        for key, expected in [('VERDICT', ['authority.shape-absent']), ('DISPATCH_ID', ['authority.header-conflict','authority.shape-absent'])]:
            action = 'merge' if key == 'VERDICT' else 'implement'
            grant = self.grant_case('VERDICT: approved', action=action)
            raw = bytes(self.ledger.execute('SELECT body FROM relays WHERE seq=?',(grant.seq,)).fetchone()[0])
            original = 'VERDICT: approved' if key=='VERDICT' else next(x for x in raw.decode().splitlines() if x.startswith('DISPATCH_ID:'))
            replacement = original+'\n'+('VERDICT: approved' if key=='VERDICT' else 'DISPATCH_ID: different')
            self.ledger.execute('UPDATE relays SET body=? WHERE seq=?',(raw.replace(original.encode(),replacement.encode()),grant.seq))
            self.assertEqual(self.check(grant, action=action)['reasons'], expected)

    def review_case(self, verdict='approve', extra='', content='', expected=()):
        pin=self.artifact('plans','P');ident=f'plan-{self.number}'
        self.add('PLAN', ident=ident, extra=pin, content='')
        review=f'review-{self.number}'
        self.add('PLAN-REVIEW','p.implementer','p.planner',review,extra='PARENT_DISPATCH_ID: '+ident+'\n'+pin+'\nPLAN_REVIEW_VERDICT: '+verdict+('\n'+extra if extra else ''),content=content)
        grant=self.add(extra='PARENT_DISPATCH_ID: '+review)
        self.assertEqual(self.check(grant)['reasons'], list(expected))

    def test_review_header_approval_is_narrow(self):
        for value in ['yes','granted','true','complete','merge-ready','**approve**','approved~~','yeſ']:
            with self.subTest(value=value):
                self.review_case(value, expected=['authority.lineage-incomplete'])
        for value in ['approve','approved','APPROVED.']:
            self.review_case(value)
        self.review_case(extra='SUBJECT: compare <tag> values', expected=['authority.lineage-incomplete'])
        self.review_case(extra='HUMAN_MERGE_AUTHORIZATION: denied', expected=['authority.lineage-incomplete'])
        self.review_case(content='VERDICT: must-revise')
        self.review_case(content='```\nVERDICT: must-revise\n```')

    def test_rule3_key_inclusion_and_body_header_distinction(self):
        keys=['VERDICT','MERGE_AUTHORIZATION','HUMAN_MERGE_AUTHORIZATION','MERGE_APPROVED']
        for key in keys:
            self.assertTrue(any(rules.merge_authorization_positive(key,v) for v in ['approved','yes','granted','true','merge-ready']))
            self.grant_case(key+': denied', reasons=['authority.denied'])
        # Body header-shaped fields cannot stand in for the parsed review/header.
        for key in ['NOTVERDICT','VERDICT_NOTE','XMERGE_APPROVED']:
            self.grant_case(key+': denied')
        self.grant_case('MERGE_AUTHORIZATION: approved', content='VERDICT: denied', action='merge')
        self.grant_case(content='MERGE_AUTHORIZATION: approved', reasons=['authority.shape-absent'], action='merge')

    def test_python_acceptance_extra_rows(self):
        for action in ['implement','merge']:
            for text in ['TOKEN\n\n  </p>','TOKEN\n\n   <div>','<div>\n```\n</div>\n\n```\n\nTOKEN\n\n```','TOKEN\n\n<div>\n```\n</div>\n\nVERDICT: denied']:
                self.grant_case(content=text, reasons=['authority.shape-absent'], action=action)
            for text in ['TOKEN\n\n< ','TOKEN\n\n<3','TOKEN\n\n`x\n> quote\nVERDICT: denied\nx`','TOKEN\n\n`x\n\nVERDICT: denied\nx`']:
                self.grant_case(content=text, action=action)
            self.grant_case(content='prose\nTOKEN', reasons=['authority.shape-absent'], action=action)
            self.grant_case(content='\f\nTOKEN', reasons=['authority.shape-absent'], action=action)
        self.review_case(content='<div>\n```\n</div>\n\nVERDICT: must-revise', expected=['authority.lineage-incomplete'])
        self.review_case(content='   <div>', expected=['authority.lineage-incomplete'])
