"""Errata 2 and 3: visible refusals, inert comments, strict positive grants."""
from relay_engine import ledger
from relay_engine.tests import test_authority as h
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_ledger import LedgerFixture, WALL


class TestAuthorityFold5(LedgerFixture):
    artifact = h.TestAuthority.artifact
    check = h.TestAuthority.check

    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger, 'r')
        self.number = 0
        self.files = {}

    def add(self, phase='IMPL', sender='p.planner', target='p.implementer',
            ident=None, extra='', content='DISPATCH IMPL'):
        self.number += 1
        raw = body(phase, sender, target, ident or f'row-{self.number}', extra, '\n'+content)
        return ledger.admit(self.ledger, self.root, ledger._server_envelope(raw), raw,
                            f'sid-{self.number}', clock=lambda: WALL+self.number)

    def outcomes(self, content, reasons):
        for action, phase, token in [('implement', 'IMPL', 'DISPATCH IMPL'),
                                     ('merge', 'MERGE-GATE', 'DISPATCH MERGE')]:
            with self.subTest(action=action, content=content):
                grant = self.add(phase, 'operator', content=content.replace('TOKEN', token))
                result = self.check(grant, action=action)
                self.assertEqual(result['reasons'], reasons, result)
                self.assertEqual(result['allowed'], not reasons, result)

    def test_G1_visible_verdict_forms_and_conflicts(self):
        for key in ['VERDICT', '**VERDICT:**', '**VERDICT**:', '*VERDICT*:',
                    '__VERDICT__:', '***VERDICT:***', '_verdict:_']:
            if key == 'VERDICT':
                key += ':'
            for prefix in ['', '- ', '* ', '+ ', '1. ', '2) ', '    ', '\t']:
                self.outcomes('TOKEN\n' + prefix + key + ' **denied**.', ['authority.denied'])
        self.outcomes('TOKEN\n**VERDICT:** denied\nVERDICT: approved',
                      ['authority.denied', 'authority.verdict-conflict'])
        self.outcomes('TOKEN', [])

    def test_G1_quoted_fenced_and_backticked_verdicts_are_inert(self):
        for text in ['> **VERDICT:** denied', '`VERDICT: denied`',
                     '```\n**VERDICT:** denied\n```', '~~~\n- VERDICT: denied\n~~~']:
            self.outcomes('TOKEN\n' + text, [])
        for text in ['**TOKEN**', '- TOKEN', '1. TOKEN', '    TOKEN']:
            self.outcomes(text, ['authority.shape-absent'])

    def test_G3_field_only_merge_shapes_preserve_values(self):
        for key, value in [('MERGE_AUTHORIZATION', 'approved'),
                           ('HUMAN_MERGE_AUTHORIZATION', 'granted'),
                           ('MERGE_APPROVED', 'true'), ('VERDICT', 'merge ready')]:
            for prefix in ['', ' ', '   ']:
                grant = self.add('MERGE-GATE', 'operator',
                                 content=f'{prefix}{key}: {value}')
                self.assertTrue(self.check(grant, action='merge')['allowed'])
            for line in [f'    {key}: {value}', f'\t{key}: {value}', f' \t{key}: {value}',
                         f'- {key}: {value}', f'1. {key}: {value}',
                         f'**{key}:** {value}', f'{key}: **{value}**',
                         f'{key[:3]}<!-- note -->{key[3:]}: {value}', f'{key}: <!-- note -->{value}', f'{key}: {value} <!-- note -->']:
                with self.subTest(line=line):
                    grant = self.add('MERGE-GATE', 'operator', content=line)
                    self.assertEqual(self.check(grant, action='merge')['reasons'],
                                     ['authority.shape-absent'])
        grant = self.add('MERGE-GATE', 'operator', content='**MERGE_AUTHORIZATION:** denied')
        self.assertEqual(self.check(grant, action='merge')['reasons'],
                         ['authority.denied', 'authority.shape-absent'])

    def test_G3_non_strict_positive_still_conflicts(self):
        text = '**VERDICT:** approved\nVERDICT: denied'
        grant = self.add('MERGE-GATE', 'operator', content=text)
        self.assertEqual(self.check(grant, action='merge')['reasons'],
                         ['authority.denied', 'authority.shape-absent', 'authority.verdict-conflict'])
        self.outcomes('TOKEN\n'+text, ['authority.denied', 'authority.verdict-conflict'])

    def test_G3_pair_approval_requires_original_line_eligibility(self):
        pin = self.artifact('plans', 'P')
        self.add('PLAN', ident='plan', extra=pin, content='')
        for text, allowed in [('VERDICT: approve', True), ('- **VERDICT:** approve', True),
                              ('   **VERDICT:** approve', True), ('    VERDICT: approve', False),
                              ('\tVERDICT: approve', False), ('VERDICT: <!-- n -->approve', False),
                              ('VERDICT: approve <!-- n -->', False),
                              ('VERDICT: approve\n    VERDICT: must-revise', False),
                              ('VERDICT: approve\n    VERDICT: approve', True)]:
            with self.subTest(text=text):
                ident = f'review-{self.number}'
                self.add('PLAN-REVIEW', 'p.implementer', 'p.planner', ident,
                         'PARENT_DISPATCH_ID: plan\n'+pin, text)
                grant = self.add(extra='PARENT_DISPATCH_ID: '+ident)
                self.assertEqual(self.check(grant)['reasons'],
                                 [] if allowed else ['authority.lineage-incomplete'])

    def test_G1_pair_review_emphasis_preserves_key_underscores(self):
        pin = self.artifact('plans', 'P')
        self.add('PLAN', ident='plan', extra=pin, content='')
        for verdict in ['**PLAN_REVIEW_VERDICT:** approve',
                        '**PLAN_REVIEW_VERDICT:** approve\nPLAN_REVIEW_VERDICT: must-revise']:
            review = f'review-{self.number}'
            self.add('PLAN-REVIEW', 'p.implementer', 'p.planner', review,
                     'PARENT_DISPATCH_ID: plan\n' + pin, verdict)
            grant = self.add(extra='PARENT_DISPATCH_ID: ' + review)
            self.assertEqual(self.check(grant)['reasons'],
                             ['authority.lineage-incomplete'] if '\n' in verdict else [])

    def test_G2_comment_tokens_do_not_grant(self):
        for text in ['<!-- TOKEN -->', '<!--\nTOKEN\n-->',
                     '<!-- open\nTOKEN', 'TOKEN <!-- note -->',
                     '<!-- note -->TOKEN', 'DISPATCH <!--note-->IMPL']:
            self.outcomes(text, ['authority.shape-absent'])

    def test_G2_comment_denials_are_inert_and_visible_suffixes_count(self):
        for text in ['<!-- VERDICT: denied -->', '<!--\nVERDICT: denied\n-->',
                     '<!-- open\nVERDICT: denied', 'prose <!--\nVERDICT: denied\n-->']:
            self.outcomes('TOKEN\n' + text, [])
        for text in ['VERDICT: <!-- note --> denied', 'VER<!-- note -->DICT: denied',
                     'VERDICT: de<!--\ncomment\n-->nied', '<!-- note -->VERDICT: denied',
                     '<!-->\nVERDICT: denied', '<!--->\nVERDICT: denied']:
            self.outcomes('TOKEN\n' + text, ['authority.denied'])
        self.outcomes('prose <!-->\nTOKEN', [])
        self.outcomes('prose <!--->\nTOKEN', [])
        self.outcomes('<!-->\nTOKEN', [])
        self.outcomes('<!--->\nTOKEN', [])

    def test_G2_inline_unclosed_comments_do_not_hide_later_paragraphs(self):
        self.outcomes('TOKEN\nprose <!-- open\n\nVERDICT: denied', ['authority.denied'])
        self.outcomes('TOKEN\nprose <!-- open\n\nVERDICT: denied\n-->', ['authority.denied'])
        self.outcomes('TOKEN\nprose \\<!--\nVERDICT: denied\n-->', ['authority.denied'])

    def test_G2_fences_and_comments_obey_opening_order(self):
        self.outcomes('TOKEN\n```\n<!--\n```\nVERDICT: denied\n```\n-->\n```',
                      ['authority.denied'])
        self.outcomes('<!--\n```\n-->\nTOKEN', [])
        self.outcomes('```\n<!--\n```\nTOKEN', [])
        self.outcomes('```\n\nTOKEN\n```', ['authority.shape-absent'])
        self.outcomes('<!--\n\n```\n-->\nTOKEN', [])
        self.outcomes('TOKEN\n<!--\n```\n-->\nVERDICT: denied', ['authority.denied'])
        self.outcomes('TOKEN\n<!-- note -->```\nVERDICT: denied', ['authority.denied'])

    def test_G2_code_spans_and_comments_obey_opening_order(self):
        self.outcomes('TOKEN\n`<!--`\nVERDICT: denied\n`-->`', ['authority.denied'])
        self.outcomes('TOKEN\nprose <!-- `\nVERDICT: denied\n` -->', [])
        self.outcomes('TOKEN\nprose <!-- ` -->\nVERDICT: denied\n`', ['authority.denied'])
        self.outcomes('`<!--\nTOKEN\n-->`', ['authority.shape-absent'])
        self.outcomes('TOKEN\n> <!--\nVERDICT: denied\n> -->', ['authority.denied'])

    def test_G2_invalid_fence_does_not_interrupt_code_span(self):
        self.outcomes('``opening\n```bad`info\nTOKEN\n``', ['authority.shape-absent'])
        self.outcomes('``opening\n```valid-info\nTOKEN\n```', ['authority.shape-absent'])
