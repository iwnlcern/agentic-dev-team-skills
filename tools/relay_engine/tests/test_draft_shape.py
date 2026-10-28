"""Behavioral contract for draft admission shapes and their diagnostic locations."""
import itertools
import random
import re
import unittest

from relay_engine import authority, envelope as E, rules

CHARS = '\v\f\x1c\x1d\x1e\x85\u2028\u2029'
KEYS = ('SUPERSEDES', 'CYCLE_TERMINUS', 'CYCLE_REOPEN')
VERDICTS = ('VERDICT', 'PLAN_REVIEW_VERDICT', 'MERGE_AUTHORIZATION',
            'HUMAN_MERGE_AUTHORIZATION', 'MERGE_APPROVED')


def draft(phase='SITREP', extra='', body='', prefix=''):
    return (prefix + 'ROLE: Pair Planner\nPHASE: ' + phase + '\n'
            'AUTHORITY: report-only\nDISPATCH_ID: shape\nRUN_ID: r\n'
            'CEREMONY_TIER: small\nEVIDENCE_TARGET: E2\nHUMAN_GATE_REQUIRED: no\n'
            'FROM: p.planner\nTO: p.implementer\nSUBJECT: shape\n' + extra + '\n' + body)


def late_verdict_cases():
    for grant, key, value, place in itertools.product(
            ('merge-body', 'merge-above', 'merge-header', 'impl'), VERDICTS,
            ('merge-blocked', 'approved', 'n/a — dispatch amendment'),
            ('list-end', 'body', 'indent', 'list', 'quote')):
        phase = 'IMPL' if grant == 'impl' else 'MERGE-GATE'
        token = 'DISPATCH IMPL' if grant == 'impl' else 'DISPATCH MERGE'
        prefix = token + '\n\n' if grant == 'merge-above' else ''
        extra = 'MERGE_AUTHORIZATION: approved\n' if grant == 'merge-header' else ''
        body = '' if grant in ('merge-above', 'merge-header') else token + '\n\n'
        line = {'indent': '    ', 'list': '- ', 'quote': '> '}.get(place, '') + key + ': ' + value
        if place == 'list-end':
            extra += '- explanation\n' + line + '\n'
        else:
            body += line + '\n'
        yield draft(phase, extra, body, prefix), key


