"""E28: foreign pair reviews use authority semantics after shared verification."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from unittest import mock

from relay_engine import rules
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5


DESIGN = '''ROLE: Pair Planner
PHASE: DESIGN
AUTHORITY: design-only
DISPATCH_ID: f-design
RUN_ID: f
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: f.planner
TO: f.implementer
SUBJECT: foreign design
DESIGN_DOC_ID: d1
DESIGN_RECORD_KIND: design-doc
'''
REVIEW = '''ROLE: Pair Implementer
PHASE: DESIGN-REVIEW
AUTHORITY: review-only
DISPATCH_ID: f-review
PARENT_DISPATCH_ID: f-design
RUN_ID: f
CEREMONY_TIER: medium
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: f.implementer
TO: f.planner
SUBJECT: foreign review
DESIGN_DOC_ID: d1
DESIGN_RECORD_KIND: design-doc
DESIGN_REVIEW_VERDICT: approve
'''


class TestAuthorityFold10(TestAuthorityFold5):
    def git(self, repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='fixture', GIT_AUTHOR_EMAIL='f@example.invalid',
                   GIT_COMMITTER_NAME='fixture', GIT_COMMITTER_EMAIL='f@example.invalid',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
        return subprocess.check_output(['git', '-C', str(repo), *args], env=env,
                                       stderr=subprocess.PIPE, text=True).strip()

    @contextmanager
    def foreign_case(self, *, review=REVIEW, origin=DESIGN, unrelated=None, bad_digest=False, direct=False):
        f = TestAuthorityFold5()
        f.setUp()
        try:
            with tempfile.TemporaryDirectory(prefix='authority-xroot-') as td:
                repo = Path(td)
                self.git(repo, 'init', '-q', '-b', 'main')
                (repo/'relays').mkdir(); (repo/'designs').mkdir()
                (repo/'relays/DESIGN-planner-20260801-100000.md').write_text(origin)
                (repo/'relays/DESIGN-REVIEW-implementer-20260801-110000.md').write_text(review)
                if unrelated is not None:
                    entries = unrelated.items() if isinstance(unrelated, dict) else [('SITREP-planner-20260801-120000.md', unrelated)]
                    for name, text in entries:
                        (repo/'relays'/name).write_text(text)
                (repo/'designs/d1.md').write_bytes(b'design')
                self.git(repo, 'add', '-A'); self.git(repo, 'commit', '-qm', 'fixture')
                commit = self.git(repo, 'rev-parse', 'HEAD')
                self.git(f.root.path, 'init', '-q', '-b', 'main')
                digest = '0'*64 if bad_digest else hashlib.sha256(b'design').hexdigest()
                declaration = dict(DESIGN_LOCK_ID='d1', DESIGN_RECORD_KIND='design-doc', DESIGN_DOC_ID='d1',
                                   DESIGN_OWNER='f', DESIGN_SOURCE_REPO=str(repo), DESIGN_SOURCE_COMMIT=commit,
                                   DESIGN_SOURCE_ROOT='relays', DESIGN_SOURCE_PATH='designs/d1.md', DESIGN_SHA256=digest)
                pin = f.artifact('plans', 'PL-base')
                extra = '\n'.join(k+': '+v for k,v in declaration.items())+'\nPLAN_LOCK_ID: PL\n'+pin
                f.add('PLAN', ident='plan', extra=extra, content='')
                f.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                      'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve', '')
                grant = (f.add('MERGE-GATE', 'operator', ident='grant', extra='PARENT_DISPATCH_ID: plan', content='DISPATCH MERGE')
                         if direct else f.add(ident='grant', extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL'))
                yield f, grant, repo, declaration
        finally:
            f.tearDown()

    def assert_case(self, reasons, **kwargs):
        with self.foreign_case(**kwargs) as (f,g,_,_):
            answer = f.check(g, action='merge' if kwargs.get('direct') else 'implement')
            self.assertEqual(answer['reasons'], reasons)
            self.assertEqual(answer['allowed'], not reasons)

    def test_foreign_review_header_and_html_refusals(self):
        variants = [REVIEW+'VERDICT: must-revise\n', REVIEW+'HUMAN_MERGE_AUTHORIZATION: denied\n',
                    REVIEW.replace('SUBJECT: foreign review','SUBJECT: compare <tag> values'),
                    REVIEW+'NOTE: x\v\nVERDICT: must-revise\n', '<!-- open\n'+REVIEW,
                    REVIEW+'VERDICT: approve with minors\n', REVIEW.replace('TO: f.planner\n',''),
                    REVIEW+'\n<!-- body comment\n']
        for review in variants:
            with self.subTest(review=review):
                self.assert_case(['authority.lineage-incomplete'], review=review)

    def test_foreign_tree_control_scan_includes_origin_and_unrelated(self):
        for kwargs in [dict(review=REVIEW+'\nprose\f tail'), dict(origin=DESIGN+'\nprose\f tail'),
                       dict(unrelated='PHASE: SITREP\n\nd1 prose\f tail')]:
            with self.subTest(kwargs=kwargs):
                self.assert_case([] if 'unrelated' in kwargs else ['authority.lineage-incomplete'], **kwargs)

    def test_foreign_clean_and_whitespace_controls(self):
        for kwargs in [{}, dict(review=REVIEW.replace('VERDICT: approve','VERDICT: approve ')),
                       dict(unrelated='PHASE: SITREP\n\nclean prose'), dict(review=REVIEW+'\nVERDICT: denied\n')]:
            with self.subTest(kwargs=kwargs):self.assert_case([], **kwargs)

    def test_failed_verification_skips_new_checks(self):
        for kwargs in [{}, dict(review=REVIEW+'\nprose\f tail'), dict(unrelated='prose\f tail'),
                       dict(review=REVIEW+'VERDICT: must-revise\n')]:
            with self.subTest(kwargs=kwargs):self.assert_case(['authority.artifact-changed'], bad_digest=True, **kwargs)

    def test_direct_issuer_preserves_old_foreign_behavior(self):
        for kwargs in [{}, dict(review=REVIEW+'VERDICT: must-revise\n'), dict(unrelated='prose\f tail')]:
            with self.subTest(kwargs=kwargs):self.assert_case([], direct=True, **kwargs)
        self.assert_case(['authority.artifact-changed'], direct=True, bad_digest=True, unrelated='prose\f tail')

    def test_population_is_from_pinned_tree_not_worktree_or_head(self):
        for original, replacement, expected in [('d1 clean', 'd1 prose\f tail', []),
                                                 ('d1 prose\f tail', 'd1 clean', [])]:
            with self.subTest(original=original), self.foreign_case(unrelated=original) as (f,g,repo,_):
                (repo/'relays/SITREP-planner-20260801-120000.md').write_text(replacement)
                self.git(repo, 'add', '-A'); self.git(repo, 'commit', '-qm', 'later head')
                self.assertEqual(f.check(g)['reasons'], expected)

    def test_population_read_failure_and_budget_refuse(self):
        # E29 reads the population once, inside the shared verifier.
        for failure in [OSError('read'), ValueError('decode'), KeyError('missing'), 'budget']:
            with self.subTest(failure=failure), self.foreign_case() as (f,g,_,_):
                original = rules.xroot_relays
                def population(*args):
                    if failure == 'budget':
                        rules._XROOT_BUDGET.set((0, rules._XROOT_BUDGET.get()[1]))
                    else:raise failure
                    return original(*args)
                with mock.patch.object(rules, 'xroot_relays', side_effect=population):
                    self.assertEqual(f.check(g)['reasons'], ['authority.could-not-evaluate'])

    def test_shared_verifier_keeps_stricter_review_and_body_reader(self):
        for review, expected in [(REVIEW.replace('VERDICT: approve','VERDICT: approved'), ['authority.lineage-incomplete']),
                                  (REVIEW+'\nDESIGN_REVIEW_VERDICT: must-revise\n', ['authority.lineage-incomplete'])]:
            with self.subTest(review=review):self.assert_case(expected, review=review)
