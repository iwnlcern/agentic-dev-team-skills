import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock

from relay_engine import client, daemon
from relay_engine.ledger import open_ledger
from relay_engine.paths import Root
from relay_engine.tests.test_identity_matrix import RunningDaemon


FP = "a" * 64
DID = {"kit": "2.9.0", "fp": FP, "install": "/readonly-context"}
HASHES = ("b" * 64, "c" * 64, "d" * 64)


def _tree_digest(root_name):
    digest = hashlib.sha256()
    root = Path(root_name)
    for path in sorted(root.rglob("*"), key=lambda value: os.fsencode(
            value.relative_to(root))):
        relative = os.fsencode(path.relative_to(root))
        info = path.lstat()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(stat.S_IFMT(info.st_mode).to_bytes(4, "big"))
        digest.update(stat.S_IMODE(info.st_mode).to_bytes(4, "big"))
        digest.update(info.st_uid.to_bytes(8, "big"))
        digest.update(info.st_gid.to_bytes(8, "big"))
        if stat.S_ISREG(info.st_mode):
            body = path.read_bytes()
            digest.update(len(body).to_bytes(8, "big"))
            digest.update(body)
        elif stat.S_ISLNK(info.st_mode):
            target = os.fsencode(os.readlink(path))
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
    return digest.hexdigest()


