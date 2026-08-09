import base64
import json
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

from relay_engine import cli, client, daemon, errors
from relay_engine.client import discover_root, submit
from relay_engine.envelope import body_sha256, content_hash, parse_draft
from relay_engine.ledger import admit, init_schema, open_ledger
from relay_engine.paths import Root, ensure_engine_dir


DRAFT = """## relay

ROLE: Planner
PHASE: PLAN
AUTHORITY: plan-only
DISPATCH_ID: v29-engine-plan-1
RUN_ID: v29
CEREMONY_TIER: large
EVIDENCE_TARGET: E2
HUMAN_GATE_REQUIRED: no
FROM: v29-a.planner
TO: v29-a.implementer
SUBJECT: end to end

body
"""


class TestSubmitE2E(unittest.TestCase):
    EDGE_CAUSE = (
        "admits_against did not resolve to exactly one existing relay "
        "under the root")
    EDGE_PREFIX = (
        "field: admits_against; expected: a single root-relative path "
        "resolving to exactly one existing relay; existing targets: ")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_name = self.temp.name
        Path(self.root_name, ".engine/drafts/v29-a.planner").mkdir(parents=True)
        Path(self.root_name, ".engine/seats/v29-a.planner").mkdir(
            parents=True)
        self.draft_rel = ".engine/drafts/v29-a.planner/one.md"
        self.key_rel = ".engine/seats/v29-a.planner/occ.key"
        Path(self.root_name, self.draft_rel).write_text(DRAFT)
        Path(self.root_name, self.key_rel).write_text("tag-value\n")
        with Root(self.root_name) as root:
            engine_fd = ensure_engine_dir(root.dirfd)
            try:
                ledger = init_schema(engine_fd, root.path)
                ledger.execute(
                    "INSERT INTO meta(key,value) VALUES('run_id','v29')")
                ledger.execute(
                    "INSERT INTO seat_events("
                    "address,event,occupant_id,key_id,boot_relay_seq) "
                    "VALUES('v29-a.planner','occupied','occ','key',NULL)")
                ledger.close()
            finally:
                os.close(engine_fd)
        self.socket_name = os.path.join(self.root_name, "socket-dir", "s")
        self.read_fd, write_fd = os.pipe()
        self.thread = threading.Thread(
            target=daemon.start, args=(self.root_name,),
            kwargs={"ready_fd": write_fd,
                    "socket_override": self.socket_name})
        self.thread.start()
        self.assertEqual(os.read(self.read_fd, 1), b"R")
        os.close(self.read_fd)
        self.read_fd = None

    def tearDown(self):
        if self.thread.is_alive():
            try:
                client.request(self.root_name, "daemon.stop", {})
            except BaseException:
                pass
            self.thread.join(5)
        if self.read_fd is not None:
            os.close(self.read_fd)
        self.temp.cleanup()

    def seed_edge_candidates(self, paths):
        envelope = parse_draft(DRAFT)
        body = DRAFT.encode("utf-8")
        with Root(self.root_name) as root:
            engine_fd = ensure_engine_dir(root.dirfd)
            try:
                ledger = open_ledger(engine_fd)
                try:
                    for number, path in enumerate(paths):
                        admit(
                            ledger, root, envelope, body,
                            "candidate-%d" % number,
                            claimed_body_sha256=body_sha256(body),
                            claimed_content_hash=content_hash(
                                envelope, body, None),
                            origin="hand", stamp="20260808-120000",
                            rendered_path=path)
                finally:
                    ledger.close()
            finally:
                os.close(engine_fd)

    @staticmethod
    def exact_budget_candidates():
        paths = [
            "a%03d/PLAN-planner-20260808-%06d.md" % (number, number)
            for number in range(105)
        ]
        suffix = "/PLAN-planner-20260808-599999.md"
        used = len(", ".join(paths).encode("utf-8"))
        final_length = 4096 - used - 2
        paths.append("z" * (final_length - len(suffix)) + suffix)
        if len(", ".join(paths).encode("utf-8")) != 4096:
            raise AssertionError("candidate fixture must exactly fill budget")
        return paths

    def assert_edge_fault(self, admits_against, expected_targets):
        with self.assertRaises(client.RemoteError) as caught:
            submit(self.root_name, self.draft_rel, self.key_rel,
                   admits_against=admits_against)
        self.assertEqual(caught.exception.code, "E-ENVELOPE")
        self.assertEqual(caught.exception.cls, "integrity")
        self.assertEqual(caught.exception.cause, self.EDGE_CAUSE)
        self.assertEqual(caught.exception.remedy,
                         self.EDGE_PREFIX + expected_targets)

    def test_full_submit_renders_body_and_index(self):
        result = submit(self.root_name, self.draft_rel, self.key_rel)
        self.assertFalse(result["duplicate"])
        self.assertEqual(Path(self.root_name, result["path"]).read_bytes(),
                         DRAFT.encode("utf-8"))
        self.assertIn(result["path"],
                      Path(self.root_name, "INDEX.md").read_text())
        self.assertFalse(Path(self.root_name,
                              self.draft_rel + ".sid").exists())

    def test_timeout_and_policy_refusal_retain_same_sidecar(self):
        wrong_key = ".engine/drafts/v29-a.planner/wrong.key"
        Path(self.root_name, wrong_key).write_text("wrong-tag\n")
        with self.assertRaises(client.RemoteError) as refused:
            submit(self.root_name, self.draft_rel, wrong_key)
        self.assertEqual(refused.exception.code, "E-KEY-MISMATCH")
        sidecar = Path(self.root_name, self.draft_rel + ".sid")
        first = sidecar.read_bytes()

        real_roundtrip = client._roundtrip

        def timeout(*args, **kwargs):
            raise TimeoutError

        client._roundtrip = timeout
        try:
            with self.assertRaises(TimeoutError):
                submit(self.root_name, self.draft_rel, self.key_rel)
            self.assertEqual(sidecar.read_bytes(), first)
            with self.assertRaises(TimeoutError):
                submit(self.root_name, self.draft_rel, self.key_rel)
            self.assertEqual(sidecar.read_bytes(), first)
        finally:
            client._roundtrip = real_roundtrip

    def test_commit_before_response_retry_uses_original_id_and_path(self):
        real_roundtrip = client._roundtrip
        committed = []

        def lose_response(*args, **kwargs):
            response = real_roundtrip(*args, **kwargs)
            committed.append((args[1], response))
            raise TimeoutError

        client._roundtrip = lose_response
        try:
            with self.assertRaises(TimeoutError):
                submit(self.root_name, self.draft_rel, self.key_rel)
        finally:
            client._roundtrip = real_roundtrip
        sidecar = Path(self.root_name, self.draft_rel + ".sid")
        original_id = sidecar.read_text()
        self.assertEqual(original_id.strip(),
                         committed[0][0]["args"]["submission_id"])
        replay = submit(self.root_name, self.draft_rel, self.key_rel)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["path"], committed[0][1]["result"]["path"])
        self.assertFalse(sidecar.exists())
        self.assertTrue(original_id.endswith("\n"))

    def test_changed_edge_and_malformed_edge_refuse(self):
        first = submit(self.root_name, self.draft_rel, self.key_rel)
        sidecar = Path(self.root_name, self.draft_rel + ".sid")
        sidecar.write_text("123e4567-e89b-12d3-a456-426614174000\n")
        second = submit(self.root_name, self.draft_rel, self.key_rel,
                        admits_against=first["path"])
        sidecar.write_text("123e4567-e89b-12d3-a456-426614174000\n")
        with self.assertRaises(client.RemoteError) as changed:
            submit(self.root_name, self.draft_rel, self.key_rel,
                   admits_against=None)
        self.assertEqual(changed.exception.code, "E-REPLAY-MISMATCH")
        self.assertTrue(sidecar.exists())
        sidecar.write_text("223e4567-e89b-12d3-a456-426614174000\n")
        with self.assertRaises(client.RemoteError) as malformed:
            submit(self.root_name, self.draft_rel, self.key_rel,
                   admits_against="../escape.md")
        self.assertEqual(malformed.exception.code, "E-ENVELOPE")
        self.assertEqual(second["render_state"], "rendered")

    def test_envelope_edge_remedy_empty_root(self):
        self.assert_edge_fault(
            "missing/relay.md",
            "none — no existing relays under the root to admit against")

    def test_envelope_edge_remedy_nonempty(self):
        first = submit(self.root_name, self.draft_rel, self.key_rel)
        self.assert_edge_fault("missing/relay.md", first["path"])

    def test_envelope_edge_remedy_malformed_syntax(self):
        first = submit(self.root_name, self.draft_rel, self.key_rel)
        self.assert_edge_fault("../escape.md", first["path"])

    def test_envelope_edge_remedy_non_edge_generic(self):
        body = DRAFT.encode("utf-8")
        envelope = parse_draft(DRAFT)
        args = {
            "envelope": envelope.headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "body_sha256": "0" * 64,
            "content_hash": content_hash(envelope, body, None),
            "submission_id": "non-edge-envelope",
            "tag": "tag-value",
        }
        with self.assertRaises(client.RemoteError) as caught:
            client.request(self.root_name, "submit", args)
        self.assertEqual(caught.exception.code, "E-ENVELOPE")
        self.assertEqual(
            caught.exception.cause,
            "submitted envelope does not match server-derived bytes")
        self.assertEqual(
            caught.exception.remedy,
            "rebuild the envelope from the unchanged draft")
        self.assertNotIn("admits_against", caught.exception.remedy)

    def test_envelope_edge_remedy_max_fitting(self):
        paths = self.exact_budget_candidates()
        self.seed_edge_candidates(paths)
        self.assert_edge_fault("missing/relay.md", ", ".join(paths))

    def test_envelope_edge_remedy_first_over_budget(self):
        paths = self.exact_budget_candidates()
        suffix = "/PLAN-planner-20260808-600000.md"
        extra = "z" * 100 + suffix
        self.assertGreater(extra.encode("utf-8"), paths[-1].encode("utf-8"))
        self.seed_edge_candidates(paths + [extra])
        self.assert_edge_fault(
            "missing/relay.md",
            ", ".join(paths) + " … plus 1 more of 107 total")

    def test_nearest_root_and_relay_key_environment(self):
        nested = Path(self.root_name, "nested/deeper")
        nested.mkdir(parents=True)
        self.assertEqual(discover_root(nested), os.path.realpath(self.root_name))
        old = os.environ.get("RELAY_KEY")
        os.environ["RELAY_KEY"] = os.path.join(self.root_name, self.key_rel)
        try:
            result = submit(self.root_name, self.draft_rel, None)
        finally:
            if old is None:
                os.environ.pop("RELAY_KEY", None)
            else:
                os.environ["RELAY_KEY"] = old
        self.assertIn("path", result)

    def test_cli_submit_preserves_wire_result_field_names(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = cli.main([
                "submit", self.draft_rel, "--root", self.root_name,
                "--key", self.key_rel,
            ])
        self.assertEqual(status, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(set(result),
                         {"path", "advisories", "render_state", "duplicate"})
        self.assertFalse(result["duplicate"])

    def test_cli_status_and_stop_route_over_daemon(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = cli.main(["status", "--root", self.root_name])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue())["epoch"], "active")
        self.assertEqual(cli.main(["daemon", "stop", "--root",
                                   self.root_name]), 0)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_daemon_down_restart_replay_composite(self):
        self.assertEqual(client.request(
            self.root_name, "daemon.stop", {}), {"ok": True})
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

        with self.assertRaises(client.RemoteError) as down:
            submit(self.root_name, self.draft_rel, self.key_rel)
        self.assertEqual(down.exception.code, "E-DAEMON-DOWN")
        sidecar = Path(self.root_name, self.draft_rel + ".sid")
        original_id = sidecar.read_text()

        def restart():
            self.read_fd, write_fd = os.pipe()
            self.thread = threading.Thread(
                target=daemon.start, args=(self.root_name,),
                kwargs={"ready_fd": write_fd,
                        "socket_override": self.socket_name})
            self.thread.start()
            self.assertEqual(os.read(self.read_fd, 1), b"R")
            os.close(self.read_fd)
            self.read_fd = None

        restart()
        real_roundtrip = client._roundtrip
        committed = []

        def lose_response(*args, **kwargs):
            response = real_roundtrip(*args, **kwargs)
            committed.append(response)
            raise TimeoutError

        client._roundtrip = lose_response
        try:
            with self.assertRaises(TimeoutError):
                submit(self.root_name, self.draft_rel, self.key_rel)
        finally:
            client._roundtrip = real_roundtrip
        self.assertEqual(sidecar.read_text(), original_id)
        self.assertFalse(committed[0]["result"]["duplicate"])

        self.assertEqual(client.request(
            self.root_name, "daemon.stop", {}), {"ok": True})
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        restart()
        replay = submit(self.root_name, self.draft_rel, self.key_rel)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["path"], committed[0]["result"]["path"])
        self.assertFalse(sidecar.exists())


