"""Erratum 2 replacement views and fold-2 regression witnesses."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from relay_engine import rules, authority
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests.test_xroot import _standalone
from relay_engine.tests.test_b9_fold1 import verify
import xrootfixgen as xf


class TestOracleBudget(unittest.TestCase):
    def test_overlapping_thread_budgets_restore_shared_state(self):
        import selectors
        import threading
        from concurrent.futures import ThreadPoolExecutor
        original_budget = rules._XROOT_BUDGET
        original_select = selectors.PollSelector.select
        a_entered, b_entered, a_left = (threading.Event() for _ in range(3))
        def first():
            with cc.budget(rules):
                a_entered.set()
                b_entered.wait()
            a_left.set()
        def second():
            a_entered.wait()
            with cc.budget(rules):
                b_entered.set()
                a_left.wait()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(first), pool.submit(second)
                a.result(); b.result()
            self.assertIs(original_budget, rules._XROOT_BUDGET)
            self.assertIs(original_select, selectors.PollSelector.select)
        finally:
            # A failing witness must not poison subsequent deadline tests.
            rules._XROOT_BUDGET = original_budget
            selectors.PollSelector.select = original_select

    def test_default_oracle_budget_cannot_expire_from_machine_load(self):
        import time
        from relay_engine.tests import xroot_baseline_b8d87eb as old
        for module in (rules, old):
            with self.subTest(module=module.__name__), corpus.budget(module):
                # A long pause cannot turn a semantic reference answer into a timeout.
                self.assertGreater(module._XROOT_BUDGET.get()[0], time.monotonic()+10**12)
                module._XROOT_BUDGET.set((time.monotonic()+5, []))
                self.assertGreater(module._XROOT_BUDGET.get()[0], time.monotonic()+10**12)

    def test_root_deadline_reference_label_matches_native(self):
        from relay_engine.tests import xroot_reference as reference
        with tempfile.TemporaryDirectory() as temporary:
            case = cc.make(Path(temporary)/'foreign')
            tree = xf._git(case.repository, 'rev-parse', case.commit+':relays')
            with mock.patch.object(rules, 'xroot_git', side_effect=rules.XrootAxisError('capacity', 'injected')):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    reference.xroot_reference_relays(case.repository, tree)
            self.assertEqual('tree .', caught.exception.progress['stopped_at'])


class TestConfigView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = (rules, _standalone())

    def caches(self, module):
        return {route: module.XrootFactCache() for route in ('verify', 'pair', 'direct')}

    def answers(self, module, consumer, case, axis, reason, caches):
        # Each caller has its own cache: its first check is genuinely cold,
        # and only its own second check can be called warm.
        with module.xroot_fact_cache(caches['verify']):
            self.assertEqual(axis, verify(module, consumer, case.declaration)['axis'])
        original_verify = module.xroot_verify
        def selected(*args):
            result = original_verify(*args)
            # Standalone verifies first; its successful None return is adapted
            # with the native selection tuple required by authority.check.
            return rules.xroot_verify(*args) if result is None else result
        for route in ('pair', 'direct'):
            with module.xroot_fact_cache(caches[route]), \
                 mock.patch.object(authority, 'rules', module), \
                 mock.patch.object(authority, 'datetime', cc.FixedDateTime), \
                 mock.patch.object(module, 'xroot_verify', side_effect=selected), \
                 cc.authority_fixture(case.declaration) as (fixture, pair, direct):
                answer = fixture.check(pair if route == 'pair' else direct,
                                       action='implement' if route == 'pair' else 'merge')
                self.assertEqual(reason is None, answer['allowed'], route)
                self.assertEqual([] if reason is None else ['authority.'+reason],
                                 sorted(answer['reasons']), route)

    def test_config_presence_including_empty_before_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            case = cc.make(home/'foreign')
            values = ['', str(home/'absent')]
            for key, text in [('false', '[core]\nuseReplaceRefs=false\n'),
                              ('true', '[core]\nuseReplaceRefs=true\n'), ('no-key', '')]:
                path = home/key
                path.write_text(text)
                values.append(str(path))
            for module in self.modules:
                for value in values:
                    with self.subTest(module=module.__name__, value=value), cc.budget(module), \
                         mock.patch.dict(os.environ, {'GIT_CONFIG': value}), \
                         mock.patch.object(module, 'xroot_git', wraps=module.xroot_git) as query:
                        self.assertIsNone(module.xroot_classify(case.repository))
                        query.assert_not_called()
                with cc.budget(module), mock.patch.dict(os.environ):
                    os.environ.pop('GIT_CONFIG', None)
                    self.assertIsInstance(module.xroot_classify(case.repository), module.XrootNamespace)

    def test_false_config_replacements_cold_and_after_clean_fill(self):
        # Removing the presence guard permits cached clean facts to hide these faults.
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home/'false'
            config.write_text('[core]\nuseReplaceRefs=false\n')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            for module in self.modules:
                for target in (cc.R, 'designs/d1.md'):
                    for shape in ('valid', 'symbolic', 'invalid', 'missing'):
                        for warm in (False, True):
                            with self.subTest(module=module.__name__, target=target, shape=shape, warm=warm), \
                                 tempfile.TemporaryDirectory(dir=home) as foreign, \
                                 corpus.budget(module), module.xroot_fact_cache(module.XrootFactCache()):
                                case = cc.make(Path(foreign))
                                caches = self.caches(module)
                                if warm:
                                    self.answers(module, consumer, case, None, None, caches)
                                oid = xf._git(case.repository, 'rev-parse', case.commit+':'+target)
                                ref = 'refs/replace/'+oid
                                if shape == 'valid':
                                    content = (cc.REVIEW.replace('approve', 'must-revise')
                                               if isinstance(cc.REVIEW, str) else cc.REVIEW.replace(b'approve', b'must-revise'))
                                    if target != cc.R:
                                        content = b'changed design'
                                    if isinstance(content, str):
                                        content = content.encode()
                                    replacement = xf._literal_object(case.repository, 'blob', content)
                                    xf._git(case.repository, 'update-ref', ref, replacement)
                                elif shape == 'symbolic':
                                    xf._git(case.repository, 'symbolic-ref', ref, 'refs/heads/absent')
                                else:
                                    path = case.repository/'.git'/ref
                                    path.parent.mkdir(parents=True, exist_ok=True)
                                    path.write_text(('3'*40 if shape == 'missing' else 'garbage')+'\n')
                                if target == cc.R:
                                    axis = 'authority-verdict' if shape == 'valid' else 'repository' if shape == 'missing' else 'authority-population'
                                    reason = 'could-not-evaluate' if shape == 'missing' else 'lineage-incomplete'
                                else:
                                    axis, reason = 'byte', 'artifact-changed' if shape == 'valid' else 'could-not-evaluate'
                                with mock.patch.dict(os.environ, {'GIT_CONFIG':str(config)}):
                                    self.answers(module, consumer, case, axis, reason, caches)

    def test_missing_candidate_and_noncandidate_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            for module in self.modules:
                for candidate in (False, True):
                    for replacement in (False, True):
                        with self.subTest(module=module.__name__, candidate=candidate, replacement=replacement), \
                             tempfile.TemporaryDirectory(dir=home) as foreign, \
                             corpus.budget(module), module.xroot_fact_cache(module.XrootFactCache()):
                            name = 'relays/'+('DESIGN-' if candidate else 'SITREP-')+'missing.md'
                            case = cc.make(Path(foreign), dict(cc.files(), **{name:b'PHASE: SITREP\nunique missing body\n'}))
                            oid = xf._git(case.repository, 'rev-parse', case.commit+':'+name)
                            if replacement:
                                path = case.repository/'.git/refs/replace'/oid
                                path.parent.mkdir(parents=True, exist_ok=True)
                                path.write_text('3'*40+'\n')
                            else:
                                (case.repository/'.git/objects'/oid[:2]/oid[2:]).unlink()
                            axis = ('repository' if replacement else 'authority-population') if candidate else None
                            reason = ('could-not-evaluate' if replacement else 'lineage-incomplete') if candidate else None
                            caches = self.caches(module)
                            for temperature in ('cold', 'warm'):
                                with self.subTest(temperature=temperature):
                                    self.answers(module, consumer, case, axis, reason, caches)
