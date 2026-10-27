"""Three independent oracles over commit, replacement and namespace sequences."""
from contextlib import nullcontext
import os
from pathlib import Path
import resource
import tempfile
import unittest
from unittest import mock

from relay_engine import rules, authority
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests import xroot_reference as reference
from relay_engine.tests import xroot_baseline_b8d87eb as old
import xrootfixgen as xf


def sequence(home):
    """Each yield fixes a repository view until the consumer asks for the next."""
    home = Path(home)
    original = cc.make(home/'foreign')
    repo = original.repository
    base = cc.files()
    yield 'base', original, None
    for folder in ('', 'a/'):
        source = 'relays/'+folder+'DESIGN-note-20260801-090000.md'
        graph = dict(base, **{source: 'PHASE: SITREP\nDESIGN_DOC_ID: other\n'})
        yield 'add-'+folder, cc.pin(repo, graph), None
        graph[source] += 'NOTE: changed\n'
        yield 'change-'+folder, cc.pin(repo, graph), None
        target = 'relays/'+folder+'DESIGN-renamed-20260801-090000.md'
        graph[target] = graph.pop(source)
        yield 'rename-inside-'+folder, cc.pin(repo, graph), None
        graph['relays/b/DESIGN-renamed-20260801-090000.md'] = graph.pop(target)
        yield 'rename-across-'+folder, cc.pin(repo, graph), None
        yield 'remove-'+folder, cc.pin(repo, base), None
    malformed = xf._literal_object(repo, 'tree', b'broken')
    link = xf._literal_object(repo, 'blob', b'destination')
    for candidate in (False, True):
        prefix = 'DESIGN-' if candidate else 'PLAN-'
        for kind, content in (
            ('utf8', b'\xff'), ('tree', (b'40000', malformed)),
            ('symlink', (b'120000', link)), ('phase', 'PHASE: DESIGN\nPHASE: SITREP\n')):
            name = 'relays/'+prefix+kind+'-20260801-090000.md'
            yield f'fault-{candidate}-{kind}', cc.pin(repo, dict(base, **{name: content})), None
            yield f'remove-fault-{candidate}-{kind}', cc.pin(repo, base), None
    for phase in ('PLAN', 'DESIGN', 'PLAN'):
        graph = dict(base, **{'relays/'+phase+'-note-20260801-090000.md': 'DESIGN_DOC_ID: other\nPHASE: SITREP\n'})
        yield 'candidate-rename-'+phase, cc.pin(repo, graph), None
    missing_review = dict(base)
    missing_review.pop(cc.R)
    yield 'before-review', cc.pin(repo, missing_review), None
    yield 'add-review', cc.pin(repo, base), None
    competitor = dict(base, **{'relays/DESIGN-planner-20260801-120000.md': cc.DESIGN.replace('f-design', 'later-design')})
    yield 'later-competing-origin', cc.pin(repo, competitor), None
    yield 'remove-competing-origin', cc.pin(repo, base), None
    earlier = cc.pin(repo, base)
    later = cc.pin(repo, dict(base, **{'relays/DESIGN-note.md': 'DESIGN_DOC_ID: other\n'}))
    for name, case in (('forward', later), ('backward', earlier), ('forward-again', later)):
        xf._git(repo, 'update-ref', 'refs/heads/main', case.commit)
        yield name, case, None
    yield 'under-limit', earlier, 2
    yield 'over-limit', later, 2
    yield 'back-under-limit', earlier, 2
    timestamps = dict(base, **{'relays/b/DESIGN-planner-20260801-103000.md': cc.DESIGN})
    yield 'same-bytes-own-timestamp', cc.pin(repo, timestamps), None
    yield 'timestamp-removed', earlier, None
    bad_review = cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise')
    changed_review = cc.REVIEW.replace('PARENT_DISPATCH_ID: f-design', 'PARENT_DISPATCH_ID: absent')
    for kind in ('blob', 'tree', 'peel'):
        case = earlier
        if kind=='blob':
            target = xf._git(repo, 'rev-parse', case.commit+':'+cc.R)
            replacements = [xf._literal_object(repo, 'blob', text.encode()) for text in (bad_review, changed_review)]
        elif kind=='tree':
            target = xf._git(repo, 'rev-parse', case.commit+':relays')
            replacements = [cc._object_tree(repo, {Path(cc.O).name: cc.DESIGN, Path(cc.R).name: text})
                            for text in (bad_review, changed_review)]
        else:
            target = xf._literal_commit(repo, cc._object_tree(repo, {'DESIGN-note.md': 'PHASE: SITREP\nDESIGN_DOC_ID: other\n'}))
            replacements = [xf._literal_commit(repo, cc._object_tree(repo, {'DESIGN-fault.md': content}))
                            for content in (b'\xff', 'PHASE: DESIGN\nPHASE: SITREP\n')]
            case = cc.pin(repo, dict(base, **{'relays/peeled': (b'40000', target)}))
        refname = 'refs/replace/'+target
        yield kind+'-before-replace', case, None
        try:
            for name, replacement in zip(('add', 'change'), replacements):
                xf._git(repo, 'update-ref', refname, replacement)
                yield kind+'-replace-'+name, case, None
        finally:
            xf._git(repo, 'update-ref', '-d', refname)
        yield kind+'-replace-remove', case, None
    second = cc.make(home/'missing')
    missing = xf._git(second.repository, 'rev-parse', second.commit+':'+cc.R)
    (second.repository/'.git/objects'/missing[:2]/missing[2:]).unlink()
    for name, case in (('namespace-valid-first', earlier), ('namespace-missing-second', second),
                       ('namespace-missing-first', second), ('namespace-valid-second', earlier)):
        yield name, case, None
    repo.rename(home/'retired')
    recreated = cc.make(repo, dict(base, **{cc.R: bad_review}))
    yield 'recreated-repository', recreated, None


