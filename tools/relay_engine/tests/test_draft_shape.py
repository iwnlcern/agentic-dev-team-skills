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


def verdict_keys(lengths):
    for length in lengths:
        yield 'A' * (length - len('_VERDICT')) + '_VERDICT'
    yield 'A1_VERDICT'
    yield 'A1' + '2' * 55 + '_VERDICT'


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
    for key in verdict_keys((64,65,4096)):
        yield draft('MERGE-GATE', body='DISPATCH MERGE\n\n'+key+': no\n')
        if len(key) != 64:
            yield admission.body('MERGE-GATE','operator',content='DISPATCH MERGE\n\n'+key+': no').decode()
    for cases in (fence_region_cases(), fence_form_cases(), wide_cases(), preserved_cases()):
        for case in cases:
            yield case[0]
    yield from (text for _, text, _ in R8_SHOWN_CASES)
    for char, marker, verdict in itertools.product(
            '\x01\x1b\x1f\x7f\x80\x9b\u0600\u06dd\ufff9\U000110bd\u2800', ('', '- '), (False, True)):
        key = 'VERDICT' if verdict else 'SUPERSEDES'
        line = marker+char+key+': x'
        yield draft('MERGE-GATE' if verdict else 'SITREP', body=('DISPATCH MERGE\n\n' if verdict else '')+line)
        yield draft(body='```\n'+line+'\n```')
    for body in ('1\u200b. SUPERSEDES: x', '1\x1f) SUPERSEDES: x', 'SUPER\x1fSEDES: x', 'SUPERSEDES:\x1f x'):
        yield draft(body=body)
    for stop in ('- note','* note','+ note','2) step','> quote','-','<!-- c -->','- - -','</div>','<?x>','<div>',
                 '    - x','-not','1.not','< not html'):
        yield draft(body=stop+'\n  ```\nSUPERSEDES: x\n  ```')
    for prefix in ('1. step\n```\n  SUPERSEDES: x\n```\n',
                   '\ufeff```\nx\n```\n  SUPERSEDES: x\n```\n',
                   '\ufeff```\n  SUPERSEDES: x\n```\n'):
        yield draft(prefix=prefix)
    for cp in (0x16130,0x1e4f0,0x11f50,0x0661,0x1d7ce,0x1fbf1,0x10d30,0x1e5f1,0x16d70):
        yield draft(body=chr(cp)+'. x\n```\nSUPERSEDES: y\n```')
        yield draft(body=chr(cp)+'. SUPERSEDES: y')
    for before in ('    - x', '-not', '1.not', '< not html'):
        yield draft(body=before+'\n```\nSUPERSEDES: x\n```')
    yield draft(body='```\nSUPERSEDES: x\n```\n- note')
    yield draft(body='```\n- item\nSUPERSEDES: x\n```')
    yield draft(prefix='```\n  SUPERSEDES: x\n```\n', body='- item')
    yield draft('MERGE-GATE', body='DISPATCH MERGE\n\n- note\n```\nVERDICT: merge-blocked\n```')
    yield draft('MERGE-GATE', body='DISPATCH MERGE').replace('SUBJECT: shape', 'SUBJECT: s see VERDICT: no')
    yield admission.body('SITREP', content='**SUPERSEDES:** x').decode()
    yield admission.body('MERGE-GATE','operator',content='DISPATCH MERGE\n\n(PLAN_REVIEW_VERDICT: approve, at x)').decode()
    yield admission.body('SITREP').decode().replace('SUBJECT: test candidate', 'SUBJECT: s see SUPERSEDES: x')
    from relay_engine import seats
    for ref, stored in (('note SUPERSEDES: x',None),('SUPERSEDES：x',None),
                        ('a&colon;b CYCLE_REOPEN&#58; c',None),('v297-b2-impl-6','see SUPERSEDES: x'),
                        ('v297-b2-impl-6',None)):
        yield seats._boot_bytes('p.planner','Planner','r',ref,'occupant',stored).decode()
    for make in closed_linear_families():
        for n in (4000,16000,2000000):
            yield draft(body=make(n))
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
    yield draft(body='**SUPERSEDES:** x'), 'SUPERSEDES', 13


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
    for marker, kind in (('1) ', 'late-control'), ('-', 'late-control')):
        yield draft(body=marker+'SUPERSEDES: x'), kind, 13
    for phase, token in (('MERGE-GATE', 'DISPATCH MERGE \t'), ('MERGE-GATE \t', 'DISPATCH MERGE')):
        yield draft(phase, body=token+'\nVERDICT: no'), 'late-verdict', 14
    yield draft('IMPL', extra='MERGE_AUTHORIZATION: approved\n', body='VERDICT: no'), None, None
    yield draft('MERGE-GATE', body='DISPATCH MERGE\nXVERDICT: no'), 'late-verdict', 14
    yield 'abc\u2028tail', 'separator', 1
    yield draft('MERGE-GATE', body='```\nDISPATCH MERGE\n```\nVERDICT: no'), None, None
    yield draft('MERGE-GATE', body='VERDICT: no'), None, None


