"""Fold 3 environment and strict semantic-budget witnesses."""
import math
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from relay_engine import rules
from relay_engine.tests import xroot_cache_cases as cc, xroot_narrow_corpus as corpus
from relay_engine.tests import test_b9_fold2
from relay_engine.tests.test_xroot import _standalone
import xrootfixgen as xf


@contextmanager
def divergent_environment():
    import ctypes
    libc=ctypes.CDLL(None)
    libc.getenv.argtypes=[ctypes.c_char_p]
    libc.getenv.restype=ctypes.c_char_p
    keys=('GIT_CONFIG','GIT_NO_REPLACE_OBJECTS')
    mapping={key:os.environ.get(key) for key in keys}
    process={key:libc.getenv(os.fsencode(key)) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key,None)
            os.unsetenv(key)
        yield
    finally:
        for key in keys:
            if mapping[key] is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=mapping[key]
            # Preserve a pre-existing C/mapping divergence as well.
            if process[key] is None:
                os.unsetenv(key)
            else:
                os.putenv(key,os.fsdecode(process[key]))


class TestChildEnvironment(unittest.TestCase):
    def test_mapping_and_c_environment_divergence_both_paths(self):
        check = test_b9_fold2.TestConfigView()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            consumer = home/'consumer'
            xf._init_repo(consumer)
            config = home/'false-config'
            config.write_text('[core]\nuseReplaceRefs=false\n')
            for module in (rules, _standalone()):
                for shape in ('valid', 'symbolic'):
                    case = cc.make(home/(module.__name__+shape))
                    oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cc.R)
                    ref = 'refs/replace/'+oid
                    if shape == 'valid':
                        replacement = xf._literal_object(case.repository, 'blob', cc.REVIEW.replace('approve', 'must-revise').encode())
                        xf._git(case.repository, 'update-ref', ref, replacement)
                    else:
                        xf._git(case.repository, 'symbolic-ref', ref, 'refs/heads/missing')
                    for key,value in (('GIT_CONFIG',str(config)),('GIT_NO_REPLACE_OBJECTS','1')):
                        for mode in ('putenv-only','mapping','mapping-then-unsetenv'):
                            with self.subTest(module=module.__name__,shape=shape,key=key,mode=mode), \
                                 divergent_environment(), corpus.budget(module):
                                for name in ('GIT_CONFIG','GIT_NO_REPLACE_OBJECTS'):
                                    os.environ.pop(name, None)
                                    os.unsetenv(name)
                                try:
                                    if mode == 'putenv-only':
                                        os.putenv(key,value)
                                    else:
                                        os.environ[key]=value
                                        if mode == 'mapping-then-unsetenv':
                                            os.unsetenv(key)
                                    allow = key == 'GIT_NO_REPLACE_OBJECTS' and mode != 'putenv-only'
                                    axis = None if allow else 'authority-verdict' if shape == 'valid' else 'authority-population'
                                    check.answers(module,consumer,case,axis,None if allow else 'lineage-incomplete',check.caches(module))
                                finally:
                                    # putenv does not update the mapping, so patch.dict alone cannot restore it.
                                    for name in ('GIT_CONFIG','GIT_NO_REPLACE_OBJECTS'):
                                        os.environ.pop(name,None)
                                        os.unsetenv(name)


