"""Seat records and the sign-in transaction (hooks design section 2.2)."""

import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SIGN_IN_STANDINS = frozenset()


def sign_in_census(source):
    """Find sign-in constants in direct arguments or keywords, not comparisons.

    Payload dicts/lists, variables and f-strings are not followed. Transactions
    use seat_record.transaction or the exact unaliased import-from spelling;
    other import aliases are not followed. A with-name.write or seat_record.write
    must occur earlier by line inside the with, not necessarily by execution.
    G-B1's base erratum pins the real sign-in client method when it first exists.
    """
    tree = ast.parse(source)
    imported_transaction = any(
        isinstance(node, ast.ImportFrom) and node.level == 0
        and node.module == "tools.adapters.seat_record"
        and any(alias.name == "transaction" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found, bad = [], []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        if not any(isinstance(arg, ast.Constant) and arg.value == "session.sign-in"
                   for arg in [*call.args, *(k.value for k in call.keywords)]):
            continue
        found.append(call.lineno)
        node, valid = call, False
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    ctx = item.context_expr
                    if not isinstance(ctx, ast.Call):
                        continue
                    module_transaction = (isinstance(ctx.func, ast.Attribute)
                                          and isinstance(ctx.func.value, ast.Name)
                                          and ctx.func.value.id == "seat_record"
                                          and ctx.func.attr == "transaction")
                    bare_transaction = (imported_transaction and isinstance(ctx.func, ast.Name)
                                        and ctx.func.id == "transaction")
                    if module_transaction or bare_transaction:
                        writer = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
                        valid = any(isinstance(n, ast.Call) and n.lineno < call.lineno
                                    and isinstance(n.func, ast.Attribute) and n.func.attr == "write"
                                    and isinstance(n.func.value, ast.Name)
                                    and n.func.value.id in (writer, "seat_record")
                                    for statement in node.body for n in ast.walk(statement))
                        if valid:
                            break
            if valid:
                break
        if not valid:
            bad.append(call.lineno)
    return found, bad


def removal_census(source, *, seat_module=False):
    """Census lexical persistent paths, distinguishing proven temporary fixtures.

    Operations are unlink/remove/rmtree/rename/replace on any method receiver,
    or on arguments/keywords of bare calls and os/shutil/pathlib attributes.
    str(record) is ignored only as a method receiver, not as a removal argument.
    Records come from record_path/sessions_dir, strings containing .adt/sessions,
    the .adt folder itself as a right operand of /, or bound names.
    Plain/annotated assignments are followed only within their own scope; loop,
    comprehension, with-as, walrus and tuple targets, helper returns and import
    aliases are not followed. Passed-in arguments count only where the receiving
    scope has its own record binding. TemporaryDirectory-derived expressions are
    scratch; a name once bound to a record stays so, even after scratch rebinding.
    Writer temporary paths use a .tmp- constant in a path / or + expression.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bindings = {}

    def scope(node, kind):
        while node in parents:
            node = parents[node]
            if isinstance(node, kind):
                return id(node)
        return 0

    def key(node):
        if isinstance(node, ast.Name):
            return (scope(node, (ast.FunctionDef, ast.AsyncFunctionDef)), node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            return (scope(node, ast.ClassDef), "self." + node.attr)
        return None

    def kind(node):
        k = key(node)
        if k in bindings:
            return bindings[k]
        if seat_module and isinstance(node, ast.Attribute) and node.attr == "_path":
            return "record"
        if isinstance(node, ast.Name) and node.id == "path" and seat_module:
            parent = node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, ast.FunctionDef):
                    if parent.name == "_replace_bytes":
                        return "record"
                    break
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "TemporaryDirectory":
                return "scratch"
            if name in ("record_path", "sessions_dir"):
                home = next((kw.value for kw in node.keywords if kw.arg == "home"), None)
                return "scratch" if home is not None and kind(home) == "scratch" else "record"
        children = [kind(n) for n in ast.iter_child_nodes(node)]
        if "scratch" in children:
            return "scratch"
        strings = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        if isinstance(node, ast.BinOp) and any(x.startswith(".tmp-") for x in strings):
            return "temporary"
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.Constant) and node.right.value == ".adt"):
            return "folder"
        if "record" in children or any(".adt/sessions" in x for x in strings) or {".adt", "sessions"} <= set(strings):
            return "record"
        # The folder itself is persistent; an arbitrary descendant is not
        # classified just because its left operand is that folder.
        if not isinstance(node, ast.BinOp) and "folder" in children:
            return "folder"
        return None

    assignments = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            value = kind(node.value) if node.value is not None else None
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                k = key(target)
                if k is not None and bindings.get(k) != value and bindings.get(k) not in ("record", "folder"):
                    bindings[k] = value
                    changed = True
        if not changed:
            break
    found, bad = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            operation, receiver = node.func.attr, [node.func.value]
            if (isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "str"):
                receiver = []
            read_arguments = (isinstance(node.func.value, ast.Name)
                              and node.func.value.id in ("os", "shutil", "pathlib"))
        elif isinstance(node.func, ast.Name):
            operation, receiver = node.func.id, []
            read_arguments = True
        else:
            continue
        if operation not in ("unlink", "remove", "rmtree", "rename", "replace"):
            continue
        arguments = [*node.args, *(k.value for k in node.keywords)] if read_arguments else []
        if not any(kind(n) in ("record", "folder") for n in [*receiver, *arguments]):
            continue
        found.append(node.lineno)
        parent, owner = node, []
        while parent in parents:
            parent = parents[parent]
            if isinstance(parent, (ast.FunctionDef, ast.ClassDef)):
                owner.append(parent.name)
        allowed = seat_module and (owner == ["restore", "SeatTransaction"]
                                  or operation in ("rename", "replace") and owner == ["_replace_bytes"])
        if not allowed:
            bad.append(node.lineno)
    return found, bad


class SeatRecordTests(unittest.TestCase):
    def setUp(self):
        self.sr = importlib.import_module("tools.adapters.seat_record")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home ü space"
        self.home.mkdir()

    def child(self, code, *args):
        p = subprocess.Popen([sys.executable, "-c", code, str(self.home), *map(str, args)],
                             cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if p.poll() is None:
                p.kill()
            out, err = p.communicate(timeout=5)
            print("SEAT_RECORD_CHILD " + json.dumps({"pid": p.pid, "exit": p.returncode,
                                                       "stdout": out, "stderr": err}))
        self.addCleanup(cleanup)
        return p

    def test_record_path_is_host_and_sha256(self):
        self.assertEqual(self.sr.record_path("claude-code", "abc", home=self.home),
                         self.home / ".adt/sessions" / ("claude-code-" + hashlib.sha256(b"abc").hexdigest()))

    def test_write_then_read_round_trips(self):
        old = os.umask(0o777)
        try:
            record = self.sr.write("codex", "id", "seat-id", "a.planner", "project", home=self.home)
        finally:
            os.umask(old)
        self.assertEqual(self.sr.read("codex", "id", home=self.home), record)
        self.assertEqual((record.host, record.host_session_id, record.seat_id, record.seat_address, record.project_id),
                         ("codex", "id", "seat-id", "a.planner", "project"))
        self.assertIs(type(record.written_at_ms), int)
        self.assertEqual(stat.S_IMODE(self.sr.sessions_dir(home=self.home).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.sr.record_path("codex", "id", home=self.home).stat().st_mode), 0o600)

    def test_read_absent_is_none(self):
        self.assertIsNone(self.sr.read("codex", "absent", home=self.home))

    def test_unreadable_record_raises(self):
        path = self.sr.record_path("codex", "id", home=self.home)
        path.parent.mkdir(parents=True)
        for text in ('{"host":', '[]', '{"host":"codex"}'):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(self.sr.SeatRecordUnreadable):
                    self.sr.read("codex", "id", home=self.home)
        path.unlink()
        path.mkdir()
        with self.assertRaises(self.sr.SeatRecordUnreadable) as caught:
            self.sr.read("codex", "id", home=self.home)
        self.assertEqual(caught.exception.cause, "type or owner")
        path.rmdir()
        self.sr.write("codex", "id", "s", "a.planner", "p", home=self.home)
        real = os.fstat

        def wrong_owner(fd):
            fields = list(real(fd))
            fields[4] = os.getuid() + 1
            return os.stat_result(fields)
        with mock.patch.object(self.sr.os, "fstat", side_effect=wrong_owner):
            with self.assertRaises(self.sr.SeatRecordUnreadable):
                self.sr.read("codex", "id", home=self.home)

    def test_deeply_nested_record_is_unreadable(self):
        path = self.sr.record_path("codex", "id", home=self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"[" * 50_000)
        with self.assertRaises(self.sr.SeatRecordUnreadable) as caught:
            self.sr.read("codex", "id", home=self.home)
        self.assertEqual(caught.exception.cause, "invalid record")
        parser = mock.Mock()
        parser.loads.side_effect = RecursionError("nested record")
        with mock.patch.object(self.sr, "json", parser):
            with self.assertRaises(self.sr.SeatRecordUnreadable) as caught:
                self.sr.read("codex", "id", home=self.home)
        self.assertEqual(caught.exception.cause, "invalid record")

    def test_oversized_record_is_unreadable(self):
        path = self.sr.record_path("codex", "id", home=self.home)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x" * (self.sr.RECORD_MAX_BYTES + 1))
        with self.assertRaises(self.sr.SeatRecordUnreadable) as caught:
            self.sr.read("codex", "id", home=self.home)
        self.assertEqual(caught.exception.cause, "too large")
        path.unlink()
        with self.assertRaises(self.sr.SeatRecordUnreadable) as caught:
            self.sr.write("codex", "id", "s", "x" * self.sr.RECORD_MAX_BYTES, "p", home=self.home)
        self.assertEqual(caught.exception.cause, "too large")
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_write_is_atomic(self):
        self.sr.write("codex", "id", "initial", "a.planner", "p", home=self.home)
        stop, ready = threading.Event(), threading.Event()
        errors, seen = [], []

        def reader():
            ready.set()
            while not stop.is_set():
                try:
                    record = self.sr.read("codex", "id", home=self.home)
                    if record is None or record.seat_address != "a.planner":
                        errors.append(record)
                    seen.append(record)
                except BaseException as exc:
                    errors.append(repr(exc))
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            for i in range(40):
                self.sr.write("codex", "id", str(i), "a.planner", "p", home=self.home)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(seen)
        self.assertEqual(errors, [])
        self.assertEqual(list(self.sr.sessions_dir(home=self.home).iterdir()),
                         [self.sr.record_path("codex", "id", home=self.home)])

    def waiter(self, wait=2, *, elapsed=False):
        return self.child('''import sys,time
from pathlib import Path
from tools.adapters import seat_record as s
print("ready", flush=True)
started = time.monotonic()
try:
    with s.transaction("codex", "id", home=Path(sys.argv[1]), wait_s=float(sys.argv[2])):
        print("acquired", flush=True)
except s.SeatRecordBusy:
    print("busy", flush=True)
    if sys.argv[3] == "1":
        print(time.monotonic() - started, flush=True)
''', wait, "1" if elapsed else "0")

    def test_transaction_serialises(self):
        with self.sr.transaction("codex", "id", home=self.home):
            p = self.waiter()
            self.assertEqual(p.stdout.readline(), "ready\n")
            self.assertEqual(select.select([p.stdout], [], [], 0.05)[0], [])
        out, err = p.communicate(timeout=5)
        self.assertEqual((p.returncode, out, err), (0, "acquired\n", ""))

    def test_transaction_busy_after_wait(self):
        with self.sr.transaction("codex", "id", home=self.home):
            p = self.waiter(0.2, elapsed=True)
            self.assertEqual(p.stdout.readline(), "ready\n")
            started = time.monotonic()
            out, err = p.communicate(timeout=0.5)
            self.assertLess(time.monotonic() - started, 0.5)
            lines = out.splitlines()
            self.assertEqual((p.returncode, lines[0], err), (0, "busy", ""))
            self.assertEqual(len(lines), 2)
            self.assertGreaterEqual(float(lines[1]), 0.2)

    def test_lock_is_on_the_directory(self):
        with self.sr.transaction("codex", "id", home=self.home) as tx:
            tx.write("s", "a.planner", "p")
            for path in self.sr.sessions_dir(home=self.home).iterdir():
                path.unlink()
                path.write_text("recreated")
            p = self.waiter(0.2)
            out, err = p.communicate(timeout=2)
            self.assertEqual((p.returncode, out, err), (0, "ready\nbusy\n", ""))

    def test_death_releases_the_lock(self):
        p = self.child('''import sys,time
from pathlib import Path
from tools.adapters import seat_record as s
with s.transaction("codex", "id", home=Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(20)
''')
        self.assertEqual(p.stdout.readline(), "locked\n")
        os.kill(p.pid, signal.SIGKILL)
        p.communicate(timeout=3)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        with self.sr.transaction("codex", "id", home=self.home, wait_s=0.2):
            pass

    def test_restore_puts_back_or_removes(self):
        self.sr.write("codex", "id", "s", "a.planner", "p", home=self.home)
        path = self.sr.record_path("codex", "id", home=self.home)
        before = path.read_bytes()
        with self.sr.transaction("codex", "id", home=self.home) as tx:
            tx.write("other", "a.implementer", "p")
            tx.restore()
        self.assertEqual(path.read_bytes(), before)
        with self.sr.transaction("codex", "absent", home=self.home) as tx:
            tx.write("s", "a.planner", "p")
            tx.restore()
        self.assertIsNone(self.sr.read("codex", "absent", home=self.home))

    def test_every_sign_in_caller_writes_the_seat_record_first(self):
        found, bad = [], []
        for path in sorted((REPO / "tools").rglob("*.py")):
            hits, misses = sign_in_census(path.read_text(encoding="utf-8"))
            rel = str(path.relative_to(REPO))
            found.extend((rel, line) for line in hits)
            if rel not in SIGN_IN_STANDINS:
                bad.extend((rel, line) for line in misses)
        print("SIGN_IN_CENSUS " + json.dumps(found))
        self.assertEqual(bad, [])
        self.assertTrue(sign_in_census('with seat_record.transaction("codex", "id") as tx:\n    client.call("session.sign-in")\n    tx.write("s", "a.planner", "p")\n')[1])
        self.assertFalse(sign_in_census('with seat_record.transaction("codex", "id") as tx:\n    tx.write("s", "a.planner", "p")\n    client.call("session.sign-in")\n')[1])
        self.assertEqual(sign_in_census('matched = any(op == "session.sign-in" for op in operations)\n'), ([], []))
        controls = [
            ('from tools.adapters.seat_record import transaction\n'
             'with transaction("codex", "id") as tx:\n'
             '    tx.write("s", "a.planner", "p")\n'
             '    client.call("session.sign-in")\n', False),
            ('from tools.adapters.seat_record import transaction\n'
             'with transaction("codex", "id") as tx:\n'
             '    client.call("session.sign-in")\n'
             '    tx.write("s", "a.planner", "p")\n', True),
            ('with seat_record.transaction("codex", "id"):\n'
             '    seat_record.write("codex", "id", "s", "a.planner", "p")\n'
             '    client.call("session.sign-in")\n', False),
            ('with seat_record.transaction("codex", "id"):\n'
             '    client.call("session.sign-in")\n'
             '    seat_record.write("codex", "id", "s", "a.planner", "p")\n', True),
        ]
        for source, flagged in controls:
            with self.subTest(source=source):
                found, bad = sign_in_census(source)
                self.assertEqual(len(found), 1)
                self.assertEqual(bool(bad), flagged)

    def test_no_record_removal_outside_restore(self):
        found, bad = [], []
        for path in sorted((REPO / "tools").rglob("*.py")):
            hits, misses = removal_census(path.read_text(encoding="utf-8"), seat_module=path == REPO / "tools/adapters/seat_record.py")
            rel = str(path.relative_to(REPO))
            found.extend((rel, line) for line in hits)
            bad.extend((rel, line) for line in misses)
        print("REMOVAL_CENSUS " + json.dumps(found))
        self.assertEqual(bad, [])
        self.assertTrue(removal_census('p = seat_record.record_path("codex", "id")\np.unlink()\n')[1])
        self.assertEqual(removal_census(
            'p = seat_record.record_path("codex", "id")\n'
            'p.unlink()\n'
            'tmp = tempfile.TemporaryDirectory()\n'
            'p = seat_record.record_path("codex", "id", home=Path(tmp.name))\n'),
            ([2], [2]))
        self.assertEqual(removal_census(
            'tmp = tempfile.TemporaryDirectory()\n'
            'p = seat_record.record_path("codex", "id", home=Path(tmp.name))\n'
            'p.unlink()\n'), ([], []))
        self.assertEqual(removal_census(
            'from shutil import rmtree\n'
            'p = seat_record.record_path("codex", "id")\n'
            'rmtree(p)\n'), ([3], [3]))
        self.assertEqual(removal_census(
            'from shutil import rmtree\n'
            'tmp = tempfile.TemporaryDirectory()\n'
            'p = seat_record.record_path("codex", "id", home=Path(tmp.name))\n'
            'rmtree(p)\n'), ([], []))
        record = 'path = seat_record.record_path("codex", "id")\n'
        controls = [
            ('p = Path.home() / ".adt/sessions" / "x"\np.unlink()\n', True),
            ('shutil.rmtree(Path.home() / ".adt")\n', True),
            (record + 'os.remove(path)\n', True),
            (record + 'path.unlink()\n', True),
            (record + 'os.remove(str(path))\n', True),
            ('shutil.rmtree(Path.home() / ".adt" / name)\n', False),
            (record + 'str(path).replace(home, "~")\n', False),
            (record + 'seen.remove(path)\n', False),
        ]
        for source, flagged in controls:
            with self.subTest(source=source):
                found, bad = removal_census(source)
                self.assertEqual(bool(found), flagged)
                self.assertEqual(bool(bad), flagged)

    def test_transaction_wait_constant(self):
        self.assertEqual(self.sr.TRANSACTION_WAIT_S, 15.0)