def scan_cases():
    yield from (text for text, _, _ in fence_region_cases())
    rng = random.Random(297)
    lines = ['```', '````', '`````', '~~~', '~~~~', '~~~~~', '```lang',
             '~~~`info', '```bad`info', '```x', '~~~ \t', 'SUPERSEDES: x', 'ordinary', '',
             '- item', '+ item', '* item', '1. item', '1) item', '-', '>', '<div>', '</div>', '<!-- x -->', '<?x>', '\ufeff```']
    for _ in range(200):
        yield draft(prefix='\n'.join(rng.choices(lines, k=8))+'\n',
                    body='\n'.join(rng.choices(lines, k=16)))


def linear_cases():
    for k, n in ((32, 3200), (600, 600000), (1200, 1200000)):
        yield draft(body='\n'.join('`'*length for length in range(k+2, 2, -1))+'\n'*n)


def closed_linear_families():
    def runs(n, paired):
        parts, total, length = [], 0, 2 if paired else 1
        while total+length+1 <= n//2:
            parts.append('`'*length+' ')
            total += length+1
            length += 1
        return (''.join(parts)+('`x` ' if paired else 'x')*n)[:n]
    families = (lambda n: ('<a>'*((n+2)//3))[:n],
                lambda n: '<a'*(n//4)+'>'*(n//2),
                lambda n: ']('*(n//4)+')'*(n//2),
                lambda n: '!['*(n//4)+']'*(n//2),
                lambda n: '[x]('*(n//8)+')'*(n//2),
                lambda n: runs(n, True), lambda n: runs(n, False),
                lambda n: ('SUPER'*((n+4)//5))[:n])
    return families


def repeated_outside(lines, block):
    """Independent repeated scan of erratum 2.2, with local raw patterns."""
    opening = re.compile(r'^ {0,3}(`{3,})[^`]*$|^ {0,3}(~{3,}).*$')
    closing = re.compile(r'^ {0,3}(`{3,}|~{3,})[ \t]*$')
    stop = re.compile(r'^ {0,3}(?:(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$)|>|<[A-Za-z/!?])')
    # Independently spelled prefix table, rather than the implementation's regex.
    spaces = '\t \xa0\u1680' + ''.join(chr(c) for c in range(0x2000,0x200b))+'\u202f\u205f\u3000'
    ranges = ((0xad,0xad),(0x34f,0x34f),(0x61c,0x61c),(0x115f,0x1160),(0x17b4,0x17b5),
              (0x180b,0x180f),(0x200b,0x200f),(0x202a,0x202e),(0x2060,0x206f),(0x3164,0x3164),
              (0xfe00,0xfe0f),(0xfeff,0xfeff),(0xffa0,0xffa0),(0xfff0,0xfff8),
              (0x1bca0,0x1bca3),(0x1d173,0x1d17a),(0xe0000,0xe0fff))
    chars = spaces+''.join(chr(c) for a,b in ranges for c in range(a,b+1))
    w = '['+re.escape(chars)+']'
    prefix = re.compile('^'+w+'*(?:(?:[-*+]|[0-9]{1,9}[.)]|>)'+w+'+|>)*'+w+'*')
    result, carried = [], False
    for side, indices in enumerate((range(block[0]), range(block[1], len(lines)))):
        demoted = set()
        while True:
            visible, active, start = [], None, None
            stopped = carried if side else False
            for i in indices:
                if active is not None:
                    close = closing.fullmatch(lines[i])
                    if close and close[1][0] == active[0] and len(close[1]) >= len(active):
                        active = None
                else:
                    stopped |= bool(stop.match(lines[i]) or (i == 0 and lines[i].startswith('\ufeff')))
                    op = opening.fullmatch(lines[i]) if not stopped and i not in demoted else None
                    if op:
                        active, start = op[1] or op[2], i
                    else:
                        visible.append((i, lines[i]))
            if active is None:
                result.extend(visible)
                if not side:
                    carried = any(stop.match(line) or (i == 0 and line.startswith('\ufeff')) or
                                  opening.fullmatch(line) or re.match(r'<[A-Za-z/!?]', line[prefix.match(line).end():])
                                  for i,line in visible)
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
            self.assertIsNone(E.draft_shape(draft(body='`'+line+'`')))
            shape = E.draft_shape(draft(body='This mentions '+line))
            self.assertIsNotNone(shape)
            self.assertEqual((shape.kind, shape.key, shape.line, shape.column),
                             ('late-control', key, 13, 15+len(key)))

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
             'move the line into the header block, or quote it in code; to mention the key, put it and its colon in inline code'),
            (E.DraftShape('late-verdict', 8, 1, None, 'VERDICT', (1, 4)),
             "line 8: VERDICT is outside this grant's header block (lines 1 to 4), so authority reads the grant as carrying no verdict",
             'move the line into the header block, where any value but an exact positive word denies the grant; or remove it; or quote it in code; to mention the key, put it and its colon in inline code'))
        for shape, cause, remedy in cases:
            error = E.shape_error(shape)
            self.assertEqual((error.code, error.cls, error.cause, error.remedy),
                             ('E-DRAFT-SHAPE', 'integrity', cause, remedy))
            self.assertEqual(E.shape_message(shape), cause+'; '+remedy)
        error = E.shape_error(cases[0][0], boot_field='COMMISSIONED_BY')
        self.assertEqual(error.cause, 'the boot relay for this registration would carry a refused line shape in its COMMISSIONED_BY line')
        self.assertEqual(error.remedy, "the line is built from the registration's inputs and the root's stored commissioning record; re-register with a dispatch reference on one line that shows no cycle or supersession key with a colon, or ask the operator to re-commission the root with a commissioning path on one line that shows none")

    def test_late_verdict_keys_of_any_length_render(self):
        import tempfile
        from pathlib import Path
        from relay_engine.tests.test_parity import _standalone
        hand = _standalone()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'MERGE-GATE-pair-planner-20261026-010000.md'
            for key in verdict_keys((64, 65, 4096)):
                with self.subTest(key=key):
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
            for key in verdict_keys((65, 4096)):
                raw = admission.body('MERGE-GATE', 'operator',
                                     content='DISPATCH MERGE\n\n' + key + ': no')
                before = fixture.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0]
                with self.subTest(key=key, consumer='handler'):
                    with self.assertRaises(errors.EngineError) as caught:
                        daemon._submit_handler(fixture.ledger, fixture.root, fixture.args(raw))
                    self.assertEqual((caught.exception.code, caught.exception.cause_key),
                                     ('E-DRAFT-SHAPE', 'error-draft-shape-late-verdict-cause'))
                    self.assertIn(key, caught.exception.cause)
                    self.assertEqual(fixture.ledger.execute('SELECT COUNT(*) FROM relays').fetchone()[0], before)
                path.write_bytes(raw)
                for consumer in ('file', 'root'):
                    with self.subTest(key=key, consumer=consumer):
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


# Literal expected answers from plan r8 task 9e; no runtime oracle dependency.
H = ("ROLE: Pair Planner\nPHASE: {phase}\nAUTHORITY: report-only\nDISPATCH_ID: d\nRUN_ID: r\n"
     "CEREMONY_TIER: small\nEVIDENCE_TARGET: E2\nHUMAN_GATE_REQUIRED: no\nFROM: p.planner\n"
     "TO: p.implementer\nSUBJECT: s\n")

def sitrep(body, before=""):
    return before + H.format(phase="SITREP") + "\n" + body + "\n"

def grant(body):
    return H.format(phase="MERGE-GATE") + "\nDISPATCH MERGE\n\n" + body + "\n"

C = "late-control"

V = "late-verdict"

R8_SHOWN_CASES = [
    # the scan's positives: (name, text, (kind, key, line, column) or None)
    ("emphasis", sitrep("**SUPERSEDES:** x"), (C, "SUPERSEDES", 13, 13)),
    ("glyph bullet", sitrep("• SUPERSEDES: x"), (C, "SUPERSEDES", 13, 13)),
    ("letter enumerator", sitrep("a. SUPERSEDES: x"), (C, "SUPERSEDES", 13, 14)),
    ("ten-digit enumerator", sitrep("1234567890. SUPERSEDES: x"), (C, "SUPERSEDES", 13, 23)),
    ("dash no space", sitrep("-SUPERSEDES: x"), (C, "SUPERSEDES", 13, 12)),
    ("cyrillic E", sitrep("SUPERSЕDES: x"), (C, "SUPERSEDES", 13, 11)),
    ("fullwidth colon", sitrep("SUPERSEDES： x"), (C, "SUPERSEDES", 13, 11)),
    ("prose", sitrep("This mentions SUPERSEDES: x"), (C, "SUPERSEDES", 13, 25)),
    ("XVERDICT grant", grant("XVERDICT: no"), (V, "VERDICT", 15, 9)),
    ("cited review verdict", grant("(PLAN_REVIEW_VERDICT: approve, at x)"), (V, "VERDICT", 15, 21)),
    ("image in key", sitrep("SUPER![i](s)SEDES: x"), (C, "SUPERSEDES", 13, 18)),
    ("tag in key", sitrep("SUPER<b></b>SEDES: x"), (C, "SUPERSEDES", 13, 18)),
    ("inline link tail", sitrep("[SUPER](u)SEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("reference label", sitrep("[SUPER][r]SEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("comment in key", sitrep("SUPER<!-- c -->SEDES: x"), (C, "SUPERSEDES", 13, 21)),
    ("decimal reference", sitrep("&#83;UPERSEDES: x"), (C, "SUPERSEDES", 13, 15)),
    ("hex reference", sitrep("&#x53;UPERSEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("reference colon", sitrep("SUPERSEDES&#58; x"), (C, "SUPERSEDES", 13, 11)),
    ("named colon", sitrep("SUPERSEDES&colon; x"), (C, "SUPERSEDES", 13, 11)),
    ("named zero width", sitrep("SUPER&ZeroWidthSpace;SEDES: x"), (C, "SUPERSEDES", 13, 27)),
    ("named soft hyphen", sitrep("SUPER&shy;SEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("soft break", sitrep("SUPER\nSEDES: x"), (C, "SUPERSEDES", 14, 6)),
    ("header value", sitrep("x").replace("SUBJECT: s", "SUBJECT: s see SUPERSEDES: x"), (C, "SUPERSEDES", 11, 26)),
    ("fullwidth underscore", sitrep("CYCLE＿TERMINUS: x"), (C, "CYCLE_TERMINUS", 13, 15)),
    ("digit one", sitrep("CYCLE_TERM1NUS: x"), (C, "CYCLE_TERMINUS", 13, 15)),
    ("bar", sitrep("CYCLE_TERM|NUS: x"), (C, "CYCLE_TERMINUS", 13, 15)),
    ("digit zero", sitrep("CYCLE_RE0PEN: x"), (C, "CYCLE_REOPEN", 13, 13)),
    ("math bold S", sitrep("\U0001d412UPERSEDES: x"), (C, "SUPERSEDES", 13, 11)),
    ("fullwidth S", sitrep("ＳUPERSEDES: x"), (C, "SUPERSEDES", 13, 11)),
    ("circled S", sitrep("ⓈUPERSEDES: x"), (C, "SUPERSEDES", 13, 11)),
    ("tie", grant("VERDICT<b>CYCLE_TERMINUS</b>: x"), (C, "CYCLE_TERMINUS", 15, 29)),
    # inline code that does not qualify
    ("unpaired run", sitrep("a ` b `SUPERSEDES: x`"), (C, "SUPERSEDES", 13, 18)),
    ("escaped backtick", sitrep("\\`SUPERSEDES: x`"), (C, "SUPERSEDES", 13, 13)),
    ("tag outside spans", sitrep("a <b> `SUPERSEDES: x`"), (C, "SUPERSEDES", 13, 18)),
    ("link outside spans", sitrep("[x](u) `SUPERSEDES: x`"), (C, "SUPERSEDES", 13, 19)),
    ("html start before", sitrep("<div>\n\n`SUPERSEDES: x`"), (C, "SUPERSEDES", 15, 12)),
    ("split at span edge", sitrep("`SUPERSEDES`: x"), (C, "SUPERSEDES", 13, 13)),
    ("split over two spans", sitrep("`SUPER` `SEDES: x`"), (C, "SUPERSEDES", 13, 15)),
    # fences CommonMark carries across the block
    ("list item touching", "- item\n" + H.format(phase="SITREP") + "  ```\nSUPERSEDES: x\n  ```\n", (C, "SUPERSEDES", 14, 1)),
    ("unclosed opener before", "```\n" + H.format(phase="SITREP") + "~~~\n```\nSUPERSEDES: x\n~~~\n", (C, "SUPERSEDES", 15, 1)),
    ("html block before", "<div>\n" + H.format(phase="SITREP") + "\n```\nSUPERSEDES: x\n```\n", (C, "SUPERSEDES", 15, 1)),
    ("grant token inside a carried fence", "- item\n" + H.format(phase="MERGE-GATE") + "  ```\nDISPATCH MERGE\nVERDICT: no\n  ```\n", (V, "VERDICT", 15, 1)),
    # added after the tests lens (panel-1/plan8-tests/REPORT-as-received.md)
    ("tab in key", sitrep("SUPER\tSEDES: x"), (C, "SUPERSEDES", 13, 12)),
    ("hex reference, upper X", sitrep("&#X53;UPERSEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("seven-digit decimal reference", sitrep("&#0000083;UPERSEDES: x"), (C, "SUPERSEDES", 13, 20)),
    ("six-digit hex reference", sitrep("&#x000053;UPERSEDES: x"), (C, "SUPERSEDES", 13, 20)),
    ("label outside spans", sitrep("[x][`] b` c `SUPERSEDES: x`\n\n[`]: /u"), (C, "SUPERSEDES", 13, 24)),
    ("unicode space line is not blank", sitrep("a `\n　\n`SUPERSEDES: x`"), (C, "SUPERSEDES", 15, 12)),
    ("two-letter map value, authorization", grant("see MERGE_AUTHORIZATЮN: no"), (V, "MERGE_AUTHORIZATION", 15, 23)),
    ("two-letter map value, approved", grant("see MERGE_AP㏚OVED: yes"), (V, "MERGE_APPROVED", 15, 18)),
    ("html start inside a list item", sitrep("- <pre>\n\n  `SUPERSEDES: x`"), (C, "SUPERSEDES", 15, 14)),
    ("html block joins across a blank line", sitrep("<pre>\nSUPER<!--\n\n-->SEDES: x\n</pre>"), (C, "SUPERSEDES", 16, 9)),
    ("processing instruction in key", sitrep("SUPER<?x?>SEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("end tag in key", sitrep("SUPER</b>SEDES: x"), (C, "SUPERSEDES", 13, 15)),
    ("tie by order, not alphabet", sitrep("SUPERSEDES<b>CYCLE_TERMINUS</b>: x"), (C, "SUPERSEDES", 13, 32)),
    ("tie by order on a grant", grant("VERDICT<b>MERGE_APPROVED</b>: x"), (V, "VERDICT", 15, 29)),
    ("a gap leaves the code span", sitrep("`SUPER![` x `]SEDES: x`"), (C, "SUPERSEDES", 13, 20)),
    ("tildes in key", sitrep("~~SUPER~~SEDES: x"), (C, "SUPERSEDES", 13, 15)),
    ("format character in emphasis", sitrep("**SUPER￹SEDES:** x"), (C, "SUPERSEDES", 13, 14)),
    ("escaped colon", sitrep("see SUPERSEDES\\: x"), (C, "SUPERSEDES", 13, 16)),
    ("escaped underscore", sitrep("CYCLE\\_TERMINUS: x"), (C, "CYCLE_TERMINUS", 13, 16)),
    ("no-break space in key", sitrep("see SUPER SEDES: x"), (C, "SUPERSEDES", 13, 16)),
    ("merge approved in prose", grant("see MERGE_APPROVED: yes"), (V, "MERGE_APPROVED", 15, 19)),
    ("emphasised merge authorization", grant("**MERGE_AUTHORIZATION:** no"), (V, "MERGE_AUTHORIZATION", 15, 22)),
    ("unequal runs do not pair", sitrep("`a`` b `SUPERSEDES: x`"), (C, "SUPERSEDES", 13, 19)),
    ("qualification is per paragraph", sitrep("a `\n`SUPERSEDES: x`"), (C, "SUPERSEDES", 14, 12)),
    ("in block, first line", sitrep("x").replace("ROLE: Pair Planner", "ROLE: Pair Planner see SUPERSEDES: x"), (C, "SUPERSEDES", 1, 34)),
    ("empty image description", sitrep("SUPER![](s)SEDES: x"), (C, "SUPERSEDES", 13, 17)),
    ("image reference", sitrep("SUPER![i][r]SEDES: x"), (C, "SUPERSEDES", 13, 18)),
    ("code in image description", sitrep("SUPER![`i`](s)SEDES: x"), (C, "SUPERSEDES", 13, 20)),
    ("star bullet stop line", sitrep("* note\n  ```\nSUPERSEDES: x\n  ```"), (C, "SUPERSEDES", 15, 1)),
    ("plus bullet stop line", sitrep("+ note\n  ```\nSUPERSEDES: x\n  ```"), (C, "SUPERSEDES", 15, 1)),
    ("end tag stop line", sitrep("</div>\n```\nSUPERSEDES: x\n```"), (C, "SUPERSEDES", 15, 1)),
    ("processing instruction stop line", sitrep("<?x>\n```\nSUPERSEDES: x\n```\n?>"), (C, "SUPERSEDES", 15, 1)),
    ("tilde opener before the block", "~~~\n" + H.format(phase="SITREP") + "```\n~~~\nSUPERSEDES: x\n```\n", (C, "SUPERSEDES", 15, 1)),
    ("BOM stop line", "﻿```\n  SUPERSEDES: x\n```\n" + H.format(phase="SITREP"), (C, "SUPERSEDES", 2, 3)),
    ("BOM pair without the BOM", "```\n  SUPERSEDES: x\n```\n" + H.format(phase="SITREP"), None),
    # negatives
    ("inline code", sitrep("`SUPERSEDES: x`"), None),
    ("double inline code", sitrep("`` SUPERSEDES: x ``"), None),
    ("case", sitrep("Supersedes: x"), None),
    ("no colon", sitrep("SUPERSEDES x"), None),
    ("visible diacritic", sitrep("SUPERSÉDES: x"), None),
    ("hyphen", sitrep("CYCLE-TERMINUS: x"), None),
    ("lt reference", sitrep("SUPER&lt;SEDES: x"), None),
    ("zero reference", sitrep("SUPERSEDES&#0;: x"), None),
    ("surrogate reference", sitrep("SUPERSEDES&#xD800;: x"), None),
    ("too large reference", sitrep("SUPERSEDES&#1114112;: x"), None),
    ("non-grant citation", sitrep("(PLAN_REVIEW_VERDICT: approve, at x)"), None),
    ("closed fence after", sitrep("```\nSUPERSEDES: x\n```"), None),
    ("closed fence before, fence after", "```\nx\n```\n" + H.format(phase="SITREP") + "\n```\nSUPERSEDES: x\n```\n", None),
    ("supplementary digit", sitrep("\U00016130. x\n```\nSUPERSEDES: x\n```"), None),
    ("arabic-indic digit", sitrep("١. x\n```\nSUPERSEDES: x\n```"), None),
    ("header own key", sitrep("x").replace("SUBJECT: s", "SUPERSEDES: v297-b2/X.md\nSUBJECT: s"), None),
]

class TestClosedRule(unittest.TestCase):
    def test_the_scan_refuses_shown_keys(self):
        # Missing scanning, wrong source positions, and incorrect exemptions fail.
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            for name, text, expected in R8_SHOWN_CASES:
                with self.subTest(copy=module.__name__, case=name):
                    shape = module.draft_shape(text)
                    actual = None if shape is None else (shape.kind, shape.key, shape.line, shape.column)
                    self.assertEqual(actual, expected)

    def test_set_aside_class_matches_its_table(self):
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            pattern = re.compile(module._SHAPE_W)
            actual = frozenset(chr(c) for c in range(0x110000) if pattern.fullmatch(chr(c)))
            self.assertEqual(actual, module.SHAPE_SET_ASIDE)

    def test_set_aside_class_is_built_from_ranges(self):
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            self.assertLess(len(module._SHAPE_W), 512)

    def test_invisible_characters_before_the_colon_are_removed(self):
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            for char, marker, verdict in itertools.product(
                    '\x01\x1b\x1f\x7f\x80\x9b\u0600\u06dd\ufff9\U000110bd\u2800', ('', '- '), (False, True)):
                key = 'VERDICT' if verdict else 'SUPERSEDES'
                prefix = marker+char
                body = ('DISPATCH MERGE\n\n' if verdict else '')+prefix+key+': x'
                text = draft('MERGE-GATE' if verdict else 'SITREP', body=body)
                with self.subTest(copy=module.__name__, prefix=prefix, verdict=verdict):
                    shape = module.draft_shape(text)
                    self.assertIsNotNone(shape)
                    self.assertEqual((shape.kind, shape.key, shape.column),
                                     ('late-verdict' if verdict else 'late-control', key, len(prefix)+1))
                    self.assertIsNone(module.draft_shape(draft(body='```\n'+prefix+key+': x\n```')))
            for body, column in (('1\u200b. SUPERSEDES: x', 5), ('1\x1f) SUPERSEDES: x', 5),
                                 ('SUPER\x1fSEDES: x', 1), ('SUPERSEDES:\x1f x', 1)):
                shape = module.draft_shape(draft(body=body))
                self.assertIsNotNone(shape)
                self.assertEqual((shape.key, shape.column), ('SUPERSEDES', column))

    def test_answers_do_not_depend_on_the_interpreters_unicode(self):
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            for c in (0x16130, 0x1e4f0, 0x11f50, 0x0661, 0x1d7ce, 0x1fbf1, 0x10d30, 0x1e5f1, 0x16d70):
                with self.subTest(copy=module.__name__, cp=c):
                    self.assertIsNone(module.draft_shape(draft(body=chr(c)+'. x\n```\nSUPERSEDES: y\n```')))
                    shape = module.draft_shape(draft(body=chr(c)+'. SUPERSEDES: y'))
                    self.assertIsNotNone(shape)
                    self.assertEqual((shape.kind, shape.key, shape.column), ('late-control', 'SUPERSEDES', 14))

    def test_key_map_table(self):
        from relay_engine.tests.test_parity import _standalone
        self.assertEqual(E.SHAPE_KEY_MAP, _standalone().SHAPE_KEY_MAP)
        self.assertEqual(len(E.SHAPE_KEY_MAP), 1914)
        for chars, want in (('：∶', ':'), ('＿﹍', '_'), ('ЕΕ', 'E'), ('Ｓ𝐒Ⓢ', 'S'), ('0','O'), ('1I|','l'), ('m','rn')):
            for char in chars:
                self.assertEqual(E.SHAPE_KEY_MAP[char], want)
        for char in ('É', '-'):
            self.assertNotIn(char, E.SHAPE_KEY_MAP)
        self.assertTrue(all(re.fullmatch('[A-Za-z0-9:_]+', value) for value in E.SHAPE_KEY_MAP.values()))

    def test_entity_table(self):
        from relay_engine.tests.test_parity import _standalone
        self.assertEqual(E.SHAPE_ENTITY_MAP, _standalone().SHAPE_ENTITY_MAP)
        self.assertEqual(len(E.SHAPE_ENTITY_MAP), 349)
        for name, value in (('colon', ':'), ('ZeroWidthSpace', '\u200b'), ('shy', '\xad')):
            self.assertEqual(E.SHAPE_ENTITY_MAP[name], value)
        for name in ('lt','LT','gt','GT','excl','grave','DiacriticalGrave','lbrack','lsqb','rbrack','rsqb'):
            self.assertNotIn(name, E.SHAPE_ENTITY_MAP)

    def test_removal_table(self):
        from relay_engine.tests.test_parity import _standalone
        cf = ((0xad,0xad),(0x600,0x605),(0x61c,0x61c),(0x6dd,0x6dd),(0x70f,0x70f),
              (0x890,0x891),(0x8e2,0x8e2),(0x180e,0x180e),(0x200b,0x200f),(0x202a,0x202e),
              (0x2060,0x2064),(0x2066,0x206f),(0xfeff,0xfeff),(0xfff9,0xfffb),
              (0x110bd,0x110bd),(0x110cd,0x110cd),(0x13430,0x1343f),(0x1bca0,0x1bca3),
              (0x1d173,0x1d17a),(0xe0001,0xe0001),(0xe0020,0xe007f))
        dicp = ((0xad,0xad),(0x34f,0x34f),(0x61c,0x61c),(0x115f,0x1160),
                (0x17b4,0x17b5),(0x180b,0x180f),(0x200b,0x200f),(0x202a,0x202e),
                (0x2060,0x206f),(0x3164,0x3164),(0xfe00,0xfe0f),(0xfeff,0xfeff),
                (0xffa0,0xffa0),(0xfff0,0xfff8),(0x1bca0,0x1bca3),(0x1d173,0x1d17a),(0xe0000,0xe0fff))
        expected = frozenset(chr(c) for a,b in cf+dicp+((0,8),(10,31),(127,159),(0x2800,0x2800)) for c in range(a,b+1))
        self.assertEqual(len(expected), 4271)
        for module in (E, _standalone()):
            self.assertEqual(module.SHAPE_REMOVAL, expected)
            self.assertNotIn('\t', module.SHAPE_REMOVAL)

    def test_scan_is_linear(self):
        from unittest.mock import patch
        from relay_engine.tests.test_parity import _standalone
        families = closed_linear_families()
        for module in (E, _standalone()):
            for number, make in enumerate(families):
                baseline = None
                for n in (4000, 16000, 2000000):
                    count = [0]
                    def visit(amount=1):
                        count[0] += amount
                    with self.subTest(copy=module.__name__, family=number, n=n):
                        with patch.object(module, '_shape_scan_work', visit):
                            module.draft_shape(draft(body=make(n)))
                        self.assertGreater(count[0], 0)
                        rate = count[0]/n
                        if baseline is None:
                            baseline = rate
                        else:
                            self.assertLessEqual(rate, 1.25*baseline)


    def test_stop_lines_end_fence_quoting(self):
        from relay_engine.tests.test_parity import _standalone
        for module in (E, _standalone()):
            for stop in ('- note','* note','+ note','2) step','> quote','-','<!-- c -->','- - -','</div>','<?x>','<div>'):
                text = draft(body=stop+'\n  ```\nSUPERSEDES: x\n  ```')
                with self.subTest(copy=module.__name__, stop=stop):
                    shape = module.draft_shape(text)
                    self.assertIsNotNone(shape)
                    self.assertEqual((shape.kind, shape.key, shape.line, shape.column), ('late-control','SUPERSEDES',15,1))
            for text, line, col in ((draft(prefix='1. step\n```\n  SUPERSEDES: x\n```\n'),3,3),
                                    (draft(prefix='\ufeff```\nx\n```\n  SUPERSEDES: x\n```\n'),4,3),
                                    (draft(prefix='\ufeff```\n  SUPERSEDES: x\n```\n'),2,3)):
                shape = module.draft_shape(text)
                self.assertIsNotNone(shape)
                self.assertEqual((shape.kind,shape.line,shape.column),('late-control',line,col))
            for before in ('    - x', '-not', '1.not', '< not html'):
                self.assertIsNone(module.draft_shape(draft(body=before+'\n```\nSUPERSEDES: x\n```')))
            for text in (draft(body='```\nSUPERSEDES: x\n```\n- note'),
                         draft(body='```\n- item\nSUPERSEDES: x\n```'),
                         draft(prefix='```\n  SUPERSEDES: x\n```\n',body='- item')):
                self.assertIsNone(module.draft_shape(text))
            shape = module.draft_shape(draft('MERGE-GATE', body='DISPATCH MERGE\n\n- note\n```\nVERDICT: merge-blocked\n```'))
            self.assertIsNotNone(shape)
            self.assertEqual((shape.kind,shape.key,shape.line),('late-verdict','VERDICT',17))

    def test_fences_carried_across_the_block_quote_nothing(self):
        from relay_engine.tests.test_parity import _standalone
        names = {'list item touching','unclosed opener before','html block before',
                 'grant token inside a carried fence','tilde opener before the block',
                 'closed fence before, fence after','supplementary digit','arabic-indic digit'}
        for module in (E, _standalone()):
            for name,text,want in R8_SHOWN_CASES:
                if name in names:
                    with self.subTest(copy=module.__name__,case=name):
                        shape = module.draft_shape(text)
                        self.assertEqual(None if shape is None else (shape.kind,shape.key,shape.line,shape.column),want)
