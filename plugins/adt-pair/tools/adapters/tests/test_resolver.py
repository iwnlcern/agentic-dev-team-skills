"""The window-record tier and Platform's unchanged credential precedence."""
import ast
from contextlib import redirect_stderr, redirect_stdout
import importlib
import importlib.util
import io
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

from relay_engine import adt_cli, api_client
from tools.adapters import window, window_record
from tools.adapters.tests.test_relay_monitor import load_script


LAUNCHER = Path(__file__).resolve().parents[2] / "adt"
THREAD = "resolver-thread"
ENV = {"CODEX_THREAD_ID": THREAD}
A_PAIR = ["--key", "key-a", "--session-id", "session-a"]


class ResolverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.draft = self.home / "draft.md"
        self.draft.write_bytes(b"scratch")
        self.env = {"HOME": str(self.home), **ENV}
        self.record = window_record.WindowRecord(
            "cx-" + THREAD, "codex", "first-host-session", "seat", "fixture.implementer",
            "project", str(self.home / "a b" / "é.key"), "session-b", 1, "ready-b", True, 1,
        )
        self.pair = adt_cli.WindowSession(self.record.key_path, "session-b", "ready-b")

    def resolver(self, required=True):
        name = "tools.adapters.resolver"
        if not required and importlib.util.find_spec(name) is None:
            return None
        self.assertIsNotNone(importlib.util.find_spec(name), "window-record resolver is absent")
        return importlib.import_module(name)

    def connected(self):
        window_record.write(self.record, home=self.home)

    def session(self, env=ENV, pid=27):
        return self.resolver().window_session(env, pid, home=self.home, sessions_dir=self.home)

    def submit(self, arguments=(), environment=None, resolver=None):
        env = self.env | (environment or {})
        stdout, stderr, trace = io.StringIO(), io.StringIO(), []
        client = mock.Mock()
        client.call.return_value = {"ok": True}

        def open_ready(ready):
            trace.append(("open", ready))
            return client

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(api_client, "read_key_file", side_effect=lambda key: api_client.SeatKey(key, "fixture")),
            mock.patch.object(api_client, "read_ready_file", side_effect=lambda path: path),
            mock.patch.object(api_client.ApiClient, "open", side_effect=open_ready),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            exit_code = adt_cli.main(["submit", str(self.draft), *arguments], resolver=resolver)
        for call in client.call.call_args_list:
            operation, payload = call.args
            if operation == "session.resume":
                trace.append((operation, payload["key_id"], payload["session_id"]))
            else:
                trace.append((operation,))
        return exit_code, stdout.getvalue(), stderr.getvalue(), trace

    def assert_submission(self, observed, key, session, ready):
        self.assertEqual(observed[0], 0, observed[2])
        self.assertEqual(observed[3], [
            ("open", ready), ("session.resume", key, session), ("relay.submit",),
        ])

    def test_resolver_by_window_identity(self):
        self.connected()
        env = ENV | {"CODEX_SESSION_ID": "different-host-session"}
        self.assertEqual(self.session(env), self.pair)

    def test_resolver_empty_when_not_connected(self):
        self.assertIsNone(self.session())
        self.connected()
        window_record.mark_not_connected(self.record.window_key, home=self.home)
        self.assertIsNone(self.session())
        self.connected()
        path = self.home / ".adt" / "windows" / f"{self.record.window_key}.json"
        path.write_bytes(b"{")
        self.assertIsNone(self.session())
        with mock.patch.object(window, "window_identity", return_value=None):
            self.assertIsNone(self.session())

    def test_submit_uses_window_pair(self):
        self.connected()
        self.assert_submission(self.submit(resolver=self.resolver().adapter_resolver),
                               self.pair.key_path, self.pair.session_id, self.pair.ready_path)

    def test_codex_resume_keeps_pair(self):
        self.connected()
        self.assertEqual(self.session(ENV | {"CODEX_SESSION_ID": "resumed-host-session"}, 51),
                         self.pair)

    def test_codex_env_with_claude_var_resolves_to_thread(self):
        self.connected()
        self.assertEqual(self.session(ENV | {"CLAUDE_CODE_SESSION_ID": "inherited"}), self.pair)

    def test_platform_tiers_hold_with_a_resolver(self):
        adapters = self.resolver(required=False)
        variants = [("stand-in", lambda: self.pair)]
        if adapters is not None:
            self.connected()
            variants.append(("adapter", adapters.adapter_resolver))
        for origin, function in variants:
            for flags, env, key, session, ready, count in (
                (A_PAIR + ["--ready", "ready-flag"], {}, "key-a", "session-a", "ready-flag", 0),
                (A_PAIR, {"ADT_READY_FILE": "ready-env"}, "key-a", "session-a", "ready-env", 0),
                (A_PAIR, {}, "key-a", "session-a", self.pair.ready_path, 1),
                ([], {"ADT_KEY_FILE": "key-a", "ADT_SESSION_ID": "session-a"},
                 "key-a", "session-a", self.pair.ready_path, 1),
                (["--ready", "ready-flag"], {}, self.pair.key_path, self.pair.session_id, "ready-flag", 1),
            ):
                with self.subTest(origin=origin, flags=flags, env=env):
                    resolver = mock.Mock(side_effect=function)
                    self.assert_submission(self.submit(flags, env, resolver), key, session, ready)
                    self.assertEqual(resolver.call_count, count)
            resolver = mock.Mock(side_effect=function)
            rc, _out, err, trace = self.submit(["--key", "key-a"], {"ADT_SESSION_ID": "session-b"}, resolver)
            self.assertEqual(rc, 1)
            self.assertIn("mixed_credentials", err)
            self.assertEqual(trace, [])
            resolver.assert_not_called()
        rc, _out, err, trace = self.submit(resolver=lambda: None)
        self.assertEqual(rc, 1)
        self.assertIn("no_session", err)
        self.assertIn("run adt sign-in", err)
        self.assertEqual(trace, [])

    def test_explicit_pair_without_ready_unchanged_with_no_record(self):
        prior = self.submit(A_PAIR, resolver=lambda: None)
        self.assert_submission(prior, "key-a", "session-a", None)
        adapters = self.resolver(required=False)
        if adapters is not None:
            self.assertEqual(self.submit(A_PAIR, resolver=adapters.adapter_resolver), prior)

    def test_resolve_key_path_tiers(self):
        f = self.resolver().resolve_key_path
        resolve = mock.Mock(return_value=self.pair)
        self.assertEqual(f("flag", {"ADT_KEY_FILE": "env"}, resolve), "flag")
        self.assertEqual(f(None, {"ADT_KEY_FILE": "env", "ADT_SESSION_ID": "independent"}, resolve), "env")
        resolve.assert_not_called()
        self.assertEqual(f(None, {}, resolve), self.pair.key_path)
        self.assertIsNone(f(None, {}, lambda: None))

    def test_key_path_with_space_round_trips(self):
        self.connected()
        adapter = self.resolver()
        key = self.record.key_path.encode()
        self.assertEqual(self.session().key_path.encode(), key)
        self.assertEqual(adapter.resolve_key_path(None, {}, lambda: self.session()).encode(), key)

    def test_doorbell_without_note_rings_for_no_seat(self):
        rm = load_script()
        with (mock.patch.object(rm, "note_dir", return_value=self.home),
              mock.patch.dict(os.environ, self.env, clear=True)):
            store = rm.NoteStore("resolver-test")
            store.update(lambda note: note.update({"seat": "fixture.implementer", "root": str(self.home)}))
            last = rm.resolve_binding(store.load(), {}, None)
            self.assertIsNotNone(last)
            store.path.unlink()
            lock = rm.WindowLock(rm.session_window_key(store.session), home=self.home)
            self.assertTrue(lock.acquire())
            self.addCleanup(lock.release)
            me = rm.leader_record()
            lock.stamp(me["instance"], me["pid"], me["start_time"])
            store.update(lambda note: note.update({"leader": me, "phase": "waiting-for-binding"}))
            binding = rm.resolve_binding(store.load(), {}, None)
            self.assertIsNone(binding)
            self.assertIn("waiting-for-binding", rm.status_line(store, None))

    def test_tools_adt_wires_the_resolver(self):
        parsed = ast.parse(LAUNCHER.read_bytes())
        calls = [n for n in ast.walk(parsed) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "main"]
        self.assertEqual(len(calls), 1)
        keywords = {item.arg: item.value for item in calls[0].keywords}
        self.assertIn("resolver", keywords, "tools/adt does not wire its resolver")
        self.assertEqual(ast.unparse(keywords["resolver"]), "adapter_resolver")
        guard, = [n for n in parsed.body if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "__name__ == '__main__'"]
        imports = [n for n in ast.walk(parsed) if isinstance(n, ast.ImportFrom)
                   and n.module == "adapters.resolver"]
        self.assertEqual(len(imports), 1)
        self.assertIn(imports[0], guard.body)
        self.assertEqual([alias.name for alias in imports[0].names], ["adapter_resolver"])

    def test_runpy_readers_import_no_adapter_code(self):
        adapter_modules = {k: v for k, v in sys.modules.copy().items()
                           if k == "adapters" or k.startswith("adapters.")}
        try:
            for name in adapter_modules:
                sys.modules.pop(name)
            runpy.run_path(str(LAUNCHER), run_name="resolver_source_reader")
            self.assertFalse([n for n in sys.modules if n == "adapters" or n.startswith("adapters.")])
        finally:
            for name in list(sys.modules):
                if name == "adapters" or name.startswith("adapters."):
                    sys.modules.pop(name)
            sys.modules.update(adapter_modules)
