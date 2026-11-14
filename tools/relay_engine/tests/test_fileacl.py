"""fileacl's private creation and access copying (OB-SNAP-ACL-1, plan Task 1)."""
import errno
import os
import unittest
from unittest import mock

from relay_engine import fileacl
from relay_engine.tests import acl_support

FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


class TestFileAcl(unittest.TestCase):
    def setUp(self):
        fileacl._bind()

    def opened(self, fd):
        self.addCleanup(os.close, fd)
        return fd

    def test_create_private_has_no_inherited_entry(self):
        # Breaks: creating the private file with the folder's inheritable entry.
        folder = acl_support.acl_folder(self)
        acl_support.set_inheritable_grant(self, folder)
        fd = self.opened(fileacl.create_private(os.path.join(folder, "private"), FLAGS))
        self.assertEqual(os.fstat(fd).st_mode & 0o7777, 0o600)
        self.assertFalse(acl_support.grants(fd), acl_support.entries(fd))
        if acl_support.DARWIN:
            self.assertEqual(acl_support.entries(fd), [])
        control = self.opened(os.open(os.path.join(folder, "control"), FLAGS, 0o600))
        # The control shows the folder passes its entry on to a file os.open creates.
        self.assertTrue(acl_support.inherited(control), acl_support.entries(control))
        if acl_support.DARWIN:
            self.assertTrue(acl_support.grants(control), acl_support.entries(control))
        else:
            # Linux masks a default ACL's named entries by the 0o600 creation mode.
            self.assertFalse(acl_support.grants(control), acl_support.entries(control))

    def test_create_private_is_exclusive(self):
        # Breaks: opening an existing name or following a symlink name.
        folder = acl_support.acl_folder(self)
        existing = os.path.join(folder, "existing")
        with open(existing, "wb") as stream:
            stream.write(b"keep")
        os.chmod(existing, 0o640)
        link = os.path.join(folder, "link")
        os.symlink(existing, link)
        for name in (existing, link):
            with self.subTest(name=os.path.basename(name)):
                with self.assertRaises(FileExistsError):
                    fileacl.create_private(name, FLAGS)
                with open(existing, "rb") as stream:
                    self.assertEqual(stream.read(), b"keep")
                self.assertEqual(os.stat(existing).st_mode & 0o7777, 0o640)
        self.assertTrue(os.path.islink(link))

    def test_create_private_errors_propagate(self):
        # Breaks: falling back to another creation when the creating call refuses.
        folder = acl_support.acl_folder(self)
        primitive = (fileacl, "_openx_np") if acl_support.DARWIN else (fileacl.os, "open")
        for code in (errno.EOPNOTSUPP, errno.EACCES):
            with self.subTest(code=errno.errorcode[code]):
                failure = OSError(code, os.strerror(code))
                with mock.patch.object(*primitive, side_effect=failure):
                    with self.assertRaises(OSError) as raised:
                        fileacl.create_private(os.path.join(folder, "refused"), FLAGS)
                self.assertIs(raised.exception, failure)
                self.assertEqual(os.listdir(folder), [])

    def test_copy_access_equal(self):
        # Breaks: dropping, widening or changing the source's ACL, or changing the mode.
        source_folder = acl_support.acl_folder(self)
        source = os.path.join(source_folder, "source")
        with open(source, "wb"):
            pass
        acl_support.set_explicit_entry(source)
        expected = acl_support.entries(source)
        self.assertTrue(expected, "fixture precondition: the source has an explicit entry")
        folder = acl_support.acl_folder(self)
        # 0o640 is the mode the source's Linux entries imply (owner rw-, mask r--,
        # other ---), so an unchanged mode is observable on both platforms.
        previous = os.umask(0o022)
        try:
            fd = self.opened(os.open(os.path.join(folder, "copy"), FLAGS, 0o640))
        finally:
            os.umask(previous)
        before = os.fstat(fd).st_mode
        self.assertEqual(before & 0o7777, 0o640, "fixture precondition")
        fileacl.copy_access(source, fd)
        self.assertEqual(acl_support.entries(fd), expected)
        if not acl_support.DARWIN:
            self.assertEqual(acl_support.entries(fd), acl_support.named_user_read())
        self.assertEqual(os.fstat(fd).st_mode, before)

    def test_copy_access_none_clears(self):
        # Breaks: leaving an inherited entry when the source has no ACL.
        source_folder = acl_support.acl_folder(self)
        source = os.path.join(source_folder, "source")
        with open(source, "wb"):
            pass
        acl_support.clear(source)
        self.assertFalse(acl_support.entries(source), "fixture precondition: the source has no ACL")
        folder = acl_support.acl_folder(self)
        acl_support.set_inheritable_grant(self, folder)
        fd = self.opened(os.open(os.path.join(folder, "copy"), FLAGS, 0o600))
        self.assertTrue(acl_support.inherited(fd), "fixture precondition: the descriptor inherited an entry")
        fileacl.copy_access(source, fd)
        if acl_support.DARWIN:
            self.assertEqual(acl_support.entries(fd), [])
        else:
            self.assertIsNone(acl_support.entries(fd))


if __name__ == "__main__":
    unittest.main()
