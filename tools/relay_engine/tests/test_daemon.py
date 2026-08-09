import contextlib
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import threading
import time
import unittest
import uuid

from relay_engine import daemon, errors, strings
from relay_engine.daemon import (FrameDecoder, SerialWriter, WireFault,
                                 acquire_lease, decode_request, encode_frame,
                                 error_response, prepare_socket,
                                 socket_path)
from relay_engine.paths import Root


class TestWire(unittest.TestCase):
    def request(self, **changes):
        value = {"v": 1, "id": str(uuid.uuid4()), "op": "status",
                 "args": {}}
        value.update(changes)
        return value

    def test_coalesced_and_dribbled_frames(self):
        requests = [self.request(), self.request()]
        data = b"".join(encode_frame(request) for request in requests)
        decoder = FrameDecoder()
        self.assertEqual([decode_request(body) for body in decoder.feed(data)],
                         requests)
        decoder = FrameDecoder()
        bodies = []
        for byte in data:
            bodies.extend(decoder.feed(bytes([byte])))
        self.assertEqual([decode_request(body) for body in bodies], requests)

    def test_fragmented_close_oversize_and_bad_json_are_framing_errors(self):
        decoder = FrameDecoder()
        decoder.feed(b"\0\0\0\x10partial")
        with self.assertRaises(WireFault) as truncated:
            decoder.finish()
        self.assertEqual(truncated.exception.error.code, "E-FRAMING")
        self.assertEqual(truncated.exception.error.params["reason"],
                         "truncated")
        with self.assertRaises(WireFault) as oversize:
            FrameDecoder().feed((16 * 1024 * 1024 + 1).to_bytes(4, "big"))
        self.assertEqual(oversize.exception.error.params["reason"],
                         "oversize")
        for body, reason in ((b"\xff", "not-utf8"), (b"{", "not-json")):
            with self.subTest(reason=reason), self.assertRaises(
                    WireFault) as malformed:
                decode_request(body)
            self.assertEqual(malformed.exception.error.params["reason"],
                             reason)

    def test_version_op_and_schema_errors_are_complete_and_redacted(self):
        forbidden = "database-secret-token"
        cases = [
            (self.request(v=forbidden), "E-WIRE-VERSION"),
            (self.request(op=forbidden), "E-WIRE-OP"),
            (self.request(args={"extra": True}), "E-WIRE-ARGS"),
        ]
        for request, code in cases:
            with self.subTest(code=code), self.assertRaises(WireFault) as caught:
                decode_request(json.dumps(request).encode("utf-8"))
            response = error_response(request["id"], caught.exception.error)
            self.assertEqual(response["id"], request["id"])
            self.assertEqual(response["error"],
                             caught.exception.error.as_dict())
            self.assertEqual(response["error"]["cls"], "wire")
            serialized = json.dumps(response, sort_keys=True)
            self.assertNotIn(forbidden, serialized)

    def test_pre_id_framing_response_uses_null_id_and_exact_error(self):
        fault = WireFault(errors.error_for("E-FRAMING", reason="not-json"))
        response = error_response(None, fault.error)
        expected = errors.error_for("E-FRAMING", reason="not-json").as_dict()
        self.assertEqual(response,
            {"v": 1, "id": None, "ok": False, "error": expected})

    def test_every_operation_schema_accepts_its_minimal_shape(self):
        args = {
            "submit": {"envelope": {}, "body_b64": "", "body_sha256": "0" * 64,
                       "content_hash": "1" * 64, "submission_id": "sid",
                       "tag": "tag"},
            "seat.register": {"address": "v29-a.planner", "role_word": "planner"},
            "seat.replace": {"address": "v29-a.planner", "role_word": "planner"},
            "seat.stand_down": {"address": "v29-a.planner"},
            "seat.show": {"address": "v29-a.planner"},
            "status": {}, "roster": {},
            "commission": {"dispatch_relay_path": "lane/a.md",
                           "child_run_id": "child"},
            "adopt_commission": {"record_b64": ""},
            "export_ruling": {"ruling_path": "lane/a.md", "for_child": "child"},
            "adopt_ruling": {"bundle_b64": ""},
            "render": {}, "verify": {}, "reconcile": {},
            "migrate.check": {}, "daemon.stop": {},
        }
        for op, op_args in args.items():
            with self.subTest(op=op):
                request = self.request(op=op, args=op_args)
                self.assertEqual(decode_request(
                    json.dumps(request).encode("utf-8")), request)


