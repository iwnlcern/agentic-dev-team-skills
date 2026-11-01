"""Immutable object facts, replacement modes, and bounded concurrent cache use."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import resource
import shutil
import signal
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay_engine import rules, authority
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests import xroot_reference as ref
from relay_engine.tests import xroot_baseline_b8d87eb as old
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests.test_xroot import _standalone
from relay_engine.tests.test_judging import body
import xrootfixgen as xf


class TestPinnedCacheOracle(unittest.TestCase):
    def test_pinned_old_reader_bytes_and_allow_control(self):
        self.assertEqual(cc.BASELINE_SHA256, hashlib.sha256(Path(old.__file__).read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory(prefix='b9-old-oracle-') as home:
            case = cc.make(Path(home)/'foreign')
            with cc.budget(old), cc.budget(rules):
                self.assertEqual(corpus.verify(old, case.repository, case.declaration),
                                 corpus.verify(rules, case.repository, case.declaration))


class TestXrootCache(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(rules, 'XrootFactCache'), 'object-fact cache is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='b9-facts-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.case = cc.make(self.home/'foreign')
        self.consumer = self.home/'consumer'
        xf._init_repo(self.consumer)
        self.cache = rules.XrootFactCache()
        self.scope = rules.xroot_fact_cache(self.cache)
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        self.clock = mock.patch.object(authority, 'datetime', cc.FixedDateTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.enterContext(corpus.unbounded_selectors())
        token = rules._XROOT_BUDGET.set((float("inf"), []))
        self.addCleanup(rules._XROOT_BUDGET.reset, token)

    def verify(self, case=None, module=rules):
        case = case or self.case
        with cc.budget(module), module.xroot_operation():
            return module.xroot_verify(self.consumer, case.declaration)

    @contextmanager
    def requests(self, module=rules):
        values = []
        original = module.XrootBatchReader.request
        def request(reader, oid):
            values.append(oid)
            return original(reader, oid)
        with mock.patch.object(module.XrootBatchReader, 'request', request):
            yield values

    def namespace(self, repo=None):
        value = rules.xroot_classify(repo or self.case.repository)
        self.assertIsInstance(value, rules.XrootNamespace)
        return value

    def test_fact_functions_take_bytes_only(self):
        raw = b'ROLE: Pair Planner\nPHASE: DESIGN\nDESIGN_DOC_ID: d1\n'
        first = rules.xroot_blob_fact(raw)
        self.assertEqual(len(raw), first.size)
        self.assertTrue(first.utf8_ok)
        self.assertTrue(first.retained)
        self.assertEqual(raw.decode(), first.text)
        self.assertEqual('d1', dict(first.fields)['DESIGN_DOC_ID'])
        with mock.patch.object(rules.time, 'monotonic', side_effect=AssertionError('fact read a clock')):
            self.assertEqual(first, rules.xroot_blob_fact(raw))
            oid = '1'*40
            tree = xf._tree_entry(b'100644', b'DESIGN-a.md', oid)
            self.assertEqual(rules.xroot_tree_fact(tree), rules.xroot_tree_fact(tree))
        self.assertFalse(rules.xroot_blob_fact(b'\xff').utf8_ok)
        discarded = rules.xroot_blob_fact(b'PHASE: SITREP\n')
        self.assertFalse(discarded.retained)
        self.assertIsNone(discarded.text)
        self.assertFalse(discarded.fields)
        with self.assertRaises((AttributeError, TypeError)):
            first.text = 'changed'
        with self.assertRaises(TypeError):
            first.fields[0] = ('DESIGN_DOC_ID', 'changed')

    def test_hit_and_miss_apply_the_same_steps(self):
        for graph in (cc.files(), dict(cc.files(), **{'relays/DESIGN-bad.md': b'\xff'})):
            case = cc.pin(self.case.repository, graph)
            with rules.xroot_fact_cache(rules.XrootFactCache()) as cache, self.requests() as requests:
                cold = corpus.verify(rules, self.consumer, case.declaration)
                count = len(requests)
                ns = self.namespace()
                framed = []
                for request in requests:
                    oid, kind = request.split('^{')
                    if cache.get((ns, oid, kind[:-1])) is not None:
                        framed.append(request)
                warm = corpus.verify(rules, self.consumer, case.declaration)
                self.assertEqual(cold, warm)
                self.assertGreater(count, 0)
                self.assertTrue(framed)
                self.assertFalse(set(framed).intersection(requests[count:]))
                if cold['axis'] is None:
                    self.assertEqual(count, len(requests))

    def test_progress_counts_equal_warm_and_cold(self):
        with mock.patch.object(rules, 'XROOT_MAX_RELAYS', 1):
            results = []
            for _ in range(2):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    self.verify()
                self.assertEqual('capacity', caught.exception.axis)
                self.assertNotIn('source', caught.exception.progress)
                results.append((caught.exception.detail, caught.exception.progress))
            self.assertEqual(results[0], results[1])

    def test_requested_event_consumes_own_response(self):
        graph = cc.files()
        graph.update({'relays/DESIGN-copy-a.md': 'DESIGN_DOC_ID: unrelated\n',
                      'relays/DESIGN-copy-b.md': 'DESIGN_DOC_ID: unrelated\n'})
        case = cc.pin(self.case.repository, graph)
        with self.requests() as requests:
            first = self.verify(case)
        oid = xf._git(case.repository, 'rev-parse', case.commit+':relays/DESIGN-copy-a.md')+'^{blob}'
        self.assertEqual(2, requests.count(oid))
        self.assertEqual(first, self.verify(case))

    def test_charge_within_bound_after_every_insert(self):
        cache = rules.XrootFactCache(2048)
        ns = self.namespace()
        for i in range(50):
            fact = rules.xroot_blob_fact(('DESIGN_DOC_ID: '+str(i)+'\n').encode())
            key = (ns, f'{i:040x}', 'blob')
            cache.put(key, fact, 300)
            self.assertLessEqual(cache.charge_total(), 2048)
        self.assertEqual(fact, cache.get(key))
        self.assertIsNone(cache.get((ns, '0'*40, 'blob')))
        print('cache-charge', cache.charge_total(), 'rss', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    def test_oversized_fact_not_inserted(self):
        cache = rules.XrootFactCache(512)
        fact = rules.xroot_blob_fact(b'DESIGN_DOC_ID: d1\n')
        key = (self.namespace(), '1'*40, 'blob')
        cache.put(key, fact, 513)
        self.assertIsNone(cache.get(key))
        self.assertEqual(0, cache.charge_total())
        with rules.xroot_fact_cache(cache):
            self.assertEqual(self.verify()[0].name, self.verify()[0].name)
            self.assertLessEqual(cache.charge_total(), 512)

    def test_returned_relay_mutation_leaves_cache(self):
        origin, review, population = self.verify()
        origin.fields['DESIGN_DOC_ID'] = 'poison'
        review.fields.clear()
        population.clear()
        next_origin, next_review, next_population = self.verify()
        self.assertEqual('d1', next_origin.fields['DESIGN_DOC_ID'])
        self.assertEqual('d1', next_review.fields['DESIGN_DOC_ID'])
        self.assertEqual(2, len(next_population))

    def test_noncandidate_blob_never_requested(self):
        graph = cc.files()
        graph.update({f'relays/PLAN-{i}.md': b'\xff' for i in range(20)})
        case = cc.pin(self.case.repository, graph)
        oid = xf._git(case.repository, 'rev-parse', case.commit+':relays/PLAN-0.md')
        with self.requests() as requests:
            self.verify(case)
        self.assertNotIn(oid+'^{blob}', requests)
        self.assertIsNone(self.cache.get((self.namespace(), oid, 'blob')))

    def test_reference_seam_bypasses_cache(self):
        self.verify()
        with mock.patch.object(self.cache, 'get', side_effect=AssertionError('reference used cache')), \
             mock.patch.object(self.cache, 'put', side_effect=AssertionError('reference published cache')), \
             ref.xroot_reference_walk():
            self.verify()

    def test_namespace_separates_repositories(self):
        second = cc.make(self.home/'second')
        oid = xf._git(second.repository, 'rev-parse', second.commit+':'+cc.R)
        (second.repository/'.git/objects'/oid[:2]/oid[2:]).unlink()
        for order in ((self.case, second), (second, self.case)):
            with self.subTest(order=[c.repository.name for c in order]), rules.xroot_fact_cache(rules.XrootFactCache()):
                for case in order:
                    answer = corpus.verify(rules, self.consumer, case.declaration)
                    self.assertEqual('authority-population' if case is second else None, answer['axis'])
        self.assertNotEqual(self.namespace(), self.namespace(second.repository))

    def test_recreated_repository_new_namespace(self):
        before = self.namespace()
        self.verify()
        self.case.repository.rename(self.home/'retired')
        graph = cc.files()
        graph[cc.R] = cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise')
        case = cc.make(self.home/'foreign', graph)
        after = self.namespace()
        self.assertEqual(before.location, after.location)
        self.assertNotEqual((before.device, before.inode), (after.device, after.inode))
        with self.assertRaises(rules.XrootAxisError) as caught:
            self.verify(case)
        self.assertEqual('authority-verdict', caught.exception.axis)

    def test_relative_common_dir_resolved_against_repository(self):
        original = rules.xroot_git
        def git(repo, *args, **kwargs):
            if '--git-common-dir' in args:
                return str(self.case.repository)+'\n.git'
            return original(repo, *args, **kwargs)
        with mock.patch.object(rules, 'xroot_git', side_effect=git):
            ns = self.namespace()
        stat = (self.case.repository/'.git').stat()
        self.assertEqual(str(self.case.repository.resolve()), ns.location)
        self.assertEqual((stat.st_dev, stat.st_ino), (ns.device, ns.inode))

    def replacement(self, base='refs/replace/'):
        oid = xf._git(self.case.repository, 'rev-parse', self.case.commit+':'+cc.R)
        bad = xf._literal_object(self.case.repository, 'blob', cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise').encode())
        return oid, bad, base+oid

    def test_replacement_present_live_mode_no_cache(self):
        oid, bad, refname = self.replacement()
        for name in (refname, 'refs/replace/not-an-object-id'):
            xf._git(self.case.repository, 'update-ref', name, bad)
            self.assertIsNone(rules.xroot_classify(self.case.repository))
            with mock.patch.object(self.cache, 'get', side_effect=AssertionError('live cache read')), \
                 mock.patch.object(self.cache, 'put', side_effect=AssertionError('live cache write')), cc.budget(old):
                self.assertEqual(corpus.verify(old, self.consumer, self.case.declaration),
                                 corpus.verify(rules, self.consumer, self.case.declaration))
            xf._git(self.case.repository, 'update-ref', '-d', name)
        self.assertEqual(0, self.cache.charge_total())

    def test_no_fact_holds_replacement_bytes(self):
        ns = self.namespace()
        self.verify()
        oid, bad, refname = self.replacement()
        before = self.cache.get((ns, oid, 'blob'))
        self.assertIn('VERDICT: approve', before.text)
        xf._git(self.case.repository, 'update-ref', refname, bad)
        with self.assertRaises(rules.XrootAxisError):
            self.verify()
        self.assertEqual(before, self.cache.get((ns, oid, 'blob')))
        xf._git(self.case.repository, 'update-ref', '-d', refname)
        self.verify()

    def test_replacement_interleavings(self):
        for workers in (1, 2):
            for stage in ('resolution', 'lookup', 'aba', 'config', 'base'):
                for hit in (False, True):
                    with self.subTest(stage=stage, hit=hit, workers=workers), rules.xroot_fact_cache(rules.XrootFactCache()) as unused:
                        base = 'refs/b9-custom/' if stage == 'base' else 'refs/replace/'
                        oid, bad, refname = self.replacement(base)
                        env = {'GIT_REPLACE_REF_BASE': base}
                        with mock.patch.dict(os.environ, env):
                            if hit:
                                self.verify()
                            # The changed tree misses even when the review fact hits.
                            case = cc.pin(self.case.repository, dict(cc.files(), **{'relays/PLAN-note.md': 'note'}))
                            ns = self.namespace()
                            if stage == 'config':
                                xf._git(self.case.repository, 'config', 'core.useReplaceRefs', 'false')
                                xf._git(self.case.repository, 'update-ref', refname, bad)
                            changed, removed = [False], [False]
                            mutation_lock = threading.Lock()
                            barrier = threading.Barrier(workers)
                            classify = rules.xroot_classify
                            request = rules.XrootBatchReader.request
                            response = rules.XrootBatchReader.next_response
                            def change():
                                with mutation_lock:
                                    if not changed[0]:
                                        changed[0] = True
                                        if stage == 'config':
                                            xf._git(self.case.repository, 'config', 'core.useReplaceRefs', 'true')
                                        else:
                                            xf._git(self.case.repository, 'update-ref', refname, bad)
                            def classify_hook(repo):
                                answer = classify(repo)
                                barrier.wait(timeout=20)
                                if stage in ('resolution', 'config', 'base'):
                                    change()
                                barrier.wait(timeout=20)
                                return answer
                            def request_hook(reader, object_id):
                                if stage in ('lookup', 'aba'):
                                    change()
                                return request(reader, object_id)
                            def response_hook(reader):
                                answer = response(reader)
                                if stage == 'aba':
                                    with mutation_lock:
                                        if not removed[0]:
                                            xf._git(self.case.repository, 'update-ref', '-d', refname)
                                            removed[0] = True
                                return answer
                            try:
                                with mock.patch.object(rules, 'xroot_classify', side_effect=classify_hook), \
                                     mock.patch.object(rules.XrootBatchReader, 'request', request_hook), \
                                     mock.patch.object(rules.XrootBatchReader, 'next_response', response_hook):
                                    with ThreadPoolExecutor(workers) as pool:
                                        answers = list(pool.map(lambda _: self.verify(case), range(workers)))
                                    for origin, review, _ in answers:
                                        self.assertEqual('approve', review.fields['DESIGN_REVIEW_VERDICT'])
                                self.assertTrue(changed[0])
                                self.assertEqual('approve', review.fields['DESIGN_REVIEW_VERDICT'])
                                xf._git(self.case.repository, 'update-ref', refname, bad)
                                with self.assertRaises(rules.XrootAxisError) as caught:
                                    self.verify(case)
                                self.assertEqual('authority-verdict', caught.exception.axis)
                                xf._git(self.case.repository, 'update-ref', '-d', refname)
                                self.verify(case)
                            finally:
                                xf._git(self.case.repository, 'update-ref', '-d', refname)
                                xf._git(self.case.repository, 'config', 'core.useReplaceRefs', 'true')

    def test_classification_failure_live_mode_no_refusal(self):
        original = rules.xroot_git
        xf._git(self.case.repository, 'config', 'core.useReplaceRefs', 'true')
        for fault in ('config-fail', 'config-malformed', 'refs-fail', 'refs-malformed',
                      'common-fail', 'common-malformed', 'common-unresolvable', 'top-mismatch', 'stat'):
            def git(repo, *args, **kwargs):
                key = args[0] if args else ''
                selected = ((fault.startswith('config') and key=='config')
                            or (fault.startswith('refs') and key=='replace')
                            or ((fault.startswith('common') or fault=='top-mismatch') and '--git-common-dir' in args))
                if selected:
                    if fault.endswith('fail'):
                        raise rules.XrootAxisError('repository', 'Git object query failed')
                    if fault=='common-unresolvable':
                        return str(self.case.repository)+'\n/no/such/b9-common-dir'
                    if fault=='top-mismatch':
                        return str(self.home)+'\n'+str(self.case.repository/'.git')
                    return b'malformed\x00value' if kwargs.get('binary') else 'malformed\x00value'
                return original(repo, *args, **kwargs)
            stat = os.stat
            def stat_hook(path, *args, **kwargs):
                if fault=='stat' and not isinstance(path, int) and os.path.realpath(path)==os.path.realpath(self.case.repository/'.git'):
                    raise PermissionError('injected common-dir stat failure')
                return stat(path, *args, **kwargs)
            with self.subTest(fault=fault), mock.patch.object(rules, 'xroot_git', side_effect=git), \
                 mock.patch.object(rules.os, 'stat', side_effect=stat_hook), \
                 mock.patch.object(self.cache, 'get', side_effect=AssertionError('unclean classification used cache')), \
                 mock.patch.object(self.cache, 'put', side_effect=AssertionError('unclean classification filled cache')):
                self.assertIsNone(rules.xroot_classify(self.case.repository))
                self.verify()

    def test_unset_config_distinct_from_failure(self):
        self.assertIsInstance(rules.xroot_classify(self.case.repository), rules.XrootNamespace)
        original = rules.xroot_git
        for code, output, stderr in ((128,b'',b''), (1,b'bad',b''), (1,b'',b'bad')):
            def git(repo, *args, **kwargs):
                if args and args[0]=='config':
                    error = rules.XrootAxisError('repository', 'Git object query failed')
                    error.returncode, error.stdout, error.stderr = code, output, stderr
                    raise error
                return original(repo, *args, **kwargs)
            with self.subTest(code=code, output=output, stderr=stderr), mock.patch.object(rules, 'xroot_git', side_effect=git):
                self.assertIsNone(rules.xroot_classify(self.case.repository))

    def test_classification_spends_shared_budget(self):
        original = rules.xroot_classify
        for late in (False, True):
            now = [0]
            def classify(repo):
                value = original(repo)
                if late:
                    now[0] = 6
                return value
            token = rules._XROOT_BUDGET.set((5, []))
            try:
                with self.subTest(late=late), rules.xroot_fact_cache(rules.XrootFactCache()) as cache, \
                     mock.patch.object(rules.time, 'monotonic', side_effect=lambda:now[0]), \
                     mock.patch.object(rules, 'xroot_classify', side_effect=classify):
                    if late:
                        with self.assertRaises(rules.XrootAxisError) as caught:
                            rules.xroot_verify(self.consumer, self.case.declaration)
                        self.assertEqual('capacity', caught.exception.axis)
                        self.assertIn('during classification; this is a time limit on a cold read', caught.exception.detail)
                        self.assertEqual(0, cache.charge_total())
                    else:
                        rules.xroot_verify(self.consumer, self.case.declaration)
            finally:
                rules._XROOT_BUDGET.reset(token)

    def test_concurrent_equal_insert_keeps_framing(self):
        barrier = threading.Barrier(2)
        original = rules.xroot_blob_fact
        computed = []
        def fact(raw):
            value = original(raw)
            if raw == cc.REVIEW.encode():
                computed.append(value)
                barrier.wait(timeout=20)
            return value
        with mock.patch.object(rules, 'xroot_blob_fact', side_effect=fact), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.verify(), range(2)))
        self.assertEqual(2, len(computed))
        self.assertEqual(computed[0], computed[1])
        self.assertEqual(results[0], results[1])
        charge = self.cache.charge_total()
        self.verify()
        self.assertEqual(charge, self.cache.charge_total())

    def test_eviction_between_scan_and_turn(self):
        cache = rules.XrootFactCache(4096)
        with rules.xroot_fact_cache(cache):
            self.verify()
            ns = self.namespace()
            oid = xf._git(self.case.repository, 'rev-parse', self.case.commit+':'+cc.R)
            key = (ns, oid, 'blob')
            get = cache.get
            self.assertIsNotNone(get(key))
            evicted = []
            def lookup(wanted):
                value = get(wanted)
                if wanted == key and not evicted:
                    for i in range(5):
                        cache.put((ns, f'{i:040x}', 'blob'), rules.xroot_blob_fact(b'note'), 2000)
                    self.assertIsNone(get(key))
                    evicted.append(value)
                return value
            with mock.patch.object(cache, 'get', side_effect=lookup), self.requests() as requests:
                self.verify()
            self.assertEqual(1, len(evicted))
            self.assertNotIn(oid+'^{blob}', requests)
            self.assertLessEqual(cache.charge_total(), 4096)

    def test_threads_equal_single_thread(self):
        from relay_engine.tests.test_xroot_cache_differential import sequence
        control = cc.make(self.home/'thread-control')
        def answer(case):
            with cc.budget(rules), rules.xroot_operation():
                return cc.observe(rules, self.consumer, case)
        original = rules.XrootBatchReader
        pids, lock = [], threading.Lock()
        class Reader(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                with lock:
                    pids.append(self.pid)
        before = set(os.listdir('/dev/fd'))
        for workers in (2, 8):
            with corpus.budget(rules), rules.xroot_fact_cache(rules.XrootFactCache()) as cache, \
                 mock.patch.object(rules, 'XrootBatchReader', Reader), ThreadPoolExecutor(workers) as pool:
                for label, case, limit in sequence(self.home/f'threads-{workers}'):
                    with self.subTest(workers=workers, step=label), \
                         mock.patch.object(rules, 'XROOT_MAX_RELAYS', limit or 20000):
                        work = [case if index%2==0 else control for index in range(workers)]
                        with rules.xroot_fact_cache(rules.XrootFactCache()):
                            expected = [answer(item) for item in work]
                        actual = list(pool.map(answer, work))
                        self.assertEqual(expected, actual)
                        self.assertLessEqual(cache.charge_total(), rules.XROOT_CACHE_BOUND)
                print('threads', workers, 'charge', cache.charge_total(), 'rss', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        self.assertTrue(pids)
        for pid in pids:
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        self.assertEqual(before, set(os.listdir('/dev/fd')))

    def test_not_cached_kinds(self):
        repo = self.case.repository
        tree_oid = xf._git(repo, 'rev-parse', self.case.commit+':relays')
        cases = [('missing', cc.pin(repo, dict(cc.files(), **{cc.R: (b'100644', '1'*40)})), '1'*40),
                 ('peel', cc.pin(repo, dict(cc.files(), **{cc.R: (b'100644', tree_oid)})), tree_oid),
                 ('size', self.case, xf._git(repo, 'rev-parse', self.case.commit+':'+cc.R))]
        for kind, case, oid in cases:
            with self.subTest(kind=kind), rules.xroot_fact_cache(rules.XrootFactCache()) as cache, \
                 mock.patch.object(rules, 'XROOT_MAX_RELAY_BYTES', 1 if kind=='size' else 256*1024*1024):
                ns = self.namespace()
                for _ in range(2):
                    with self.requests() as requests, self.assertRaises(rules.XrootAxisError):
                        self.verify(case)
                    self.assertIn(oid+'^{blob}', requests)
                    self.assertIsNone(cache.get((ns, oid, 'blob')))
        for kind in ('transport', 'deadline'):
            original = rules.XrootBatchReader
            class Failed(original):
                def next_response(self):
                    if kind=='transport':
                        os.kill(self.pid, signal.SIGKILL)
                    else:
                        self.deadline = time.monotonic()-1
                    return super().next_response()
            with self.subTest(kind=kind), rules.xroot_fact_cache(rules.XrootFactCache()) as cache, \
                 mock.patch.object(rules, 'XrootBatchReader', Failed):
                for _ in range(2):
                    with self.requests() as requests, self.assertRaises(rules.XrootAxisError) as caught:
                        self.verify()
                    self.assertEqual('capacity' if kind=='deadline' else 'repository', caught.exception.axis)
                    self.assertTrue(requests)
                    self.assertEqual(0, cache.charge_total())

    @contextmanager
    def charged_responses(self, module):
        now, requests, framed = [0], [], []
        original = module.XrootBatchReader
        class Reader(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.requested = []
            def request(self, oid):
                self.requested.append(oid)
                requests.append(oid)
                return super().request(oid)
            def next_response(self):
                now[0] += 2
                answer = super().next_response()
                framed.append(self.requested.pop(0))
                return answer
        token = module._XROOT_BUDGET.set((5, []))
        try:
            with mock.patch.object(module, 'XrootBatchReader', Reader), \
                 mock.patch.object(module.time, 'monotonic', side_effect=lambda:now[0]):
                yield now, requests, framed
        finally:
            module._XROOT_BUDGET.reset(token)

    def test_deadline_partial_facts_reused(self):
        with self.charged_responses(rules) as (now, requests, framed):
            token = rules._XROOT_BUDGET.set((5, []))
            try:
                with self.assertRaises(rules.XrootAxisError) as caught:
                    rules.xroot_verify(self.consumer, self.case.declaration)
                self.assertEqual('capacity', caught.exception.axis)
                self.assertEqual(1, caught.exception.progress['relays_read'])
                first_framed = set(framed)
                self.assertEqual(2, len(first_framed))
                count = len(requests)
                now[0] = 0
                result = rules.xroot_verify(self.consumer, self.case.declaration)
                self.assertEqual(2, len(result[2]))
                self.assertTrue(requests[count:])
                self.assertFalse(first_framed.intersection(requests[count:]))
            finally:
                rules._XROOT_BUDGET.reset(token)

    def test_deadline_cut_then_second_check_completes(self):
        raw = body(extra='\n'.join(k+': '+v for k,v in self.case.declaration.items()))
        (self.consumer/'PLAN-pair-planner-20261013-120000.md').write_bytes(raw)
        for caller in ('pair', 'root', 'standalone'):
            module = _standalone() if caller=='standalone' else rules
            with self.subTest(caller=caller), ExitStack() as stack:
                fixture, pair, _ = stack.enter_context(cc.authority_fixture(self.case.declaration))
                selection, errors = [], []
                original_select, original_verify = module.xroot_unique_latest, module.xroot_verify
                def select(*args, **kwargs):
                    value = original_select(*args, **kwargs)
                    selection.append(value.name)
                    return value
                def verify(*args, **kwargs):
                    try:
                        return original_verify(*args, **kwargs)
                    except module.XrootAxisError as exc:
                        errors.append((exc.axis, exc.detail))
                        raise
                stack.enter_context(mock.patch.object(module, 'xroot_unique_latest', side_effect=select))
                stack.enter_context(mock.patch.object(module, 'xroot_verify', side_effect=verify))
                def check():
                    selection.clear(); errors.clear()
                    if caller=='pair':
                        value = fixture.check(pair)
                        answer = dict(allowed=value['allowed'], reasons=value['reasons'])
                    else:
                        value = module.lint_relay_root(self.consumer)
                        answer = dict(allowed=not value.errors, reasons=list(value.errors))
                    answer.update(axis=errors[0][0] if errors else None,
                                  detail=errors[0][1] if errors else None, selected=list(selection))
                    return answer
                with module.xroot_fact_cache(module.XrootFactCache()), corpus.budget(module), self.requests(module) as all_requests:
                    expected = check()
                self.assertTrue(expected['allowed'])
                self.assertEqual(2, len(expected['selected']))
                with module.xroot_fact_cache(module.XrootFactCache()), self.charged_responses(module) as (now, requests, framed):
                    first = check()
                    self.assertFalse(first['allowed'])
                    self.assertEqual('capacity', first['axis'])
                    self.assertIn('time limit on a cold read', first['detail'])
                    self.assertNotRegex(first['detail'], r'retry|again')
                    first_framed, count = set(framed), len(requests)
                    now[0] = 0
                    second = check()
                    self.assertEqual(expected, second)
                    self.assertEqual(set(all_requests)-first_framed, set(requests[count:]))
                    with module.xroot_fact_cache(module.XrootFactCache()):
                        now[0] = 0
                        third = check()
                    self.assertFalse(third['allowed'])
                    self.assertEqual('capacity', third['axis'])

    def test_fact_charge_matches_encoded_bytes(self):
        with self.requests() as requests:
            self.verify()
        ns = self.namespace()
        expected = 256
        for requested in set(requests):
            oid, suffix = requested.split('^{')
            kind = suffix[:-1]
            fact = self.cache.get((ns, oid, kind))
            self.assertIsNotNone(fact)
            expected += 256
            if kind == 'tree':
                expected += sum(len(name)+20+64 for name, _, _ in fact.entries)
            elif fact.retained:
                expected += len(fact.text.encode())
                expected += sum(len(k.encode())+len(v.encode()) for k,v in fact.fields)
        self.assertEqual(expected, self.cache.charge_total())
        self.assertEqual(512*1024*1024, rules.XROOT_CACHE_BOUND)

    def test_tree_fact_preserves_duplicates_and_malformed_position(self):
        raw = xf._tree_entry(b'100644', b'DESIGN-a.md', '1'*40)
        raw += xf._tree_entry(b'100644', b'DESIGN-a.md', '2'*40)
        fact = rules.xroot_tree_fact(raw+b'broken')
        self.assertEqual(((b'DESIGN-a.md', 'blob', '1'*40),
                          (b'DESIGN-a.md', 'blob', '2'*40)), fact.entries)
        self.assertEqual(2, fact.error[0])
        self.assertIn('malformed entry bytes', fact.error[1])

    def test_live_mode_reclassifies_between_edges(self):
        oid, _, refname = self.replacement()
        with rules.xroot_operation():
            for note in ('first', 'second'):
                replacement = xf._literal_object(self.case.repository, 'blob', (cc.REVIEW+'NOTE: '+note+'\n').encode())
                xf._git(self.case.repository, 'update-ref', refname, replacement)
                try:
                    _, review, _ = rules.xroot_verify(self.consumer, self.case.declaration)
                    self.assertEqual(note, review.fields['NOTE'])
                    self.assertEqual(0, self.cache.charge_total())
                finally:
                    xf._git(self.case.repository, 'update-ref', '-d', refname)

    def test_classification_query_timeout_names_classification(self):
        now = [0]
        original = rules.xroot_git
        def git(repo, *args, **kwargs):
            if args and args[0]=='config':
                now[0] = 6
            return original(repo, *args, **kwargs)
        token = rules._XROOT_BUDGET.set((5, []))
        try:
            with mock.patch.object(rules.time, 'monotonic', side_effect=lambda:now[0]), \
                 mock.patch.object(rules, 'xroot_git', side_effect=git):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    rules.xroot_verify(self.consumer, self.case.declaration)
                self.assertEqual('capacity', caught.exception.axis)
                self.assertEqual('Git object query timed out at the 5 s bound during classification; '
                                 'this is a time limit on a cold read, not a lineage fault; '
                                 'a large root can reach it on a busy machine', caught.exception.detail)
                self.assertEqual(0, self.cache.charge_total())
        finally:
            rules._XROOT_BUDGET.reset(token)

    def test_classification_rejects_non_directory_common_path(self):
        original = rules.xroot_git
        def git(repo, *args, **kwargs):
            if '--git-common-dir' in args:
                return str(self.case.repository.resolve())+'\n'+str(self.case.repository/'.git/HEAD')
            return original(repo, *args, **kwargs)
        with mock.patch.object(rules, 'xroot_git', side_effect=git):
            self.assertIsNone(rules.xroot_classify(self.case.repository))

    def test_replace_base_without_trailing_slash_keeps_live_view(self):
        base = 'refs/b9-prefix'
        oid, bad, refname = self.replacement(base)
        xf._git(self.case.repository, 'update-ref', refname, bad)
        with mock.patch.dict(os.environ, {'GIT_REPLACE_REF_BASE': base}), cc.budget(old):
            expected = corpus.verify(old, self.consumer, self.case.declaration)
            self.assertEqual('authority-verdict', expected['axis'])
            self.assertIsNone(rules.xroot_classify(self.case.repository))
            self.assertEqual(expected, corpus.verify(rules, self.consumer, self.case.declaration))