def diagnostic_texts():
    """Shared input population for the standalone/engine differential."""
    yield draft()
    for ch in CHARS:
        yield draft(body='text' + ch + 'tail')
    for key in KEYS:
        for prefix in ('', '    ', '\t', '- ', '1. ', '> ', '> - '):
            yield draft(body=prefix + key + ': x\n')
    for text, _ in late_verdict_cases():
        yield text
    for fence in ('```', '~~~'):
        yield draft(body=fence + '\nSUPERSEDES: x\n' + fence + '\n')
        yield draft(body=fence + '\nSUPERSEDES: x\n')
    yield draft(body='SUPERSEDES: x\u2028')
    yield draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: no\u2029')


    # Every remaining predicate input from the original contract, including
    # negative controls, locations, long-key rendering and raw handler inputs.
    for char in CHARS:
        yield draft().replace('SUBJECT: shape', 'SUBJECT: a'+char+'b')
        yield draft(body='abc'+char+'tail')
        yield draft(prefix='title'+char+'\n')
    for ending in ('\n', '\r\n', '\r'):
        yield draft(body='> ordinary note\n').replace('\n', ending)
    for prefix, extra in (('', '- list\n'), ('', 'prose\n'), ('# title\n\n', ''),
                          ('\ufeff# title\n\n', ''), ('\n\n', '')):
        yield draft(prefix=prefix, extra=extra, body='SUPERSEDES: x\n')
    for key in KEYS:
        yield draft(extra='prose\n'+key+': x\n')
        yield draft(prefix='    '+key+': x\n')
        yield draft(body=key+': x\n').replace('\n', '\r\n')
        yield draft(extra=key+': x\n')
        for fence in ('```', '~~~'):
            yield draft(body=fence+'\n'+key+': x\n'+fence)
            yield draft(extra=fence+'\n'+key+': x\n'+fence+'\n')
        for quoted in ('`'+key+': x`', 'This mentions '+key+': x'):
            yield draft(body=quoted)
    yield draft(extra='```\nCYCLE_TERMINUS: withdrawn\nFINAL_GIT_STATUS_SHORT: clean\n')
    yield draft(body='```\nSUPERSEDES: x')
    yield draft(body='```\n~~~\nSUPERSEDES: x')
    yield draft('MERGE-GATE', extra='VERDICT: approved\n', body='DISPATCH MERGE')
    yield draft('MERGE-GATE', body='DISPATCH MERGE')
    yield draft('MERGE-GATE', body='RECOMMENDED_VERDICT: merge — merge PR #8')
    yield draft(body='VERDICT: no')
    yield draft('MERGE-GATE', body='```\nDISPATCH MERGE\n```\nVERDICT: no')
    yield draft('MERGE-GATE', body='- DISPATCH MERGE\n\nVERDICT: no')
    prefix = 'PHASE: SITREP\n\n'+'\n'*6
    yield prefix+'SUPERSEDES: x\n'+'\n'*4+'a\u2028'
    yield prefix+'a\u2028'+'\n'*4+'SUPERSEDES: x'
    for char in ('\u2028', '\x85'):
        yield draft(body='SUPERSEDES: x'+char)
    yield draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: merge-blocked\u2029')
    from relay_engine.tests import test_admission_judging as admission
    for length in (64,65,4096):
        key = 'A'*(length-len('_VERDICT'))+'_VERDICT'
        yield draft('MERGE-GATE', body='DISPATCH MERGE\n\n'+key+': no\n')
        if length != 64:
            yield admission.body('MERGE-GATE','operator',content='DISPATCH MERGE\n\n'+key+': no').decode()
    for cases in (fence_region_cases(), fence_form_cases(), wide_cases(), preserved_cases()):
        for case in cases:
            yield case[0]
    yield from scan_cases()
    yield from linear_cases()


def fence_region_cases():
    for opener, closer in (('~~~text', '~~~'), ('```text', '```')):
        for key in ('CYCLE_TERMINUS', 'SUPERSEDES'):
            yield draft(prefix=opener+'\n', body=key+': withdrawn\n'+closer), 'late-control', key
        yield draft('MERGE-GATE', prefix=opener+'\n',
                    body='DISPATCH MERGE\nVERDICT: merge-blocked\n'+closer), 'late-verdict', 'VERDICT'


def fence_form_cases():
    for body, kind in (
            ('```\n```x\n```\nSUPERSEDES: x\n```', 'late-control'),
            ('```a`b\nSUPERSEDES: x\n```', 'late-control'),
            ('~~~a`b\nSUPERSEDES: x\n~~~', None),
            ('```\nSUPERSEDES: x\n``` \t', None)):
        yield draft(body=body), kind


def wide_cases():
    for char, marker, verdict in itertools.product(
            '\u00a0\u2003\u3000\u200b\u061c\u3164\ufeff', ('', '- '), (False, True)):
        prefix = marker + char
        key = 'VERDICT' if verdict else 'SUPERSEDES'
        body = ('DISPATCH MERGE\n' if verdict else '') + prefix + key + ': x'
        yield draft('MERGE-GATE' if verdict else 'SITREP', body=body), key, len(prefix)+1
    yield draft(prefix='\ufeffSUPERSEDES: x\n'), 'SUPERSEDES', 2
    for key in ('SUPER\u200bSEDES', 'SUPERSEDES\U000e0041'):
        yield draft(body=key+': x'), 'SUPERSEDES', 1
    yield draft(body='\u200b```\nSUPERSEDES: x\n```'), 'SUPERSEDES', 1
    yield draft(body='**SUPERSEDES:** x'), None, None