class TestStrictLift(unittest.TestCase):
    def test_explicit_budget_is_honoured_or_rejected(self):
        for module in (rules,_standalone()):
            with self.subTest(module=module.__name__),corpus.budget(module):
                for deadline in (0,0.2,123.5):
                    try:
                        token=module._XROOT_BUDGET.set((deadline,[]))
                    except AssertionError:
                        continue
                    try:
                        self.assertEqual(deadline,module._XROOT_BUDGET.get()[0])
                    finally:
                        module._XROOT_BUDGET.reset(token)
                try:
                    with corpus.budget(module,0.2):
                        self.assertTrue(math.isfinite(module._XROOT_BUDGET.get()[0]))
                except AssertionError as exc:
                    # Only a deliberate refusal is acceptable, not our assertion above.
                    self.assertIn('explicit finite',str(exc))

    def test_cache_budget_default_is_unbounded(self):
        for module in (rules,_standalone()):
            with self.subTest(module=module.__name__),cc.budget(module):
                self.assertTrue(math.isinf(module._XROOT_BUDGET.get()[0]))


    def test_worker_semantic_scope_survives_authority_reset(self):
        from concurrent.futures import ThreadPoolExecutor
        seen=[]
        original=rules.xroot_verify
        def verify(*args):
            seen.append(rules._XROOT_BUDGET.get()[0])
            return original(*args)
        with tempfile.TemporaryDirectory() as temporary:
            case=cc.make(Path(temporary))
            def answer():
                with cc.budget(rules):
                    return cc.whole_answers(case.declaration)
            with corpus.budget(rules),mock.patch.object(rules,'xroot_verify',side_effect=verify),ThreadPoolExecutor(max_workers=1) as pool:
                answers=pool.submit(answer).result(timeout=120)
            self.assertTrue(all(value['allowed'] for value in answers.values()))
            self.assertTrue(seen)
            self.assertTrue(all(math.isinf(deadline) for deadline in seen),seen)


class TestObservationCleanup(unittest.TestCase):
    def test_failed_descriptor_observation_reaps_started_child(self):
        import subprocess
        from relay_engine.tests import test_xroot_spawn
        fixture = test_xroot_spawn.TestXrootSpawn()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        oid = 'a'*40
        obj = fixture.repo/'.git/objects'/oid[:2]/oid[2:]
        obj.parent.mkdir(exist_ok=True)
        os.mkfifo(obj)
        for module in (rules,_standalone()):
            processes=[]
            original=module._xroot_start_git
            def record(*args,**kwargs):
                child=original(*args,**kwargs)
                processes.append(child)
                return child
            try:
                with self.subTest(module=module.__name__), \
                     mock.patch.object(module,'_xroot_start_git',side_effect=record), \
                     mock.patch.object(fixture,'descriptors',side_effect=subprocess.TimeoutExpired('lsof',2)), \
                     fixture.observe_start(module,[]):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        module.xroot_git(fixture.repo,'cat-file','blob',oid,deadline=float('inf'))
                    self.assertEqual(1,len(processes))
                    self.assertIsNotNone(processes[0].returncode,'observation leaked the Git child')
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(processes[0].pid,os.WNOHANG)
            finally:
                for child in processes:
                    module._xroot_kill_and_reap(child)
                    for stream in (child.stdin,child.stdout,child.stderr):
                        if stream is not None:
                            stream.close()


class TestFiniteReadStop(unittest.TestCase):
    def test_real_deadline_stops_unanswered_git_query(self):
        from relay_engine.tests.xroot_test_stops import stop
        with tempfile.TemporaryDirectory() as temporary:
            repo=Path(temporary)
            xf._init_repo(repo)
            oid='a'*40
            obj=repo/'.git/objects'/oid[:2]/oid[2:]
            obj.parent.mkdir(exist_ok=True)
            os.mkfifo(obj)
            for module in (rules,_standalone()):
                with self.subTest(module=module.__name__), stop(30), \
                     mock.patch.object(module,'XROOT_GIT_TIMEOUT_SECONDS',1):
                    # The default finite deadline starts after Git is spawned;
                    # the clock and the selector wait remain real.
                    with self.assertRaises(module.XrootAxisError) as caught:
                        module.xroot_git(repo,'cat-file','blob',oid)
                    self.assertEqual('capacity',caught.exception.axis)

    def test_real_deadline_stops_unanswered_git_read(self):
        import time
        from relay_engine.tests.xroot_test_stops import stop
        with tempfile.TemporaryDirectory() as temporary:
            repo=Path(temporary)
            xf._init_repo(repo)
            oid='a'*40
            obj=repo/'.git/objects'/oid[:2]/oid[2:]
            obj.parent.mkdir(exist_ok=True)
            os.mkfifo(obj)
            for module in (rules,_standalone()):
                with self.subTest(module=module.__name__), stop(30):
                    with module.XrootBatchReader(repo,float('inf')) as reader:
                        reader.request(oid+'^{tree}')
                        # The real deadline starts after Git has been spawned.
                        reader.deadline=time.monotonic()+0.05
                        with self.assertRaises(module.XrootAxisError) as caught:
                            reader.next_response()
                        self.assertEqual('capacity',caught.exception.axis)


