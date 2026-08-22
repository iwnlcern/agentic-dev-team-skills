import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from relay_engine import cli, client, daemon, errors, rules
from relay_engine.ledger import open_ledger
from relay_engine.tests.test_identity_matrix import RunningDaemon
from relay_engine.tests.test_lint_record_readonly import (
    _crash_with_committed_wal,
    _stopped_root,
    _tree_digest,
)
from relay_engine.tests.test_rules_index import INDEX_TEXT


TOOLS = Path(__file__).parents[2]
REPO = TOOLS.parent
FIXTURES = TOOLS / "relay-engine-fixtures" / "record-root"
SCRIPT = TOOLS / "relay-lint.py"
SUMMARY_PREFIX = "engine-root sweep:"


def _digest(body):
    return hashlib.sha256(body).hexdigest()


def _copy_fixture(temporary, member):
    root = Path(temporary, "relay-root")
    shutil.copytree(FIXTURES / member, root)
    return root


def _context(root, paths=None, *, origin="daemon"):
    if paths is None:
        paths = sorted(path for path in root.rglob("*") if path.is_file())
    entries = []
    for path in paths:
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "body_sha256": _digest(path.read_bytes()),
            "origin": origin,
        })
    entries.sort(key=lambda entry: entry["path"].encode("utf-8"))
    return {"snapshot": str(len(entries)), "entries": entries}


def _record_lint(test, root, context, mode="daemon", **kwargs):
    try:
        return rules.lint_relay_root(
            root, engine_root=True, record_context=context,
            context_mode=mode, **kwargs)
    except TypeError as exc:
        test.fail("record-scoped lint_relay_root interface is absent: %s" % exc)


def _run_cli(arguments):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = cli.main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


def _payload(stdout, root):
    value = json.loads(stdout)
    return value[os.fspath(root)]


def _strict_result(root):
    result = rules.lint_relay_root(
        root, engine_root=True, projection_digests={})
    return {"errors": result.errors, "warnings": result.warnings}


def _strict_stdout(root, result):
    return json.dumps(
        {os.fspath(root): result}, sort_keys=True,
        separators=(",", ":")) + "\n"


