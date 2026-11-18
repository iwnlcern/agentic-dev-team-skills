"""E29: the shared verifier supplies E28's single pinned population."""
from pathlib import Path
import tempfile
from unittest import mock

from relay_engine import authority, rules
from relay_engine.tests import test_authority_fold10 as foreign_fixture
from relay_engine.tests import xroot_cache_cases
from relay_engine.tests.authority_legacy_fixtures import TestAuthorityFold5


class TestAuthorityFold11(TestAuthorityFold5):
    git = foreign_fixture.TestAuthorityFold10.git
    foreign_case = foreign_fixture.TestAuthorityFold10.foreign_case
    assert_case = foreign_fixture.TestAuthorityFold10.assert_case

    def test_unrelated_vt_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\n\nprose\v tail')

    def test_unrelated_nul_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\n\nprose\0 tail')

    def test_unrelated_del_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\n\nprose\x7f tail')

    def test_unrelated_nel_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\n\nprose\x85 tail')

    def test_unrelated_line_separator_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\n\nprose\u2028 tail')

    def test_unrelated_header_ff_allows(self):
        self.assert_case([], unrelated='PHASE: SITREP\nSUBJECT: prose\f tail\n\nclean body')

    def test_expired_budget_at_declaration_refuses(self):
        with self.foreign_case(timed=True) as (f,g,_,_):
            now = [0]
            original = rules.xroot_declaration
            def declaration(*args):
                value = original(*args)
                now[0] = 6
                return value
            with mock.patch.object(rules, 'xroot_declaration', side_effect=declaration), \
                 mock.patch.object(authority.time, 'monotonic', side_effect=lambda: now[0]):
                self.assertEqual(f.check(g)['reasons'], ['authority.could-not-evaluate'])

    def test_expired_budget_before_population_is_not_restarted(self):
        with self.foreign_case(timed=True) as (f,g,_,_):
            now = [0]
            original = rules.xroot_authority
            def select(*args):
                now[0] = 6
                return original(*args)
            with mock.patch.object(rules, 'xroot_authority', side_effect=select), \
                 mock.patch.object(authority.time, 'monotonic', side_effect=lambda: now[0]):
                self.assertEqual(f.check(g)['reasons'], ['authority.could-not-evaluate'])

    def test_managed_post_review_deadline_refuses(self):
        original_approved = authority._approved
        with self.foreign_case(timed=True) as (fixture, grant, _, _):
            selected_path, = fixture.ledger.execute(
                'SELECT rendered_path FROM relays WHERE dispatch_id=?', ('plan',)).fetchone()
            for expire in (False, True):
                with self.subTest(expire=expire):
                    now = 100
                    review_deadlines = []

                    def approve(item):
                        nonlocal now
                        approved = original_approved(item)
                        if isinstance(item, rules.XrootRelay):
                            self.assertTrue(approved)
                            review_deadlines.append(rules._XROOT_BUDGET.get()[0])
                            if expire:
                                now = 106
                        return approved

                    with mock.patch.object(authority.time, 'monotonic', side_effect=lambda: now), \
                         mock.patch.object(authority, '_approved', side_effect=approve):
                        answer = fixture.check(grant)
                    self.assertEqual([105], review_deadlines)
                    if expire:
                        self.assertFalse(answer['allowed'], answer)
                        self.assertEqual(['authority.could-not-evaluate'], answer['reasons'])
                        capacity, = answer['external']
                        self.assertEqual('capacity', capacity['axis'])
                        self.assertEqual('authority.could-not-evaluate', capacity['reason'])
                        self.assertEqual([selected_path], capacity['relays'])
                        self.assertEqual('review-approval', capacity['progress']['stopped_at'])
                    else:
                        self.assertTrue(answer['allowed'], answer)
                        self.assertEqual([], answer['reasons'])
                        self.assertEqual([], answer['external'])

    def test_managed_ancestor_deadline_keeps_prior_byte_refusal(self):
        with tempfile.TemporaryDirectory(prefix='authority-older-edges-') as directory:
            home = Path(directory)
            far = xroot_cache_cases.make(home / 'far')
            near = xroot_cache_cases.make(home / 'near')
            selected = xroot_cache_cases.make(home / 'selected')
            self.git(self.root.path, 'init', '-q', '-b', 'main')
            pin = self.artifact('plans', 'PL-older-edges')

            def add_plan(ident, declaration, extra=''):
                fields = '\n'.join(f'{key}: {value}' for key, value in declaration.items())
                return self.add('PLAN', ident=ident, extra=fields + extra, content='')

            far_row = add_plan('far', far.declaration)
            near_row = add_plan('near', dict(near.declaration, DESIGN_SHA256='0' * 64),
                                '\nPARENT_DISPATCH_ID: far')
            add_plan('plan', selected.declaration, f'\nPARENT_DISPATCH_ID: near\nPLAN_LOCK_ID: PL\n{pin}')
            self.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                     f'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n{pin}\nVERDICT: approve', '')
            grant = self.add(extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
            original_verify = rules.xroot_verify

            def check(expire):
                now = 100
                deadlines = []

                def verify(root, declaration):
                    nonlocal now
                    repo = declaration['DESIGN_SOURCE_REPO']
                    deadlines.append((repo, rules._XROOT_BUDGET.get()[0]))
                    if expire and repo == str(far.repository):
                        now = 106
                    return original_verify(root, declaration)

                with mock.patch.object(authority.time, 'monotonic', side_effect=lambda: now), \
                     mock.patch.object(rules, 'xroot_verify', side_effect=verify):
                    answer = self.check(grant)
                self.assertEqual([(str(edge.repository), 105) for edge in (selected, near, far)],
                                 deadlines)
                return answer

            control = check(expire=False)
            self.assertFalse(control['allowed'], control)
            self.assertEqual(['authority.artifact-changed'], control['reasons'])
            byte, = control['external']
            self.assertEqual('byte', byte['axis'])
            self.assertEqual('authority.artifact-changed', byte['reason'])
            self.assertEqual([near_row.rendered_path], byte['relays'])

            expired = check(expire=True)
            self.assertFalse(expired['allowed'], expired)
            self.assertEqual(['authority.artifact-changed', 'authority.could-not-evaluate'],
                             expired['reasons'])
            retained, capacity = expired['external']
            self.assertEqual(byte, retained)
            self.assertEqual('capacity', capacity['axis'])
            self.assertEqual('authority.could-not-evaluate', capacity['reason'])
            self.assertEqual([far_row.rendered_path], capacity['relays'])
            self.assertIn('time limit', capacity['detail'])

    def test_pair_and_direct_use_identical_git_queries(self):
        unrelated = {f'SITREP-planner-20260801-12000{i}.md': 'PHASE: SITREP\n\nclean' for i in range(4)}
        with self.foreign_case(unrelated=unrelated) as (f,g,_,_):
            with mock.patch.object(rules, 'xroot_git', wraps=rules.xroot_git) as queries:
                self.assertEqual(f.check(g)['reasons'], [])
                pair_queries = list(queries.call_args_list)
            direct = f.add('MERGE-GATE', 'operator', extra='PARENT_DISPATCH_ID: plan', content='DISPATCH MERGE')
            with mock.patch.object(rules, 'xroot_git', wraps=rules.xroot_git) as queries:
                self.assertEqual(f.check(direct, action='merge')['reasons'], [])
                self.assertGreater(len(queries.call_args_list), 0)
                self.assertEqual(len(pair_queries), len(queries.call_args_list))
                self.assertEqual(pair_queries, queries.call_args_list)