class TestFailureStops(unittest.TestCase):
    def test_stop_escapes_domain_conversion(self):
        import time
        from relay_engine.tests.xroot_test_stops import stop, TestStop
        for module in (rules,_standalone()):
            declaration={key:'value' for key in module.XROOT_REQUIRED}
            declaration.update(DESIGN_LOCK_ID='d1',DESIGN_RECORD_KIND='design-doc')
            with self.subTest(module=module.__name__), \
                 mock.patch.object(module,'xroot_declaration',return_value=declaration), \
                 mock.patch.object(module,'xroot_pair_plan_carrier_shape',return_value=(True,True)), \
                 mock.patch.object(module,'xroot_field_conflicts',return_value=[]), \
                 mock.patch.object(module,'xroot_verify',side_effect=lambda *args:time.sleep(.1)):
                with self.assertRaises(TestStop):
                    with stop(.01):
                        module.xroot_design_gate(Path('/tmp'),Path('/tmp/PLAN.md'),{},'',[],module.LintResult())

    def test_worker_waits_observe_failure_stop(self):
        import selectors
        import time
        from concurrent.futures import ThreadPoolExecutor
        from relay_engine.tests.xroot_test_stops import stop, TestStop
        for kind in ('selector','sleep'):
            read,write=os.pipe()
            def blocked():
                with cc.budget(rules), stop(.02):
                    if kind=='sleep':
                        time.sleep(.2)
                    else:
                        with selectors.DefaultSelector() as selector:
                            selector.register(read,selectors.EVENT_READ)
                            selector.select(None)
            # Release a broken selector guard so the RED witness cannot hang.
            timer=threading.Timer(.2,lambda:os.write(write,b'x'))
            try:
                with self.subTest(kind=kind),ThreadPoolExecutor(max_workers=1) as pool:
                    timer.start()
                    future=pool.submit(blocked)
                    with self.assertRaises(TestStop):
                        future.result(timeout=2)
            finally:
                timer.cancel();timer.join()
                os.close(read);os.close(write)

    def test_nested_default_does_not_erase_explicit_expiry(self):
        for module in (rules,_standalone()):
            with self.subTest(module=module.__name__),corpus.budget(module):
                token=module._XROOT_BUDGET.set((123.5,[]))
                try:
                    try:
                        with corpus.budget(module):
                            self.assertEqual(123.5,module._XROOT_BUDGET.get()[0])
                    except AssertionError as exc:
                        self.assertIn('explicit finite',str(exc))
                finally:
                    module._XROOT_BUDGET.reset(token)

    def test_worker_stop_reaps_git_with_no_open_streams(self):
        import sys
        from concurrent.futures import ThreadPoolExecutor
        from relay_engine.tests.xroot_test_stops import stop, TestStop
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary)
            script=home/'git'
            script.write_text('#!'+sys.executable+'\nimport os,time\nos.close(1)\nos.close(2)\ntime.sleep(2)\n')
            script.chmod(0o755)
            for module in (rules,_standalone()):
                children=[]
                original=module._xroot_start_git
                def start(*args,**kwargs):
                    child=original(*args,**kwargs)
                    children.append(child)
                    return child
                def query():
                    with cc.budget(module),stop(.2):
                        module.xroot_git(home,'rev-parse','HEAD')
                with self.subTest(module=module.__name__), \
                     mock.patch.dict(os.environ,{'PATH':str(home)+os.pathsep+os.environ['PATH']}), \
                     mock.patch.object(module,'_xroot_start_git',side_effect=start), \
                     ThreadPoolExecutor(max_workers=1) as pool:
                    with self.assertRaises(TestStop):
                        pool.submit(query).result(timeout=120)
                self.assertEqual(1,len(children))
                self.assertIsNotNone(children[0].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(children[0].pid,os.WNOHANG)