def _insert_context_rows(root, paths):
    engine_fd = os.open(root / ".engine", os.O_RDONLY | os.O_DIRECTORY)
    try:
        ledger = open_ledger(engine_fd)
        try:
            rows = []
            for seq, path in enumerate(paths, start=1):
                body = path.read_bytes()
                rows.append((
                    seq, "task4-%d" % seq, "20260821-%06d" % seq,
                    path.relative_to(root).as_posix(), "AUDIT", "Planner",
                    "task4-%d" % seq, "task4.planner", "{}", body,
                    _digest(body), "f" * 64, "daemon", "[]",
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


def _down_error():
    return client.RemoteError(errors.error_for("E-DAEMON-DOWN").as_dict())


class TestRecordSuppression(unittest.TestCase):
    def test_live_and_stopped_sources_suppress_the_same_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            relay = next(root.rglob("*.md"))
            identity = client._local_identity()
            running = RunningDaemon(
                os.fspath(root), identity,
                socket_name=os.fspath(root / ".engine/task4.sock"),
            ).start()
            try:
                _insert_context_rows(root, [relay])
                (root / "INDEX.md").write_text(INDEX_TEXT, encoding="utf-8")
                with mock.patch.object(
                        cli.daemon, "read_lint_context",
                        wraps=daemon.read_lint_context) as readonly:
                    live_status, live_stdout, live_stderr = _run_cli(
                        ["lint", "--relay-root", os.fspath(root)])
                self.assertEqual(readonly.call_count, 0)
            finally:
                running.stop()

            before = _tree_digest(root)
            with mock.patch.object(
                    cli.daemon, "read_lint_context",
                    wraps=daemon.read_lint_context) as readonly:
                stopped_status, stopped_stdout, stopped_stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
            self.assertEqual(readonly.call_count, 1)
            self.assertEqual(_tree_digest(root), before)

            self.assertEqual((live_status, live_stderr), (0, ""))
            self.assertEqual((stopped_status, stopped_stderr), (0, ""))
            live = _payload(live_stdout, root)
            stopped = _payload(stopped_stdout, root)
            self.assertEqual(live["errors"], [])
            self.assertEqual(stopped["errors"], [])
            self.assertEqual(live["warnings"], [
                "engine-root sweep: 1 record-known relays not re-judged; "
                "context source: daemon",
            ])
            self.assertEqual(stopped["warnings"], [
                "engine-root sweep: 1 record-known relays not re-judged; "
                "context source: read-only record",
            ])

    def test_matching_identity_is_opened_once_no_follow_and_descriptor_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            relay_name = context["entries"][0]["path"]
            relay_basename = Path(relay_name).name
            real_open = os.open
            held = []

            def tracking_open(name, flags, *args, **kwargs):
                descriptor = real_open(name, flags, *args, **kwargs)
                if name == relay_basename and not flags & os.O_DIRECTORY:
                    held.append((descriptor, flags))
                return descriptor

            if not hasattr(rules, "os"):
                self.fail("record-scoped descriptor seam is absent")
            with mock.patch.object(rules.os, "open",
                                   side_effect=tracking_open):
                result = _record_lint(self, root, context)

            self.assertEqual(result.errors, [])
            self.assertEqual(len(held), 1)
            self.assertTrue(held[0][1] & os.O_NOFOLLOW)
            self.assertTrue(stat.S_ISREG(os.stat(
                root / relay_name, follow_symlinks=False).st_mode))
            with self.assertRaises(OSError):
                os.fstat(held[0][0])

    def test_digest_mismatch_is_integrity_not_path_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            relay = root / context["entries"][0]["path"]
            before = context["entries"][0]["body_sha256"]
            relay.write_bytes(relay.read_bytes() + b"modified\n")
            self.assertNotEqual(_digest(relay.read_bytes()), before)

            result = _record_lint(self, root, context)

            self.assertIn(
                "record-integrity: digest-mismatch: %s" %
                context["entries"][0]["path"], result.errors)
            self.assertFalse(any("outside-the-record" in error
                                 for error in result.errors))
            self.assertIn(
                "engine-root sweep: 0 record-known relays not re-judged; "
                "context source: daemon", result.warnings)


class TestRecordOriginFixtures(unittest.TestCase):
    def test_hand_and_adopted_rows_suppress_their_dedicated_defect_fixtures(self):
        for member, origin in (("origin-hand", "hand"),
                               ("origin-adopted", "adopted")):
            with self.subTest(member=member, origin=origin), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, member)
                relay = next(root.rglob("*.md"))
                self.assertTrue(rules.lint_file(relay).errors)

                result = _record_lint(
                    self, root, _context(root, origin=origin))

                self.assertEqual(result.errors, [])
                self.assertEqual(result.warnings, [
                    "engine-root sweep: 1 record-known relays not re-judged; "
                    "context source: daemon",
                ])

    def test_suppression_keeps_integrity_foreign_and_chain_gates_active(self):
        cases = (
            ("origin-integrity", "hand", None,
             "record-integrity: digest-mismatch: lane/AUDIT-pair-planner-20260821-120000.md"),
            ("origin-foreign", "adopted", "lane/AUDIT-pair-planner-20260821-120000.md",
             "outside-the-record: foreign entry: foreign.bin"),
            ("origin-chain", "hand", None,
             "DISPATCH IMPL parent must be an earlier PLAN-REVIEW relay with verdict approve"),
        )
        for member, origin, known_path, finding in cases:
            with self.subTest(member=member, origin=origin), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, member)
                paths = None if known_path is None else [root / known_path]
                context = _context(root, paths, origin=origin)
                if member == "origin-integrity":
                    context["entries"][0]["body_sha256"] = "0" * 64

                result = _record_lint(self, root, context)

                self.assertTrue(any(finding in error for error in result.errors))


