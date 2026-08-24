import base64
from contextlib import redirect_stderr
import io
import inspect
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from relay_engine import cli, client, daemon, version
from relay_engine.daemon import FrameDecoder, encode_frame


TOOLS = Path(__file__).parents[2]
FP_A = "a" * 64
FP_B = "b" * 64
ADMIN_OPS = {"status", "daemon.stop"}
RECORD_OPS = {
    "submit", "seat.register", "seat.replace", "seat.stand_down",
    "seat.show", "show", "roster", "commission", "adopt_commission",
    "export_ruling", "adopt_ruling", "render", "verify", "reconcile",
    "migrate.check", "lint.context",
}
FROZEN_V1_ERROR_CODES = {
    "E-KEY-MISMATCH", "E-ID-COLLISION", "E-SUPERSEDED",
    "E-PATH-ESCAPE", "E-HEADER", "E-ENVELOPE",
    "E-REPLAY-MISMATCH", "E-STORAGE", "E-DAEMON-DOWN",
    "seat-occupied", "commission-conflict", "commission-late",
    "run-id-mismatch", "run-id-uninitialized", "run-id-invalid",
    "E-FRAMING", "E-WIRE-VERSION", "E-WIRE-OP", "E-WIRE-ARGS",
    "E-DAEMON-STOPPING",
}
DRAFT = """## matrix relay

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: identity-matrix-submit
RUN_ID: identity-matrix
CEREMONY_TIER: low
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: identity-matrix.pair.planner
TO: identity-matrix.pair.implementer
SUBJECT: identity matrix submission

fixture body

FINAL_GIT_STATUS_SHORT: none - fixture root only
"""


def _identity_for(tools_dir, *, install=None):
    tools_dir = Path(tools_dir).resolve()
    identity = {
        "kit": version.KIT_VERSION,
        "install": os.fspath(tools_dir if install is None else install),
    }
    try:
        identity["fp"] = version.fingerprint(tools_dir)
    except version.FingerprintError as error:
        identity["fp_error"] = str(error)
    return identity


def cid_did(client_tools=TOOLS, daemon_tools=TOOLS, *,
            client_install=None, daemon_install=None):
    """Return independent client and daemon identities for test plumbing."""
    return (
        _identity_for(client_tools, install=client_install),
        _identity_for(daemon_tools, install=daemon_install),
    )


def _raw_roundtrip(socket_name, request):
    connection = socket.socket(socket.AF_UNIX)
    try:
        connection.connect(socket_name)
        connection.sendall(encode_frame(request))
        decoder = FrameDecoder()
        while True:
            data = connection.recv(65536)
            if not data:
                raise AssertionError("daemon closed before response")
            frames = decoder.feed(data)
            if frames:
                return json.loads(frames[0].decode("utf-8"))
    finally:
        connection.close()


def _submit_args(root_name, registration):
    draft = Path(root_name, ".engine/drafts/identity-matrix/one.md")
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(DRAFT, encoding="utf-8")
    return os.fspath(draft), os.fspath(Path(root_name, registration["key_path"]))


def _frozen_v1_cli_bytes(error):
    """Render with the heading/text gates frozen at hygiene base 9ad3734."""
    if error["code"] not in FROZEN_V1_ERROR_CODES:
        raise ValueError("invalid inventory parameter")
    for field in ("cause", "remedy"):
        if (not isinstance(error[field], str) or not error[field] or
                "\n" in error[field]):
            raise ValueError("invalid inventory parameter")
    return ("%s\ncause: %s\nremedy: %s\n" % (
        error["code"], error["cause"], error["remedy"])).encode("utf-8")


class RunningDaemon:
    def __init__(self, root_name, did, *, socket_name=None):
        self.root_name = root_name
        self.did = dict(did)
        self.socket_name = socket_name or os.path.join(
            root_name, "socket-dir", "s")
        self.read_fd, write_fd = os.pipe()
        start_kwargs = {
            "ready_fd": write_fd,
            "socket_override": self.socket_name,
            "run_id": "identity-matrix",
        }
        if "did" in inspect.signature(daemon.start).parameters:
            start_kwargs["did"] = self.did
        self.thread = threading.Thread(
            target=daemon.start,
            args=(root_name,),
            kwargs=start_kwargs,
        )

    def start(self):
        self.thread.start()
        if os.read(self.read_fd, 1) != b"R":
            raise AssertionError("daemon did not become ready")
        os.close(self.read_fd)
        self.read_fd = None
        return self

    def stop(self, cid=None):
        if self.thread.is_alive():
            identity = self.did if cid is None else cid
            try:
                if "cid" in inspect.signature(client.request).parameters:
                    _raw_roundtrip(self.socket_name, {
                        "v": 2, "id": str(uuid.uuid4()),
                        "op": "daemon.stop", "args": {}, "cid": identity,
                    })
                else:
                    _raw_roundtrip(self.socket_name, {
                        "v": 1, "id": str(uuid.uuid4()),
                        "op": "daemon.stop", "args": {},
                    })
            except BaseException:
                pass
            self.thread.join(5)
            if self.thread.is_alive():
                raise AssertionError("owned daemon did not stop")
        if self.read_fd is not None:
            os.close(self.read_fd)
            self.read_fd = None


