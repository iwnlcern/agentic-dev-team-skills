"""Window records hold credential paths, survive temporary-folder cleanup and share a durable lock."""
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from tools.adapters.tests.test_relay_monitor import load_script


class WindowRecordTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home)}))

    def module(self):
        self.assertTrue((Path(__file__).parents[1] / "window_record.py").is_file(),
                        "window record storage is not implemented")
        return importlib.import_module("tools.adapters.window_record")

    def record(self, module):
        return module.WindowRecord("cx-thread-1", "codex", "host-session", "seat-id", "v3-adapter.implementer",
                                   "project-id", str(self.home / "a b" / "é.key"), "internal-session", 2,
                                   str(self.home / "ready.json"), True, 1234)

    def test_window_record_fields_no_secret(self):
        m = self.module()
        record = self.record(m)
        key = Path(record.key_path)
        key.parent.mkdir()
        key.write_text("SECRET-NEVER-COPIED")
        m.write(record, home=self.home)
        path = self.home / ".adt/windows/cx-thread-1.json"
        body = path.read_text()
        self.assertNotIn("SECRET-NEVER-COPIED", body)
        self.assertEqual(json.loads(body), {
            "window_key": "cx-thread-1", "host": "codex", "host_session_id": "host-session",
            "seat_id": "seat-id", "seat_address": "v3-adapter.implementer", "project_id": "project-id",
            "key_path": str(key), "session_id": "internal-session", "generation": 2,
            "ready_path": str(self.home / "ready.json"), "connected": True, "written_at_ms": 1234})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(m.read(record.window_key, home=self.home), record)
        # A writer copying the credential bytes must fail the same no-secret assertion.
        from dataclasses import asdict
        copied = asdict(record)
        copied["key_path"] = key.read_text()
        with mock.patch.object(m, "asdict", return_value=copied):
            m.write(record, home=self.home)
        with self.assertRaises(AssertionError):
            self.assertNotIn("SECRET-NEVER-COPIED", path.read_text())
        m.write(record, home=self.home)
        real_replace = os.replace
        observed = []
        def replace(source, destination):
            self.assertEqual(m.read(record.window_key, home=self.home), record)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            observed.append(Path(destination))
            return real_replace(source, destination)
        with mock.patch.object(m.os, "replace", side_effect=replace):
            changed = m.mark_not_connected(record.window_key, home=self.home)
        self.assertEqual(observed, [path])
        self.assertFalse(changed.connected)
        self.assertEqual(m.read(record.window_key, home=self.home), changed)
        self.assertEqual(changed.key_path, record.key_path)
        self.assertIsNone(m.mark_not_connected("missing-window", home=self.home))
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_window_record_survives_tmpdir_clean(self):
        m = self.module()
        record = self.record(m)
        # This removes only a private runtime fixture, never the inherited TMPDIR.
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.runtime)}):
            m.write(record, home=self.home)
            doomed = self.runtime / "old-record.json"
            with mock.patch.object(m, "_path", return_value=doomed):
                m.write(record, home=self.home)
            shutil.rmtree(self.runtime)
            self.assertEqual(m.read(record.window_key, home=self.home), record)
            with mock.patch.object(m, "_path", return_value=doomed):
                with self.assertRaises(AssertionError):
                    self.assertEqual(m.read(record.window_key, home=self.home), record)

    def test_key_path_with_space_round_trips(self):
        m = self.module()
        record = self.record(m)
        m.write(record, home=self.home)
        self.assertEqual(m.read(record.window_key, home=self.home).key_path.encode(),
                         str(self.home / "a b" / "é.key").encode())
        self.assertIsNone(m.read("missing-window", home=self.home))

    def test_window_lock_lives_beside_window_record(self):
        rm = load_script()
        with mock.patch.dict(os.environ, {"TMPDIR": str(self.runtime)}):
            first = rm.WindowLock("cx-lock-test")
            self.assertTrue(first.acquire())
            self.addCleanup(first.release)
            expected = self.home / ".adt/windows/cx-lock-test.lock"
            self.assertTrue(expected.is_file(), "window lock still lives in temporary storage")
            self.assertEqual(stat.S_IMODE(expected.parent.stat().st_mode), 0o700)
            shutil.rmtree(self.runtime)
            second = rm.WindowLock("cx-lock-test", home=self.home)
            self.assertFalse(second.acquire(), "temporary cleanup let a second doorbell run")
            first.release()
            self.assertTrue(second.acquire())
            second.release()
        # The old temporary-folder placement loses exclusion when its file is removed.
        import fcntl
        self.runtime.mkdir()
        old = self.runtime / "window.lock"
        a = os.open(old, os.O_CREAT | os.O_RDWR, 0o600)
        b = None
        try:
            fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
            old.unlink()
            b = os.open(old, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(a)
            if b is not None:
                os.close(b)
