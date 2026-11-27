"""Pending-end storage ordering, eligibility and non-destructive malformed handling."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import types
from unittest import mock

from tools.adapters.tests.test_relay_monitor import TmpEnv


class PendingEndTests(TmpEnv):
    def module(self):
        self.assertTrue((Path(__file__).parents[1] / "pending_ends.py").is_file(),
                        "pending-end record store is not implemented")
        return importlib.import_module("tools.adapters.pending_ends")

    def record(self, m, session, *, last=None, next_ms=0, failed=False, created=1):
        return m.PendingEnd("codex", session, "project-id", "internal-" + session, 3,
                            str(self.base / "home" / "a b" / "é.key"), created, last, failed, next_ms,
                            operation_key="close-" + session, ready_path=str(self.base / "ready"))

    def mutant(self, m, old, new):
        source = Path(m.__file__).read_text()
        self.assertEqual(source.count(old), 1)
        variant = types.ModuleType(m.__name__)
        exec(compile(source.replace(old, new), "<pending-end-control>", "exec"), variant.__dict__)
        return variant

    def status(self):
        with mock.patch.object(self.rm, "readiness", return_value=("armed", {"progress": {}})):
            return self.rm.status_line(self.rm.NoteStore("pending-test"), None)

    def test_pending_end_order_by_last_attempt(self):
        m = self.module()
        home = self.base / "home"
        records = [self.record(m, "newer-attempt", last=50), self.record(m, "never-b", created=20),
                   self.record(m, "oldest-attempt", last=10), self.record(m, "never-a", created=10)]
        for record in records:
            path = m.write(record, home=home)
            self.assertEqual(path.name, "codex-" + hashlib.sha256(record.host_session_id.encode()).hexdigest() + "-3.json")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(json.loads(path.read_text())["key_path"], record.key_path)
        def witness(module):
            ordered = module.eligible(home=home, now_ms=1000)
            self.assertEqual({p.host_session_id for p in ordered[:2]}, {"never-a", "never-b"})
            self.assertEqual([p.host_session_id for p in ordered[2:]], ["oldest-attempt", "newer-attempt"])
        witness(m)
        wrong = self.mutant(m, "return (pe.last_attempt_ms is not None,", "return (pe.last_attempt_ms is None,")
        with self.assertRaises(AssertionError):
            witness(wrong)

    def test_ineligible_record_skipped_not_waited_behind(self):
        m = self.module()
        home = self.base / "home"
        for i in range(12):
            m.write(self.record(m, str(i), last=i, next_ms=1001 if i < 4 else 1000,
                                failed=i == 0), home=home)
        def witness(module):
            self.assertEqual([p.host_session_id for p in module.eligible(home=home, now_ms=1000)],
                             [str(i) for i in range(4, 12)])
        witness(m)
        wrong = self.mutant(m, "if pe.next_eligible_ms <= now_ms", "if True")
        with self.assertRaises(AssertionError):
            witness(wrong)
        output = self.status()
        self.assertIn("pending-ends=12 credential-failed=1", output)
        # Read-only eligibility/status never stamp, delete or send a record.
        self.assertEqual(len(list((home / ".adt/pending-ends").glob("*.json"))), 12)

    def test_malformed_pending_end_is_skipped_and_shown(self):
        m = self.module()
        home = self.base / "home"
        record = self.record(m, "bad")
        path = m.write(record, home=home)
        good = path.read_bytes()
        def unchanged():
            self.assertTrue(path.exists(), "malformed record was deleted")
            info = path.stat()
            return path.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid
        for case in ("json", "mode", "owner"):
            with self.subTest(case=case):
                path.write_bytes(b"{broken-json" if case == "json" else good)
                path.chmod(0o644 if case == "mode" else 0o600)
                before = unchanged()
                owner = mock.patch.object(m.os, "getuid", return_value=before[2] + 1) if case == "owner" else mock.patch.dict(os.environ)
                with owner:
                    self.assertEqual(m.eligible(home=home, now_ms=1000), [])
                    output = self.status()
                    self.assertIn("pending-ends=0 credential-failed=0", output)
                    self.assertIn(str(path), output)
                    self.assertEqual(len([line for line in output.splitlines() if str(path) in line]), 1)
                    with self.assertRaises(ValueError):
                        m.write(record, home=home)
                self.assertEqual(unchanged(), before)
        # Deleting malformed input is detected by the same real-file preservation witness.
        path.write_bytes(b"{broken-json")
        path.chmod(0o600)
        wrong = self.mutant(m, "except (OSError, ValueError, TypeError, UnicodeError, RecursionError):\n        return None",
                           "except (OSError, ValueError, TypeError, UnicodeError, RecursionError):\n        path.unlink(missing_ok=True)\n        return None")
        before = unchanged()
        wrong.eligible(home=home, now_ms=1000)
        with self.assertRaises(AssertionError):
            self.assertEqual(unchanged(), before)

    def test_status_pending_count_unknown_when_budget_ends(self):
        m = self.module()
        home = self.base / "home"
        m.write(self.record(m, "deadline"), home=home)
        with mock.patch.object(m.time, "monotonic", side_effect=[0.0, 0.0, 2.0]):
            with self.assertRaises(TimeoutError):
                m._scan(home=home, deadline=1.0)
        store = self.rm.NoteStore("pending-budget")
        script_module = importlib.import_module("pending_ends")
        def witness(module):
            with mock.patch.object(script_module, "_scan", module._scan):
                with mock.patch.object(self.rm, "readiness", return_value=("armed", {"progress": {}})):
                    with mock.patch.object(m.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 2.0]):
                        output = self.rm.status_line(store, None, end=1.0)
            self.assertIn("pending-ends=- credential-failed=-", output)
            self.assertNotIn("pending-end unreadable:", output)
        witness(m)
        wrong = self.mutant(m, "    except TimeoutError:\n        raise\n    except OSError:\n        return [], [folder]",
                           "    except OSError:\n        return [], [folder]")
        with self.assertRaisesRegex(AssertionError, "pending-ends=0"):
            witness(wrong)