class TestRecordIntegrityMatrix(unittest.TestCase):
    def _case(self, label, mutate, expected):
        with self.subTest(label=label), \
                tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            relay = root / context["entries"][0]["path"]
            mutate(root, relay)
            result = _record_lint(self, root, context)
            finding = "record-integrity: %s: %s" % (
                expected, context["entries"][0]["path"])
            self.assertIn(finding, result.errors)
            self.assertFalse(any("outside-the-record" in error
                                 for error in result.errors))
            self.assertIn(
                "engine-root sweep: 0 record-known relays not re-judged; "
                "context source: daemon", result.warnings)

    def test_missing_nonregular_and_unreadable_are_distinct(self):
        def missing(_root, relay):
            relay.unlink()

        def directory(_root, relay):
            relay.unlink()
            relay.mkdir()

        def fifo(_root, relay):
            relay.unlink()
            os.mkfifo(relay)

        def unreadable(_root, relay):
            relay.chmod(0)

        for label, mutate, expected in (
                ("missing", missing, "missing"),
                ("directory", directory, "non-regular"),
                ("fifo", fifo, "non-regular"),
                ("unreadable", unreadable, "unreadable")):
            self._case(label, mutate, expected)

    def test_valid_and_dangling_symlinks_are_unfollowed_integrity(self):
        target_marker = "TARGET-BYTES-MUST-NOT-BE-LINTED"

        def valid(root, relay):
            engine = root / ".engine"
            engine.mkdir()
            target = engine / "target"
            target.write_text(target_marker, encoding="utf-8")
            relay.unlink()
            relay.symlink_to("../../.engine/target")

        def dangling(_root, relay):
            relay.unlink()
            relay.symlink_to("missing-target")

        for label, mutate in (("valid target", valid),
                              ("dangling target", dangling)):
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                context = _context(root)
                relay = root / context["entries"][0]["path"]
                mutate(root, relay)
                with mock.patch.object(rules, "read",
                                       wraps=rules.read) as read_file:
                    result = _record_lint(self, root, context)
                self.assertEqual(result.errors, [
                    "record-integrity: symlinked: %s" %
                    context["entries"][0]["path"],
                ])
                self.assertFalse(any(call.args and call.args[0] == relay
                                     for call in read_file.call_args_list))
                self.assertNotIn(target_marker, "\n".join(result.errors))

    def test_known_metadata_denial_is_unreadable_without_candidate_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            relative = context["entries"][0]["path"]
            basename = Path(relative).name
            real_stat = os.stat
            real_open = os.open
            denied = False
            candidate_opens = []

            def denying_stat(name, *args, **kwargs):
                nonlocal denied
                if (not denied and name == basename and
                        kwargs.get("follow_symlinks") is False):
                    denied = True
                    raise PermissionError("deterministic metadata denial")
                return real_stat(name, *args, **kwargs)

            def tracking_open(name, flags, *args, **kwargs):
                if name in {relative, basename} and not flags & os.O_DIRECTORY:
                    candidate_opens.append(name)
                return real_open(name, flags, *args, **kwargs)

            with mock.patch.object(rules.os, "stat",
                                   side_effect=denying_stat), \
                    mock.patch.object(rules.os, "open",
                                      side_effect=tracking_open):
                result = _record_lint(self, root, context)

            self.assertTrue(denied)
            self.assertEqual(result.errors, [
                "record-integrity: unreadable: %s" % relative,
            ])
            self.assertEqual(candidate_opens, [])
            self.assertIn(
                "engine-root sweep: 0 record-known relays not re-judged; "
                "context source: daemon", result.warnings)