def _insert_population(root_name):
    entries = [
        {"path": "lane/a.md", "body_sha256": HASHES[0],
         "origin": "daemon"},
        {"path": "lane/escaped-雪.md", "body_sha256": HASHES[1],
         "origin": "hand"},
        {"path": "lane/z.md", "body_sha256": HASHES[2],
         "origin": "adopted"},
    ]
    engine_fd = os.open(os.path.join(root_name, ".engine"),
                        os.O_RDONLY | os.O_DIRECTORY)
    try:
        ledger = open_ledger(engine_fd)
        try:
            rows = []
            for seq, entry in enumerate(reversed(entries), start=1):
                rows.append((
                    seq, "readonly-%d" % seq, "20260821-%06d" % seq,
                    entry["path"], "IMPL", "Pair Implementer",
                    "readonly-%d" % seq, "readonly.implementer", "{}",
                    b"context", entry["body_sha256"], "e" * 64,
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
    return entries


def _stopped_root(root_name, *, populated=False):
    running = RunningDaemon(root_name, DID).start()
    try:
        expected_entries = _insert_population(root_name) if populated else []
        live = client.lint_context(
            root_name, cid=dict(DID, install="/readonly-client"))
        if populated:
            assert live["entries"] == expected_entries
    finally:
        running.stop()
    return live


def _crash_with_committed_wal(root_name):
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ready_read)
            root = Root(root_name)
            lease = daemon.acquire_lease(root)
            ledger = open_ledger(lease.engine_dirfd)
            ledger.execute(
                "INSERT INTO relays("
                "seq,submission_id,stamp,rendered_path,phase,role,"
                "dispatch_id,from_seat,headers_json,body,body_sha256,"
                "content_hash,origin,advisories_json) "
                "VALUES(1,'crash-1','20260821-235959','lane/crash.md',"
                "'IMPL','Pair Implementer','crash-1','crash.implementer',"
                "'{}',?,?,?,'daemon','[]')",
                (b"context", HASHES[0], HASHES[1]))
            wal = os.stat("ledger.db-wal", dir_fd=lease.engine_dirfd,
                          follow_symlinks=False)
            if not stat.S_ISREG(wal.st_mode) or wal.st_size == 0:
                raise AssertionError("committed WAL residue is absent")
            os.write(ready_write, b"R")
            os._exit(0)
        except BaseException:
            os._exit(2)
    os.close(ready_write)
    try:
        ready = os.read(ready_read, 1)
    finally:
        os.close(ready_read)
    _, status = os.waitpid(pid, 0)
    if ready != b"R" or os.waitstatus_to_exitcode(status) != 0:
        raise AssertionError("crash writer did not commit WAL state")


class TestLintRecordReadonly(unittest.TestCase):
    def _read(self, root_name):
        reader = getattr(daemon, "read_lint_context", None)
        self.assertTrue(callable(reader), "read-only context API is absent")
        return reader(root_name)

    def _assert_strict_unchanged(self, root_name, *, call=None):
        before = _tree_digest(root_name)
        result = self._read(root_name) if call is None else call()
        self.assertIsNone(result)
        self.assertEqual(_tree_digest(root_name), before)

    def test_stopped_daemon_matches_live_population_without_mutation(self):
        with tempfile.TemporaryDirectory() as root_name:
            live = _stopped_root(root_name, populated=True)
            before = _tree_digest(root_name)

            stopped = self._read(root_name)

            self.assertEqual(stopped, live)
            self.assertEqual(_tree_digest(root_name), before)

    def test_success_opens_record_read_only(self):
        with tempfile.TemporaryDirectory() as root_name:
            live = _stopped_root(root_name, populated=True)
            record = Path(root_name, ".engine/ledger.db")
            record.chmod(0o400)
            before = _tree_digest(root_name)

            stopped = self._read(root_name)

            self.assertEqual(stopped, live)
            self.assertEqual(_tree_digest(root_name), before)

    def test_committed_crash_wal_fails_closed_without_stale_success(self):
        with tempfile.TemporaryDirectory() as root_name:
            self.assertEqual(_stopped_root(root_name),
                             {"snapshot": "0", "entries": []})
            _crash_with_committed_wal(root_name)
            engine = Path(root_name, ".engine")
            self.assertGreater((engine / "ledger.db-wal").stat().st_size, 0)
            self.assertTrue((engine / "ledger.db-shm").exists())
            lock_fd = os.open(engine / "daemon.lock", os.O_RDONLY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
            before = _tree_digest(root_name)

            stopped = self._read(root_name)

            self.assertIsNone(
                stopped, "committed WAL state must not yield stale success")
            self.assertEqual(_tree_digest(root_name), before)
            logical = sqlite3.connect(
                "file:%s?mode=ro" % (engine / "ledger.db"), uri=True)
            try:
                self.assertEqual(logical.execute(
                    "SELECT rendered_path,body_sha256,origin FROM relays"
                ).fetchall(), [("lane/crash.md", HASHES[0], "daemon")])
            finally:
                logical.close()

    def test_hostile_or_empty_sidecar_presence_fails_closed_unfollowed(self):
        def empty_wal(engine):
            (engine / "ledger.db-wal").write_bytes(b"")

        def directory_shm(engine):
            (engine / "ledger.db-shm").mkdir()

        def symlink_journal(engine):
            (engine / "ledger.db-journal").symlink_to("daemon.json")

        cases = {
            "empty WAL": empty_wal,
            "directory SHM": directory_shm,
            "symlink rollback journal": symlink_journal,
        }
        for label, create in cases.items():
            with self.subTest(label=label), \
                    tempfile.TemporaryDirectory() as root_name:
                _stopped_root(root_name)
                create(Path(root_name, ".engine"))
                self._assert_strict_unchanged(root_name)

    def test_absence_never_creates_engine_or_lock(self):
        with tempfile.TemporaryDirectory() as root_name:
            self._assert_strict_unchanged(root_name)
            self.assertFalse(Path(root_name, ".engine").exists())

        with tempfile.TemporaryDirectory() as root_name:
            Path(root_name, ".engine").mkdir(mode=0o700)
            self._assert_strict_unchanged(root_name)
            self.assertFalse(Path(root_name, ".engine/daemon.lock").exists())

    def test_every_failed_read_selects_strict_and_preserves_bytes(self):
        def absent_record(root_name):
            Path(root_name, ".engine/ledger.db").unlink()

        def corrupt_record(root_name):
            Path(root_name, ".engine/ledger.db").write_bytes(
                b"not a sqlite record")

        def unsupported_schema(root_name):
            ledger = sqlite3.connect(Path(root_name, ".engine/ledger.db"))
            try:
                ledger.execute(
                    "UPDATE meta SET value='999' WHERE key='schema_version'")
                ledger.commit()
            finally:
                ledger.close()

        def lock_directory(root_name):
            path = Path(root_name, ".engine/daemon.lock")
            path.unlink()
            path.mkdir()

        def record_directory(root_name):
            path = Path(root_name, ".engine/ledger.db")
            path.unlink()
            path.mkdir()

        def lock_symlink(root_name):
            path = Path(root_name, ".engine/daemon.lock")
            path.unlink()
            path.symlink_to("daemon.json")

        def record_symlink(root_name):
            path = Path(root_name, ".engine/ledger.db")
            path.unlink()
            path.symlink_to("daemon.json")

        def engine_mode(root_name):
            Path(root_name, ".engine").chmod(0o755)

        def lock_mode(root_name):
            Path(root_name, ".engine/daemon.lock").chmod(0o644)

        def record_mode(root_name):
            Path(root_name, ".engine/ledger.db").chmod(0o666)

        mutations = {
            "absent record": absent_record,
            "corrupt record": corrupt_record,
            "unsupported schema": unsupported_schema,
            "lock wrong type": lock_directory,
            "record wrong type": record_directory,
            "lock open failure": lock_symlink,
            "record open failure": record_symlink,
            "engine wrong mode": engine_mode,
            "lock wrong mode": lock_mode,
            "record wrong mode": record_mode,
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root_name:
                _stopped_root(root_name)
                mutate(root_name)
                self._assert_strict_unchanged(root_name)

        with self.subTest(label="engine wrong type"), \
                tempfile.TemporaryDirectory() as root_name:
            Path(root_name, ".engine").write_bytes(b"not a directory")
            self._assert_strict_unchanged(root_name)

        with self.subTest(label="engine open failure"), \
                tempfile.TemporaryDirectory() as root_name:
            outside = Path(root_name, "outside-engine")
            outside.mkdir(mode=0o700)
            Path(root_name, ".engine").symlink_to(outside.name)
            self._assert_strict_unchanged(root_name)

        with self.subTest(label="wrong owner"), \
                tempfile.TemporaryDirectory() as root_name:
            _stopped_root(root_name)
            with mock.patch.object(daemon.os, "geteuid",
                                   return_value=os.geteuid() + 1):
                self._assert_strict_unchanged(root_name)

        with self.subTest(label="lock contention"), \
                tempfile.TemporaryDirectory() as root_name:
            _stopped_root(root_name)
            lock_fd = os.open(Path(root_name, ".engine/daemon.lock"),
                              os.O_RDONLY)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._assert_strict_unchanged(root_name)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


if __name__ == "__main__":
    unittest.main()