class TestXrootCacheDifferential(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(rules, 'XrootFactCache'), 'object-fact cache is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='b9-cache-differential-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.consumer = self.home/'consumer'
        xf._init_repo(self.consumer)
        clock = mock.patch.object(authority, 'datetime', cc.FixedDateTime)
        clock.start()
        self.addCleanup(clock.stop)

    def run_sequence(self, bound, require_external=False):
        cache = rules.XrootFactCache(bound)
        counts = dict(steps=0, inside=0, outside=0, structural=0)
        hits = [0]
        cold_facts = {}
        for label, case, limit in sequence(self.home/f'bound-{bound}'):
            with self.subTest(bound=bound, step=label), corpus.budget(rules), cc.budget(old), \
                 mock.patch.object(rules, 'XROOT_MAX_RELAYS', limit or 20000):
                warm_facts = []
                original_get, original_put = cache.get, cache.put
                def get(key):
                    fact = original_get(key)
                    if fact is not None:
                        hits[0] += 1
                        warm_facts.append((key, fact))
                    return fact
                def put(key, fact, charge):
                    warm_facts.append((key, fact))
                    return original_put(key, fact, charge)
                with rules.xroot_fact_cache(cache), mock.patch.object(cache, 'get', side_effect=get), \
                     mock.patch.object(cache, 'put', side_effect=put):
                    warm = cc.observe(rules, self.consumer, case, require_external=require_external)
                empty_cache = rules.XrootFactCache(bound)
                empty_put = empty_cache.put
                def cold_put(key, fact, charge):
                    if key in cold_facts:
                        self.assertEqual(cold_facts[key], fact)
                    cold_facts[key] = fact
                    return empty_put(key, fact, charge)
                with rules.xroot_fact_cache(empty_cache), mock.patch.object(empty_cache, 'put', side_effect=cold_put):
                    empty = cc.observe(rules, self.consumer, case, require_external=require_external)
                self.assertEqual(empty, warm)
                for key, fact in warm_facts:
                    self.assertIn(key, cold_facts)
                    self.assertEqual(cold_facts[key], fact)
                with reference.xroot_reference_walk(), \
                     mock.patch.object(cache, 'get', side_effect=AssertionError('reference cache lookup')), \
                     mock.patch.object(cache, 'put', side_effect=AssertionError('reference cache insertion')), \
                     rules.xroot_fact_cache(cache):
                    reference_answer = cc.observe(rules, self.consumer, case, require_external=require_external)
                self.assertEqual(reference_answer, warm)
                verdict = corpus.domain(self.consumer, case.declaration, reference.xroot_old_domain)
                counts['steps'] += 1
                counts[verdict['kind']] += 1
                if verdict['kind'] != 'outside':
                    baseline = corpus.verify(old, self.consumer, case.declaration)
                    if label=='over-limit':
                        self.assertIsNone(baseline['axis'])
                        self.assertEqual('capacity', warm['verify']['axis'])
                        self.assertIn('XROOT_MAX_RELAYS', warm['verify']['detail'])
                    elif verdict['kind']=='structural':
                        self.assertEqual((baseline['axis'], baseline['detail']),
                                         (warm['verify']['axis'], warm['verify']['detail']))
                    else:
                        self.assertEqual(baseline, warm['verify'])
                if label=='same-bytes-own-timestamp':
                    self.assertEqual('b/DESIGN-planner-20260801-103000.md', warm['verify']['origin'])
                self.assertLessEqual(cache.charge_total(), bound)
                if bound < 768:
                    self.assertLessEqual(len(cache._entries), 1)
        self.assertGreater(hits[0], 0, 'eviction arm never reused a warm fact')
        self.assertEqual(dict(steps=59, inside=52, outside=5, structural=2), counts)
        print('cache-differential', counts, 'bound', bound, 'charge', cache.charge_total(),
              'rss', resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    def test_cache_differential_sequences(self):
        self.run_sequence(rules.XROOT_CACHE_BOUND)

    def test_cache_differential_under_eviction(self):
        case = cc.make(self.home/'one-entry-size')
        raw = rules.xroot_git(case.repository, 'cat-file', 'tree', case.commit+':relays', binary=True)
        one_entry = rules._xroot_fact_charge(rules.xroot_tree_fact(raw)) + 256
        self.assertLess(one_entry, 768)  # namespace plus two minimum facts cannot fit.
        for bound in (one_entry, 4096):
            self.run_sequence(bound)