class TestWriter(unittest.TestCase):
    def test_fifo_and_stop_cutoff(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        def handler(request):
            order.append(request["n"])
            if request["n"] == 1:
                entered.set()
                release.wait(2)
            return request["n"]

        writer = SerialWriter(handler)
        writer.start()
        first = writer.enqueue({"n": 1})
        self.assertTrue(entered.wait(2))
        second = writer.enqueue({"n": 2})
        writer.begin_stop()
        with self.assertRaises(errors.EngineError) as stopped:
            writer.enqueue({"n": 3})
        self.assertEqual(stopped.exception.code, "E-DAEMON-STOPPING")
        release.set()
        self.assertEqual(first.result(2), 1)
        self.assertEqual(second.result(2), 2)
        writer.join(2)
        self.assertEqual(order, [1, 2])

    def test_concurrent_enqueue_order_is_the_execution_order(self):
        order = []
        writer = SerialWriter(lambda request: order.append(request["n"]) or
                              request["n"])
        writer.start()
        futures = [writer.enqueue({"n": number}) for number in range(20)]
        writer.begin_stop()
        self.assertEqual([future.result(2) for future in futures], list(range(20)))
        writer.join(2)
        self.assertEqual(order, list(range(20)))


class TestDiagnostics(unittest.TestCase):
    def tearDown(self):
        strings.set_diagnostic_sink(None)
        daemon._log_fd = None
        daemon._diag_clock = time.time

    def test_prebind_discards_and_bound_callback_is_byte_exact(self):
        class DiagProbe(Exception):
            def __repr__(self):
                return "DiagProbe(a\nb\rc\x01d\\e)"

        strings.report_diagnostic(DiagProbe())
        read_fd, write_fd = os.pipe()
        try:
            daemon._log_fd = write_fd
            daemon._diag_clock = lambda: 0.0
            daemon.start()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                strings.report_diagnostic(DiagProbe())
            os.close(write_fd)
            write_fd = None
            actual = os.read(read_fd, 4096)
            self.assertEqual(
                actual,
                b"diag 1970-01-01T00:00:00Z DiagProbe(a\\nb\\rc\\x01d\\\\e)\n")
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)

    def test_partial_eintr_two_threads_are_serialized_and_zero_stops(self):
        class Probe(Exception):
            pass

        written = bytearray()
        calls = 0
        real_write = daemon.os.write

        def partial(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError(errno.EINTR, "interrupted")
            count = min(3, len(data))
            written.extend(data[:count])
            return count

        daemon._log_fd = 99
        daemon._diag_clock = lambda: 0.0
        daemon.os.write = partial
        try:
            threads = [threading.Thread(target=daemon._log_diagnostic,
                                        args=(Probe(str(number)),))
                       for number in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            records = bytes(written).splitlines(keepends=True)
            self.assertEqual(len(records), 2)
            self.assertTrue(all(record.startswith(
                b"diag 1970-01-01T00:00:00Z Probe(") for record in records))

            daemon.os.write = lambda fd, data: 0
            thread = threading.Thread(target=daemon._log_diagnostic,
                                      args=(Probe("zero"),))
            thread.start()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        finally:
            daemon.os.write = real_write


class TestOwnership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)

    def tearDown(self):
        self.root.close()
        self.temp.cleanup()

    def test_fresh_root_two_lease_race_has_one_owner(self):
        lease = acquire_lease(self.root)
        try:
            with self.assertRaises(BlockingIOError):
                acquire_lease(self.root)
            self.assertTrue(Path(self.temp.name, ".engine/daemon.lock").is_file())
        finally:
            lease.close()
        replacement = acquire_lease(self.root)
        replacement.close()

    def test_log_mode_flags_and_hostile_fifo_refusal(self):
        lease = acquire_lease(self.root)
        try:
            fd = daemon.open_log(lease.engine_dirfd)
            try:
                info = os.fstat(fd)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                self.assertEqual(info.st_mode & 0o777, 0o600)
                self.assertTrue(flags & os.O_APPEND)
                self.assertFalse(flags & os.O_NONBLOCK)
            finally:
                os.close(fd)
        finally:
            lease.close()

        os.unlink(Path(self.temp.name, ".engine/daemon.log"))
        os.mkfifo(Path(self.temp.name, ".engine/daemon.log"), 0o600)
        lease = acquire_lease(self.root)
        try:
            started = time.monotonic()
            with self.assertRaises(OSError):
                daemon.open_log(lease.engine_dirfd)
            self.assertLess(time.monotonic() - started, 1.0)
            reader = os.open(Path(self.temp.name, ".engine/daemon.log"),
                             os.O_RDONLY | os.O_NONBLOCK)
            try:
                with self.assertRaises(OSError):
                    daemon.open_log(lease.engine_dirfd)
            finally:
                os.close(reader)
        finally:
            lease.close()

    def test_socket_contract_long_root_override_mode_and_owner(self):
        long_root = "/" + "x" * 200
        derived = socket_path(long_root)
        self.assertLess(len(derived.encode("utf-8")), 60)
        with tempfile.TemporaryDirectory() as rendezvous:
            path = os.path.join(rendezvous, "nested", "s")
            listener = prepare_socket(path)
            try:
                self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777,
                                 0o700)
                client = socket.socket(socket.AF_UNIX)
                try:
                    client.connect(path)
                finally:
                    client.close()
                accepted, _ = listener.accept()
                accepted.close()
            finally:
                listener.close()
                os.unlink(path)
                os.rmdir(os.path.dirname(path))
            with self.assertRaises(PermissionError):
                prepare_socket(path, expected_uid=os.geteuid() + 1)


