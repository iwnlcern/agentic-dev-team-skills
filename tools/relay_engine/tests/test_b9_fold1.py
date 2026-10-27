"""PR39 fold witnesses; DD r11 erratum 1 governs replacement classification."""
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from relay_engine import rules, authority
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests.test_xroot import _standalone
import xrootfixgen as xf


def verify(module, consumer, declaration):
    # Standalone succeeds with None; native additionally returns selected relays.
    try:
        module.xroot_verify(consumer, declaration)
        return dict(axis=None, detail=None)
    except module.XrootAxisError as exc:
        return corpus.normalize(dict(axis=exc.axis, detail=exc.detail), consumer, declaration)


class TestFoldReplacement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = (rules, _standalone())

    def test_broken_refs_use_live_view_both_copies_and_paths(self):
        # Omitting broken refs from classification produces a wrong allow.
        with tempfile.TemporaryDirectory(prefix='fold-broken-') as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            for backend in ('files', 'reftable'):
                repo = home/backend
                probe = subprocess.run(['git', 'init', '-q', '--ref-format='+backend, str(repo)],
                                       capture_output=True)
                if probe.returncode:
                    self.assertEqual('reftable', backend)
                    self.assertIn(b'unknown option', probe.stderr)
                    continue
                case = cc.pin(repo, cc.files())
                targets = dict(origin=cc.O, review=cc.R, tree='relays',
                               design='designs/d1.md', commit=None)
                for target, path in targets.items():
                    oid = case.commit if path is None else xf._git(repo, 'rev-parse', case.commit+':'+path)
                    # Invalid loose contents have no representation in reftable.
                    shapes = ('symbolic', 'invalid') if backend == 'files' else ('symbolic',)
                    for shape in shapes:
                        ref = 'refs/replace/'+oid
                        if shape == 'symbolic':
                            xf._git(repo, 'symbolic-ref', ref, 'refs/heads/absent')
                        else:
                            loose = repo/'.git'/ref
                            loose.parent.mkdir(parents=True, exist_ok=True)
                            loose.write_text('not-an-object-id\n')
                        try:
                            for module in self.modules:
                                with self.subTest(backend=backend, target=target, shape=shape,
                                                  module=module.__name__), cc.budget(module):
                                    self.assertIsNone(module.xroot_classify(repo))
                                    answer = verify(module, consumer, case.declaration)
                                    self.assertIsNotNone(answer['axis'])
                                    with mock.patch.object(authority, 'rules', module), \
                                         mock.patch.object(authority, 'datetime', cc.FixedDateTime):
                                        answers = cc.whole_answers(case.declaration)
                                    for route, result in answers.items():
                                        self.assertFalse(result['allowed'], route)
                        finally:
                            if shape == 'symbolic':
                                xf._git(repo, 'symbolic-ref', '--delete', ref)
                            else:
                                loose.unlink()

    def test_custom_base_positive_and_negative_both_copies(self):
        with tempfile.TemporaryDirectory(prefix='fold-base-') as temporary:
            case = cc.make(Path(temporary)/'foreign')
            consumer = Path(temporary)/'consumer'
            xf._init_repo(consumer)
            oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cc.R)
            xf._git(case.repository, 'symbolic-ref', 'refs/alternate/'+oid, 'refs/heads/absent')
            for module in self.modules:
                with self.subTest(module=module.__name__), cc.budget(module):
                    with mock.patch.dict(os.environ, {'GIT_REPLACE_REF_BASE': 'refs/alternate/'}):
                        self.assertIsNone(module.xroot_classify(case.repository))
                        self.assertIsNotNone(verify(module, consumer, case.declaration)['axis'])
                    with mock.patch.dict(os.environ, {'GIT_REPLACE_REF_BASE': 'refs/replace/'}):
                        self.assertIsInstance(module.xroot_classify(case.repository), module.XrootNamespace)
                        self.assertIsNone(verify(module, consumer, case.declaration)['axis'])

    def test_success_with_stderr_is_live_both_copies(self):
        # A real wrapper preserves Git output/status but emits a warning.
        with tempfile.TemporaryDirectory(prefix='fold-stderr-') as temporary:
            home = Path(temporary)
            case = cc.make(home/'foreign')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            real_git = shutil.which('git')
            wrapper = home/'bin'
            wrapper.mkdir()
            script = wrapper/'git'
            script.write_text('#!/bin/sh\ncase "$*" in *"replace -l"*) echo warning >&2;; esac\nexec "'+
                              real_git+'" "$@"\n')
            script.chmod(0o755)
            for module in self.modules:
                with self.subTest(module=module.__name__), cc.budget(module), \
                     mock.patch.dict(os.environ, {'PATH':str(wrapper)+os.pathsep+os.environ['PATH']}):
                    self.assertIsNone(module.xroot_classify(case.repository))
                    self.assertIsNone(verify(module, consumer, case.declaration)['axis'])