class TestDaemonDown(unittest.TestCase):
    def test_down_refusal_has_exact_escalation_and_zero_effects(self):
        with tempfile.TemporaryDirectory() as root_name:
            Path(root_name, ".engine/drafts/a").mkdir(parents=True)
            draft = ".engine/drafts/a/one.md"
            key = ".engine/drafts/a/key"
            Path(root_name, draft).write_text(DRAFT)
            Path(root_name, key).write_text("tag\n")
            Path(root_name, ".engine/ledger.db").write_bytes(b"stable-store")
            Path(root_name, "INDEX.md").write_bytes(b"stable-index")
            Path(root_name, "SEATS.md").write_bytes(b"stable-seats")
            before = sorted((path.relative_to(root_name), path.read_bytes())
                            for path in Path(root_name).rglob("*")
                            if path.is_file())
            with self.assertRaises(client.RemoteError) as down:
                submit(root_name, draft, key)
            self.assertEqual(down.exception.code, "E-DAEMON-DOWN")
            self.assertEqual(down.exception.remedy,
                             errors.E_DAEMON_DOWN_ESCALATION)
            self.assertIn("master-planner if a master tier exists",
                          down.exception.remedy)
            self.assertIn("else orchestrator-planner",
                          down.exception.remedy)
            self.assertIn("else the operator", down.exception.remedy)
            self.assertIn("never the orchestrator session",
                          down.exception.remedy)
            after_without_sid = sorted(
                (path.relative_to(root_name), path.read_bytes())
                for path in Path(root_name).rglob("*")
                if path.is_file() and not path.name.endswith(".sid"))
            self.assertEqual(after_without_sid, before)

    def test_cli_daemon_down_emits_registered_escalation(self):
        with tempfile.TemporaryDirectory() as root_name:
            Path(root_name, ".engine").mkdir()
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                status = cli.main(["status", "--root", root_name])
            self.assertEqual(status, 1)
            self.assertIn(errors.E_DAEMON_DOWN_ESCALATION,
                          stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
