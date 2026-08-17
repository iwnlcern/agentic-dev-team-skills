import contextlib
import datetime
import hashlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from relay_engine import cli, client, rules


INDEX_TEXT = (
    "# INDEX — v291\n\n"
    "| time | phase | role | dispatch | to | owner | status | file |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| 20260816-120001 | SITREP | Pair Planner | v291-boot | "
    "v291.orchestrator-planner | v291-engine | filed | "
    "v291-boot/SITREP-pair-planner-20260816-120001.md |\n"
)


class TestIndexCeiling(unittest.TestCase):
    def _lint(self, text=INDEX_TEXT, **kw):
        with TemporaryDirectory() as td:
            path = Path(td) / "INDEX.md"
            path.write_text(text)
            frozen = datetime.datetime(2026, 8, 16, 12, 0, 0)
            with patch.object(rules, "clock_now", return_value=frozen):
                return rules.lint_relay_index(path, **kw)

    def test_verified_projection_tolerates_daemon_slot_lead(self):
        result = self._lint(daemon_projection=True)
        self.assertFalse([e for e in result.errors if "ahead of the" in e])

    def test_unverified_index_keeps_the_ceiling(self):
        result = self._lint()
        self.assertTrue([e for e in result.errors if "ahead of the" in e])

    def test_decreasing_order_survives_verification(self):
        decreasing = INDEX_TEXT.replace(
            "| 20260816-120001 | SITREP",
            "| 20260816-120002 | SITREP",
        ).replace(
            "SITREP-pair-planner-20260816-120001.md",
            "SITREP-pair-planner-20260816-120002.md",
        ) + (
            "| 20260816-120001 | SITREP | Pair Planner | v291-next | "
            "v291.orchestrator-planner | v291-engine | filed | "
            "v291-next/SITREP-pair-planner-20260816-120001.md |\n"
        )
        result = self._lint(decreasing, daemon_projection=True)
        self.assertTrue([e for e in result.errors if "precedes the previous row" in e])

    def test_filename_disagreement_survives_verification(self):
        disagreement = INDEX_TEXT.replace(
            "SITREP-pair-planner-20260816-120001.md",
            "SITREP-pair-planner-20260816-120002.md",
        )
        result = self._lint(disagreement, daemon_projection=True)
        self.assertTrue([e for e in result.errors if "disagrees with its filename" in e])


class TestIndexCeilingCli(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".engine").mkdir()
        self.index = self.root / "INDEX.md"
        self.index.write_text(INDEX_TEXT)
        self.frozen = datetime.datetime(2026, 8, 16, 12, 0, 0)

    def tearDown(self):
        self.temp.cleanup()

    def _status(self, *, digest=None, event="rendered", epoch="active"):
        if digest is None:
            digest = hashlib.sha256(self.index.read_bytes()).hexdigest()
        return {
            "daemon": {"state": "ready"},
            "epoch": epoch,
            "pending_renders": 0,
            "conflicts": 0,
            "projection_events": [{
                "target": "index",
                "event": event,
                "path": "INDEX.md",
                "digest": digest,
            }],
        }

    def _run(self, arguments, status_provider=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        request_patch = patch.object(
            cli.client,
            "request",
            side_effect=status_provider,
        ) if callable(status_provider) else patch.object(
            cli.client,
            "request",
            return_value=status_provider,
        )
        with patch.object(rules, "clock_now", return_value=self.frozen), \
                request_patch, contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = cli.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_matching_latest_render_event_exempts_root_index(self):
        for event in ("rendered", "repaired"):
            with self.subTest(event=event):
                code, stdout, _stderr = self._run(
                    ["lint", "--relay-root", str(self.root)],
                    self._status(event=event),
                )
                self.assertEqual(code, 0)
                self.assertNotIn("ahead of the", stdout)

    def test_digest_mismatch_reapplies_ceiling(self):
        status = self._status()
        self.index.write_text(INDEX_TEXT + "\n")
        code, stdout, _stderr = self._run(
            ["lint", "--relay-root", str(self.root)],
            status,
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_mutation_between_selection_and_lint_reapplies_ceiling(self):
        status = self._status()
        lint_relay_root = rules.lint_relay_root

        def mutate_then_lint(*args, **kwargs):
            self.index.write_text(INDEX_TEXT + "\n")
            return lint_relay_root(*args, **kwargs)

        with patch.object(
                cli.rules, "lint_relay_root", side_effect=mutate_then_lint):
            code, stdout, _stderr = self._run(
                ["lint", "--relay-root", str(self.root)], status,
            )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_direct_index_never_uses_daemon_evidence(self):
        code, stdout, _stderr = self._run(
            ["lint", "--index", str(self.index)],
            AssertionError("direct --index requested daemon status"),
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_absent_status_reapplies_ceiling(self):
        code, stdout, _stderr = self._run(
            ["lint", "--relay-root", str(self.root)],
            {"epoch": "active"},
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_daemon_down_reapplies_ceiling(self):
        unavailable = client.RemoteError({
            "code": "E-DAEMON-DOWN",
            "cause": "daemon unavailable",
            "remedy": "start daemon",
            "cls": "environment",
        })
        code, stdout, stderr = self._run(
            ["lint", "--relay-root", str(self.root)], unavailable,
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)
        self.assertEqual(stderr, "")

    def test_latest_non_render_event_reapplies_ceiling(self):
        status = self._status()
        status["projection_events"].append({
            "target": "index",
            "event": "divergence",
            "path": "INDEX.md",
            "digest": hashlib.sha256(self.index.read_bytes()).hexdigest(),
        })
        code, stdout, _stderr = self._run(
            ["lint", "--relay-root", str(self.root)], status,
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_two_stale_render_events_reapply_ceiling(self):
        status = self._status(digest="1" * 64)
        status["projection_events"].append({
            "target": "index",
            "event": "repaired",
            "path": "INDEX.md",
            "digest": "2" * 64,
        })
        code, stdout, _stderr = self._run(
            ["lint", "--relay-root", str(self.root)], status,
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)

    def test_prior_epoch_event_reapplies_ceiling(self):
        code, stdout, _stderr = self._run(
            ["lint", "--relay-root", str(self.root)],
            self._status(epoch="inert"),
        )
        self.assertEqual(code, 1)
        self.assertIn("ahead of the", stdout)


if __name__ == "__main__":
    unittest.main()