class TestRecordTopology(unittest.TestCase):
    def test_record_ancestor_directory_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            result = _record_lint(self, root, _context(root))
            self.assertEqual(result.errors, [])

    def test_all_foreign_entry_types_are_inventory_findings(self):
        def markdown(root):
            (root / "notes.md").write_text("foreign\n", encoding="utf-8")

        def non_markdown(root):
            (root / "payload.bin").write_bytes(b"foreign")

        def symlink(root):
            (root / ".engine").mkdir()
            (root / ".engine/target").write_bytes(b"foreign")
            (root / "foreign-link").symlink_to(".engine/target")

        def fifo(root):
            os.mkfifo(root / "foreign-fifo")

        for label, create, name in (
                ("noncanonical markdown", markdown, "notes.md"),
                ("non-markdown", non_markdown, "payload.bin"),
                ("symlink", symlink, "foreign-link"),
                ("non-regular", fifo, "foreign-fifo")):
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                context = _context(root)
                create(root)
                result = _record_lint(self, root, context)
                self.assertIn(
                    "outside-the-record: foreign entry: %s" % name,
                    result.errors)

    def test_unexpected_empty_and_nested_directories_gate(self):
        for label, relative in (("empty", "unexpected"),
                                ("nested", "lane/unexpected")):
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                context = _context(root)
                (root / relative).mkdir()
                result = _record_lint(self, root, context)
                self.assertIn(
                    "outside-the-record: unexpected directory: %s" %
                    relative, result.errors)

    def test_symlinked_component_and_non_directory_ancestor_are_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            engine = root / ".engine"
            engine.mkdir()
            shutil.move(os.fspath(root / "lane"),
                        os.fspath(engine / "lane-target"))
            (root / "lane").symlink_to(".engine/lane-target")
            result = _record_lint(self, root, context)
            self.assertEqual(result.errors, [
                "outside-the-record: symlinked component: lane",
            ])

        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            shutil.rmtree(root / "lane")
            (root / "lane").write_bytes(b"not a directory")
            result = _record_lint(self, root, context)
            self.assertEqual(result.errors, [
                "outside-the-record: non-directory ancestor: lane",
            ])

    def test_root_escape_symlink_is_unfollowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            outside = Path(temporary, "outside-secret")
            outside.write_text("must never be read", encoding="utf-8")
            (root / "escape").symlink_to(outside)
            with mock.patch.object(rules, "read",
                                   wraps=rules.read) as read_file:
                result = _record_lint(self, root, context)
            self.assertIn("outside-the-record: root escape: escape",
                          result.errors)
            self.assertFalse(any(call.args and call.args[0] == outside
                                 for call in read_file.call_args_list))

    def test_intermediate_directory_swap_never_reads_outside_bytes(self):
        marker = b"OUTSIDE-BYTES-MUST-NEVER-BE-READ\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            relative = next(root.rglob("*.md")).relative_to(root).as_posix()
            context = {"snapshot": "1", "entries": [{
                "path": relative,
                "body_sha256": _digest(marker),
                "origin": "daemon",
            }]}
            outside = Path(temporary, "outside-lane")
            outside.mkdir()
            (outside / Path(relative).name).write_bytes(marker)
            held = root / "held-lane"
            real_open = os.open
            real_read = os.read
            lane_opens = 0
            swapped = False
            read_chunks = []

            def swapping_open(name, flags, *args, **kwargs):
                nonlocal lane_opens, swapped
                if name == "lane" and flags & os.O_DIRECTORY:
                    lane_opens += 1
                    if lane_opens == 2:
                        (root / "lane").rename(held)
                        (root / "lane").symlink_to(outside)
                        swapped = True
                elif name == relative and not swapped:
                    (root / "lane").rename(held)
                    (root / "lane").symlink_to(outside)
                    swapped = True
                return real_open(name, flags, *args, **kwargs)

            def tracking_read(descriptor, count):
                chunk = real_read(descriptor, count)
                read_chunks.append(chunk)
                return chunk

            with mock.patch.object(rules.os, "open",
                                   side_effect=swapping_open), \
                    mock.patch.object(rules.os, "read",
                                      side_effect=tracking_read):
                result = _record_lint(self, root, context)

            self.assertTrue(swapped)
            self.assertNotIn(marker, b"".join(read_chunks))
            self.assertEqual(result.errors, [
                "outside-the-record: symlinked component: lane",
            ])
            self.assertIn(
                "engine-root sweep: 0 record-known relays not re-judged; "
                "context source: daemon", result.warnings)

    def test_hostile_filesystem_names_have_bounded_stable_safe_displays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "relay-root")
            root.mkdir()
            control_relative = "line\nbreak"
            (root / control_relative).write_bytes(b"foreign")
            components = ("a" * 180, "b" * 180, "c" * 180)
            deep_relative = "/".join(components)
            (root / deep_relative).mkdir(parents=True)
            context = {"snapshot": "0", "entries": []}

            first = _record_lint(self, root, context)
            second = _record_lint(self, root, context)

            control_display = "entry-%s" % hashlib.sha256(
                control_relative.encode("utf-8")).hexdigest()[:12]
            deep_display = "entry-%s" % hashlib.sha256(
                deep_relative.encode("utf-8")).hexdigest()[:12]
            self.assertEqual(
                rules._safe_engine_finding_path(control_relative),
                control_display)
            self.assertEqual(
                rules._safe_engine_finding_path(deep_relative), deep_display)
            self.assertEqual(first.errors, second.errors)
            self.assertIn(
                "outside-the-record: foreign entry: %s" % control_display,
                first.errors)
            self.assertIn(
                "outside-the-record: unexpected directory: %s" %
                deep_display, first.errors)
            self.assertTrue(first.errors)
            self.assertTrue(all("\n" not in error and "\r" not in error
                                for error in first.errors))
            self.assertNotIn(control_relative, "\n".join(first.errors))

    def test_dotdot_context_seam_is_a_root_escape_boundary_injection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "relay-root")
            root.mkdir()
            outside = Path(temporary, "outside.md")
            outside.write_bytes(b"outside")
            context = {"snapshot": "1", "entries": [{
                "path": "../outside.md",
                "body_sha256": _digest(outside.read_bytes()),
                "origin": "hand",
            }]}
            result = _record_lint(self, root, context)
            self.assertEqual(result.errors, [
                "outside-the-record: root escape: outside.md",
            ])

    def test_closed_exclusions_are_not_foreign(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            context = _context(root)
            (root / ".engine/deep").mkdir(parents=True)
            (root / ".engine/deep/state.bin").write_bytes(b"state")
            (root / "INDEX.md").write_text("# INDEX\n", encoding="utf-8")
            (root / "SEATS.md").write_text("# SEATS\n", encoding="utf-8")
            result = _record_lint(self, root, context)
            self.assertFalse(any("outside-the-record" in error
                                 for error in result.errors))


class TestRecordFallback(unittest.TestCase):
    def _assert_strict_cli(self, root, acquire, *, direct=None,
                           extra_patch=None):
        expected = _strict_result(root)
        before = _tree_digest(root)
        direct_value = None if direct is None else direct
        patches = [
            mock.patch.object(cli.client, "lint_context",
                              side_effect=acquire),
            mock.patch.object(cli.daemon, "read_lint_context",
                              return_value=direct_value),
            mock.patch.object(cli, "_index_projection_digests",
                              return_value={}),
        ]
        if extra_patch is not None:
            patches.append(extra_patch)
        with contextlib.ExitStack() as stack:
            entered = [stack.enter_context(patch) for patch in patches]
            status, stdout, stderr = _run_cli(
                ["lint", "--relay-root", os.fspath(root)])
        self.assertEqual(_tree_digest(root), before)
        self.assertEqual(stderr, "")
        self.assertEqual(status, 1 if expected["errors"] else 0)
        self.assertEqual(stdout, _strict_stdout(root, expected))
        self.assertEqual(_payload(stdout, root), expected)
        self.assertNotIn(SUMMARY_PREFIX, stdout)
        self.assertEqual(entered[1].call_count, 1)

    def test_every_hostile_page_and_budget_failure_is_total_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            example_root = _copy_fixture(temporary, "suppressed")
            entry = _context(example_root)["entries"][0]
        other = dict(entry, path="lane/z.md")
        page_cases = (
            ("truncated page set", (
                {"snapshot": "2", "entries": [entry], "cursor": "2:1"},
                {"snapshot": "2", "entries": []},
            )),
            ("duplicate entry", (
                {"snapshot": "2", "entries": [entry], "cursor": "2:1"},
                {"snapshot": "2", "entries": [entry]},
            )),
            ("reordered entries", (
                {"snapshot": "2", "entries": [other, entry]},
            )),
            ("snapshot churn", (
                {"snapshot": "2", "entries": [entry], "cursor": "2:1"},
                {"snapshot": "3", "entries": [other]},
            )),
            ("cursor gap", (
                {"snapshot": "2", "entries": [entry], "cursor": "2:2"},
            )),
            ("malformed hash", (
                {"snapshot": "1", "entries": [
                    dict(entry, body_sha256="not-a-hash")
                ]},
            )),
            ("explicit null cursor", (
                {"snapshot": "1", "entries": [entry], "cursor": None},
            )),
        )
        for label, pages in page_cases:
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                expected = _strict_result(root)
                before = _tree_digest(root)
                responses = iter(pages)

                def request(_root, operation, _args, **_kwargs):
                    self.assertEqual(operation, "lint.context")
                    return next(responses)

                with mock.patch.object(cli.client, "request",
                                       side_effect=request), \
                        mock.patch.object(cli.daemon, "read_lint_context",
                                          return_value=None) as direct, \
                        mock.patch.object(cli, "_index_projection_digests",
                                          return_value={}):
                    status, stdout, stderr = _run_cli(
                        ["lint", "--relay-root", os.fspath(root)])
                self.assertEqual(_tree_digest(root), before)
                self.assertEqual(stderr, "")
                self.assertEqual(status, 1)
                self.assertEqual(stdout, _strict_stdout(root, expected))
                self.assertEqual(_payload(stdout, root), expected)
                self.assertNotIn(SUMMARY_PREFIX, stdout)
                self.assertEqual(direct.call_count, 1)

        budget = client.RemoteError(
            errors.error_for("E-CONTEXT-BUDGET").as_dict())
        failures = (
            ("overall deadline", TimeoutError("context acquisition timed out")),
            ("framed deadline", TimeoutError("request timed out")),
            ("delayed page", TimeoutError("request timed out")),
            ("first over-budget candidate", budget),
            ("individual over-budget entry", budget),
        )
        for label, failure in failures:
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                self._assert_strict_cli(root, failure)

    def test_unexpected_live_exception_reaches_top_level_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            with mock.patch.object(
                    cli.client, "lint_context",
                    side_effect=AssertionError("live programming fault")), \
                    mock.patch.object(
                        cli.daemon, "read_lint_context",
                        side_effect=AssertionError(
                            "unexpected live fault entered stopped fallback")) \
                    as stopped, \
                    mock.patch.object(
                        cli.rules, "lint_relay_root",
                        side_effect=AssertionError(
                            "unexpected live fault emitted a lint verdict")) \
                    as lint_root:
                status, stdout, stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "unexpected error\n")
            self.assertEqual(stopped.call_count, 0)
            self.assertEqual(lint_root.call_count, 0)

    def test_unexpected_stopped_exception_reaches_top_level_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            with mock.patch.object(cli.client, "lint_context",
                                   side_effect=_down_error()), \
                    mock.patch.object(
                        cli.daemon, "read_lint_context",
                        side_effect=AssertionError("stopped programming fault")), \
                    mock.patch.object(
                        cli.rules, "lint_relay_root",
                        side_effect=AssertionError(
                            "unexpected stopped fault emitted a lint verdict")) \
                    as lint_root:
                status, stdout, stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
            self.assertEqual(status, 1)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "unexpected error\n")
            self.assertEqual(lint_root.call_count, 0)

    def test_absent_record_selects_strict_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            expected = _strict_result(root)
            before = _tree_digest(root)
            with mock.patch.object(cli, "_index_projection_digests",
                                   return_value={}):
                status, stdout, stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
            self.assertEqual(_tree_digest(root), before)
            self.assertEqual((status, stderr), (1, ""))
            self.assertEqual(stdout, _strict_stdout(root, expected))
            self.assertEqual(_payload(stdout, root), expected)
            self.assertNotIn(SUMMARY_PREFIX, stdout)

    def test_every_stopped_record_failure_selects_strict_unchanged(self):
        def absent_lock(root):
            Path(root, ".engine/daemon.lock").unlink()

        def absent_record(root):
            Path(root, ".engine/ledger.db").unlink()

        def corrupt_record(root):
            Path(root, ".engine/ledger.db").write_bytes(b"not sqlite")

        def unsupported_schema(root):
            record = sqlite3.connect(Path(root, ".engine/ledger.db"))
            try:
                record.execute(
                    "UPDATE meta SET value='999' WHERE key='schema_version'")
                record.commit()
            finally:
                record.close()

        def lock_directory(root):
            path = Path(root, ".engine/daemon.lock")
            path.unlink()
            path.mkdir()

        def record_directory(root):
            path = Path(root, ".engine/ledger.db")
            path.unlink()
            path.mkdir()

        def lock_symlink(root):
            path = Path(root, ".engine/daemon.lock")
            path.unlink()
            path.symlink_to("daemon.json")

        def record_symlink(root):
            path = Path(root, ".engine/ledger.db")
            path.unlink()
            path.symlink_to("daemon.json")

        def engine_mode(root):
            Path(root, ".engine").chmod(0o755)

        def lock_mode(root):
            Path(root, ".engine/daemon.lock").chmod(0o644)

        def record_mode(root):
            Path(root, ".engine/ledger.db").chmod(0o666)

        def empty_wal(root):
            Path(root, ".engine/ledger.db-wal").write_bytes(b"")

        def directory_shm(root):
            Path(root, ".engine/ledger.db-shm").mkdir()

        def symlink_journal(root):
            Path(root, ".engine/ledger.db-journal").symlink_to("daemon.json")

        cases = {
            "absent lock": absent_lock,
            "absent record": absent_record,
            "corrupt record": corrupt_record,
            "unsupported schema": unsupported_schema,
            "lock directory": lock_directory,
            "record directory": record_directory,
            "lock symlink": lock_symlink,
            "record symlink": record_symlink,
            "engine wrong mode": engine_mode,
            "lock wrong mode": lock_mode,
            "record wrong mode": record_mode,
            "empty WAL": empty_wal,
            "directory SHM": directory_shm,
            "symlink journal": symlink_journal,
            "committed crash WAL": _crash_with_committed_wal,
        }
        for label, mutate in cases.items():
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root = _copy_fixture(temporary, "suppressed")
                _stopped_root(os.fspath(root))
                mutate(root)
                expected = _strict_result(root)
                before = _tree_digest(root)
                with mock.patch.object(cli, "_index_projection_digests",
                                       return_value={}):
                    status, stdout, stderr = _run_cli(
                        ["lint", "--relay-root", os.fspath(root)])
                self.assertEqual(_tree_digest(root), before)
                self.assertEqual(stderr, "")
                self.assertEqual(status, 1)
                self.assertEqual(stdout, _strict_stdout(root, expected))
                self.assertEqual(_payload(stdout, root), expected)
                self.assertNotIn(SUMMARY_PREFIX, stdout)

    def test_wrong_engine_types_owner_and_lock_contention_fallback(self):
        def assert_case(root, patch=None):
            expected = _strict_result(root)
            before = _tree_digest(root)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    cli, "_index_projection_digests", return_value={}))
                if patch is not None:
                    stack.enter_context(patch)
                status, stdout, stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
            self.assertEqual(_tree_digest(root), before)
            self.assertEqual((status, stderr), (1, ""))
            self.assertEqual(stdout, _strict_stdout(root, expected))
            self.assertEqual(_payload(stdout, root), expected)
            self.assertNotIn(SUMMARY_PREFIX, stdout)

        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            (root / ".engine").write_bytes(b"not a directory")
            assert_case(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            outside = root / "outside-engine"
            outside.mkdir(mode=0o700)
            (root / ".engine").symlink_to(outside.name)
            assert_case(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            _stopped_root(os.fspath(root))
            assert_case(root, mock.patch.object(
                daemon.os, "geteuid", return_value=os.geteuid() + 1))

        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            _stopped_root(os.fspath(root))
            lock_fd = os.open(root / ".engine/daemon.lock", os.O_RDONLY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                assert_case(root)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


class TestCriterion11CrashMatrix(unittest.TestCase):
    def _record_root(self, temporary, *, running=True):
        root = _copy_fixture(temporary, "suppressed")
        relay = next(root.rglob("*.md"))
        identity = client._local_identity()
        daemon_owner = RunningDaemon(
            os.fspath(root), identity,
            socket_name=os.fspath(root / ".engine/criterion11.sock"),
        ).start()
        _insert_context_rows(root, [relay])
        (root / "INDEX.md").write_text(INDEX_TEXT, encoding="utf-8")
        if not running:
            daemon_owner.stop()
        return root, daemon_owner

    def _assert_record_success(self, root, mode):
        projection_paths = (root / "INDEX.md", next((root / "lane").rglob(
            "*.md")))
        projection_bytes = {path: path.read_bytes()
                            for path in projection_paths}
        before = (_tree_digest(root)
                  if mode == "read-only record" else None)
        status, stdout, stderr = _run_cli(
            ["lint", "--relay-root", os.fspath(root)])
        expected = {
            "errors": [],
            "warnings": [
                "engine-root sweep: 1 record-known relays not re-judged; "
                "context source: %s" % mode,
            ],
        }
        if before is not None:
            self.assertEqual(_tree_digest(root), before)
        self.assertEqual(
            {path: path.read_bytes() for path in projection_paths},
            projection_bytes)
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(stdout, _strict_stdout(root, expected))
        self.assertEqual(_payload(stdout, root), expected)

    def _assert_strict_unchanged(self, root, *, patches=()):
        expected = _strict_result(root)
        before = _tree_digest(root)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                cli, "_index_projection_digests", return_value={}))
            for patch in patches:
                stack.enter_context(patch)
            status, stdout, stderr = _run_cli(
                ["lint", "--relay-root", os.fspath(root)])
        self.assertEqual(_tree_digest(root), before)
        self.assertEqual(stderr, "")
        self.assertEqual(status, 1 if expected["errors"] else 0)
        self.assertEqual(stdout, _strict_stdout(root, expected))
        self.assertEqual(_payload(stdout, root), expected)
        self.assertNotIn(SUMMARY_PREFIX, stdout)

    def test_live_daemon_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, daemon_owner = self._record_root(temporary)
            try:
                self._assert_record_success(root, "daemon")
            finally:
                daemon_owner.stop()

    def test_stopped_daemon_read_only_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            self._assert_record_success(root, "read-only record")

    def test_daemon_death_between_probe_and_request_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            state_path = root / ".engine/daemon.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["state"] = "ready"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (root / ".engine/ledger.db-wal").write_bytes(b"")
            reached_request = []

            def die_after_probe(socket_name, request, timeout):
                reached_request.append((socket_name, request["op"], timeout))
                raise ConnectionError("daemon died after metadata probe")

            self._assert_strict_unchanged(root, patches=(
                mock.patch.object(client, "_roundtrip",
                                  side_effect=die_after_probe),
            ))
            self.assertEqual(len(reached_request), 1)
            self.assertEqual(reached_request[0][1], "lint.context")

    def test_stale_daemon_metadata_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            engine = root / ".engine"
            engine.mkdir(mode=0o700)
            (engine / "daemon.json").write_text(json.dumps({
                "state": "ready",
                "socket": os.fspath(engine / "stale.sock"),
                "identity": client._local_identity(),
            }), encoding="utf-8")
            self._assert_strict_unchanged(root)

    def test_held_writer_lease_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            lock_fd = os.open(root / ".engine/daemon.lock", os.O_RDONLY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._assert_strict_unchanged(root)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def test_absent_record_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            (root / ".engine/ledger.db").unlink()
            self._assert_strict_unchanged(root)

    def test_corrupt_record_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            (root / ".engine/ledger.db").write_bytes(b"not sqlite")
            self._assert_strict_unchanged(root)

    def test_unsupported_schema_falls_back_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, _daemon_owner = self._record_root(
                temporary, running=False)
            record = sqlite3.connect(root / ".engine/ledger.db")
            try:
                record.execute(
                    "UPDATE meta SET value='999' WHERE key='schema_version'")
                record.commit()
            finally:
                record.close()
            self._assert_strict_unchanged(root)

    def test_hostile_lock_and_store_file_types_fall_back_strict(self):
        def lock_directory(root):
            lock = root / ".engine/daemon.lock"
            lock.unlink()
            lock.mkdir()

        def store_symlink(root):
            record = root / ".engine/ledger.db"
            record.unlink()
            record.symlink_to("daemon.json")

        for label, mutate in (
                ("lock directory", lock_directory),
                ("store symlink", store_symlink)):
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as temporary:
                root, _daemon_owner = self._record_root(
                    temporary, running=False)
                mutate(root)
                self._assert_strict_unchanged(root)


class TestRecordComposition(unittest.TestCase):
    def test_cross_relay_chain_still_parses_suppressed_relays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "chain")
            context = _context(root)
            result = _record_lint(self, root, context)
            self.assertTrue(any(
                "DISPATCH IMPL parent must be an earlier PLAN-REVIEW relay "
                "with verdict approve" in error for error in result.errors))
            self.assertIn(
                "engine-root sweep: 2 record-known relays not re-judged; "
                "context source: daemon", result.warnings)

    def test_structural_index_lint_composes_with_record_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary, "relay-root")
            root.mkdir()
            index = root / "INDEX.md"
            index.write_text(INDEX_TEXT, encoding="utf-8")
            digest = _digest(index.read_bytes())
            strict = rules.lint_relay_root(
                root, engine_root=True,
                projection_digests={index: digest})
            record = _record_lint(
                self, root, {"snapshot": "0", "entries": []},
                projection_digests={index: digest})
            self.assertEqual(record.errors, strict.errors)
            self.assertEqual(
                [warning for warning in record.warnings
                 if not warning.startswith(SUMMARY_PREFIX)], strict.warnings)

            index.write_text(INDEX_TEXT + (
                "| 20260816-120000 | SITREP | Pair Planner | task4-old | "
                "task4.orchestrator-planner | task4 | filed | "
                "task4/SITREP-pair-planner-20260816-120000.md |\n"
            ), encoding="utf-8")
            result = _record_lint(
                self, root, {"snapshot": "0", "entries": []})
            self.assertTrue(any("precedes the previous row" in error
                                for error in result.errors))

    def test_single_file_cli_bytes_and_exits_match_frozen_population(self):
        spec = importlib.util.spec_from_file_location(
            "task4_frozen_relay_lint", SCRIPT)
        frozen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(frozen)
        roots = [TOOLS / "relay-lint-fixtures",
                 TOOLS / "relay-engine-fixtures/corpora"]
        paths = sorted(path for root in roots for path in root.rglob("*.md")
                       if path.is_file())
        names = [os.fspath(path.relative_to(REPO)) for path in paths]
        expected = {}
        for name, path in zip(names, paths):
            result = frozen.lint_file(path, freshness=False)
            expected[name] = {
                "errors": result.errors,
                "warnings": result.warnings,
            }
        expected_stdout = json.dumps(
            expected, sort_keys=True, separators=(",", ":")) + "\n"
        expected_status = 1 if any(
            value["errors"] for value in expected.values()) else 0

        status, stdout, stderr = _run_cli(
            ["lint", "--no-freshness"] + names)

        self.assertEqual(status, expected_status)
        self.assertEqual(stdout, expected_stdout)
        self.assertEqual(stderr, "")