def preserved_cases():
    for ending in ('\r\n', '\r'):
        yield draft(body='abc\u2028tail').replace('\n', ending), 'separator', 13
        yield draft('MERGE-GATE', body='DISPATCH MERGE\nVERDICT: no').replace('\n', ending), 'late-verdict', 14
    yield draft(body='SUPERSEDES: x').replace('\n', '\r'), 'late-control', 13
    for middle in ('~~~', '```'):
        for after in (False, True):
            body = '````\n'+middle+'\n'+('' if after else 'SUPERSEDES: x\n')+'````\n'
            if after:
                body += 'SUPERSEDES: x'
            yield draft(body=body), 'late-control' if after else None, 16 if after else None
    for indent in ('   ', '    '):
        yield draft(body=indent+'```\nSUPERSEDES: x\n'+indent+'```'), ('late-control' if len(indent)==4 else None), 14
    for marker, kind in (('1) ', 'late-control'), ('-', None)):
        yield draft(body=marker+'SUPERSEDES: x'), kind, 13
    for phase, token in (('MERGE-GATE', 'DISPATCH MERGE \t'), ('MERGE-GATE \t', 'DISPATCH MERGE')):
        yield draft(phase, body=token+'\nVERDICT: no'), 'late-verdict', 14
    yield draft('IMPL', extra='MERGE_AUTHORIZATION: approved\n', body='VERDICT: no'), None, None
    yield draft('MERGE-GATE', body='DISPATCH MERGE\nXVERDICT: no'), None, None
    yield 'abc\u2028tail', 'separator', 1
    yield draft('MERGE-GATE', body='```\nDISPATCH MERGE\n```\nVERDICT: no'), None, None
    yield draft('MERGE-GATE', body='VERDICT: no'), None, None


def scan_cases():
    yield from (text for text, _, _ in fence_region_cases())
    rng = random.Random(297)
    lines = ['```', '````', '`````', '~~~', '~~~~', '~~~~~', '```lang',
             '~~~`info', '```bad`info', '```x', '~~~ \t', 'SUPERSEDES: x', 'ordinary', '']
    for _ in range(200):
        yield draft(prefix='\n'.join(rng.choices(lines, k=8))+'\n',
                    body='\n'.join(rng.choices(lines, k=16)))


def linear_cases():
    for k, n in ((32, 3200), (600, 600000), (1200, 1200000)):
        yield draft(body='\n'.join('`'*length for length in range(k+2, 2, -1))+'\n'*n)


def repeated_outside(lines, block):
    """Independent R217 reference; deliberately repeated, not the fast algorithm."""
    result = []
    opening = re.compile(r'^ {0,3}(`{3,})[^`]*$|^ {0,3}(~{3,}).*$')
    closing = re.compile(r'^ {0,3}(`{3,}|~{3,})[ \t]*$')
    for indices in (range(block[0]), range(block[1], len(lines))):
        demoted = set()
        while True:
            visible, active, start = [], None, None
            for i in indices:
                if active is not None:
                    close = closing.fullmatch(lines[i])
                    if close and close[1][0] == active[0] and len(close[1]) >= len(active):
                        active = None
                else:
                    op = opening.fullmatch(lines[i]) if i not in demoted else None
                    if op:
                        active, start = op[1] or op[2], i
                    else:
                        visible.append((i, lines[i]))
            if active is None:
                result.extend(visible)
                break
            demoted.add(start)
    return result


class CountedLines(list):
    def __init__(self, lines, visits):
        super().__init__(lines)
        self.visits = visits

    def __iter__(self):
        for line in super().__iter__():
            self.visits[0] += 1
            yield line

    def __getitem__(self, index):
        result = super().__getitem__(index)
        if isinstance(index, slice):
            self.visits[0] += len(result)
            return CountedLines(result, self.visits)
        self.visits[0] += 1
        return result