class TestFoldProcess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = (rules, _standalone())

    def test_launch_seam_refuses_non_git(self):
        for module in self.modules:
            for command in ([], ['sh', '-c', 'true'], ['/usr/bin/git', '--version']):
                with self.subTest(module=module.__name__, command=command), \
                     mock.patch.object(module.subprocess, 'Popen') as popen:
                    with self.assertRaises(ValueError):
                        module._xroot_start_git(command, stdin=-3, stdout=-1, stderr=-1)
                    popen.assert_not_called()

    def test_operation_reaps_all_readers_if_first_close_raises(self):
        import time
        with tempfile.TemporaryDirectory(prefix='fold-close-') as temporary:
            repo = cc.make(Path(temporary)/'foreign').repository
            for module in self.modules:
                with self.subTest(module=module.__name__), ExitStack() as cleanup:
                    readers = [module.XrootBatchReader(repo, time.monotonic()+600) for _ in range(2)]
                    for reader in readers:
                        cleanup.callback(reader.close)
                    close = readers[0].close
                    def failing_close():
                        close()
                        raise RuntimeError('first close failed')
                    with mock.patch.object(readers[0], 'close', side_effect=failing_close):
                        with self.assertRaisesRegex(RuntimeError, 'first close failed'):
                            with module.xroot_operation():
                                module._XROOT_READERS.get().update(enumerate(readers))
                    for reader in readers:
                        self.assertIsNotNone(reader._process.poll())
                        self.assertTrue(reader._process.stdin.closed)
                        self.assertTrue(reader._process.stdout.closed)
                        with self.assertRaises(ChildProcessError):
                            os.waitpid(reader.pid, os.WNOHANG)

    def test_idle_stdin_hangup_waits_without_cpu_spin(self):
        import sys
        import time
        with tempfile.TemporaryDirectory(prefix='fold-hup-') as temporary:
            home = Path(temporary)
            script = home/'git'
            script.write_text('#!'+sys.executable+'\nimport os,time\nos.close(0)\ntime.sleep(20)\n')
            script.chmod(0o755)
            for module in self.modules:
                with self.subTest(module=module.__name__), \
                     mock.patch.dict(os.environ, {'PATH':str(home)+os.pathsep+os.environ['PATH']}):
                    wall = time.monotonic()
                    cpu = time.process_time()
                    with module.XrootBatchReader(home, wall+2) as reader:
                        with self.assertRaises(module.XrootAxisError) as caught:
                            reader.next_response()
                        self.assertEqual('capacity', caught.exception.axis)
                        self.assertIn('Git batch reader timed out', caught.exception.detail)
                    elapsed = time.monotonic()-wall
                    used = time.process_time()-cpu
                    self.assertLess(used, elapsed/4, (used, elapsed))


class TestFoldCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = (rules, _standalone())

    def test_clean_queries_ignore_interleaved_design_blob_replacement(self):
        with tempfile.TemporaryDirectory(prefix='fold-query-view-') as temporary:
            home = Path(temporary)
            case = cc.make(home/'foreign')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            oid = xf._git(case.repository, 'rev-parse', case.commit+':designs/d1.md')
            replacement = xf._literal_object(case.repository, 'blob', b'changed design')
            ref = 'refs/replace/'+oid
            for module in self.modules:
                observed = []
                classified = [False]
                classify, start = module.xroot_classify, module._xroot_start_git
                def classify_then_replace(repo):
                    namespace = classify(repo)
                    self.assertIsNotNone(namespace)
                    xf._git(repo, 'update-ref', ref, replacement)
                    classified[0] = True
                    return namespace
                def launch(command, **kwargs):
                    if classified[0]:
                        observed.append(command)
                        self.assertIn('--no-replace-objects', command)
                    return start(command, **kwargs)
                try:
                    with self.subTest(module=module.__name__), cc.budget(module), \
                         mock.patch.object(module, 'xroot_classify', side_effect=classify_then_replace), \
                         mock.patch.object(module, '_xroot_start_git', side_effect=launch):
                        self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                        self.assertTrue(observed)
                        self.assertTrue(any('cat-file' in argv for argv in observed))
                finally:
                    xf._git(case.repository, 'update-ref', '-d', ref)

    def test_namespace_isolation_both_copies(self):
        with tempfile.TemporaryDirectory(prefix='fold-namespace-') as temporary:
            home = Path(temporary)
            first, second = cc.make(home/'first'), cc.make(home/'second')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            oid = xf._git(second.repository, 'rev-parse', second.commit+':'+cc.R)
            (second.repository/'.git/objects'/oid[:2]/oid[2:]).unlink()
            for module in self.modules:
                for order in ((first, second), (second, first)):
                    with self.subTest(module=module.__name__, order=[c.repository.name for c in order]), \
                         cc.budget(module), module.xroot_fact_cache(module.XrootFactCache()):
                        for case in order:
                            answer = verify(module, consumer, case.declaration)
                            if case is first:
                                self.assertIsNone(answer['axis'])
                            else:
                                self.assertIsNotNone(answer['axis'])

    def test_replacement_add_change_remove_and_disabled_views_both_copies(self):
        with tempfile.TemporaryDirectory(prefix='fold-stable-views-') as temporary:
            home = Path(temporary)
            case = cc.make(home/'foreign')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cc.R)
            bad = xf._literal_object(case.repository, 'blob',
                                     cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise').encode())
            changed = xf._literal_object(case.repository, 'blob',
                                         cc.REVIEW.replace('PARENT_DISPATCH_ID: f-design', 'PARENT_DISPATCH_ID: missing').encode())
            ref = 'refs/replace/'+oid
            for module in self.modules:
                with self.subTest(module=module.__name__), cc.budget(module), \
                     module.xroot_fact_cache(module.XrootFactCache()) as cache:
                    self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                    ns = module.xroot_classify(case.repository)
                    original = cache.get((ns, oid, 'blob'))
                    self.assertIsNotNone(original)
                    try:
                        for replacement in (bad, changed):
                            xf._git(case.repository, 'update-ref', ref, replacement)
                            self.assertIsNotNone(verify(module, consumer, case.declaration)['axis'])
                            self.assertEqual(original, cache.get((ns, oid, 'blob')))
                        for disabled in ('config', 'environment'):
                            xf._git(case.repository, 'config', 'core.useReplaceRefs',
                                    'false' if disabled == 'config' else 'true')
                            with mock.patch.dict(os.environ, {'GIT_NO_REPLACE_OBJECTS':
                                                              '1' if disabled == 'environment' else ''}):
                                self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                    finally:
                        xf._git(case.repository, 'config', 'core.useReplaceRefs', 'true')
                        xf._git(case.repository, 'update-ref', '-d', ref)
                    self.assertIsNone(verify(module, consumer, case.declaration)['axis'])

    def test_recreated_repository_changes_namespace_both_copies(self):
        with tempfile.TemporaryDirectory(prefix='fold-recreated-') as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            for index, module in enumerate(self.modules):
                case = cc.make(home/str(index))
                with self.subTest(module=module.__name__), cc.budget(module), \
                     module.xroot_fact_cache(module.XrootFactCache()):
                    before = module.xroot_classify(case.repository)
                    self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                    case.repository.rename(home/(str(index)+'-retired'))
                    replacement = cc.make(case.repository, dict(cc.files(), **{
                        cc.R: cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise')}))
                    after = module.xroot_classify(case.repository)
                    self.assertEqual(before.location, after.location)
                    self.assertNotEqual((before.device, before.inode), (after.device, after.inode))
                    self.assertEqual('authority-verdict', verify(module, consumer, replacement.declaration)['axis'])

    def test_fully_warm_first_and_last_hit_obey_deadline(self):
        import time
        with tempfile.TemporaryDirectory(prefix='fold-hit-deadline-') as temporary:
            home = Path(temporary)
            case = cc.make(home/'foreign')
            consumer = home/'consumer'
            xf._init_repo(consumer)
            for module in self.modules:
                cache = module.XrootFactCache()
                with cc.budget(module), module.xroot_fact_cache(cache):
                    self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                    hits = []
                    get = cache.get
                    def observe(key):
                        value = get(key)
                        self.assertIsNotNone(value)
                        hits.append(key)
                        return value
                    with mock.patch.object(cache, 'get', side_effect=observe), \
                         mock.patch.object(module.XrootBatchReader, 'request',
                                           side_effect=AssertionError('warm control requested batch')):
                        self.assertIsNone(verify(module, consumer, case.declaration)['axis'])
                    self.assertGreater(len(hits), 1)
                    for at in (1, len(hits)):
                        calls, expired = [0], [False]
                        real_clock = time.monotonic
                        def clock():
                            return real_clock() + (1200 if expired[0] else 0)
                        def hit(key):
                            value = get(key)
                            self.assertIsNotNone(value)
                            calls[0] += 1
                            expired[0] = calls[0] == at
                            return value
                        with self.subTest(module=module.__name__, hit=at), \
                             mock.patch.object(cache, 'get', side_effect=hit), \
                             mock.patch.object(module.time, 'monotonic', side_effect=clock), \
                             mock.patch.object(module.XrootBatchReader, 'request',
                                               side_effect=AssertionError('warm expiry requested batch')):
                            answer = verify(module, consumer, case.declaration)
                            self.assertEqual('capacity', answer['axis'])
                            self.assertEqual(at, calls[0])
                            if at == 1:
                                self.assertIn('at tree relays,', answer['detail'])

    def test_predicate_needs_hyphen(self):
        for module in self.modules:
            for name in (b'DESIGNx.md', b'DESIGN.md'):
                with self.subTest(module=module.__name__, name=name):
                    self.assertFalse(module.xroot_is_candidate(name))
            self.assertTrue(module.xroot_is_candidate(b'DESIGN-x.md'))


class TestFoldReasons(unittest.TestCase):
    def test_erratum_two_rows_isolated_and_with_lineage_reason(self):
        # DD-v297-b9 r11 erratum 1 section 2, rows 1 and 2 (R229).
        from relay_engine.tests.test_authority_b9 import TestAuthorityB9
        helper = TestAuthorityB9()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        bad_lineage = cc.make(helper.home/'other', dict(cc.files(), **{
            cc.R: cc.REVIEW.replace('VERDICT: approve', 'VERDICT: must-revise')}))
        with mock.patch.object(authority, 'datetime', cc.FixedDateTime):
            for row in ('plan-digest', 'live-missing-target'):
                declaration = dict(helper.case.declaration)
                ref = None
                if row == 'plan-digest':
                    declaration.update(PLAN_SOURCE_REPO=str(helper.case.repository),
                                       PLAN_SOURCE_COMMIT=helper.case.commit,
                                       PLAN_LOCK_ID='designs/d1.md @ '+'0'*64)
                    expected = 'authority.artifact-changed'
                else:
                    oid = xf._git(helper.case.repository, 'rev-parse', helper.case.commit+':'+cc.R)
                    ref = helper.case.repository/'.git/refs/replace'/oid
                    ref.parent.mkdir(parents=True, exist_ok=True)
                    ref.write_text('f'*40+'\n')
                    expected = 'authority.could-not-evaluate'
                try:
                    for coexist in (False, True):
                        declarations = [declaration] + ([bad_lineage.declaration] if coexist else [])
                        with self.subTest(row=row, coexist=coexist), cc.budget(rules), \
                             helper.direct_chain(declarations) as (fixture, grant, _):
                            answer = fixture.check(grant, action='merge')
                            reasons = [expected] + (['authority.lineage-incomplete'] if coexist else [])
                            self.assertEqual(sorted(reasons), sorted(answer['reasons']))
                            self.assertFalse(answer['allowed'])
                            entry = next(item for item in answer['external'] if item['reason'] == expected)
                            self.assertEqual('byte' if row == 'plan-digest' else 'repository', entry['axis'])
                            self.assertEqual('plan-source' if row == 'plan-digest' else 'cross-root-design', entry['gate'])
                finally:
                    if ref is not None:
                        ref.unlink()


class TestFoldParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = (rules, _standalone())

    def messages(self, module, consumer, case, label=''):
        from relay_engine.tests import xroot_b9_cases as cases
        from contextlib import nullcontext
        fields = dict(case.declaration, ROLE='Pair Planner', FROM='p.planner', PHASE='PLAN')
        text = '\n'.join(key+': '+value for key, value in fields.items())
        result = module.LintResult()
        with cc.budget(module), module.xroot_fact_cache(module.XrootFactCache()), ExitStack() as stack:
            if label == 'T1b-timeout-population-blob':
                import time
                oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cc.R)
                original = module.XrootBatchReader
                class Expired(original):
                    def request(self, spec):
                        if spec == oid+'^{blob}':
                            self.deadline = time.monotonic()-1
                        return super().request(spec)
                stack.enter_context(mock.patch.object(module, 'XrootBatchReader', Expired))
            elif label.startswith('T'):
                stack.enter_context(cases.deadline_fault(case, module))
            module.xroot_design_gate(consumer, consumer/'PLAN-pair-planner-20260817-120000.md',
                                    fields, text, [], result)
        return sorted(result.errors), sorted(result.warnings)

    def test_generated_transition_rows_render_identical_messages(self):
        from relay_engine.tests import xroot_b9_cases as cases
        with tempfile.TemporaryDirectory(prefix='fold-transition-parity-') as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            # All original table rows, including real count, byte and metadata limits.
            rows = cases.materialize(home/'rows')
            self.assertEqual(set(cases.CASES), set(rows))
            for label, case in rows.items():
                with self.subTest(row=label):
                    answers = [self.messages(module, consumer, case, label) for module in self.modules]
                    self.assertEqual(answers[0], answers[1])

    def test_r181_twins_render_identical_messages(self):
        from relay_engine.tests import xroot_b9_cases as cases
        with tempfile.TemporaryDirectory(prefix='fold-twins-') as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            repo = home/'foreign'
            xf._init_repo(repo)
            blob = xf._literal_object(repo, 'blob', b'target')
            for candidate in (False, True):
                prefix = 'DESIGN-' if candidate else 'SITREP-'
                for shape, content in (
                    ('symlink', (b'120000', blob)), ('utf8', b'\xff'),
                    ('phase', 'PHASE: DESIGN\nPHASE: SITREP\n'),
                    ('gitlink', (b'160000', '3'*40)),
                    ('control', 'PHASE: SITREP\nDESIGN_DOC_ID: d1\n\f')):
                    graph = dict(cc.files(), **{'relays/'+prefix+'probe.md': content})
                    case = cc.pin(repo, graph)
                    with self.subTest(candidate=candidate, shape=shape):
                        answers = [self.messages(module, consumer, case) for module in self.modules]
                        self.assertEqual(answers[0], answers[1])
                        if shape != 'control':
                            self.assertEqual(candidate, bool(answers[0][0]))
                tree = xf._literal_object(repo, 'tree', xf._tree_entry(
                    b'100644', prefix.encode()+b'\xff.md', blob))
                case = cc.pin(repo, dict(cc.files(), **{'relays/lane': (b'40000', tree)}))
                with self.subTest(candidate=candidate, shape='filename'):
                    answers = [self.messages(module, consumer, case) for module in self.modules]
                    self.assertEqual(answers[0], answers[1])
                    self.assertEqual(candidate, bool(answers[0][0]))
            # R183's candidate twins retain the original real limits and sizes.
            for label in ('C1-over-count-20001', 'C2-over-bytes-300MiB'):
                spec = cases.CASES[label]
                def candidate_graph(spec=spec):
                    return {name.replace('/SITREP-', '/DESIGN-'): value
                            for name, value in spec['gen']().items()}
                with mock.patch.dict(cases.CASES, {'fold-candidate': dict(gen=candidate_graph)}):
                    case = cases.materialize(home/label, only=['fold-candidate'])['fold-candidate']
                with self.subTest(row=label, candidate=True):
                    answers = [self.messages(module, consumer, case) for module in self.modules]
                    self.assertEqual(answers[0], answers[1])
                    self.assertTrue(answers[0][0])
                    self.assertIn('capacity', answers[0][0][0])


class TestFoldCaptures(unittest.TestCase):
    def test_four_captures_have_pinned_bytes(self):
        import hashlib
        pins = {
            'baseline-b8d87eb.json': 'd93f2e8fafe3f85a64e9b091ab60ad6390efca0020e2eddc5888cdfe820fae46',
            'baseline-b8d87eb-lifted.json': 'acc8e967cdaf051dd9f7f679aad61e977c0bf38579657fb4047fa728bfa3f181',
            'baseline-b8d87eb-corpus.json': '7d7a1ecfde7d87de511ff72b984450317f2f0954c1eb0f889d53a0a9870f73ca',
            'baseline-b8d87eb-narrow.json': 'ef7f7f9cee66593fbb22917fe8df4b20f4428a7a63d0545087d4bf4b55e56c78',
        }
        root = Path(__file__).parents[2]/'relay-engine-fixtures/xroot'
        self.assertEqual(set(pins), {p.name for p in root.glob('baseline-*.json')})
        for name, digest in pins.items():
            with self.subTest(capture=name):
                self.assertEqual(digest, hashlib.sha256((root/name).read_bytes()).hexdigest())
