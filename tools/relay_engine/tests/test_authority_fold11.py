"""E29: the shared verifier supplies E28's single pinned population."""
from unittest import mock

from relay_engine import authority, rules
from relay_engine.tests import test_authority_fold10 as foreign_fixture
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5


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
