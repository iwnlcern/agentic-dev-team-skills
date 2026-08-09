import os
import stat
import tempfile
import unittest
import uuid

from relay_engine import paths
from relay_engine.paths import (Root, TempWrite, clear_sid,
                                ensure_engine_dir, sid_for)


class TestRoot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_path = self.temp.name
        os.mkdir(os.path.join(self.root_path, "drafts"))
        with open(os.path.join(self.root_path, "drafts", "one.md"),
                  "wb") as target:
            target.write(b"one\n")
        self.root = Root(self.root_path)

    def tearDown(self):
        self.root.close()
        self.temp.cleanup()

    def test_open_read_and_resolve_inside(self):
        self.assertEqual(self.root.open_read("drafts/one.md"), b"one\n")
        self.assertEqual(self.root.resolve_inside("drafts/one.md"),
                         os.path.join(self.root.path, "drafts", "one.md"))
        self.assertEqual(self.root.resolve_inside("drafts/new.md"),
                         os.path.join(self.root.path, "drafts", "new.md"))

    def test_path_escape_set_refuses(self):
        outside = os.path.join(self.root_path, "outside.md")
        with open(outside, "wb") as target:
            target.write(b"outside")
        os.symlink(outside, os.path.join(self.root_path, "drafts", "link.md"))
        os.symlink(self.root_path,
                   os.path.join(self.root_path, "linked-directory"))
        for rel in ("../outside.md", outside, "drafts/link.md",
                    "linked-directory/outside.md", "drafts/../outside.md",
                    "", "."):
            with self.subTest(rel=rel), self.assertRaises(
                    (OSError, ValueError)):
                self.root.open_read(rel)

    def test_non_regular_read_refuses_without_blocking(self):
        fifo = os.path.join(self.root_path, "drafts", "pipe")
        os.mkfifo(fifo)
        with self.assertRaises(OSError):
            self.root.open_read("drafts/pipe")


class TestTempWrite(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_path = self.temp.name
        os.mkdir(os.path.join(self.root_path, "dest"))
        self.root = Root(self.root_path)

    def tearDown(self):
        self.root.close()
        self.temp.cleanup()

    def test_noclobber_and_replace(self):
        with TempWrite(self.root, "dest/value") as pending:
            pending.write(b"first")
            pending.rename_noclobber()
        with open(os.path.join(self.root_path, "dest", "value"), "rb") as f:
            self.assertEqual(f.read(), b"first")
        with TempWrite(self.root, "dest/value") as pending:
            pending.write(b"second")
            with self.assertRaises(FileExistsError):
                pending.rename_noclobber()
        with open(os.path.join(self.root_path, "dest", "value"), "rb") as f:
            self.assertEqual(f.read(), b"first")
        with TempWrite(self.root, "dest/value") as pending:
            pending.write(b"third")
            pending.rename_replace()
        with open(os.path.join(self.root_path, "dest", "value"), "rb") as f:
            self.assertEqual(f.read(), b"third")

    def test_parent_swap_lands_on_held_directory(self):
        outside = os.path.join(self.root_path, "outside")
        os.mkdir(outside)
        moved = os.path.join(self.root_path, "original-dest")
        with TempWrite(self.root, "dest/value") as pending:
            os.rename(os.path.join(self.root_path, "dest"), moved)
            os.symlink(outside, os.path.join(self.root_path, "dest"))
            pending.write(b"held")
            pending.rename_noclobber()
        with open(os.path.join(moved, "value"), "rb") as f:
            self.assertEqual(f.read(), b"held")
        self.assertFalse(os.path.exists(os.path.join(outside, "value")))

    def test_abandoned_temp_is_removed(self):
        with TempWrite(self.root, "dest/value") as pending:
            pending.write(b"never published")
        self.assertEqual(os.listdir(os.path.join(self.root_path, "dest")), [])


class TestEngineDir(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Root(self.temp.name)

    def tearDown(self):
        self.root.close()
        self.temp.cleanup()

    def test_fresh_create_and_reuse(self):
        first = ensure_engine_dir(self.root.dirfd)
        second = ensure_engine_dir(self.root.dirfd)
        try:
            self.assertTrue(stat.S_ISDIR(os.fstat(first).st_mode))
            self.assertEqual(os.fstat(first).st_ino, os.fstat(second).st_ino)
        finally:
            os.close(first)
            os.close(second)

    def test_symlink_and_plain_file_refuse(self):
        outside = os.path.join(self.temp.name, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.temp.name, ".engine"))
        with self.assertRaises(OSError):
            ensure_engine_dir(self.root.dirfd)
        self.assertEqual(os.listdir(outside), [])
        os.unlink(os.path.join(self.temp.name, ".engine"))
        with open(os.path.join(self.temp.name, ".engine"), "wb") as f:
            f.write(b"occupied")
        with self.assertRaises(OSError):
            ensure_engine_dir(self.root.dirfd)
        with open(os.path.join(self.temp.name, ".engine"), "rb") as f:
            self.assertEqual(f.read(), b"occupied")


class TestSubmissionId(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_path = self.temp.name
        os.mkdir(os.path.join(self.root_path, "drafts"))
        with open(os.path.join(self.root_path, "drafts", "one.md"),
                  "wb") as f:
            f.write(b"draft")
        self.root = Root(self.root_path)

    def tearDown(self):
        self.root.close()
        self.temp.cleanup()

    def test_sid_stability_and_reset(self):
        first = sid_for(self.root, "drafts/one.md")
        self.assertEqual(sid_for(self.root, "drafts/one.md"), first)
        self.assertEqual(str(uuid.UUID(first)), first)
        clear_sid(self.root, "drafts/one.md")
        second = sid_for(self.root, "drafts/one.md")
        self.assertNotEqual(first, second)

    def test_symlinked_draft_and_parent_refuse(self):
        os.symlink("one.md",
                   os.path.join(self.root_path, "drafts", "link.md"))
        with self.assertRaises(OSError):
            sid_for(self.root, "drafts/link.md")
        outside = os.path.join(self.root_path, "outside")
        os.mkdir(outside)
        os.symlink(outside, os.path.join(self.root_path, "linked"))
        with self.assertRaises(OSError):
            sid_for(self.root, "linked/one.md")
        self.assertEqual(os.listdir(outside), [])

    def test_parent_swap_cannot_divert_sidecar(self):
        outside = os.path.join(self.root_path, "outside")
        os.mkdir(outside)
        moved = os.path.join(self.root_path, "original-drafts")
        real_uuid4 = paths.uuid.uuid4
        chosen = uuid.UUID("123e4567-e89b-12d3-a456-426614174000")

        def swap_then_uuid():
            os.rename(os.path.join(self.root_path, "drafts"), moved)
            os.symlink(outside, os.path.join(self.root_path, "drafts"))
            return chosen

        paths.uuid.uuid4 = swap_then_uuid
        try:
            self.assertEqual(sid_for(self.root, "drafts/one.md"),
                             str(chosen))
        finally:
            paths.uuid.uuid4 = real_uuid4
        self.assertTrue(os.path.exists(os.path.join(moved, "one.md.sid")))
        self.assertEqual(os.listdir(outside), [])


if __name__ == "__main__":
    unittest.main()