class TestRuntime(unittest.TestCase):
    @staticmethod
    def roundtrip(path, request):
        client = socket.socket(socket.AF_UNIX)
        try:
            client.connect(path)
            client.sendall(encode_frame(request))
            decoder = FrameDecoder()
            while True:
                data = client.recv(65536)
                if not data:
                    raise AssertionError("daemon closed before response")
                frames = decoder.feed(data)
                if frames:
                    return json.loads(frames[0])
        finally:
            client.close()

    def request(self, op):
        return {"v": 1, "id": str(uuid.uuid4()), "op": op, "args": {}}

    def test_startup_trace_ready_serve_stop_and_release(self):
        with tempfile.TemporaryDirectory() as root_name:
            socket_name = os.path.join(root_name, "socket-dir", "s")
            trace = []
            read_fd, write_fd = os.pipe()
            thread = threading.Thread(
                target=daemon.start,
                args=(root_name,),
                kwargs={"ready_fd": write_fd, "trace": trace,
                        "socket_override": socket_name})
            thread.start()
            self.assertEqual(os.read(read_fd, 1), b"R")
            os.close(read_fd)
            status_request = self.request("status")
            response = self.roundtrip(socket_name, status_request)
            self.assertEqual(response["id"], status_request["id"])
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["daemon"]["state"], "ready")
            stop_request = self.request("daemon.stop")
            self.assertTrue(self.roundtrip(socket_name, stop_request)["ok"])
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(trace, [
                "open-root", "ensure-engine", "flock", "starting-record",
                "log-bound", "writer-started", "schema-init",
                "run-identity:absent", "top-seat:present", "recovery:absent",
                "socket-bound", "ready-record", "ready-byte",
                "ready-pipe-closed",
            ])
            state = json.loads(Path(root_name, ".engine/daemon.json").read_bytes())
            self.assertEqual(state["state"], "stopped")
            with Root(root_name) as root:
                lease = acquire_lease(root)
                lease.close()

    def test_pre_ready_log_failure_closes_pipe_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as root_name:
            engine = Path(root_name, ".engine")
            engine.mkdir()
            os.mkfifo(engine / "daemon.log", 0o600)
            socket_name = os.path.join(root_name, "socket-dir", "s")
            read_fd, write_fd = os.pipe()
            failures = []

            def run():
                try:
                    daemon.start(root_name, ready_fd=write_fd,
                                 socket_override=socket_name)
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=run)
            started = time.monotonic()
            thread.start()
            self.assertEqual(os.read(read_fd, 1), b"")
            os.close(read_fd)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertTrue(failures)
            with Root(root_name) as root:
                lease = acquire_lease(root)
                lease.close()

    def test_close_inherited_keeps_only_readiness_descriptor(self):
        result_read, result_write = os.pipe()
        unwanted_read, unwanted_write = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(result_read)
            daemon._close_inherited({result_write})
            try:
                os.fstat(unwanted_read)
            except OSError:
                marker = b"closed"
            else:
                marker = b"open"
            stream = io.FileIO(result_write, mode="wb", closefd=True)
            stream.write(marker)
            stream.close()
            os._exit(0)
        os.close(result_write)
        os.close(unwanted_read)
        os.close(unwanted_write)
        self.assertEqual(os.read(result_read, 16), b"closed")
        os.close(result_read)
        os.waitpid(child, 0)

    def test_double_fork_parent_returns_while_daemon_holds_lock(self):
        with tempfile.TemporaryDirectory() as root_name:
            socket_name = os.path.join(root_name, "socket-dir", "s")
            self.assertTrue(daemon.launch(
                root_name, socket_override=socket_name, timeout=5))
            with Root(root_name) as root:
                with self.assertRaises(BlockingIOError):
                    acquire_lease(root)
            status = self.roundtrip(socket_name, self.request("status"))
            self.assertTrue(status["ok"])
            self.assertTrue(self.roundtrip(
                socket_name, self.request("daemon.stop"))["ok"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    with Root(root_name) as root:
                        lease = acquire_lease(root)
                except BlockingIOError:
                    time.sleep(0.02)
                    continue
                lease.close()
                break
            else:
                self.fail("daemon did not release the lock")

    def test_paused_owner_cannot_be_taken_over_and_killed_owner_can(self):
        with tempfile.TemporaryDirectory() as root_name:
            socket_name = os.path.join(root_name, "socket-dir", "s")
            self.assertTrue(daemon.launch(
                root_name, socket_override=socket_name, timeout=5))
            state_path = Path(root_name, ".engine/daemon.json")
            pid = json.loads(state_path.read_bytes())["pid"]
            os.kill(pid, signal.SIGSTOP)
            try:
                self.assertFalse(daemon.launch(
                    root_name, socket_override=socket_name, timeout=1))
            finally:
                os.kill(pid, signal.SIGCONT)
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            self.assertTrue(daemon.launch(
                root_name, socket_override=socket_name, timeout=5))
            self.assertTrue(self.roundtrip(
                socket_name, self.request("daemon.stop"))["ok"])

    def test_pid_reuse_token_allows_stale_socket_cleanup(self):
        with tempfile.TemporaryDirectory() as root_name:
            engine = Path(root_name, ".engine")
            engine.mkdir()
            socket_name = os.path.join(root_name, "socket-dir", "s")
            listener = prepare_socket(socket_name)
            listener.close()
            Path(engine, "daemon.json").write_text(json.dumps({
                "pid": os.getpid(), "pid_start_time": "reused-old-token",
                "socket": socket_name, "state": "ready"}))
            real_start_time = daemon._pid_start_time
            daemon._pid_start_time = lambda pid: "current-process-token"
            try:
                self.assertTrue(daemon.launch(
                    root_name, socket_override=socket_name, timeout=5))
                self.assertTrue(self.roundtrip(
                    socket_name, self.request("daemon.stop"))["ok"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with Root(root_name) as root:
                            lease = acquire_lease(root)
                    except BlockingIOError:
                        time.sleep(0.02)
                        continue
                    lease.close()
                    break
                else:
                    self.fail("replacement daemon did not stop")
            finally:
                daemon._pid_start_time = real_start_time


if __name__ == "__main__":
    unittest.main()
