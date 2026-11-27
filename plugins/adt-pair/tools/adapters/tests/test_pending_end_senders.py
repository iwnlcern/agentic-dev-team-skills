"""Pending-end replay, budgets and lifecycle on independent client connections."""
import dataclasses
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from adapters import pending_ends as pe
from relay_engine.api_client import (
    ApiClient, ApiErrorValue, EndpointUnavailable, LocalRefusal, ReadyFile,
    _registry_and_digest,
)


class Clock:
    tick = 100.0

    def monotonic(self):
        return self.tick

    def time(self):
        return 1000.0 + self.tick


class Client:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = threading.Event()

    def call(self, op, args, *, deadline=None):
        self.calls.append((op, args, deadline))
        answer = next(self.responses, {"session_id": args["session_id"]})
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        self.closed.set()


def refusal(reason):
    return ApiErrorValue("E_API_AUTH", "auth", {"reason": reason}, "")


class PendingEndSendersTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.key = self.home / "seat.key"
        self.key.write_text("adt-seat-key-v1 12345678-abcd-1234-abcd-123456789abc " + "a" * 43 + "\n")
        self.key.chmod(0o600)
        self.clock = Clock()
        self.now = int(self.clock.time() * 1000)

    def record(self, name="A", *, ready="a", **changes):
        return pe.PendingEnd(**(dict(host="codex", host_session_id=name,
                                    project_id="project", session_id="session-" + name,
                                    generation=1, key_path=str(self.key),
                                    created_ms=1, last_attempt_ms=None,
                                    credential_failed=False, next_eligible_ms=0,
                                    operation_key="close-" + name, ready_path=ready) | changes))

    def stored(self, record):
        return pe._load(self.home / ".adt/pending-ends" / pe._filename(record))

    def drain(self, client, *, ready="a", share=2):
        return pe.drain_share(client=client, ready_path=ready, home=self.home,
                              share_s=share, clock=self.clock)

    def test_pending_end_key_survives_reload(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        first = Client({}, EndpointUnavailable("unknown"))
        self.drain(first)
        self.assertEqual(self.stored(rec).last_attempt_ms, self.now)
        code = """
import dataclasses, sys
from pathlib import Path
from adapters import pending_ends as p
class Client:
    def call(self, op, args, *, deadline):
        if op == 'session.end':
            print(args['operation_key'])
        return {'session_id': args['session_id']}
record, = p.eligible(home=Path(sys.argv[1]), now_ms=1100000)
if sys.argv[2] == 'control':
    record = dataclasses.replace(record, operation_key='different-key')
assert p.send(record, client=Client(), deadline=123.0) == 'deleted'
"""
        expected = (first.calls[1][1]["operation_key"] + "\n").encode()
        def witness(variant):
            self.assertEqual(subprocess.check_output(
                [sys.executable, "-c", code, str(self.home), variant], timeout=5), expected)
        witness("recorded")
        with self.assertRaises(AssertionError):
            witness("control")

    def test_share_sends_only_its_ready_path(self):
        a, b = self.record(), self.record("B", ready="b", last_attempt_ms=5)
        for rec in (a, b):
            pe.write(rec, home=self.home)
        client = Client()
        self.drain(client)
        def witness(calls):
            self.assertEqual([a[1]["session_id"] for a in calls], ["session-A"] * 2)
        witness(client.calls)
        self.assertEqual(self.stored(b), b)
        with self.assertRaises(AssertionError):
            witness(client.calls + [("session.resume", {"session_id": "session-B"}, 102)])

    def test_pending_end_answers(self):
        for i, (replies, state) in enumerate((
            (({}, {}), "deleted"),
            ((refusal("session_not_current"),), "deleted"),
            (({}, refusal("session_not_current")), "deleted"),
            ((refusal("key_replaced"),), "credential-failed"),
            (({}, refusal("key_rejected")), "credential-failed"),
            ((refusal("runtime_not_ready"),), "kept"),
            ((EndpointUnavailable("not_performed"),), "kept"),
            ((EndpointUnavailable("unknown"),), "kept"),
            (({}, EndpointUnavailable("unknown")), "kept"),
            ((LocalRefusal("ready_file_mismatch"),), "kept"),
            ((EndpointUnavailable("endpoint_mismatch"),), "kept"),
        )):
            with self.subTest(i=i, state=state):
                rec = self.record(str(i), ready=str(i))
                pe.write(rec, home=self.home)
                self.drain(Client(*replies), ready=str(i))
                after = self.stored(rec)
                if state == "deleted":
                    self.assertIsNone(after)
                else:
                    self.assertEqual(after, dataclasses.replace(
                        rec, last_attempt_ms=self.now,
                        credential_failed=(state == "credential-failed"),
                        next_eligible_ms=self.now + 3600000 if state == "credential-failed" else 0))
        rec = self.record("missing", ready="missing", key_path=str(self.home / "missing.key"))
        pe.write(rec, home=self.home)
        client = Client()
        self.drain(client, ready="missing")
        self.assertTrue(self.stored(rec).credential_failed)
        self.assertEqual(client.calls, [])

    def test_credential_failed_asks_nothing_before_g_b3(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        c = Client(refusal("key_rejected"))
        self.drain(c)
        def witness():
            self.assertEqual([a[0] for a in c.calls], ["session.resume"])
            self.assertTrue(self.stored(rec).credential_failed)
        witness()
        c.calls.append(("session.is-open", {}, 102))
        with self.assertRaises(AssertionError):
            witness()

    def test_credential_failed_not_retried_within_the_hour(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        c = Client(refusal("key_rejected"), {}, {})
        self.drain(c)
        self.clock.tick += 3599
        self.drain(c)
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(self.stored(rec).next_eligible_ms, self.now + 3600000)
        self.clock.tick += 1
        self.drain(c)
        self.assertEqual(len(c.calls), 3)
        self.assertIsNone(self.stored(rec))

    def test_ineligible_record_skipped_not_waited_behind(self):
        for i in range(12):
            pe.write(self.record(str(i), next_eligible_ms=self.now + (i < 4)), home=self.home)
        client = Client()
        self.drain(client)
        self.assertCountEqual([args["session_id"] for op, args, _ in client.calls
                               if op == "session.end"], [f"session-{i}" for i in range(4, 12)])
        self.assertTrue(all(self.stored(self.record(str(i))).last_attempt_ms is None for i in range(4)))

    def ready(self, path):
        return ReadyFile(path, "", "", "", "", 1)

    def worker(self, factory, *, clock=None):
        worker = pe.ReplayWorker(client_factory=factory, home=self.home, clock=clock or self.clock)
        self.addCleanup(worker.cancel)
        return worker

    def test_replay_open_carries_the_share_deadline(self):
        for name, path in (("a1", "a"), ("a2", "a"), ("b1", "b")):
            pe.write(self.record(name, ready=path), home=self.home)
        calls = []
        a, b = Client(), Client()
        def factory(ready, *, deadline=None):
            start = self.clock.tick
            calls.append((ready.path, start, deadline))
            self.clock.tick += 7 if ready.path == "a" else 0
            return a if ready.path == "a" else b
        with mock.patch("relay_engine.api_client.read_ready_file", side_effect=self.ready):
            worker = self.worker(factory)
            worker.start()
            self.assertTrue(b.closed.wait(2))
            worker.cancel()
        def witness(observed):
            self.assertEqual(observed, [("a", 100, 105), ("b", 107, 110)])
        witness(calls)
        for incorrect in (None, 112, 105):
            with self.assertRaises(AssertionError):
                witness([calls[0], ("b", 107, incorrect)])
        self.assertEqual(len(a.calls), 4)
        self.assertEqual(len(b.calls), 2)

    def test_pending_end_sent_by_bounded_senders(self):
        for name in ("connect", "list", "stop"):
            with self.subTest(name=name):
                rec = self.record(name)
                pe.write(rec, home=self.home)
                c = Client(EndpointUnavailable("not_performed"))
                self.drain(c, share=1 if name == "stop" else 2)
                self.assertEqual(c.calls[0][2], self.clock.tick + (1 if name == "stop" else 2))
                self.assertEqual(self.stored(rec).last_attempt_ms, self.now)
                c = Client()
                self.drain(c, share=2)
                self.assertIsNone(self.stored(rec))
        rec = self.record("doorbell")
        pe.write(rec, home=self.home)
        c = Client()
        with mock.patch("relay_engine.api_client.read_ready_file", side_effect=self.ready):
            w = self.worker(lambda *a, **kw: c)
            w.start()
            self.assertTrue(c.closed.wait(2))
            w.cancel()
        self.assertIsNone(self.stored(rec))
        self.assertEqual([entry[0] for entry in c.calls], ["session.resume", "session.end"])
        self.assertTrue(all(entry[2] == 105.0 for entry in c.calls))

    def test_malformed_pending_end_is_skipped_and_shown(self):
        recs = [self.record("old"), self.record("bad"), self.record("mode")]
        paths = [pe.write(r, home=self.home) for r in recs]
        old = json.loads(paths[0].read_bytes())
        del old["ready_path"], old["operation_key"]
        paths[0].write_text(json.dumps(old))
        paths[1].write_bytes(b"{broken")
        paths[2].chmod(0o644)
        def snapshot():
            return [(p.read_bytes(), p.stat().st_mode, p.stat().st_uid) for p in paths]
        before = snapshot()
        for _ in ("connect", "list", "stop"):
            c = Client()
            self.drain(c)
            self.assertEqual(c.calls, [])
        w = self.worker(lambda *a, **kw: self.fail("opened on a malformed record"))
        self.assertEqual(w._pass(), pe.SendReport())
        self.assertEqual(pe._scan(home=self.home), ([], sorted(paths)))
        self.assertEqual(snapshot(), before)
        paths[2].unlink()
        with self.assertRaises(FileNotFoundError):
            snapshot()

    def hung_client(self):
        pair = socket.socketpair()
        for conn in pair:
            self.addCleanup(conn.close)
        registry, digest = _registry_and_digest()
        return ApiClient(pair[0], registry, digest), pair[1]

    def test_call_bounded_on_a_hung_transport(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        c, _ = self.hung_client()
        start = time.monotonic()
        pe.drain_share(client=c, ready_path="a", home=self.home, share_s=0.15, clock=time)
        self.assertLess(time.monotonic() - start, 0.65)
        self.assertIsNotNone(self.stored(rec).last_attempt_ms)
        self.assertEqual(self.stored(rec).operation_key, rec.operation_key)
        unbounded, peer = self.hung_client()
        finished = threading.Event()
        def without_deadline():
            try:
                unbounded.call("session.resume", {"session_id": rec.session_id})
            except EndpointUnavailable:
                pass
            finally:
                finished.set()
        thread = threading.Thread(target=without_deadline)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.65))
        finally:
            peer.shutdown(socket.SHUT_RDWR)
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_replay_cancel_ends_in_flight_call_before_exit_line(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        client, peer = self.hung_client()
        peer.settimeout(2)
        actual_call = client.call
        bounds = []
        def call(op, args, *, deadline):
            bounds.append(deadline)
            return actual_call(op, args, deadline=deadline)
        with mock.patch("relay_engine.api_client.read_ready_file", side_effect=self.ready):
            with mock.patch.object(client, "call", side_effect=call):
                worker = self.worker(lambda *args, **kwargs: client, clock=time)
                worker.start()
                header = peer.recv(4, socket.MSG_WAITALL)
                self.assertEqual(len(header), 4)
                size = int.from_bytes(header, "big")
                self.assertGreater(size, 0)
                payload = bytearray()
                while len(payload) < size:
                    chunk = peer.recv(size - len(payload))
                    self.assertTrue(chunk)
                    payload.extend(chunk)
                peer.setblocking(False)
                def before_exit():
                    self.assertEqual(peer.recv(1), b"")
                    self.assertFalse(worker._thread.is_alive())
                with self.assertRaises(BlockingIOError):
                    before_exit()
                worker.cancel()
                before_exit()
                self.assertLessEqual(time.monotonic(), bounds[0] + 0.5)

    def test_doorbell_replay_never_blocks_delivery(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        entered, release = threading.Event(), threading.Event()
        class HeldClient(Client):
            def call(self, *args, deadline):
                entered.set()
                release.wait(max(0, min(2, deadline - time.monotonic())))
                raise EndpointUnavailable("unknown")
        c = HeldClient()
        with mock.patch("relay_engine.api_client.read_ready_file", side_effect=self.ready):
            worker = self.worker(lambda *a, **kw: c, clock=time)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                # The stand-in delivery loop advances while replay has not retired its call.
                lines = ["armed", "beat", "notice"]
                self.assertEqual(lines, ["armed", "beat", "notice"])
                self.assertFalse(c.closed.is_set())
            finally:
                release.set()
                worker.cancel()

    def test_replay_task_cancelled_on_exit(self):
        pe.write(self.record(), home=self.home)
        client = Client(EndpointUnavailable("not_performed"))
        with mock.patch("relay_engine.api_client.read_ready_file", side_effect=self.ready):
            worker = self.worker(lambda *a, **kw: client)
            worker.start()
            self.assertTrue(client.closed.wait(2))
            worker.cancel()
            self.assertFalse(worker._thread.is_alive())
            self.assertTrue(client.closed.is_set())

    def test_share_ends_between_resume_and_end(self):
        rec = self.record()
        pe.write(rec, home=self.home)
        clock = self.clock
        class ResumesAtShareEnd(Client):
            def call(self, op, args, *, deadline):
                result = super().call(op, args, deadline=deadline)
                clock.tick = deadline
                return result
        c = ResumesAtShareEnd()
        report = self.drain(c)
        self.assertEqual([call[0] for call in c.calls], ["session.resume"])
        self.assertEqual(report, pe.SendReport(attempted=1, kept=1))
        self.assertEqual(self.stored(rec), dataclasses.replace(rec, last_attempt_ms=self.now))

    def test_replay_backoff_caps_and_resets(self):
        pe.write(self.record(), home=self.home)
        worker = self.worker(lambda *a, **kw: self.fail("a pass was not stubbed"))
        waits = []
        class Stop:
            def is_set(self):
                return False

            def set(self):
                pass

            def wait(self, seconds):
                waits.append(seconds)
                return len(waits) == 8
        with mock.patch.object(worker, "_stop", Stop()):
            kept = pe.SendReport(attempted=1, kept=1)
            deleted = pe.SendReport(attempted=1, deleted=1)
            with mock.patch.object(worker, "_pass", side_effect=[kept]*6+[deleted, kept]):
                worker._run()
        self.assertEqual(waits, [30, 60, 120, 240, 300, 300, 30, 60])