class CountedText(str):
    def __new__(cls, text, visits):
        obj = super().__new__(cls, text)
        obj.visits = visits
        return obj

    def splitlines(self, keepends=False):
        return CountedLines(super().splitlines(keepends), self.visits)


class TestDraftShape(unittest.TestCase):
    def test_line_breaks_equal_splitlines_breaks(self):
        expected = frozenset(chr(c) for c in range(0x110000)
                             if len(('a' + chr(c) + 'b').splitlines()) == 2)
        self.assertEqual(E.LINE_BREAKS, expected)

    def test_separators_are_line_breaks_minus_lf_and_cr(self):
        self.assertEqual(set(E.SEPARATORS), E.LINE_BREAKS - {'\n', '\r'})
        for char, (code, _) in E.SEPARATORS.items():
            self.assertEqual(code, f'U+{ord(char):04X}')

    def test_each_separator_is_reported_with_line_and_column(self):
        for char in CHARS:
            for text, line, column in (
                    (draft().replace('SUBJECT: shape', 'SUBJECT: a' + char + 'b'), 11, 11),
                    (draft(body='abc' + char + 'tail'), 13, 4),
                    (draft(prefix='title' + char + '\n'), 1, 6)):
                with self.subTest(char=repr(char), line=line):
                    shape = E.draft_shape(text)
                    self.assertEqual((shape.kind, shape.line, shape.column, shape.char),
                                     ('separator', line, column, char))
                    self.assertEqual(E.first_separator(text), shape)

    def test_clean_lf_crlf_and_cr_drafts_have_no_shape(self):
        for ending in ('\n', '\r\n', '\r'):
            self.assertIsNone(E.draft_shape(draft(body='> ordinary note\n').replace('\n', ending)))

    def test_block_matches_parse_draft(self):
        for prefix, extra in (('', '- list\n'), ('', 'prose\n'),
                              ('# title\n\n', ''), ('\ufeff# title\n\n', ''), ('\n\n', '')):
            text = draft(prefix=prefix, extra=extra, body='SUPERSEDES: x\n')
            shape = E.draft_shape(text)
            first, last = shape.block
            pairs = [E._HEADER.fullmatch(x).groups() for x in text.splitlines()[first-1:last]]
            self.assertEqual(dict(pairs), E.parse_draft(text).headers)

    def test_late_control_forms_are_refused(self):
        for key in KEYS:
            for prefix in ('', '    ', '\t', '- ', '1. ', '> ', '> - '):
                text = draft(body=prefix + key + ': x\n')
                shape = E.draft_shape(text)
                self.assertEqual((shape.kind, shape.key, shape.line, shape.column, shape.block),
                                 ('late-control', key, 13, len(prefix)+1, (1, 11)))
            for text, line, block in (
                    (draft(extra='prose\n'+key+': x\n'), 13, (1, 11)),
                    (draft(prefix='    '+key+': x\n'), 1, (2, 12)),
                    (draft(body=key+': x\n').replace('\n', '\r\n'), 13, (1, 11))):
                shape = E.draft_shape(text)
                self.assertEqual((shape.kind, shape.key, shape.line, shape.block),
                                 ('late-control', key, line, block))

    def test_controls_in_block_fence_or_inline_code_are_not_refused(self):
        for key in KEYS:
            line = key + ': x'
            self.assertIsNone(E.draft_shape(draft(extra=line+'\n')))
            for fence in ('```', '~~~'):
                self.assertIsNone(E.draft_shape(draft(body=fence+'\n'+line+'\n'+fence)))
                self.assertIsNone(E.draft_shape(draft(extra=fence+'\n'+line+'\n'+fence+'\n')))
            for quoted in ('`'+line+'`', 'This mentions '+line):
                self.assertIsNone(E.draft_shape(draft(body=quoted)))

    def test_late_verdicts_on_grants_are_refused(self):
        for text, key in late_verdict_cases():
            with self.subTest(text=text):
                shape = E.draft_shape(text)
                self.assertEqual((shape.kind, shape.key), ('late-verdict', key))

    def test_unclosed_fence_exempts_nothing(self):
        for text, key in (
                (draft(extra='```\nCYCLE_TERMINUS: withdrawn\nFINAL_GIT_STATUS_SHORT: clean\n'), 'CYCLE_TERMINUS'),
                (draft(body='```\nSUPERSEDES: x'), 'SUPERSEDES'),
                (draft(body='```\n~~~\nSUPERSEDES: x'), 'SUPERSEDES')):
            shape = E.draft_shape(text)
            self.assertEqual((shape.kind, shape.key), ('late-control', key))
            self.assertEqual(text.splitlines()[shape.line-1].split(':')[0], key)

    def test_verdicts_that_stay_unrefused(self):
        for text in (draft('MERGE-GATE', extra='VERDICT: approved\n', body='DISPATCH MERGE'),
                     draft('MERGE-GATE', body='DISPATCH MERGE'),
                     draft('MERGE-GATE', body='RECOMMENDED_VERDICT: merge — merge PR #8'),
                     draft(body='VERDICT: no'),
                     draft('MERGE-GATE', body='```\nDISPATCH MERGE\n```\nVERDICT: no'),
                     draft('MERGE-GATE', body='- DISPATCH MERGE\n\nVERDICT: no')):
            self.assertIsNone(E.draft_shape(text))

    def test_first_shape_by_position(self):
        prefix = 'PHASE: SITREP\n\n' + '\n'*6
        text = prefix + 'SUPERSEDES: x\n' + '\n'*4 + 'a\u2028'
        self.assertEqual((E.draft_shape(text).kind, E.draft_shape(text).line), ('late-control', 9))
        text = prefix + 'a\u2028' + '\n'*4 + 'SUPERSEDES: x'
        self.assertEqual((E.draft_shape(text).kind, E.draft_shape(text).line), ('separator', 9))

    def test_same_line_late_control_and_separator(self):
        for char in ('\u2028', '\x85'):
            shape = E.draft_shape(draft(body='SUPERSEDES: x'+char))
            self.assertEqual((shape.kind, shape.line, shape.column), ('late-control', 13, 1))

    def test_same_line_late_verdict_and_separator(self):
        shape = E.draft_shape(draft('MERGE-GATE', body='DISPATCH MERGE\n\nVERDICT: merge-blocked\u2029'))
        self.assertEqual((shape.kind, shape.line, shape.column), ('late-verdict', 15, 1))

    def test_verdict_key_copy_matches_authority(self):
        for key in ('VERDICT', 'X_VERDICT', 'VERDICTS', 'MERGE_APPROVED',
                    'HUMAN_MERGE_AUTHORIZATION', 'MERGE_AUTHORIZATIONX',
                    'REVIEW_VERDICT_NOTE', 'RECOMMENDED_VERDICT'):
            self.assertEqual(E._shape_verdict_key(key), authority._verdict_key(key))

    def test_header_positive_copy_matches_authority(self):
        for key, value in itertools.product(
                ('MERGE_AUTHORIZATION', 'HUMAN_MERGE_AUTHORIZATION', 'MERGE_APPROVED', 'VERDICT', 'OTHER'),
                ('approved', 'Approved', 'granted', 'yes', ' yes ', 'true', 'complete',
                 'merge-ready', 'merge ready', 'approved — note', 'no', 'n/a')):
            self.assertEqual(E._shape_header_positive(key, value),
                             rules.merge_authorization_positive(key, value) and authority._positive_value(value))

    def test_shape_errors_render_the_table(self):
        cases = (
            (E.DraftShape('separator', 3, 4, '\u2028', None, None),
             'line 3, column 4: U+2028 LINE SEPARATOR is a line break to some readers and ordinary text to others',
             'use a newline there, or write the character as text, for example U+2028'),
            (E.DraftShape('late-control', 8, 1, None, 'SUPERSEDES', (1, 4)),
             'line 8: SUPERSEDES is outside the header block (lines 1 to 4), where the engine never reads it',
             'move the line into the header block, or quote it in code'),
            (E.DraftShape('late-verdict', 8, 1, None, 'VERDICT', (1, 4)),
             "line 8: VERDICT is outside this grant's header block (lines 1 to 4), so authority reads the grant as carrying no verdict",
             'move the line into the header block, where any value but an exact positive word denies the grant; or remove it; or quote it in code'))
        for shape, cause, remedy in cases:
            error = E.shape_error(shape)
            self.assertEqual((error.code, error.cls, error.cause, error.remedy),
                             ('E-DRAFT-SHAPE', 'integrity', cause, remedy))
            self.assertEqual(E.shape_message(shape), cause+'; '+remedy)
        error = E.shape_error(cases[0][0], boot_field='COMMISSIONED_BY')
        self.assertEqual(error.cause, 'the boot relay for this registration would carry a refused line shape in its COMMISSIONED_BY line')
        self.assertEqual(error.remedy, "the line is built from the registration's inputs and the root's stored commissioning record; re-register with a dispatch reference on one line, or ask the operator to re-commission the root with a commissioning path on one line")

    def test_late_verdict_keys_of_any_length_render(self):
        import tempfile
        from pathlib import Path
        from relay_engine.tests.test_parity import _standalone
        hand = _standalone()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'MERGE-GATE-pair-planner-20261026-010000.md'
            for length in (64, 65, 4096):
                with self.subTest(length=length):
                    key = 'A' * (length - len('_VERDICT')) + '_VERDICT'
                    text = draft('MERGE-GATE', body='DISPATCH MERGE\n\n' + key + ': no\n')
                    shape = E.draft_shape(text)
                    self.assertEqual((shape.kind, shape.key), ('late-verdict', key))
                    error = E.shape_error(shape)
                    self.assertEqual((error.code, error.cause_key),
                                     ('E-DRAFT-SHAPE', 'error-draft-shape-late-verdict-cause'))
                    self.assertIn(key, error.cause)
                    expected = error.cause + '; ' + error.remedy
                    self.assertEqual(rules.draft_shape_errors(text), [expected])
                    path.write_text(text)
                    messages = [e for e in hand.lint_file(path).errors
                                if "outside this grant's header block" in e]
                    self.assertEqual(messages, [expected])

    def test_long_verdict_keys_are_refused_by_every_consumer(self):
        import tempfile
        from pathlib import Path
        from relay_engine import daemon, errors
        from relay_engine.tests import test_admission_judging as admission
        from relay_engine.tests.test_parity import _standalone
        hand = _standalone()
        fixture = admission.TestAdmissionJudging()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'MERGE-GATE-pair-planner-20261026-010000.md'
            for length in (65, 4096):
                key = 'A' * (length - len('_VERDICT')) + '_VERDICT'
                raw = admission.body('MERGE-GATE', 'operator',
                                     content='DISPATCH MERGE\n\n' + key + ': no')
                before = fixture.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                with self.subTest(length=length, consumer='handler'):
                    with self.assertRaises(errors.EngineError) as caught:
                        daemon._submit_handler(fixture.ledger, fixture.root, fixture.args(raw))
                    self.assertEqual((caught.exception.code, caught.exception.cause_key),
                                     ('E-DRAFT-SHAPE', 'error-draft-shape-late-verdict-cause'))
                    self.assertIn(key, caught.exception.cause)
                    self.assertEqual(fixture.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
                path.write_bytes(raw)
                for consumer in ('file', 'root'):
                    with self.subTest(length=length, consumer=consumer):
                        expected = hand.draft_shape_errors(raw.decode())[0]
                        result = (rules.lint_file(path) if consumer == 'file'
                                  else rules.lint_relay_root(root, engine_root=False))
                        messages = [e for e in result.errors if "outside this grant's header block" in e]
                        self.assertEqual(messages, [expected if consumer == 'file' else path.name + ': ' + expected])

    def test_shape_validators_refuse_values_outside_the_grammar(self):
        from relay_engine import errors
        for key in ('x_VERDICT', 'VERDICTS', 'A_VERDICT\u2028'):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    errors.error_for('E-DRAFT-SHAPE', variant='late-verdict',
                                     line=4, first=1, last=2, key=key)
        for field in ('', 'commissioned_by', 'A B'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    errors.error_for('E-DRAFT-SHAPE', variant='boot-field', field=field)
        for field in ('body', 'COMMISSIONED_BY'):
            error = errors.error_for('E-DRAFT-SHAPE', variant='boot-field', field=field)
            self.assertEqual(error.code, 'E-DRAFT-SHAPE')
            self.assertIn(field, error.cause)

    def test_a_fence_cannot_span_the_header_block(self):
        for text, kind, key in fence_region_cases():
            with self.subTest(kind=kind, key=key):
                shape = E.draft_shape(text)
                self.assertIsNotNone(shape)
                self.assertEqual((shape.kind, shape.key), (kind, key))
                self.assertEqual(text.splitlines()[shape.line-1], key+(': merge-blocked' if key=='VERDICT' else ': withdrawn'))

    def test_fence_lines_take_commonmark_forms(self):
        for text, kind in fence_form_cases():
            with self.subTest(text=text):
                shape = E.draft_shape(text)
                self.assertEqual(None if shape is None else shape.kind, kind)

    def test_wide_and_ignorable_characters_are_set_aside(self):
        for text, key, column in wide_cases():
            with self.subTest(text=text):
                shape = E.draft_shape(text)
                if key is None:
                    self.assertIsNone(shape)
                else:
                    self.assertIsNotNone(shape)
                    self.assertEqual((shape.key, shape.column), (key, column))
                    self.assertEqual(shape.kind, 'late-verdict' if key=='VERDICT' else 'late-control')

    def test_set_aside_table(self):
        from relay_engine.tests.test_parity import _standalone
        ranges = ((9,9),(32,32),(0xa0,0xa0),(0x1680,0x1680),(0x2000,0x200a),
                  (0x202f,0x202f),(0x205f,0x205f),(0x3000,0x3000),
                  (0xad,0xad),(0x34f,0x34f),(0x61c,0x61c),(0x115f,0x1160),
                  (0x17b4,0x17b5),(0x180b,0x180f),(0x200b,0x200f),(0x202a,0x202e),
                  (0x2060,0x206f),(0x3164,0x3164),(0xfe00,0xfe0f),(0xfeff,0xfeff),
                  (0xffa0,0xffa0),(0xfff0,0xfff8),(0x1bca0,0x1bca3),
                  (0x1d173,0x1d17a),(0xe0000,0xe0fff))
        expected = frozenset(chr(c) for a,b in ranges for c in range(a,b+1))
        self.assertEqual(E.SHAPE_SET_ASIDE, expected)
        self.assertEqual(_standalone().SHAPE_SET_ASIDE, expected)

    def test_one_pass_equals_the_repeated_scan(self):
        from unittest import mock
        for text in scan_cases():
            with self.subTest(text=text):
                actual = E.draft_shape(text)
                with mock.patch.object(E, '_shape_outside', repeated_outside):
                    expected = E.draft_shape(text)
                self.assertEqual(actual, expected)

    def test_fence_scan_is_linear(self):
        for text in linear_cases():
            visits = [0]
            self.assertIsNone(E.draft_shape(CountedText(text, visits)))
            self.assertLessEqual(visits[0], 12*len(text.splitlines()))

    def test_previously_unpinned_rules(self):
        for text, kind, line in preserved_cases():
            with self.subTest(text=text):
                shape = E.draft_shape(text)
                self.assertEqual(None if shape is None else shape.kind, kind)
                if kind:
                    self.assertEqual(shape.line, line)
