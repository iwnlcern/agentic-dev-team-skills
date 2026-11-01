"""B9 child descriptor isolation and query completion after child exit."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from relay_engine import rules
from relay_engine.tests.test_xroot import _standalone
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests.xroot_test_stops import stop
import xrootfixgen as xf


class TestXrootSpawn(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b9-spawn-')
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        xf._init_repo(self.repo)
        xf._git(self.repo, 'commit', '--allow-empty', '-m', 'spawn control')

    def descriptors(self, pid):
        result = {}
        proc = Path(f'/proc/{pid}/fd')
        if proc.is_dir():
            for entry in proc.iterdir():
                try:
                    name = os.readlink(entry)
                except FileNotFoundError:
                    continue
                result[int(entry.name)] = dict(kind='PIPE' if name.startswith('pipe:[') else 'FILE',
                                               identity=name, name=name)
        else:
            answer = subprocess.run(['lsof', '-nP', '-p', str(pid)],
                                    capture_output=True, text=True, timeout=2)
            for row in answer.stdout.splitlines():
                fields = row.split()
                if len(fields) < 8 or not fields[3].rstrip('rwu').isdigit():
                    continue
                result[int(fields[3].rstrip('rwu'))] = dict(
                    kind=fields[4], identity=fields[5], name=' '.join(fields[8:]))
        self.assertTrue(result, f'no descriptor view for child {pid}')
        return result

    @contextmanager
    def observe_start(self, module, observations):
        if hasattr(module, '_xroot_start_git'):
            original = module._xroot_start_git
            def start(*args, **kwargs):
                process = original(*args, **kwargs)
                try:
                    observations.append(self.descriptors(process.pid))
                except BaseException:
                    module._xroot_kill_and_reap(process)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
                    raise
                return process
            with mock.patch.object(module, '_xroot_start_git', side_effect=start):
                yield
        else:
            original = os.posix_spawnp
            def start(*args, **kwargs):
                pid = original(*args, **kwargs)
                try:
                    observations.append(self.descriptors(pid))
                except BaseException:
                    import signal
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
                    raise
                return pid
            with mock.patch.object(os, 'posix_spawnp', side_effect=start):
                yield

    def test_git_children_hold_no_other_descriptor(self):
        marker = self.repo/'unrelated-descriptor'
        marker.write_text('not a Git stream')
        fd = os.open(marker, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        os.set_inheritable(fd, True)
        oid = 'a'*40
        obj = self.repo/'.git/objects'/oid[:2]/oid[2:]
        obj.parent.mkdir(exist_ok=True)
        os.mkfifo(obj)
        for module in (rules, _standalone()):
            for kind in ('query', 'batch'):
                with self.subTest(module=module.__name__, kind=kind):
                    observations = []
                    with self.observe_start(module, observations):
                        if kind == 'query':
                            now = [0]
                            original_start = module._xroot_start_git
                            def expire_after_spawn(*args, **kwargs):
                                process = original_start(*args, **kwargs)
                                now[0] = 6
                                return process
                            with mock.patch.object(module.time, 'monotonic', side_effect=lambda: now[0]), \
                                 mock.patch.object(module, '_xroot_start_git', side_effect=expire_after_spawn):
                                with self.assertRaises(module.XrootAxisError) as caught:
                                    module.xroot_git(self.repo, 'cat-file', 'blob', oid, deadline=5)
                            self.assertEqual('capacity', caught.exception.axis)
                        else:
                            with module.XrootBatchReader(self.repo, time.monotonic()+5):
                                pass
                    self.assertEqual(1, len(observations))
                    self.assertNotIn(str(marker.resolve()),
                                     [os.path.realpath(row['name']) for row in observations[0].values()
                                      if row['name']])

    def test_uncooperative_producer_beside_batch_spawn_completes(self):
        self.enterContext(stop(300))
        expected = xf._git(self.repo, 'rev-parse', 'HEAD')
        for module in (rules, _standalone()):
            with self.subTest(module=module.__name__):
                ready, started = threading.Event(), threading.Event()
                original = os.pipe
                state, output = {}, {}
                def pipe():
                    fds = original()
                    if threading.current_thread().name == 'uncooperative' and not state:
                        state['fds'] = fds
                        for fd in fds:
                            os.set_inheritable(fd, True)
                        try:
                            state['creation_view'] = self.descriptors(os.getpid())
                        except BaseException as exc:
                            state['observation_error'] = exc
                            for fd in fds:
                                os.close(fd)
                            ready.set()
                            raise
                        ready.set()
                        state['overlapped'] = started.wait(120)
                        for fd in fds:
                            os.set_inheritable(fd, False)
                    return fds
                def producer():
                    try:
                        output['value'] = xf._git(self.repo, 'rev-parse', 'HEAD')
                    except BaseException as exc:
                        output['error'] = repr(exc)
                thread = threading.Thread(target=producer, name='uncooperative', daemon=True)
                reader = None
                with mock.patch.object(os, 'pipe', side_effect=pipe):
                    thread.start()
                    try:
                        self.assertTrue(ready.wait(120), "producer setup reached its failure stop")
                        if "observation_error" in state:
                            raise state["observation_error"]
                        # Pipe identities are captured while the producer holds
                        # the creation window open; it does not share a lock.
                        parent = state['creation_view']
                        reader = module.XrootBatchReader(self.repo, float("inf"))
                        started.set()
                        child = self.descriptors(reader.pid)
                        thread.join(120)
                        completed = not thread.is_alive()
                    finally:
                        started.set()
                        if reader is not None:
                            reader.close()
                        thread.join(120)
                self.assertFalse(thread.is_alive(), 'producer did not clean up')
                self.assertTrue(state['overlapped'])
                self.assertEqual({'value': expected}, output)
                created = {fd: parent[fd] for fd in state['fds'] if fd in parent}
                self.assertEqual(2, len(created))
                self.assertEqual({'PIPE'}, {row['kind'] for row in created.values()})
                endpoints = {row['identity'] for row in created.values()}
                held = {row['identity'] for row in child.values() if row['kind'] == 'PIPE'}
                self.assertTrue(completed, 'fixture waited for batch child closure')
                self.assertEqual(set(), endpoints & held)

    def test_query_answers_after_child_exit_with_pipe_held_elsewhere(self):
        self.enterContext(stop(300))
        content = b'full-output\n'*20000
        oid = xf._literal_object(self.repo, 'blob', content)
        for module in (rules, _standalone()):
            for failure in (False, True):
                with self.subTest(module=module.__name__, failure=failure):
                    original = os.pipe
                    held, answer = [], {}
                    def pipe():
                        fds = original()
                        if threading.current_thread().name == 'query-held-writer' and not held:
                            held.append(os.dup(fds[1]))
                        return fds
                    def query():
                        try:
                            args = ('rev-parse', '--verify', 'missing-ref') if failure else ('cat-file', 'blob', oid)
                            with cc.budget(module):
                                answer['value'] = module.xroot_git(self.repo, *args, binary=True)
                        except module.XrootAxisError as exc:
                            answer.update(axis=exc.axis, detail=exc.detail,
                                          returncode=getattr(exc, 'returncode', None),
                                          stdout=getattr(exc, 'stdout', None), stderr=getattr(exc, 'stderr', None))
                        except BaseException as exc:
                            answer['error'] = repr(exc)
                    thread = threading.Thread(target=query, name='query-held-writer', daemon=True)
                    with mock.patch.object(os, 'pipe', side_effect=pipe):
                        thread.start()
                        try:
                            thread.join(120)
                            completed = not thread.is_alive()
                        finally:
                            for fd in held:
                                os.close(fd)
                            thread.join(120)
                    self.assertFalse(thread.is_alive(), 'query did not clean up')
                    self.assertEqual(1, len(held))
                    self.assertTrue(completed, 'query waited for a writer unrelated to its child')
                    if failure:
                        expected = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', '--verify', 'missing-ref'], capture_output=True)
                        self.assertEqual(dict(axis='repository', detail='Git object query failed',
                                              returncode=expected.returncode, stdout=expected.stdout, stderr=expected.stderr), answer)
                    else:
                        self.assertEqual({'value': content}, answer)
