"""B9 operation isolation, caller budgets and deadline establishment."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import rules, judging
from relay_engine.tests import test_authority_fold10 as a10, test_judging as jt
from relay_engine.tests.test_xroot import _standalone
from relay_engine.tests.xroot_b9_cases import materialize, _object_tree, DESIGN, REVIEW, O, R
import xrootfixgen as xf


class TestXrootDeadline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b9-cache-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home/'consumer'
        xf._init_repo(self.repo)
        self.case = materialize(self.home/'cases', only=['control-allow'])['control-allow']

    @contextmanager
    def budget(self, deadline, module=rules):
        token = module._XROOT_BUDGET.set((deadline, []))
        try:
            yield
        finally:
            module._XROOT_BUDGET.reset(token)

    def verify(self, module=rules, declaration=None):
        return module.xroot_verify(self.repo, declaration or self.case.declaration)

    def operation(self, module=rules):
        self.assertTrue(hasattr(module, 'xroot_operation'), 'operation scope is missing')
        return module.xroot_operation()

    def test_error_established_before_deadline_counts(self):
        tree = _object_tree(self.repo, {'DESIGN-a.md': b'\xff'})
        now = [0]
        original = rules._xroot_kill_and_reap
        def cleanup(pid):
            original(pid)
            now[0] = 6
        with mock.patch.object(rules.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(rules, '_xroot_kill_and_reap', side_effect=cleanup), self.budget(5):
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_relays(self.repo, tree)
        self.assertEqual('authority-population', caught.exception.axis)
        self.assertEqual('authority carrier DESIGN-a.md is not UTF-8', caught.exception.detail)
        self.assertEqual(6, now[0])

    def test_error_established_after_deadline_answers_capacity(self):
        tree = _object_tree(self.repo, {'DESIGN-a.md': 'DESIGN_DOC_ID: d1\n'})
        now = [0]
        def late_header(text):
            now[0] = 6
            raise rules.XrootAxisError('authority-population', 'late parse error')
        with mock.patch.object(rules.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(rules, 'header_fields', side_effect=late_header), self.budget(5):
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_relays(self.repo, tree)
        self.assertEqual('capacity', caught.exception.axis)

    def test_allow_after_deadline_answers_capacity(self):
        original = rules.xroot_authority
        for late in (False, True):
            now = [0]
            def select(*args):
                answer = original(*args)
                if late:
                    now[0] = 6
                return answer
            with self.subTest(late=late), self.budget(5), \
                 mock.patch.object(rules.time, 'monotonic', side_effect=lambda: now[0]), \
                 mock.patch.object(rules, 'xroot_authority', side_effect=select):
                if late:
                    with self.assertRaises(rules.XrootAxisError) as caught:
                        self.verify()
                    self.assertEqual('capacity', caught.exception.axis)
                else:
                    self.verify()


    def test_same_tree_two_doc_ids_both_orders(self):
        files = {O: DESIGN, R: REVIEW, 'designs/d1.md': b'design',
                 'relays/DESIGN-planner-20260801-120000.md': DESIGN.replace('d1','d2').replace('f-design','g-design'),
                 'relays/DESIGN-REVIEW-implementer-20260801-130000.md': REVIEW.replace('d1','d2').replace('f-review','g-review').replace('f-design','missing')}
        for conflict in (False, True):
            graph = dict(files)
            if conflict:
                graph['relays/DESIGN-zz.md'] = 'PHASE: DESIGN\nPHASE: SITREP\n'
            commit = xf._literal_commit(self.repo, _object_tree(self.repo, graph))
            base = dict(self.case.declaration, DESIGN_SOURCE_REPO=str(self.repo), DESIGN_SOURCE_COMMIT=commit)
            for order in (('d1','d2'), ('d2','d1')):
                with self.subTest(conflict=conflict, order=order), rules.xroot_fact_cache(rules.XrootFactCache()), self.operation(), self.budget(time.monotonic()+5), \
                     mock.patch.object(rules, 'XrootBatchReader', wraps=rules.XrootBatchReader) as reader:
                    for doc in order:
                        declaration = dict(base, DESIGN_DOC_ID=doc)
                        if conflict or doc == 'd2':
                            with self.assertRaises(rules.XrootAxisError) as caught:
                                self.verify(declaration=declaration)
                            self.assertEqual('authority-population' if conflict else 'authority-parent', caught.exception.axis)
                        else:
                            self.verify(declaration=declaration)
                    self.assertEqual(1, reader.call_count)




    def test_two_repositories_share_one_budget_and_both_reap(self):
        other = materialize(self.home/'second', only=['control-allow'])['control-allow']
        now, readers = [0], []
        original = rules.XrootBatchReader
        class Observed(original):
            def __init__(self, *args):
                super().__init__(*args)
                readers.append(self)
                self.initial_deadline = self.deadline
            def next_response(self):
                if len(readers) == 2:
                    now[0] = 6
                return super().next_response()
        before = set(os.listdir('/dev/fd'))
        with rules.xroot_fact_cache(rules.XrootFactCache()), self.operation(), self.budget(5), mock.patch.object(rules.time,'monotonic',side_effect=lambda:now[0]), \
             mock.patch.object(rules,'XrootBatchReader',Observed):
            self.verify()
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.verify(declaration=other.declaration)
            self.assertEqual('capacity',caught.exception.axis)
        self.assertEqual([5,5],[r.initial_deadline for r in readers])
        for reader in readers:
            with self.assertRaises(ChildProcessError):
                os.waitpid(reader.pid,os.WNOHANG)
        self.assertEqual(before,set(os.listdir('/dev/fd')))

    def raw(self, ident='candidate'):
        return jt.body(ident=ident, extra='\n'.join(k+': '+v for k,v in self.case.declaration.items()))

    def judge(self, raw):
        return judging.judge_candidate(judging.Corpus((),None,None), 'PLAN-pair-planner-20261013-120000.md',
                                       jt.envelope(raw), raw, artifact_root=self.repo, deadline=time.monotonic()+5)

    def test_lint_per_edge_bound(self):
        for i in (1,2):
            (self.repo/f'PLAN-pair-planner-20261013-12000{i}.md').write_bytes(self.raw(f'plan-{i}'))
        for module in (rules, _standalone()):
            now, deadlines = [0], []
            original = module.XrootBatchReader
            class FirstExpired(original):
                def __init__(self,*args):
                    super().__init__(*args)
                    deadlines.append(self.deadline)
                def next_response(self):
                    if len(deadlines) == 1:
                        now[0] = 6
                    return super().next_response()
            with self.subTest(module=module.__name__), module.xroot_fact_cache(module.XrootFactCache()), mock.patch.object(module.time,'monotonic',side_effect=lambda:now[0]), \
                 mock.patch.object(module,'XrootBatchReader',FirstExpired):
                answer = module.lint_relay_root(self.repo)
            failures = [e for e in answer.errors if 'declared design edge failed verification:' in e]
            self.assertEqual(1,len(failures),failures)
            self.assertIn('capacity:',failures[0])
            self.assertEqual([5,11],deadlines)

    def test_submit_finding_carries_capacity_detail(self):
        with mock.patch.object(rules,'XROOT_MAX_RELAYS',1):
            findings = self.judge(self.raw())
        external = [f for f in findings if f.id=='external.could-not-evaluate']
        self.assertEqual(1,len(external))
        item = external[0]
        self.assertEqual('error',item.lint_severity)
        self.assertEqual('cross-root-design',item.params['gate'])
        self.assertEqual('capacity',item.params['axis'])
        self.assertEqual(item.params['detail'],item.params['cause'])
        self.assertEqual(self.case.declaration['DESIGN_SOURCE_ROOT'],item.params['root'])
        self.assertIn(self.case.commit[:12],item.params['detail'])
        self.assertIn('XROOT_MAX_RELAYS',item.params['detail'])
        self.assertNotIn('source',item.params['progress'])

    def test_post_verification_deadline_keeps_read_progress(self):
        now = [0]
        with self.operation(), self.budget(5), rules._xroot_edge(), \
             mock.patch.object(rules.time,'monotonic',side_effect=lambda:now[0]):
            self.verify()
            now[0] = 6
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_selection_relevant('DESIGN_DOC_ID: d1\n', 'd1')
            self.assertEqual('capacity',caught.exception.axis)
            self.assertEqual(2,caught.exception.progress['relays_read'])
            self.assertEqual(1,caught.exception.progress['trees_read'])

    def test_successful_distinct_roots_share_one_reader(self):
        trees = [_object_tree(self.repo, {'DESIGN-a.md':'DESIGN_DOC_ID: '+doc+'\n'}) for doc in ('one','two')]
        original = rules.XrootBatchReader
        readers = []
        def reader(*args):
            value = original(*args)
            readers.append(value)
            return value
        with self.operation(), self.budget(time.monotonic()+5), mock.patch.object(rules,'XrootBatchReader',side_effect=reader):
            for tree in trees:
                rules.xroot_relays(self.repo,tree)
            self.assertEqual(1,len(readers))
        with self.assertRaises(ChildProcessError):
            os.waitpid(readers[0].pid,os.WNOHANG)


    def test_decode_and_relevance_checkpoints(self):
        original_reader = rules.XrootBatchReader
        original_relevance = rules.xroot_retained
        for stage in ('decode','relevance'):
            for late in (False,True):
                now = [0]
                class Bytes(bytes):
                    def decode(self,*args,**kwargs):
                        answer = super().decode(*args,**kwargs)
                        if late:
                            now[0] = 6
                        return answer
                class Reader(original_reader):
                    def next_response(self):
                        kind,data = super().next_response()
                        return kind,Bytes(data) if kind=='blob' and data is not None else data
                def relevance(text):
                    answer = original_relevance(text)
                    if late:
                        now[0] = 6
                    return answer
                target,replacement = ('XrootBatchReader',Reader) if stage=='decode' else ('xroot_retained',relevance)
                with self.subTest(stage=stage,late=late), rules.xroot_fact_cache(rules.XrootFactCache()), self.budget(5), \
                     mock.patch.object(rules.time,'monotonic',side_effect=lambda:now[0]), \
                     mock.patch.object(rules,target,replacement):
                    if late:
                        with self.assertRaises(rules.XrootAxisError) as caught:
                            self.verify()
                        self.assertEqual('capacity',caught.exception.axis)
                    else:
                        self.verify()

    def test_plan_source_submit_keeps_each_lock_gate(self):
        raw = self.raw().replace(b'DESIGN_LOCK_ID: d1',b'DESIGN_LOCK_ID: designs/d1.md')
        raw += ('PLAN_SOURCE_REPO: '+str(self.case.repository)+'\nPLAN_SOURCE_COMMIT: '+self.case.commit+'\nPLAN_LOCK_ID: plans/p1.md\n').encode()
        findings = judging.judge_candidate(judging.Corpus((),None,None), 'PLAN-pair-planner-20261013-120000.md',
                                           jt.envelope(raw),raw,artifact_root=self.repo,deadline=0)
        external = {f.params['gate']: f for f in findings if f.id=='external.could-not-evaluate'}
        self.assertEqual({'cross-root-design','cross-root-DESIGN_LOCK_ID','cross-root-PLAN_LOCK_ID'},set(external))
        for key,path in (('DESIGN_LOCK_ID','designs/d1.md'),('PLAN_LOCK_ID','plans/p1.md')):
            item = external['cross-root-'+key]
            self.assertEqual('capacity',item.params['axis'])
            self.assertEqual(dict(key=key,path=path),item.params['lock'])
            self.assertEqual(item.params['cause'],item.params['detail'])

    def test_judging_shared_deadline_unchanged(self):
        fixture = jt.TestJudging('test_shared_git_deadline_bounds_the_whole_admission')
        result = unittest.TestResult()
        fixture.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors+result.failures)


    @contextmanager
    def injected_deadline(self, module, stage):
        now = [0]
        original_git = module.xroot_git
        original_reader = module.XrootBatchReader
        def git(*args, **kwargs):
            if stage == 'classification' or (stage == 'classification-query' and len(args)>1 and args[1]=='config'):
                now[0] = 6
            return original_git(*args, **kwargs)
        class Reader(original_reader):
            def _check_deadline(self):
                if stage == 'batch':
                    now[0] = 6
                return super()._check_deadline()
            def request(self, oid):
                value = super().request(oid)
                if stage == 'event':
                    now[0] = 6
                return value
        with self.budget(5, module), mock.patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(module, 'xroot_git', side_effect=git), mock.patch.object(module, 'XrootBatchReader', Reader):
            yield

    def test_deadline_detail_says_time_limit_on_cold_read(self):
        tail = 'this is a time limit on a cold read, not a lineage fault; a large root can reach it on a busy machine'
        root = self.case.declaration['DESIGN_SOURCE_ROOT']
        label = root+' at '+self.case.commit[:12]
        (self.repo/'PLAN-pair-planner-20261013-120000.md').write_bytes(self.raw())
        for module in (rules, _standalone()):
            for stage in ('classification', 'classification-query', 'batch', 'event', 'limit'):
                expected = ('Git object query timed out at the 5 s bound during rev-parse; '+tail
                            if stage == 'classification' else
                            f'foreign read of {label} stopped at the 5 s bound at tree relays, after 0 relays and 0 trees; '+tail)
                if stage == 'classification-query':
                    expected = 'Git object query timed out at the 5 s bound during classification; '+tail
                if stage == 'limit':
                    expected = label+' exceeds the supported XROOT_MAX_RELAYS of 1; this is a capacity limit, not a lineage gap'
                with self.subTest(module=module.__name__, stage=stage), module.xroot_fact_cache(module.XrootFactCache()), self.injected_deadline(module, stage), \
                     mock.patch.object(module, 'XROOT_MAX_RELAYS', 1 if stage == 'limit' else 20000):
                    result = module.lint_relay_root(self.repo)
                    self.assertEqual(['PLAN-pair-planner-20261013-120000.md: '+rules.xroot_error('capacity', expected)], result.errors)
                    self.assertNotRegex(expected, r'retry|again')
                if module is rules:
                    with self.subTest(caller='submit', stage=stage), module.xroot_fact_cache(module.XrootFactCache()), self.injected_deadline(module, stage), \
                         mock.patch.object(module, 'XROOT_MAX_RELAYS', 1 if stage == 'limit' else 20000):
                        external = [f for f in self.judge(self.raw()) if f.id == 'external.could-not-evaluate']
                        self.assertEqual(1, len(external))
                        self.assertEqual('capacity', external[0].params['axis'])
                        self.assertEqual(expected, external[0].params['cause'])
        # The raw batch form is retained for direct reader consumers.
        for module in (rules, _standalone()):
            with self.subTest(raw_batch=module.__name__), self.injected_deadline(module, 'batch'):
                with module.XrootBatchReader(self.case.repository, 5) as reader:
                    with self.assertRaises(module.XrootAxisError) as caught:
                        reader._check_deadline()
                    self.assertEqual('capacity', caught.exception.axis)
                    self.assertEqual('Git batch reader timed out at the 5 s bound; '+tail, caught.exception.detail)

    def test_deadline_after_completed_walk_reports_walk_progress(self):
        from relay_engine import authority
        from relay_engine.tests import xroot_cache_cases as cc
        tail = 'this is a time limit on a cold read, not a lineage fault; a large root can reach it on a busy machine'
        label = self.case.declaration['DESIGN_SOURCE_ROOT']+' at '+self.case.commit[:12]
        counts = dict(relays_read=2, trees_read=1,
                      bytes_read=len(DESIGN.encode())+len(REVIEW.encode()))
        (self.repo/'PLAN-pair-planner-20261013-120000.md').write_bytes(self.raw())

        @contextmanager
        def expire(module, target, late):
            now = [0]
            owner, name = (authority, target) if target.startswith('_') else (module, target)
            original = getattr(owner, name)
            def observed(*args, **kwargs):
                answer = original(*args, **kwargs)
                if late and (name != '_approved' or isinstance(args[0], rules.XrootRelay)):
                    now[0] = 6
                return answer
            with module.xroot_fact_cache(module.XrootFactCache()), self.budget(5, module), \
                 mock.patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
                 mock.patch.object(owner, name, side_effect=observed):
                yield

        def check(entry, step):
            self.assertEqual('capacity', entry['axis'])
            self.assertEqual(f'foreign read of {label} read 2 relays and 1 trees, then stopped at the 5 s bound during {step}; '+tail, entry['detail'])
            self.assertEqual(step, entry['progress']['stopped_at'])
            for key, value in counts.items():
                self.assertEqual(value, entry['progress'][key], key)
                self.assertGreater(value, 0)

        # Stage 2 runs in both linters and submit judging.
        for module in (rules, _standalone()):
            for late in (False, True):
                with self.subTest(caller=module.__name__, late=late), expire(module, 'xroot_relays', late):
                    result = module.lint_relay_root(self.repo)
                if late:
                    self.assertEqual(1, len(result.external))
                    check(result.external[0], 'selection')
                else:
                    self.assertEqual([], result.errors)
        for late in (False, True):
            with self.subTest(caller='submit', late=late), expire(rules, 'xroot_relays', late):
                external = [f for f in self.judge(self.raw()) if f.id == 'external.could-not-evaluate']
            self.assertEqual(int(late), len(external))
            if late:
                check(external[0].params, 'selection')

        with cc.authority_fixture(self.case.declaration) as (fixture, pair, direct):
            for path, grant, action in (('pair', pair, 'implement'), ('direct', direct, 'merge')):
                stages = [('xroot_relays', 'selection'),
                          ('xroot_verify', 'review-approval' if path == 'pair' else 'authority-completion')]
                if path == 'pair':
                    stages += [('_approved', 'review-approval'),
                               ('_foreign_selection_relevant', 'foreign-control-character')]
                for target, step in stages:
                    for late in (False, True):
                        with self.subTest(path=path, target=target, late=late), expire(rules, target, late):
                            answer = fixture.check(grant, action=action)
                        self.assertEqual(not late, answer['allowed'])
                        self.assertEqual(int(late), len(answer['external']))
                        if late:
                            self.assertEqual(['authority.could-not-evaluate'], answer['reasons'])
                            check(answer['external'][0], step)

        # Existing event and pre-walk forms, including their zero counts, stand.
        for stage in ('event', 'classification'):
            with self.subTest(control=stage), rules.xroot_fact_cache(rules.XrootFactCache()), self.injected_deadline(rules, stage):
                entries = [f.params for f in self.judge(self.raw()) if f.id == 'external.could-not-evaluate']
            self.assertEqual(1, len(entries))
            entry = entries[0]
            self.assertEqual('tree relays' if stage == 'event' else 'query', entry['progress']['stopped_at'])
            for key in counts:
                self.assertEqual(0, entry['progress'][key])
            expected = (f'foreign read of {label} stopped at the 5 s bound at tree relays, after 0 relays and 0 trees; '
                        if stage == 'event' else 'Git object query timed out at the 5 s bound during rev-parse; ')
            self.assertEqual(expected+tail, entry['detail'])

        # A second edge that expires before its walk must not inherit the
        # first edge's completed counters, even in the same operation.
        from relay_engine.tests.test_authority_b9 import TestAuthorityB9
        other = cc.make(self.home/'later-edge')
        helper = TestAuthorityB9()
        now = [0]
        original_git = rules.xroot_git
        observed_walks = []
        original_set_event = rules._xroot_set_event
        def record_walk(*args):
            observed_walks.append(args[1])
            return original_set_event(*args)
        def late_git(repo, *args, **kwargs):
            if Path(repo).resolve() == other.repository.resolve():
                now[0] = 6
            return original_git(repo, *args, **kwargs)
        with helper.direct_chain((other.declaration, self.case.declaration)) as (fixture, grant, carriers), \
             mock.patch.object(rules.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(rules, '_xroot_set_event', side_effect=record_walk), \
             mock.patch.object(rules, 'xroot_git', side_effect=late_git):
            answer = fixture.check(grant, action='merge')
        self.assertEqual(1, len(observed_walks))
        self.assertEqual(2, observed_walks[0]['relays_read'])
        self.assertEqual(1, len(answer['external']))
        entry = answer['external'][0]
        self.assertEqual(other.commit, entry['commit'])
        self.assertEqual('query', entry['progress']['stopped_at'])
        for key in counts:
            self.assertEqual(0, entry['progress'][key])
        self.assertEqual('Git object query timed out at the 5 s bound during classification; '+tail, entry['detail'])

    def test_capacity_detail_names_root_and_commit(self):
        from relay_engine.tests.capture_xroot_baseline import LiftedBudget
        from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
        repo = self.case.repository
        tree = xf._git(repo, 'rev-parse', self.case.commit+':relays')
        raw = xf._git(repo, 'cat-file', 'tree', tree, text=False)
        filler = xf._literal_object(repo, 'blob', b'PHASE: SITREP\n')
        raw += b''.join(xf._tree_entry(b'100644', f'DESIGN-filler-{i:05d}.md'.encode(), filler) for i in range(19999))
        relays = xf._literal_object(repo, 'tree', raw)
        designs = xf._git(repo, 'rev-parse', self.case.commit+':designs')
        tree = _object_tree(repo, {'relays': (b'40000', relays), 'designs': (b'40000', designs)})
        commit = xf._literal_commit(repo, tree)
        declaration = dict(self.case.declaration, DESIGN_SOURCE_COMMIT=commit)
        expected = 'relays at '+commit[:12]+' exceeds the supported XROOT_MAX_RELAYS of 20000; this is a capacity limit, not a lineage gap'
        raw = jt.body(extra='\n'.join(k+': '+v for k,v in declaration.items()))
        (self.repo/'PLAN-pair-planner-20261013-120000.md').write_bytes(raw)
        fixture = TestAuthorityFold5()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        xf._git(fixture.root.path, 'init', '-q', '-b', 'main')
        pin = fixture.artifact('plans', 'PL-base')
        extra = '\n'.join(k+': '+v for k,v in declaration.items())
        fixture.add('PLAN', ident='plan', extra=extra+'\nPLAN_LOCK_ID: PL\n'+pin, content='')
        fixture.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                    'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve', '')
        grant = fixture.add(ident='grant', extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
        with mock.patch.object(rules, '_XROOT_BUDGET', LiftedBudget(rules._XROOT_BUDGET)):
            result = rules.lint_relay_root(self.repo)
            self.assertEqual(['PLAN-pair-planner-20261013-120000.md: '+rules.xroot_error('capacity', expected)], result.errors)
            # Task 5 adds public external rows; Task 3 observes the real
            # verifier exception consumed by authority.check without changing it.
            observed = []
            original = rules.xroot_verify
            def verify(*args, **kwargs):
                try:
                    return original(*args, **kwargs)
                except rules.XrootAxisError as exc:
                    observed.append((exc.axis, exc.detail))
                    raise
            with mock.patch.object(rules, 'xroot_verify', side_effect=verify):
                answer = fixture.check(grant)
            self.assertFalse(answer['allowed'])
            self.assertEqual(['authority.could-not-evaluate'], answer['reasons'])
            self.assertEqual([('capacity', expected)], observed)
