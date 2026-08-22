import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from relay_engine import client, daemon, strings
from relay_engine.daemon import FrameDecoder, encode_frame
from relay_engine.ledger import open_ledger
from relay_engine.tests.test_identity_matrix import RunningDaemon


FP = "a" * 64
DID = {"kit": "2.9.0", "fp": FP, "install": "/context-daemon"}
REQUEST_ID = "00000000-0000-0000-0000-000000000201"
HASH = "b" * 64


def _json_string(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _predicted_response_bytes(request_id, did, page):
    did_json = ("{\"fp\":" + _json_string(did["fp"]) +
                ",\"install\":" + _json_string(did["install"]) +
                ",\"kit\":" + _json_string(did["kit"]) + "}")
    entries = []
    for entry in page["entries"]:
        entries.append(
            "{\"body_sha256\":" + _json_string(entry["body_sha256"]) +
            ",\"origin\":" + _json_string(entry["origin"]) +
            ",\"path\":" + _json_string(entry["path"]) + "}")
    page_parts = []
    if "cursor" in page:
        page_parts.append("\"cursor\":" + _json_string(page["cursor"]))
    page_parts.extend(("\"entries\":[" + ",".join(entries) + "]",
                       "\"snapshot\":" + _json_string(page["snapshot"])))
    page_json = "{" + ",".join(page_parts) + "}"
    return ("{\"did\":" + did_json +
            ",\"id\":" + _json_string(request_id) +
            ",\"ok\":true,\"result\":" + page_json +
            ",\"v\":2}").encode("utf-8")


def _entry(path, *, origin="hand", body_sha256=HASH):
    return {"path": path, "body_sha256": body_sha256, "origin": origin}


class _StubDaemon:
    def __init__(self, root_name, responses, *, delay_at=None):
        self.root_name = root_name
        self.responses = list(responses)
        self.delay_at = delay_at
        self.socket_name = os.path.join(root_name, "stub", "s")
        Path(self.socket_name).parent.mkdir(parents=True)
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.socket_name)
        self.listener.listen()
        self.failures = []
        self.requests = []
        self.thread = threading.Thread(target=self._serve)
        Path(root_name, ".engine").mkdir(exist_ok=True)
        Path(root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "context-stub",
            "nonce": "context-stub",
            "state": "ready",
            "socket": self.socket_name,
            "version": 2,
            "schema_version": 1,
            "identity": DID,
        }), encoding="utf-8")

    def start(self):
        self.thread.start()
        return self

    def _read_request(self, connection):
        decoder = FrameDecoder()
        while True:
            data = connection.recv(65536)
            if not data:
                raise AssertionError("client closed before request")
            frames = decoder.feed(data)
            if frames:
                return json.loads(frames[0].decode("utf-8"))

    def _serve(self):
        try:
            for index, result in enumerate(self.responses):
                connection, _ = self.listener.accept()
                try:
                    request = self._read_request(connection)
                    self.requests.append(request)
                    if self.delay_at == index:
                        time.sleep(0.2)
                    response = {"v": 2, "id": request["id"], "ok": True,
                                "result": result, "did": DID}
                    connection.sendall(encode_frame(response))
                except BrokenPipeError:
                    pass
                finally:
                    connection.close()
        except BaseException as error:
            self.failures.append(error)
        finally:
            self.listener.close()
            try:
                os.unlink(self.socket_name)
            except FileNotFoundError:
                pass

    def join(self):
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("stub daemon did not finish")
        if self.failures:
            raise self.failures[0]


class ContextDaemonCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_name = self.temp.name
        self.running = RunningDaemon(self.root_name, DID).start()

    def tearDown(self):
        self.running.stop()
        self.temp.cleanup()

    def insert_entries(self, entries, *, start_seq=1):
        engine_fd = os.open(os.path.join(self.root_name, ".engine"),
                            os.O_RDONLY | os.O_DIRECTORY)
        try:
            ledger = open_ledger(engine_fd)
            try:
                rows = []
                for seq, entry in enumerate(entries, start=start_seq):
                    rows.append((
                        seq, "context-%d" % seq, "20260821-%06d" % seq,
                        entry["path"], "IMPL", "Pair Implementer",
                        "context-%d" % seq, "context.implementer", "{}",
                        b"context", entry["body_sha256"], "c" * 64,
                        entry["origin"], "[]",
                    ))
                ledger.executemany(
                    "INSERT INTO relays("
                    "seq,submission_id,stamp,rendered_path,phase,role,"
                    "dispatch_id,from_seat,headers_json,body,body_sha256,"
                    "content_hash,origin,advisories_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            finally:
                ledger.close()
        finally:
            os.close(engine_fd)

    def capture_daemon_frames(self):
        captured = []
        original = daemon.encode_frame

        def capture(value):
            frame = original(value)
            captured.append(frame[4:])
            return frame

        return captured, mock.patch.object(daemon, "encode_frame",
                                            side_effect=capture)


class TestLintContextBudget(ContextDaemonCase):
    def test_budget_has_positive_explicit_margin_below_frame_maximum(self):
        self.assertGreater(strings.CONTEXT_PAGE_BUDGET, 0)
        self.assertGreater(strings.CONTEXT_FRAME_MARGIN, 0)
        self.assertEqual(strings.CONTEXT_PAGE_BUDGET +
                         strings.CONTEXT_FRAME_MARGIN, daemon.MAX_FRAME)
        self.assertLess(strings.CONTEXT_PAGE_BUDGET, daemon.MAX_FRAME)

    def test_largest_accepted_page_uses_complete_response_bytes(self):
        empty = _entry("")
        empty_page = {"snapshot": "1", "entries": [empty]}
        empty_size = len(_predicted_response_bytes(REQUEST_ID, DID, empty_page))
        path = "a" * (strings.CONTEXT_PAGE_BUDGET - empty_size - 3)
        expected_entry = _entry(path)
        expected_page = {"snapshot": "1", "entries": [expected_entry]}
        predicted = _predicted_response_bytes(REQUEST_ID, DID, expected_page)
        self.assertEqual(len(predicted), strings.CONTEXT_PAGE_BUDGET - 3)
        self.insert_entries([expected_entry])

        captured, patch = self.capture_daemon_frames()
        with patch, mock.patch.object(client.uuid, "uuid4",
                                      return_value=uuid.UUID(REQUEST_ID)):
            context = client.lint_context(
                self.root_name, cid=dict(DID, install="/context-client"))

        self.assertEqual(context,
                         {"snapshot": "1", "entries": [expected_entry]})
        self.assertEqual(captured, [predicted])
        self.assertLess(len(captured[0]), strings.CONTEXT_PAGE_BUDGET)

    def test_multibyte_and_escaped_paths_have_exact_byte_accounting(self):
        entries = [
            _entry('lane/quote-"-and-backslash-\\.md', origin="daemon"),
            _entry("lane/éclair-雪-😀.md", origin="adopted"),
        ]
        ordered = sorted(entries, key=lambda entry: entry["path"].encode("utf-8"))
        expected_page = {"snapshot": "2", "entries": ordered}
        predicted = _predicted_response_bytes(REQUEST_ID, DID, expected_page)
        generic = json.dumps(
            daemon.success_response(REQUEST_ID, expected_page, DID),
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(predicted, generic)
        self.assertLess(len(predicted), strings.CONTEXT_PAGE_BUDGET)
        self.insert_entries(list(reversed(entries)))

        captured, patch = self.capture_daemon_frames()
        with patch, mock.patch.object(client.uuid, "uuid4",
                                      return_value=uuid.UUID(REQUEST_ID)):
            context = client.lint_context(
                self.root_name, cid=dict(DID, install="/context-client"))

        self.assertEqual(context,
                         {"snapshot": "2", "entries": ordered})
        self.assertEqual(captured, [predicted])

    def test_first_over_budget_candidate_refuses_before_frame_encoding(self):
        first = _entry("a")
        empty_second = _entry("")
        empty_page = {"snapshot": "2", "entries": [first, empty_second]}
        empty_size = len(_predicted_response_bytes(REQUEST_ID, DID, empty_page))
        second = _entry("b" * (strings.CONTEXT_PAGE_BUDGET - empty_size))
        candidate = {"snapshot": "2", "entries": [first, second]}
        self.assertEqual(len(_predicted_response_bytes(
            REQUEST_ID, DID, candidate)), strings.CONTEXT_PAGE_BUDGET)
        self.assertLess(len(_predicted_response_bytes(
            REQUEST_ID, DID, {"snapshot": "2", "entries": [first]})),
            strings.CONTEXT_PAGE_BUDGET)
        self.insert_entries([first, second])

        captured, patch = self.capture_daemon_frames()
        with patch, mock.patch.object(client.uuid, "uuid4",
                                      return_value=uuid.UUID(REQUEST_ID)):
            with self.assertRaises(client.RemoteError) as caught:
                client.lint_context(
                    self.root_name, cid=dict(DID, install="/context-client"))

        self.assertEqual(caught.exception.code, "E-CONTEXT-BUDGET")
        self.assertTrue(captured)
        self.assertTrue(all(len(body) < strings.CONTEXT_PAGE_BUDGET
                            for body in captured))

    def test_individually_unrepresentable_entry_refuses_without_large_frame(self):
        empty_page = {"snapshot": "1", "entries": [_entry("")]}
        empty_size = len(_predicted_response_bytes(REQUEST_ID, DID, empty_page))
        entry = _entry("z" * (strings.CONTEXT_PAGE_BUDGET - empty_size))
        candidate = {"snapshot": "1", "entries": [entry]}
        self.assertEqual(len(_predicted_response_bytes(
            REQUEST_ID, DID, candidate)), strings.CONTEXT_PAGE_BUDGET)
        self.insert_entries([entry])

        captured, patch = self.capture_daemon_frames()
        with patch, mock.patch.object(client.uuid, "uuid4",
                                      return_value=uuid.UUID(REQUEST_ID)):
            with self.assertRaises(client.RemoteError) as caught:
                client.lint_context(
                    self.root_name, cid=dict(DID, install="/context-client"))

        self.assertEqual(caught.exception.code, "E-CONTEXT-BUDGET")
        self.assertTrue(captured)
        self.assertTrue(all(len(body) < strings.CONTEXT_PAGE_BUDGET
                            for body in captured))


class TestLintContextPagination(ContextDaemonCase):
    def test_entry_cap_paginates_and_client_reassembles_total_byte_order(self):
        entries = [_entry("lane/%04d.md" % index,
                          origin=("daemon", "hand", "adopted")[index % 3])
                   for index in range(strings.CONTEXT_ENTRY_CAP + 1)]
        self.insert_entries(list(reversed(entries)))
        cid = dict(DID, install="/context-client")

        first = client.request(self.root_name, "lint.context", {}, cid=cid)
        self.assertEqual(len(first["entries"]), strings.CONTEXT_ENTRY_CAP)
        self.assertEqual(first["cursor"], "%d:%d" % (
            len(entries), strings.CONTEXT_ENTRY_CAP))
        appended = _entry("lane/zzzz-appended.md", origin="daemon")
        self.insert_entries([appended], start_seq=len(entries) + 1)
        second = client.request(
            self.root_name, "lint.context", {"cursor": first["cursor"]},
            cid=cid)
        self.assertEqual(second,
                         {"snapshot": str(len(entries)),
                          "entries": [entries[-1]]})
        self.assertNotIn("cursor", second)

        context = client.lint_context(self.root_name, cid=cid)
        self.assertEqual(context,
                         {"snapshot": str(len(entries) + 1),
                          "entries": entries + [appended]})


class TestLintContextHostilePages(unittest.TestCase):
    def _assert_total_abort(self, pages, *, timeout=0.5, delay_at=None):
        with tempfile.TemporaryDirectory() as root_name:
            stub = _StubDaemon(root_name, pages, delay_at=delay_at).start()
            try:
                with self.assertRaises((ValueError, TimeoutError,
                                        client.RemoteError)):
                    client.lint_context(
                        root_name, timeout=timeout,
                        cid=dict(DID, install="/context-client"))
            finally:
                stub.join()

    def test_truncated_page_set_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "2", "entries": [_entry("a")], "cursor": "2:1"},
            {"snapshot": "2", "entries": []},
        ])

    def test_duplicate_entry_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "2", "entries": [_entry("a")], "cursor": "2:1"},
            {"snapshot": "2", "entries": [_entry("a")]},
        ])

    def test_reordered_entries_abort_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "2", "entries": [_entry("b"), _entry("a")]},
        ])

    def test_snapshot_churn_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "2", "entries": [_entry("a")], "cursor": "2:1"},
            {"snapshot": "3", "entries": [_entry("b")]},
        ])

    def test_page_gap_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "3", "entries": [_entry("a")], "cursor": "3:2"},
        ])

    def test_malformed_page_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "1", "entries": [
                {"path": "a", "body_sha256": "not-a-hash",
                 "origin": "hand"},
            ]},
        ])

    def test_timeout_aborts_without_partial_context(self):
        self._assert_total_abort([
            {"snapshot": "2", "entries": [_entry("a")], "cursor": "2:1"},
            {"snapshot": "2", "entries": [_entry("b")]},
        ], timeout=0.05, delay_at=1)


if __name__ == "__main__":
    unittest.main()