class TestRecordIdentityAbort(unittest.TestCase):
    def test_version_mismatch_aborts_without_lint_verdict_or_fallback(self):
        mismatch = errors.error_for(
            "E-VERSION-MISMATCH",
            client_install="/task4-client", client_kit="2.9.0",
            client_fp="a" * 64,
            daemon_install="/task4-daemon", daemon_kit="2.9.0",
            daemon_fp="b" * 64,
        )
        refusal = client.RemoteError(mismatch.as_dict())
        with tempfile.TemporaryDirectory() as temporary:
            root = _copy_fixture(temporary, "suppressed")
            with mock.patch.object(cli.client, "lint_context",
                                   side_effect=refusal), \
                    mock.patch.object(
                        cli.daemon, "read_lint_context",
                        side_effect=AssertionError(
                            "identity refusal fell back to direct read")), \
                    mock.patch.object(
                        cli.rules, "lint_relay_root",
                        side_effect=AssertionError(
                            "identity refusal emitted a lint verdict")):
                status, stdout, stderr = _run_cli(
                    ["lint", "--relay-root", os.fspath(root)])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "%s\ncause: %s\nremedy: %s\n" % (
            mismatch.code, mismatch.cause, mismatch.remedy))
        self.assertNotIn("command-result", stderr)
        self.assertNotIn(SUMMARY_PREFIX, stderr)


if __name__ == "__main__":
    unittest.main()