class V1Stub:
    def __init__(self, root_name, pairs):
        self.socket_name = os.path.join(root_name, "old-socket", "s")
        Path(self.socket_name).parent.mkdir()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.socket_name)
        self.listener.listen()
        self.pairs = list(pairs)
        self.failures = []
        self.seen = []
        self.thread = threading.Thread(target=self._serve)
        Path(root_name, ".engine").mkdir(exist_ok=True)
        Path(root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "old-daemon",
            "nonce": "old-daemon",
            "state": "ready",
            "socket": self.socket_name,
            "version": 1,
            "schema_version": 1,
        }), encoding="utf-8")

    def _serve(self):
        try:
            for expected, response in self.pairs:
                connection, _ = self.listener.accept()
                try:
                    request = connection.recv(65536)
                    self.seen.append(request)
                    if request != expected:
                        raise AssertionError(
                            "v1 request bytes changed: %r != %r" % (
                                request, expected))
                    connection.sendall(response)
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

    def start(self):
        self.thread.start()
        return self

    def join(self):
        self.thread.join(5)
        if self.thread.is_alive():
            raise AssertionError("v1 stub did not finish")
        if self.failures:
            raise self.failures[0]


class TestIdentityMatrix(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_name = self.temp.name
        self.running = []

    def tearDown(self):
        for running in reversed(self.running):
            running.stop()
        self.temp.cleanup()

    def start_daemon(self, did):
        running = RunningDaemon(self.root_name, did).start()
        self.running.append(running)
        return running

    def assert_mismatch(self, operation):
        with self.assertRaises(client.RemoteError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "E-VERSION-MISMATCH")
        return caught.exception

    def register_and_submit(self, cid):
        registration = client.request(
            self.root_name, "seat.register", {
                "address": "identity-matrix.pair.planner",
                "role_word": "Planner",
            }, cid=cid)
        draft, key = _submit_args(self.root_name, registration)
        return client.submit(self.root_name, draft, key, cid=cid)

    def test_cell_1_new_new_match_allows_record_and_admin(self):
        cid, did = cid_did()
        self.start_daemon(did)
        status = client.request(self.root_name, "status", {}, cid=cid)
        roster = client.request(self.root_name, "roster", {}, cid=cid)
        self.assertEqual(status["daemon"]["identity"], did)
        self.assertEqual(roster["count"], 0)
        self.assertEqual(json.loads(Path(
            self.root_name, ".engine/daemon.json").read_text())["identity"], did)

    def test_cells_2_and_3_identity_mismatch_has_independent_paths(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        self.start_daemon(did)

        # This negative frame is deliberately literal and bypasses cid_did().
        raw_cid = {"kit": "2.9.0", "fp": FP_B, "install": "/raw-client"}
        raw_record = {
            "v": 2,
            "id": "00000000-0000-0000-0000-000000000002",
            "op": "roster",
            "args": {},
            "cid": raw_cid,
        }
        response = _raw_roundtrip(self.running[-1].socket_name, raw_record)
        self.assertEqual(response["error"]["code"], "E-VERSION-MISMATCH")
        self.assertEqual(response["did"], did)

        with mock.patch.object(
                client, "_roundtrip",
                side_effect=AssertionError("mismatched client contacted socket")):
            self.assert_mismatch(lambda: client.request(
                self.root_name, "roster", {}, cid=raw_cid))

        status = client.request(self.root_name, "status", {}, cid=raw_cid)
        self.assertEqual(status["daemon"]["identity"], did)
        raw_admin = dict(raw_record, op="status")
        raw_admin["id"] = "00000000-0000-0000-0000-000000000003"
        self.assertTrue(_raw_roundtrip(
            self.running[-1].socket_name, raw_admin)["ok"])

        raw_kit_cid = {
            "kit": "2.9.1", "fp": FP_A, "install": "/raw-kit-client",
        }
        raw_kit_record = dict(raw_record, cid=raw_kit_cid)
        raw_kit_record["id"] = "00000000-0000-0000-0000-000000000004"
        response = _raw_roundtrip(
            self.running[-1].socket_name, raw_kit_record)
        self.assertEqual(response["error"]["code"], "E-VERSION-MISMATCH")
        self.assertIn("update the daemon install", response["error"]["remedy"])

        Path(self.root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "new-daemon",
            "nonce": "new-daemon",
            "state": "ready",
            "socket": "/tmp/must-not-contact",
            "version": 2,
            "schema_version": 1,
            "identity": dict(did, kit="2.9.1"),
        }), encoding="utf-8")
        with mock.patch.object(
                client, "_roundtrip",
                side_effect=AssertionError("kit-mismatched client contacted socket")):
            mismatch = self.assert_mismatch(lambda: client.request(
                self.root_name, "roster", {}, cid={
                    "kit": "2.9.0", "fp": FP_A,
                    "install": "/precheck-kit-client",
                }))
        self.assertIn("update the client install", mismatch.remedy)

    def test_lint_context_record_identity_refuses_on_both_enforcement_paths(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        running = self.start_daemon(did)
        mismatched_cid = {
            "kit": "2.9.0", "fp": FP_B, "install": "/raw-client",
        }
        raw = _raw_roundtrip(running.socket_name, {
            "v": 2,
            "id": "00000000-0000-0000-0000-000000000005",
            "op": "lint.context",
            "args": {},
            "cid": mismatched_cid,
        })
        self.assertEqual(raw["error"]["code"], "E-VERSION-MISMATCH")
        self.assertEqual(raw["did"], did)

        state_path = Path(self.root_name, ".engine/daemon.json")
        state = json.loads(state_path.read_text())
        state["identity"] = {
            "kit": "2.9.0", "fp": FP_B, "install": "/new-daemon",
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with mock.patch.object(
                client, "_roundtrip",
                side_effect=AssertionError("mismatched client contacted socket")):
            self.assert_mismatch(lambda: client.request(
                self.root_name, "lint.context", {}, cid={
                    "kit": "2.9.0", "fp": FP_A,
                    "install": "/precheck-client",
                }))
        self.assertEqual(daemon.ADMIN_OPS, ADMIN_OPS)

    def test_lint_context_oversized_cursor_is_typed_and_keeps_connection(self):
        cid, did = cid_did()
        running = self.start_daemon(did)
        malformed = {
            "v": 2,
            "id": "00000000-0000-0000-0000-000000000006",
            "op": "lint.context",
            "args": {"cursor": "9" * 5000 + ":0"},
            "cid": cid,
        }
        status = {
            "v": 2,
            "id": "00000000-0000-0000-0000-000000000007",
            "op": "status",
            "args": {},
            "cid": cid,
        }
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.settimeout(2)
            connection.connect(running.socket_name)
            connection.sendall(encode_frame(malformed))
            decoder = FrameDecoder()
            while True:
                frames = decoder.feed(connection.recv(65536))
                if frames:
                    refusal = json.loads(frames[0].decode("utf-8"))
                    break
            self.assertFalse(refusal["ok"])
            self.assertEqual(refusal["error"]["code"], "E-WIRE-ARGS")

            connection.sendall(encode_frame(status))
            while True:
                frames = decoder.feed(connection.recv(65536))
                if frames:
                    recovered = json.loads(frames[0].decode("utf-8"))
                    break
            self.assertTrue(recovered["ok"])
        finally:
            connection.close()

    def test_v2_admin_requires_grammar_valid_client_identity(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        running = self.start_daemon(did)
        cases = (
            ("missing", None),
            ("null", None),
            ("not-object", "client"),
            ("missing-kit", {"fp": FP_B, "install": "/client"}),
            ("malformed-kit", {"kit": "02.009.0001", "fp": FP_B,
                               "install": "/client"}),
        )
        for offset, (name, malformed_cid) in enumerate(cases, start=1):
            with self.subTest(name=name):
                request = {
                    "v": 2,
                    "id": "00000000-0000-0000-0000-%012d" % offset,
                    "op": "status",
                    "args": {},
                }
                if name != "missing":
                    request["cid"] = malformed_cid
                response = _raw_roundtrip(running.socket_name, request)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"],
                                 "E-VERSION-MISMATCH")
                self.assertEqual(response["did"], did)

    def test_v2_admin_prechecks_malformed_client_identity_without_contact(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        Path(self.root_name, ".engine").mkdir()
        Path(self.root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "new-daemon",
            "nonce": "new-daemon",
            "state": "ready",
            "socket": "/tmp/must-not-contact",
            "version": 2,
            "schema_version": 1,
            "identity": did,
        }), encoding="utf-8")

        def accepting_daemon(_socket_name, request, _timeout):
            return {
                "v": 2,
                "id": request["id"],
                "ok": True,
                "result": {"accepted": True},
                "did": did,
            }

        malformed_cids = (
            {},
            "client",
            {"kit": "02.009.0001", "fp": FP_B, "install": "/client"},
        )
        for op in ADMIN_OPS:
            for malformed_cid in malformed_cids:
                with self.subTest(op=op, cid=malformed_cid), mock.patch.object(
                        client, "_roundtrip",
                        side_effect=accepting_daemon) as contacted:
                    self.assert_mismatch(lambda: client.request(
                        self.root_name, op, {}, cid=malformed_cid))
                    contacted.assert_not_called()

    def test_v2_admin_prechecks_malformed_daemon_identity_without_contact(self):
        cid = {"kit": "2.9.0", "fp": FP_B, "install": "/client"}
        Path(self.root_name, ".engine").mkdir()
        state_path = Path(self.root_name, ".engine/daemon.json")
        base = {
            "pid": os.getpid(),
            "pid_start_time": "new-daemon",
            "nonce": "new-daemon",
            "state": "ready",
            "socket": "/tmp/must-not-contact",
            "version": 2,
            "schema_version": 1,
        }
        malformed_dids = (
            None,
            {},
            {"fp": FP_A, "install": "/daemon"},
            {"kit": "2.9.0", "fp": "A" * 64, "install": "/daemon"},
        )

        def accepting_daemon(_socket_name, request, _timeout):
            return {
                "v": 2,
                "id": request["id"],
                "ok": True,
                "result": {"accepted": True},
                "did": {"kit": "2.9.0", "fp": FP_A,
                        "install": "/daemon"},
            }

        for op in ADMIN_OPS:
            for malformed_did in malformed_dids:
                with self.subTest(op=op, did=malformed_did):
                    state_path.write_text(json.dumps(
                        dict(base, identity=malformed_did)), encoding="utf-8")
                    with mock.patch.object(
                            client, "_roundtrip",
                            side_effect=accepting_daemon) as contacted:
                        self.assert_mismatch(lambda: client.request(
                            self.root_name, op, {}, cid=cid))
                        contacted.assert_not_called()

    def test_v2_admin_authenticates_response_daemon_identity(self):
        cid = {"kit": "2.9.0", "fp": FP_B, "install": "/client"}
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        Path(self.root_name, ".engine").mkdir()
        Path(self.root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "new-daemon",
            "nonce": "new-daemon",
            "state": "ready",
            "socket": "/tmp/fake-daemon",
            "version": 2,
            "schema_version": 1,
            "identity": did,
        }), encoding="utf-8")
        absent = object()
        response_dids = (
            ("missing", absent),
            ("null", None),
            ("malformed", {"kit": "2.9.0", "fp": "A" * 64,
                           "install": "/daemon"}),
            ("different", {"kit": "2.9.0", "fp": FP_B,
                           "install": "/other-daemon"}),
        )
        for name, response_did in response_dids:
            with self.subTest(name=name):
                def response(_socket_name, request, _timeout):
                    value = {
                        "v": 2,
                        "id": request["id"],
                        "ok": True,
                        "result": {"accepted": True},
                    }
                    if response_did is not absent:
                        value["did"] = response_did
                    return value

                with mock.patch.object(client, "_roundtrip",
                                       side_effect=response):
                    self.assert_mismatch(lambda: client.request(
                        self.root_name, "status", {}, cid=cid))

    def test_v2_valid_mismatched_admin_status_and_stop_remain_usable(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        mismatched_cid = {
            "kit": "2.9.0", "fp": FP_B, "install": "/client",
        }
        running = self.start_daemon(did)
        status = client.request(
            self.root_name, "status", {}, cid=mismatched_cid)
        self.assertEqual(status["daemon"]["identity"], did)
        self.assertEqual(client.request(
            self.root_name, "daemon.stop", {}, cid=mismatched_cid),
            {"ok": True})
        running.thread.join(5)
        self.assertFalse(running.thread.is_alive())

    def test_cell_4_old_client_new_daemon_refuses_v1_then_recovers(self):
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/new-daemon"}
        running = self.start_daemon(did)

        # These old-client frames are frozen v1 fixtures and bypass cid_did().
        raw_requests = (
            {"v": 1, "id": "00000000-0000-0000-0000-000000000011",
             "op": "submit", "args": {
                 "envelope": {}, "body_b64": "", "body_sha256": "0" * 64,
                 "content_hash": "1" * 64, "submission_id": "old",
                 "tag": "old"}},
            {"v": 1, "id": "00000000-0000-0000-0000-000000000012",
             "op": "status", "args": {}},
            {"v": 1, "id": "00000000-0000-0000-0000-000000000013",
             "op": "daemon.stop", "args": {}},
        )
        for request in raw_requests:
            with self.subTest(op=request["op"]):
                response = _raw_roundtrip(running.socket_name, request)
                self.assertEqual(
                    response["error"], {
                        "code": "E-WIRE-VERSION",
                        "cause": "wire v1 client did not provide an identity to this wire v2 daemon",
                        "remedy": "update the client install and retry; leave the daemon running",
                        "cls": "wire",
                    })
                self.assertEqual(
                    _frozen_v1_cli_bytes(response["error"]),
                    b"E-WIRE-VERSION\ncause: wire v1 client did not provide an identity to this wire v2 daemon\nremedy: update the client install and retry; leave the daemon running\n",
                )
        self.assertTrue(running.thread.is_alive())

        updated_cid = dict(did, install="/updated-client")
        status = client.request(
            self.root_name, "status", {}, cid=updated_cid)
        self.assertEqual(status["daemon"]["identity"]["kit"],
                         updated_cid["kit"])
        self.assertEqual(status["daemon"]["identity"]["fp"],
                         updated_cid["fp"])
        self.assertIn("path", self.register_and_submit(updated_cid))

    def test_cells_5_and_6_new_client_old_daemon_full_recovery(self):
        cid = {"kit": "2.9.0", "fp": FP_A, "install": "/new-client"}
        status_id = "00000000-0000-0000-0000-000000000021"
        stop_id = "00000000-0000-0000-0000-000000000022"
        status_request = encode_frame(
            {"v": 1, "id": status_id, "op": "status", "args": {}})
        stop_request = encode_frame(
            {"v": 1, "id": stop_id, "op": "daemon.stop", "args": {}})
        status_response = encode_frame({
            "v": 1, "id": status_id, "ok": True,
            "result": {"daemon": {"state": "ready", "version": 1}}})
        stop_response = encode_frame({
            "v": 1, "id": stop_id, "ok": True, "result": {"ok": True}})
        stub = V1Stub(self.root_name, (
            (status_request, status_response), (stop_request, stop_response),
        )).start()

        mismatch = self.assert_mismatch(lambda: client.request(
            self.root_name, "roster", {}, cid=cid))
        self.assertEqual(
            (mismatch.code, mismatch.cause, mismatch.remedy, mismatch.cls),
            (
                "E-VERSION-MISMATCH",
                "wire v1 daemon at socket %s does not expose a daemon identity" %
                stub.socket_name,
                "update the daemon install serving socket %s, then retry" %
                stub.socket_name,
                "policy",
            ),
        )
        with mock.patch.object(client.uuid, "uuid4", side_effect=(
                uuid.UUID(status_id), uuid.UUID(stop_id))):
            self.assertEqual(client.request(
                self.root_name, "status", {}, cid=cid)["daemon"]["version"], 1)
            self.assertEqual(client.request(
                self.root_name, "daemon.stop", {}, cid=cid), {"ok": True})
        stub.join()
        self.assertEqual(client.V1_ADMIN_OPS, ADMIN_OPS)
        self.assertEqual(daemon.ADMIN_OPS, ADMIN_OPS)
        self.assertEqual(set(daemon._SCHEMAS), RECORD_OPS | ADMIN_OPS)
        self.assertFalse(RECORD_OPS & ADMIN_OPS)

        did = dict(cid, install="/updated-daemon")
        running = self.start_daemon(did)
        status = client.request(self.root_name, "status", {}, cid=cid)
        self.assertEqual(
            (status["daemon"]["identity"]["kit"],
             status["daemon"]["identity"]["fp"]),
            (cid["kit"], cid["fp"]),
        )
        self.assertIn("path", self.register_and_submit(cid))
        self.assertTrue(running.thread.is_alive())

    def test_cell_7_identity_semantics_and_nonadmissible_state(self):
        cid = {"kit": "2.9.0", "fp": FP_A, "install": "/client/a/../b"}
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon/other"}
        self.start_daemon(did)
        self.assertEqual(client.request(
            self.root_name, "roster", {}, cid=cid)["count"], 0)

        malformed = (
            (dict(cid, kit="02.009.0001"), did),
            (cid, dict(did, kit="02.009.0001")),
            (dict(cid, kit="02.009.0001"),
             dict(did, kit="02.009.0001")),
            (dict(cid, kit="02.009.0001", fp=FP_B),
             dict(did, kit="02.009.0001")),
        )
        for bad_cid, bad_did in malformed:
            with self.subTest(cid=bad_cid, did=bad_did):
                state_path = Path(self.root_name, ".engine/daemon.json")
                state = json.loads(state_path.read_text())
                state["identity"] = bad_did
                state_path.write_text(json.dumps(state), encoding="utf-8")
                error = self.assert_mismatch(lambda: client.request(
                    self.root_name, "roster", {}, cid=bad_cid))
                self.assertIn("refresh both installs", error.remedy)

        state_path = Path(self.root_name, ".engine/daemon.json")
        original = json.loads(state_path.read_text())
        states = (
            dict(original, identity={"kit": "2.9.0", "fp": FP_B,
                                     "install": "/replacement"}),
            {key: value for key, value in original.items()
             if key != "identity"},
            dict(original, state="stopped"),
        )
        for state in states:
            with self.subTest(state=state):
                state_path.write_text(json.dumps(state), encoding="utf-8")
                with mock.patch.object(
                        client, "_roundtrip",
                        side_effect=AssertionError("inadmissible state contacted")):
                    with self.assertRaises(client.RemoteError):
                        client.request(self.root_name, "roster", {}, cid=cid)

    def test_cell_7b_malformed_daemon_metadata_is_typed_and_not_downgraded(self):
        cid = {"kit": "2.9.0", "fp": FP_A, "install": "/client"}
        state_path = Path(self.root_name, ".engine/daemon.json")
        Path(self.root_name, ".engine").mkdir()
        base = {"pid": os.getpid(), "pid_start_time": "x", "nonce": "x",
                "state": "ready", "socket": "/tmp/unused", "version": 2,
                "schema_version": 1}
        identities = (
            "not-json",
            dict(base, identity="identity-string"),
            dict(base, identity=["identity-list"]),
            dict(base, identity={"fp": FP_A, "install": "/daemon"}),
            dict(base, identity={"kit": "2.9.0", "install": "/daemon"}),
            dict(base, identity={"kit": "2.9.0", "fp": FP_A}),
            dict(base, identity={"kit": "2.9.0", "fp": "A" * 64,
                                 "install": "/daemon"}),
            dict(base, identity={"kit": "2.9.0", "fp": FP_A,
                                 "install": "relative"}),
        )
        for value in identities:
            with self.subTest(value=value):
                state_path.write_text(
                    value if isinstance(value, str) else json.dumps(value),
                    encoding="utf-8")
                with mock.patch.object(client, "_roundtrip") as contacted:
                    with self.assertRaises(client.RemoteError) as caught:
                        client.request(self.root_name, "roster", {}, cid=cid)
                self.assertIn(caught.exception.code,
                              {"E-VERSION-MISMATCH", "E-DAEMON-DOWN"})
                contacted.assert_not_called()

    def test_cell_7b_v2_without_identity_refuses_admin_without_downgrade(self):
        cid = {"kit": "2.9.0", "fp": FP_A, "install": "/client"}
        Path(self.root_name, ".engine").mkdir()
        Path(self.root_name, ".engine/daemon.json").write_text(json.dumps({
            "pid": os.getpid(),
            "pid_start_time": "new-daemon",
            "nonce": "new-daemon",
            "state": "ready",
            "socket": "/tmp/must-not-contact",
            "version": 2,
            "schema_version": 1,
        }), encoding="utf-8")

        def accepting_old_daemon(_socket_name, request, _timeout):
            return {
                "v": 1,
                "id": request["id"],
                "ok": True,
                "result": {"accepted_protocol": request["v"]},
            }

        for op in ("roster", "status", "daemon.stop"):
            with self.subTest(op=op), mock.patch.object(
                    client, "_roundtrip",
                    side_effect=accepting_old_daemon) as contacted:
                with self.assertRaises(client.RemoteError) as caught:
                    client.request(self.root_name, op, {}, cid=cid)
                self.assertEqual(caught.exception.code,
                                 "E-VERSION-MISMATCH")
                contacted.assert_not_called()

    def test_cell_7b_unhashable_fp_error_is_typed_on_both_proof_paths(self):
        cid = {"kit": "2.9.0", "fp": FP_A, "install": "/client"}
        did = {"kit": "2.9.0", "fp": FP_A, "install": "/daemon"}
        running = self.start_daemon(did)
        state_path = Path(self.root_name, ".engine/daemon.json")
        ready_state = json.loads(state_path.read_text())

        for malformed in ([], {}):
            with self.subTest(path="client-daemon-metadata",
                              malformed=malformed):
                bad_did = {"kit": "2.9.0", "fp_error": malformed,
                           "install": "/daemon"}
                state_path.write_text(json.dumps(
                    dict(ready_state, identity=bad_did)), encoding="utf-8")
                with mock.patch.object(client, "_roundtrip") as contacted:
                    try:
                        client.request(self.root_name, "roster", {}, cid=cid)
                    except BaseException as error:
                        self.assertIsInstance(error, client.RemoteError)
                        self.assertEqual(error.code, "E-VERSION-MISMATCH")
                    else:
                        self.fail("malformed daemon fp_error was admitted")
                contacted.assert_not_called()

            with self.subTest(path="client-cid", malformed=malformed):
                state_path.write_text(json.dumps(ready_state),
                                      encoding="utf-8")
                bad_cid = {"kit": "2.9.0", "fp_error": malformed,
                           "install": "/client"}
                with mock.patch.object(client, "_roundtrip") as contacted:
                    try:
                        client.request(
                            self.root_name, "roster", {}, cid=bad_cid)
                    except BaseException as error:
                        self.assertIsInstance(error, client.RemoteError)
                        self.assertEqual(error.code, "E-VERSION-MISMATCH")
                    else:
                        self.fail("malformed client fp_error was admitted")
                contacted.assert_not_called()

            with self.subTest(path="raw-daemon", malformed=malformed):
                raw = {
                    "v": 2,
                    "id": "00000000-0000-0000-0000-000000000071",
                    "op": "roster",
                    "args": {},
                    "cid": {"kit": "2.9.0", "fp_error": malformed,
                            "install": "/raw-client"},
                }
                try:
                    response = _raw_roundtrip(running.socket_name, raw)
                except BaseException as error:
                    self.fail("raw daemon did not return a typed refusal: %r" %
                              (error,))
                self.assertEqual(response["error"]["code"],
                                 "E-VERSION-MISMATCH")
                self.assertEqual(response["did"], did)

    def test_cell_8_full_fingerprint_failure_family(self):
        Path(self.root_name, ".engine").mkdir()
        clean = {"kit": "2.9.0", "fp": FP_A, "install": "/clean"}
        damaged = {"kit": "2.9.0", "fp_error": "missing-member",
                   "install": "/damaged"}
        state_path = Path(self.root_name, ".engine/daemon.json")
        state_base = {"pid": os.getpid(), "pid_start_time": "x",
                      "nonce": "x", "state": "ready",
                      "socket": "/tmp/unused", "version": 2,
                      "schema_version": 1}
        for one_cid, one_did, stale in (
                (damaged, clean, "client"),
                (clean, damaged, "daemon")):
            with self.subTest(one_sided_damage=stale):
                state_path.write_text(json.dumps(
                    dict(state_base, identity=one_did)), encoding="utf-8")
                with mock.patch.object(
                        client, "_roundtrip",
                        side_effect=AssertionError("damaged record contacted")):
                    error = self.assert_mismatch(lambda: client.request(
                        self.root_name, "roster", {}, cid=one_cid))
                self.assertIn("update the %s install" % stale, error.remedy)

        damage_cases = (
            ("extra-direct", "extra-member", lambda staged: (
                staged / "relay_engine/evil.py").write_text("evil\n")),
            ("extra-nested", "extra-member", lambda staged: (
                (staged / "relay_engine/sub").mkdir(),
                (staged / "relay_engine/sub/x.py").write_text("x\n"))),
            ("symlinked", "non-regular-member", lambda staged: (
                (staged / "relay_engine/README.md").unlink(),
                (staged / "relay_engine/README.md").symlink_to("__init__.py"))),
            ("fifo", "non-regular-member", lambda staged: (
                (staged / "relay_engine/README.md").unlink(),
                os.mkfifo(staged / "relay_engine/README.md"))),
            ("removed", "missing-member", lambda staged: (
                staged / "relay_engine/README.md").unlink()),
            ("unreadable", "unreadable-member", lambda staged: (
                staged / "relay_engine/README.md").chmod(0)),
        )
        for name, reason, damage in damage_cases:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as staging:
                    client_tools = Path(staging, "client")
                    daemon_tools = Path(staging, "daemon")
                    shutil.copytree(TOOLS, client_tools)
                    shutil.copytree(TOOLS, daemon_tools)
                    damage(client_tools)
                    damage(daemon_tools)
                    try:
                        cid, did = cid_did(client_tools, daemon_tools)
                    finally:
                        if name == "unreadable":
                            for staged in (client_tools, daemon_tools):
                                (staged / "relay_engine/README.md").chmod(
                                    stat.S_IRUSR | stat.S_IWUSR)
                    self.assertEqual(cid["fp_error"], reason)
                    self.assertEqual(did["fp_error"], reason)
                    running = RunningDaemon(
                        self.root_name, did,
                        socket_name=os.path.join(
                            self.root_name, "socket-%s" % name, "s"),
                    ).start()
                    self.running.append(running)
                    state = json.loads(Path(
                        self.root_name, ".engine/daemon.json").read_text())
                    self.assertEqual(state["identity"], did)
                    self.assertNotIn("fp", state["identity"])
                    self.assert_mismatch(lambda: client.request(
                        self.root_name, "roster", {}, cid=cid))

                    # This negative frame deliberately bypasses cid_did().
                    raw_cid = {"kit": "2.9.0", "fp_error": reason,
                               "install": "/raw-damaged-client"}
                    raw = {"v": 2,
                           "id": "00000000-0000-0000-0000-000000000081",
                           "op": "roster", "args": {}, "cid": raw_cid}
                    response = _raw_roundtrip(running.socket_name, raw)
                    self.assertEqual(
                        response["error"]["code"], "E-VERSION-MISMATCH")
                    self.assertEqual(response["did"], did)
                    raw_clean = dict(raw, cid={
                        "kit": "2.9.0", "fp": FP_A,
                        "install": "/raw-clean-client"})
                    raw_clean["id"] = (
                        "00000000-0000-0000-0000-000000000082")
                    daemon_side = _raw_roundtrip(
                        running.socket_name, raw_clean)
                    self.assertIn(
                        "update the daemon install",
                        daemon_side["error"]["remedy"])
                    self.assertTrue(client.request(
                        self.root_name, "status", {}, cid=cid)["daemon"])
                    running.stop(cid)
                    self.running.remove(running)

    def test_cell_9_cli_renders_registered_errors_and_degrades_remote_shape(self):
        cid, did = cid_did()
        did = dict(did, fp=FP_B)
        self.start_daemon(did)
        stderr = io.StringIO()
        with mock.patch.object(client, "_local_identity", return_value=cid), \
                redirect_stderr(stderr):
            status = cli.main(["roster", "--root", self.root_name])
        self.assertEqual(status, 1)
        self.assertIn("E-VERSION-MISMATCH", stderr.getvalue())
        self.assertNotIn("unexpected error", stderr.getvalue())

        cases = (
            ({"code": "E-FUTURE-DAEMON", "cause": "future cause",
              "remedy": "future remedy", "cls": "wire"},
             "ae11568440b6", 87),
            ({}, "44136fa355b3", 2),
            ({"code": [], "cause": "x", "remedy": "y", "cls": "wire"},
             "4ca70b667eab", 49),
            ({"code": "E-KEY-MISMATCH", "cause": "bad\ncause",
              "remedy": "retry", "cls": "policy"},
             "2c955f7e1cbf", 78),
        )
        for value, digest, length in cases:
            with self.subTest(value=value):
                stderr = io.StringIO()
                error = client.RemoteError(value)
                with redirect_stderr(stderr):
                    cli._report_command_error(error)
                self.assertEqual(
                    stderr.getvalue(),
                    "unexpected error\nunrecognized-input "
                    "(sha256:%s, length %d)\n" % (digest, length),
                )
                self.assertNotIn("future cause", stderr.getvalue())
                self.assertNotIn("bad", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
