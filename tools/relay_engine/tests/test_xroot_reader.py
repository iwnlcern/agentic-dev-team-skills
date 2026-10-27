"""Ordered Git batch reads preserve errors and reject forbidden content early."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import rules
from relay_engine.tests.xroot_b9_cases import _object_tree
import xrootfixgen as xf


class TestXrootReader(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b9-reader-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        xf._init_repo(self.repo)

    def tree(self, files):
        return _object_tree(self.repo, files)

    def reader(self, deadline=None):
        self.assertTrue(hasattr(rules, 'XrootBatchReader'), 'ordered batch reader is missing')
        return rules.XrootBatchReader(self.repo, deadline or time.monotonic()+5)

    def read(self, files):
        return rules.xroot_relays(self.repo, self.tree(files))

    def test_reader_byte_limit_boundary(self):
        blob = xf._literal_object(self.repo, 'blob', b'abc')
        for limit, expected in ((3, b'abc'), (2, None)):
            with self.subTest(limit=limit), self.reader() as reader:
                reader.max_blob_bytes = limit
                reader.request(blob+'^{blob}')
                self.assertEqual(('blob', expected), reader.next_response())

    def test_two_outstanding_requests_first_header_over_budget(self):
        # Only the first header is framed; bounded transport read-ahead is discarded.
        blob = xf._literal_object(self.repo, 'blob', b'abc')
        with self.reader() as reader:
            reader.max_blob_bytes = 2
            reader.request(blob+'^{blob}')
            reader.request(blob+'^{blob}')
            observed = bytearray()
            real_read = os.read
            def read(fd, size):
                data = real_read(fd, size)
                if fd == reader._output:
                    observed.extend(data)
                return data
            with mock.patch.object(os, 'read', side_effect=read):
                self.assertEqual(('blob', None), reader.next_response())
            header = (blob+' blob 3\n').encode()
            self.assertEqual(header, bytes(observed[:len(header)]))
            self.assertLessEqual(len(observed)-len(header), 65536)
            self.assertEqual(bytes(observed[len(header):]), bytes(reader._read_ahead))
            self.assertEqual(0, reader.content_bytes_read)

    def test_buffered_framing_reads_in_chunks(self):
        # A return to one-byte header reads violates the transport-call bound.
        blob = xf._literal_object(self.repo, 'blob', b'abc')
        calls = 0
        real_read = os.read
        with self.reader(time.monotonic()+600) as reader:
            def read(fd, size):
                nonlocal calls
                if fd == reader._output:
                    calls += 1
                return real_read(fd, size)
            with mock.patch.object(os, 'read', side_effect=read):
                for _ in range(20000//32):
                    for _ in range(32):
                        reader.request(blob+'^{blob}')
                    for _ in range(32):
                        self.assertEqual(('blob', b'abc'), reader.next_response())
            self.assertLess(calls, 20000*4)

    def test_read_ahead_never_exceeds_bound(self):
        # A large permitted body must not accumulate as unframed read-ahead.
        payload = b'x'*(3*65536+17)
        blob = xf._literal_object(self.repo, 'blob', payload)
        with self.reader(time.monotonic()+600) as reader:
            self.assertTrue(hasattr(reader, '_read_ahead'), 'bounded transport buffer missing')
            real_read = os.read
            observed = []
            def read(fd, size):
                if fd == reader._output:
                    observed.append(len(reader._read_ahead))
                    self.assertLessEqual(size+len(reader._read_ahead), 65536)
                return real_read(fd, size)
            for _ in range(3):
                reader.request(blob+'^{blob}')
            with mock.patch.object(os, 'read', side_effect=read):
                for _ in range(3):
                    self.assertEqual(('blob', payload), reader.next_response())
                    self.assertLessEqual(len(reader._read_ahead), 65536)
            self.assertTrue(observed)

    def test_one_chunk_over_budget_header_and_forbidden_body(self):
        # Coalescing forbidden body bytes must never frame either queued body.
        blob = xf._literal_object(self.repo, 'blob', b'abc')
        header = (blob+' blob 3\n').encode()
        response = header+b'abc\n'+header+b'abc\n'
        with self.reader() as reader:
            reader.max_blob_bytes = 2
            reader.request(blob+'^{blob}')
            reader.request(blob+'^{blob}')
            pending = bytearray(response)
            real_read = os.read
            def read(fd, size):
                if fd != reader._output:
                    return real_read(fd, size)
                data = bytes(pending[:size]); del pending[:size]
                return data
            with mock.patch.object(os, 'read', side_effect=read):
                self.assertEqual(('blob', None), reader.next_response())
            self.assertEqual(b'', bytes(pending), 'transport did not read the coalesced chunk')
            self.assertEqual(0, reader.content_bytes_read)
            self.assertEqual(response[len(header):], bytes(reader._read_ahead))
        self.assertEqual(b'', bytes(reader._read_ahead))
        tree = self.tree({'a.md': b'abc', 'b.md': b'abc'})
        with mock.patch.object(rules, 'XROOT_MAX_RELAY_BYTES', 2):
            self.assertEqual([], rules.xroot_relays(self.repo, tree))
            candidate_tree = self.tree({'DESIGN-a.md': b'abc', 'DESIGN-b.md': b'abc'})
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_relays(self.repo, candidate_tree)
            self.assertEqual('capacity', caught.exception.axis)
            self.assertIn('XROOT_MAX_RELAY_BYTES', caught.exception.detail)
            self.assertEqual(0, caught.exception.progress['relays_read'])
            self.assertEqual(0, caught.exception.progress['bytes_read'])

    def test_selector_registered_once(self):
        # Rebuilding selector registrations on every wait breaks this bound.
        registered = []
        selector_type = rules.selectors.DefaultSelector
        # Cover the backend selected by the reader, including PollSelector.
        types = {selector_type, rules.selectors.PollSelector}
        from contextlib import ExitStack
        with ExitStack() as stack:
            for cls in types:
                real = cls.register
                def register(obj, fd, events, data=None, real=real):
                    registered.append((id(obj), fd))
                    return real(obj, fd, events, data)
                stack.enter_context(mock.patch.object(cls, 'register', register))
            blob = xf._literal_object(self.repo, 'blob', b'abc')
            with self.reader() as reader:
                for _ in range(5):
                    reader.request(blob+'^{blob}')
                    reader._wait()
                    self.assertEqual(('blob', b'abc'), reader.next_response())
                own = [fd for identity, fd in registered if identity == id(reader._selector)]
                self.assertEqual(sorted([reader._input, reader._output]), sorted(own))

    def test_no_replace_flag_returns_own_bytes(self):
        import inspect
        self.assertIn('no_replace', inspect.signature(rules.XrootBatchReader).parameters)
        original = xf._literal_object(self.repo, 'blob', b'original')
        replacement = xf._literal_object(self.repo, 'blob', b'replacement')
        rules.xroot_git(self.repo, 'update-ref', 'refs/replace/'+original, replacement)
        for no_replace, expected in ((False, b'replacement'), (True, b'original')):
            with self.subTest(no_replace=no_replace):
                with rules.XrootBatchReader(self.repo, time.monotonic()+5, no_replace=no_replace) as reader:
                    reader.request(original+'^{blob}')
                    self.assertEqual(('blob', expected), reader.next_response())

    def test_reader_transport_failure_is_terminal(self):
        blob = xf._literal_object(self.repo, 'blob', b'x')
        with self.reader() as reader:
            os.kill(reader.pid, signal.SIGKILL)
            with self.assertRaises(rules.XrootAxisError) as caught:
                reader.request(blob+'^{blob}')
                reader.next_response()
            self.assertEqual('repository', caught.exception.axis)
            self.assertEqual('Git batch reader failed', caught.exception.detail)

    def test_malformed_batch_responses_are_transport_failures(self):
        oid = b'1'*40
        for response in (b'not-a-header\n', oid+b' blob invalid\n',
                         oid+b' blob 1\nx?', b'x'*257+b'\n'):
            with self.subTest(response=response), self.reader() as reader:
                pending = bytearray(response)
                real_read = os.read
                def read(fd, size):
                    if fd != reader._output:
                        return real_read(fd, size)
                    data = bytes(pending[:size])
                    del pending[:size]
                    return data
                with mock.patch.object(os, 'read', side_effect=read):
                    with self.assertRaises(rules.XrootAxisError) as caught:
                        reader.next_response()
                self.assertEqual(('repository', 'Git batch reader failed'),
                                 (caught.exception.axis, caught.exception.detail))

    def test_reader_reaped_on_every_exit(self):
        blob = xf._literal_object(self.repo, 'blob', b'x')
        for outcome in ('pass', 'deadline', 'transport', 'caller'):
            with self.subTest(outcome=outcome):
                before = set(os.listdir('/dev/fd'))
                reader = self.reader()
                try:
                    with reader:
                        if outcome == 'caller':
                            raise ValueError('caller')
                        if outcome == 'deadline':
                            reader.deadline = time.monotonic()-1
                        if outcome == 'transport':
                            os.kill(reader.pid, signal.SIGKILL)
                        reader.request(blob+'^{blob}')
                        reader.next_response()
                except (ValueError, rules.XrootAxisError):
                    self.assertNotEqual('pass', outcome)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(reader.pid, os.WNOHANG)
                self.assertEqual(before, set(os.listdir('/dev/fd')))

    def test_peel_matches_cat_file_type(self):
        blob = xf._literal_object(self.repo, 'blob', b'hello')
        tree = self.tree({'x': b'hello'})
        commit = xf._literal_commit(self.repo, tree)
        tag = xf._literal_object(self.repo, 'tag', f'object {commit}\ntype commit\ntag t\ntagger f <f@example.invalid> 1 +0000\n\ntag\n'.encode())
        blobtag = xf._literal_object(self.repo, 'tag', f'object {blob}\ntype blob\ntag t\ntagger f <f@example.invalid> 1 +0000\n\ntag\n'.encode())
        cases = ((tree,'tree'), (commit,'tree'), (tag,'tree'), (blob,'blob'),
                 (blobtag,'blob'), (commit,'blob'), (blob,'tree'), ('2'*40,'tree'))
        for oid, kind in cases:
            with self.subTest(oid=oid, kind=kind), self.reader() as reader:
                try:
                    old = rules.xroot_git(self.repo, 'cat-file', kind, oid, binary=True)
                except rules.XrootAxisError:
                    old = None
                reader.request(oid+'^{'+kind+'}')
                actual_kind, actual = reader.next_response()
                self.assertEqual(old, actual)
                self.assertEqual('missing' if old is None else kind, actual_kind)

    def test_every_candidate_and_conflict_carrier_is_retained(self):
        files = {'a.md':'PHASE: SITREP\n', 'b.md':'DESIGN_DOC_ID: d1\n',
                 'c.md':'PHASE: DESIGN\nPHASE: SITREP\n', 'd.md':'DESIGN_\x00DOC_ID: D2\n',
                 'e.md':'hello\x85world', 'f.md':'PHASE: DESIGN\nDESIGN_DOC_ID: d2\n'}
        self.assertEqual([], [r.name for r in self.read(files)])
        candidates = {'DESIGN-'+name: text for name, text in files.items()}
        self.assertEqual(['DESIGN-b.md','DESIGN-c.md','DESIGN-d.md','DESIGN-e.md','DESIGN-f.md'],
                         [r.name for r in self.read(candidates)])

    def test_reader_deep_single_child_chain_within_bound(self):
        tree = self.tree({'/'.join(['d']*256)+'/x.md':'DESIGN_DOC_ID: d1\n'})
        from relay_engine.tests.xroot_cache_cases import budget
        with budget(rules):
            self.assertEqual(0, len(rules.xroot_relays(self.repo, tree)))

    def test_mixed_faults_give_first_error_in_walk_order(self):
        badtree = xf._literal_object(self.repo, 'tree', b'broken')
        for later in ('3'*40, badtree):
            with self.subTest(later=later):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    self.read({'a.md':b'\xff', 'z':(b'40000',later)})
                detail = 'Git object query failed' if later == '3'*40 else 'pinned tree object has malformed entry bytes'
                self.assertEqual(('repository', detail),
                                 (caught.exception.axis,caught.exception.detail))
        with self.assertRaises(rules.XrootAxisError) as caught:
            self.read({'a':(b'40000','3'*40), 'z.md':b'\xff'})
        self.assertEqual('repository',caught.exception.axis)

    def test_name_errors_at_expansion(self):
        invalid = xf._literal_object(self.repo, 'blob', b'\xff')
        raw = xf._tree_entry(b'100644', b'a.md', invalid)+xf._tree_entry(b'100644', b'z\xff.md', invalid)
        tree = xf._literal_object(self.repo, 'tree', raw)
        self.assertEqual([], rules.xroot_relays(self.repo, tree))
        self.assertEqual([], self.read({'a.md':b'\xff','z':(b'40000',tree)}))

    def test_index_md_tree_symlink_and_md_gitlink(self):
        blob = xf._literal_object(self.repo,'blob',b'not a path')
        self.assertEqual([], self.read({'INDEX.md':(b'120000',blob)}))
        self.assertEqual([],[r.name for r in self.read({'INDEX.md/x.md':'DESIGN_DOC_ID: d1\n'})])
        self.assertEqual([], self.read({'x.md':(b'160000','3'*40)}))

    def test_limits_at_and_one_over(self):
        # Small exact-boundary controls use the same counters as the full retained capacity proof.
        controls = (
            ('XROOT_MAX_TREES', 2, {'d/x':'x'}, {'d/x':'x','e/x':'x'}),
            ('XROOT_MAX_ENTRIES',2, {'a':'x','b':'x'}, {'a':'x','b':'x','c':'x'}),
            ('XROOT_MAX_RELAYS',2, {'a.md':'x','b.md':'x'}, {'a.md':'x','b.md':'x','c.md':'x'}),
            ('XROOT_MAX_RELAY_BYTES',2, {'a.md':'xx'}, {'a.md':'xxx'}),
        )
        for name, value, at, over in controls:
            with self.subTest(limit=name):
                self.assertTrue(hasattr(rules,name),name+' missing')
                with mock.patch.object(rules,name,value):
                    self.read(at)
                    if name in ('XROOT_MAX_RELAYS', 'XROOT_MAX_RELAY_BYTES'):
                        self.assertEqual([], self.read(over))
                        at = {'DESIGN-'+key: value for key, value in at.items()}
                        over = {'DESIGN-'+key: value for key, value in over.items()}
                        self.read(at)
                    with self.assertRaises(rules.XrootAxisError) as caught:
                        self.read(over)
                    self.assertEqual('capacity',caught.exception.axis)
                    self.assertIn(name,caught.exception.detail)

    def test_past_limit_later_error_answers_capacity(self):
        self.assertTrue(hasattr(rules,'XROOT_MAX_RELAYS'))
        with mock.patch.object(rules,'XROOT_MAX_RELAYS',1):
            self.assertEqual([], self.read({'a.md':'x','b.md':b'\xff'}))
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.read({'DESIGN-a.md':'x','DESIGN-b.md':b'\xff'})
            self.assertEqual('capacity',caught.exception.axis)

    def test_in_envelope_error_beats_limit(self):
        self.assertTrue(hasattr(rules,'XROOT_MAX_RELAYS'))
        with mock.patch.object(rules,'XROOT_MAX_RELAYS',1):
            self.assertEqual([], self.read({'a.md':b'\xff','b.md':'x'}))
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.read({'DESIGN-a.md':b'\xff','DESIGN-b.md':'x'})
            self.assertEqual('authority-population',caught.exception.axis)

    def test_pre_reader_deadline_is_capacity(self):
        for args in (('rev-parse','--show-toplevel'), ('cat-file','-p','2'*40), ('cat-file','blob','2'*40)):
            with self.subTest(args=args):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    rules.xroot_git(self.repo,*args,deadline=time.monotonic()-1)
                self.assertEqual('capacity',caught.exception.axis)
                self.assertIn('timed out',caught.exception.detail)

    def test_window_never_crosses_unexpanded_tree(self):
        self.assertTrue(hasattr(rules,'XrootBatchReader'))
        original = rules.XrootBatchReader
        requested, consumed = [], []
        class Observed(original):
            def request(self, spec):
                # No descendant or sibling beyond this tree may be requested until consumed.
                self_outer.assertFalse(any(x.endswith('^{tree}') for x in requested[len(consumed):]))
                requested.append(spec)
                self_outer.assertLessEqual(len(requested)-len(consumed),64)
                return super().request(spec)
            def next_response(self):
                answer = super().next_response()
                consumed.append(answer)
                return answer
        self_outer=self
        with mock.patch.object(rules,'XrootBatchReader',Observed):
            self.read({**{f'a{i:03d}.md':'x' for i in range(90)}, 'b/x.md':'x', 'z.md':'x'})
        self.assertEqual(len(requested),len(consumed))

    def test_early_invalid_relay_beats_blocked_later_tree(self):
        self.assertTrue(hasattr(rules,'XrootBatchReader'))
        original = rules.XrootBatchReader
        queued = []
        subtree = self.tree({'x.md':'x'})
        class Observed(original):
            def request(self, spec):
                queued.append(spec)
                return super().request(spec)
            def next_response(self):
                if len(queued)>1:
                    self_outer.assertIn(subtree+'^{tree}',queued)
                return super().next_response()
        self_outer=self
        # A FIFO loose object makes the later Git read block until killed.
        obj=self.repo/'.git/objects'/subtree[:2]/subtree[2:]
        obj.unlink(); os.mkfifo(obj)
        with mock.patch.object(rules,'XrootBatchReader',Observed):
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.read({'a.md':b'\xff', 'z':(b'40000',subtree)})
        self.assertEqual('capacity',caught.exception.axis)

    def test_selector_os_error_is_transport_failure(self):
        before = set(os.listdir('/dev/fd'))
        with self.reader() as reader:
            pid = reader.pid
            with mock.patch.object(reader._selector, 'select', side_effect=OSError('injected selector failure')):
                with self.assertRaises(rules.XrootAxisError) as caught:
                    reader._wait()
            self.assertEqual(('repository', 'Git batch reader failed'), (caught.exception.axis, caught.exception.detail))
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        self.assertEqual(before, set(os.listdir('/dev/fd')))

    def test_pipe_setup_failure_closes_partial_resources(self):
        real_pipe = os.pipe
        calls = 0
        def pipe():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected pipe exhaustion')
            return real_pipe()
        before = set(os.listdir('/dev/fd'))
        with mock.patch.object(os, 'pipe', side_effect=pipe):
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.reader()
        self.assertEqual(('repository','Git batch reader failed'),(caught.exception.axis,caught.exception.detail))
        self.assertEqual(before,set(os.listdir('/dev/fd')))

    def captures(self):
        import json
        root = Path(__file__).parents[2]/'relay-engine-fixtures/xroot'
        return (json.loads((root/'baseline-b8d87eb.json').read_text()),
                json.loads((root/'baseline-b8d87eb-lifted.json').read_text()))

    def assert_governing(self, label, actual, baseline, lifted, path, elapsed, *, lifted_deadline=False):
        from relay_engine.tests.capture_xroot_baseline import NATURAL_TIMEOUTS
        capacity = {'M1-over-trees-20001':'XROOT_MAX_TREES',
                    'M2-over-entries-200001':'XROOT_MAX_ENTRIES',
                    'F-invalid-before-metadata':'XROOT_MAX_TREES'}
        from relay_engine.tests.xroot_b9_cases import NARROW_ALLOWS, NARROW_TREE_FAILURES
        # Keep the planted historical-capture corruption control meaningful.
        if label == 'G2-non-utf8-blob':
            self.assertEqual('authority carrier SITREP-x-20260801-120000.md is not UTF-8',
                             baseline['cases'][label]['paths'][path]['detail'])
        if label in capacity:
            self.assertEqual('capacity', actual['axis'])
            self.assertIn(capacity[label], actual['detail'])
            if not lifted_deadline:
                self.assertLess(elapsed, 5)
        elif label in NARROW_ALLOWS | {'L7-orchestrator-peer', 'B1-complete-within-envelope'}:
            self.assertIsNone(actual['axis'])
            from relay_engine.tests.xroot_b9_cases import O, R
            self.assertEqual((O.removeprefix('relays/'), R.removeprefix('relays/')),
                             (actual['origin'], actual['review']))
            if not lifted_deadline:
                self.assertLess(elapsed, 5)
        elif label in NARROW_TREE_FAILURES:
            self.assertEqual(NARROW_TREE_FAILURES[label], (actual['axis'], actual['detail']))
        else:
            old = (lifted if label in NATURAL_TIMEOUTS else baseline)['cases'][label]['paths'][path]
            if not lifted_deadline and label in NATURAL_TIMEOUTS and actual['axis'] == 'capacity':
                self.assertGreaterEqual(elapsed, 5)
            else:
                self.assertEqual((old['axis'], old['detail']), (actual['axis'], actual['detail']))
                if old['axis'] is None:
                    self.assertEqual((old['origin'], old['review']), (actual['origin'], actual['review']))

    def verify_case(self, case, *, budget_seconds=5):
        started = time.monotonic()
        token = rules._XROOT_BUDGET.set((started+budget_seconds, []))
        try:
            try:
                origin, review, population = rules.xroot_verify(self.repo, case.declaration)
                self.assertTrue(all(rules.xroot_retained(r.text) for r in population))
                self.assertIn(origin, population)
                self.assertIn(review, population)
                actual = dict(axis=None, detail=None, origin=origin.name, review=review.name)
            except rules.XrootAxisError as exc:
                actual = dict(axis=exc.axis, detail=exc.detail.replace(str(case.repository), '<repository>'))
            return actual, time.monotonic()-started
        finally:
            rules._XROOT_BUDGET.reset(token)

    def test_reader_matches_baseline_except_declared_rows(self):
        from copy import deepcopy
        from relay_engine.tests.xroot_b9_cases import materialize, CASES
        baseline, lifted = self.captures()
        cases = materialize(self.repo/'cases', only=[k for k in CASES if not k.startswith('T')])
        for label, case in cases.items():
            with self.subTest(case=label):
                actual, elapsed = self.verify_case(case, budget_seconds=600)
                for path in ('pair', 'direct'):
                    self.assert_governing(label, actual, baseline, lifted, path, elapsed, lifted_deadline=True)
                if label in ('F-depth-2500', 'G2-non-utf8-blob'):
                    bad_bounded, bad_lifted = deepcopy(baseline), deepcopy(lifted)
                    target = bad_lifted if label == 'F-depth-2500' else bad_bounded
                    target['cases'][label]['paths']['pair']['detail'] = 'planted undeclared change'
                    with self.assertRaises(AssertionError):
                        self.assert_governing(label, actual, bad_bounded, bad_lifted, 'pair', elapsed, lifted_deadline=True)

    def test_deep_tree_reaches_depth_refusal(self):
        from relay_engine.tests.xroot_b9_cases import materialize
        case = materialize(self.repo/'deep', only=['F-depth-2500'])['F-depth-2500']
        actual, elapsed = self.verify_case(case, budget_seconds=600)
        self.assertEqual(dict(axis='repository', detail='DESIGN_SOURCE_ROOT exceeds maximum tree depth 256'), actual)
        # A genuinely blocked tree still reaches its own deadline.
        tree = self.tree({'x.md':'x'})
        obj = self.repo/'.git/objects'/tree[:2]/tree[2:]
        obj.unlink(); os.mkfifo(obj)
        token = rules._XROOT_BUDGET.set((time.monotonic()+0.05, []))
        try:
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_relays(self.repo, tree)
            self.assertEqual('capacity', caught.exception.axis)
        finally:
            rules._XROOT_BUDGET.reset(token)

    def test_declared_limit_beats_lifted_allow(self):
        from relay_engine.tests.xroot_b9_cases import materialize
        baseline, lifted = self.captures()
        for label, case in materialize(self.repo/'limits', only=['C1-over-count-20001', 'M1-over-trees-20001']).items():
            with self.subTest(case=label):
                actual, elapsed = self.verify_case(case, budget_seconds=600)
                for path in ('pair', 'direct'):
                    self.assert_governing(label, actual, baseline, lifted, path, elapsed, lifted_deadline=True)

    def test_injected_deadlines_stay_deadline_controls(self):
        from contextlib import ExitStack
        from relay_engine.tests.xroot_b9_cases import materialize, deadline_fault, R
        from relay_engine.tests.capture_xroot_baseline import INJECTED_TIMEOUTS
        from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
        cases = materialize(self.repo/'injected', only=INJECTED_TIMEOUTS)
        original_reader = rules.XrootBatchReader
        original_verify = rules.xroot_verify
        for label, case in cases.items():
            for direct in (False, True):
                with self.subTest(case=label, direct=direct):
                    fixture = TestAuthorityFold5()
                    fixture.setUp()
                    try:
                        xf._git(Path(fixture.root.path), 'init', '-q', '-b', 'main')
                        pin = fixture.artifact('plans', 'PL-base')
                        extra = '\n'.join(k+': '+v for k,v in case.declaration.items())
                        fixture.add('PLAN', ident='plan', extra=extra+'\nPLAN_LOCK_ID: PL\n'+pin, content='')
                        fixture.add('PLAN-REVIEW', 'p.implementer', 'p.planner', 'review',
                                    'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve', '')
                        grant = (fixture.add('MERGE-GATE','operator',ident='grant',extra='PARENT_DISPATCH_ID: plan',content='DISPATCH MERGE')
                                 if direct else fixture.add(ident='grant',extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL'))
                        observed = []
                        def verify(*args, **kwargs):
                            try:
                                return original_verify(*args, **kwargs)
                            except rules.XrootAxisError as exc:
                                observed.append(exc.axis)
                                raise
                        review_oid = xf._git(case.repository, 'rev-parse', case.commit+':'+R)
                        class ExpiredReview(original_reader):
                            def request(self, spec):
                                if spec == review_oid+'^{blob}':
                                    self.deadline = time.monotonic()-1
                                return super().request(spec)
                        with ExitStack() as stack:
                            stack.enter_context(rules.xroot_fact_cache(rules.XrootFactCache()))
                            stack.enter_context(mock.patch.object(rules, 'xroot_verify', side_effect=verify))
                            if label == 'T1b-timeout-population-blob':
                                stack.enter_context(mock.patch.object(rules, 'XrootBatchReader', ExpiredReview))
                            else:
                                stack.enter_context(deadline_fault(case, rules))
                            from relay_engine import authority
                            answer = authority.check(fixture.ledger, fixture.root, grant.rendered_path,
                                                     'p.implementer', 'merge' if direct else 'implement')
                        self.assertFalse(answer['allowed'])
                        self.assertEqual(['capacity'], observed)
                    finally:
                        fixture.tearDown()

    def test_lifted_budget_includes_authority_internal_reset(self):
        from contextvars import ContextVar
        from relay_engine.tests.capture_xroot_baseline import LiftedBudget
        original = ContextVar('lifted-test', default=None)
        budget = LiftedBudget(original)
        outer_failures, inner_failures = [], []
        with mock.patch('relay_engine.tests.capture_xroot_baseline.time.monotonic', return_value=10):
            outer = budget.set((15, outer_failures))
            inner = budget.set((15, inner_failures))
        self.assertEqual((610, inner_failures), budget.get())
        budget.reset(inner)
        self.assertEqual((610, outer_failures), budget.get())
        budget.reset(outer)
        self.assertIsNone(budget.get())

    def test_governing_capture_selection(self):
        import hashlib
        import json
        from relay_engine.tests.capture_xroot_baseline import NATURAL_TIMEOUTS, INJECTED_TIMEOUTS
        root = Path(__file__).parents[2]/'relay-engine-fixtures/xroot'
        raw = (root/'baseline-b8d87eb.json').read_bytes()
        self.assertEqual('d93f2e8fafe3f85a64e9b091ab60ad6390efca0020e2eddc5888cdfe820fae46', hashlib.sha256(raw).hexdigest())
        bounded = json.loads(raw)
        lifted = json.loads((root/'baseline-b8d87eb-lifted.json').read_text())
        self.assertEqual(set(NATURAL_TIMEOUTS), set(lifted['cases']))
        self.assertEqual(bounded['source_commit'], lifted['source_commit'])
        self.assertEqual(bounded['source_sha256'], lifted['source_sha256'])
        self.assertEqual(600, lifted['budget_seconds'])
        self.assertIs(False, lifted['fault_injection'])
        for row in lifted['cases'].values():
            self.assertEqual({'pair', 'direct'}, set(row['paths']))
            for answer in row['paths'].values():
                self.assertIs(False, answer['lifted_timeout'])
                self.assertLess(answer['seconds'], 600)
                self.assertLess(answer['verify_seconds'], 600)
        for path in ('pair', 'direct'):
            timed = {k for k,v in bounded['cases'].items() if 'timed out' in (v['paths'][path]['detail'] or '')}
            self.assertEqual(set(NATURAL_TIMEOUTS) | set(INJECTED_TIMEOUTS), timed)
            for label in NATURAL_TIMEOUTS:
                self.assertEqual(bounded['cases'][label]['paths'][path], lifted['cases'][label]['paths'][path]['bounded'])
                self.assertNotIn('timed out', lifted['cases'][label]['paths'][path]['detail'] or '')

    def test_population_deadline_is_capacity(self):
        original = rules.XrootBatchReader
        class Expired(original):
            def next_response(self):
                self.deadline = time.monotonic()-1
                return super().next_response()
        with mock.patch.object(rules,'XrootBatchReader',Expired):
            with self.assertRaises(rules.XrootAxisError) as caught:
                self.read({'x.md':'DESIGN_DOC_ID: d1\n'})
        self.assertEqual('capacity',caught.exception.axis)
        self.assertEqual(0,caught.exception.progress['relays_read'])

    def test_repository_resolution_keeps_capacity_axis(self):
        repo,commit,digest = xf._foreign_repo(self.repo/'foreign')
        declaration = dict(xf._plan_fields(repo,commit,digest))
        original = rules.xroot_git
        def expired(repo,*args,**kwargs):
            if args[0] == 'rev-parse':
                kwargs['deadline'] = time.monotonic()-1
            return original(repo,*args,**kwargs)
        with mock.patch.object(rules,'xroot_git',side_effect=expired):
            with self.assertRaises(rules.XrootAxisError) as caught:
                rules.xroot_verify(self.repo,declaration)
        self.assertEqual('capacity',caught.exception.axis)
        self.assertIn('timed out',caught.exception.detail)
