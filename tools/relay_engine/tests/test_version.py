import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from relay_engine.version import (
    FingerprintError,
    KIT_VERSION,
    ROSTER,
    fingerprint,
)


TOOLS = Path(__file__).parents[2]
REPO = TOOLS.parent
EXPECTED_ROSTER = (
    "relay",
    "relay_engine/README.md",
    "relay_engine/__init__.py",
    "relay_engine/cli.py",
    "relay_engine/client.py",
    "relay_engine/commission.py",
    "relay_engine/cycles.py",
    "relay_engine/daemon.py",
    "relay_engine/envelope.py",
    "relay_engine/errors.py",
    "relay_engine/jcs.py",
    "relay_engine/ledger.py",
    "relay_engine/migrate.py",
    "relay_engine/paths.py",
    "relay_engine/reconcile.py",
    "relay_engine/render.py",
    "relay_engine/rules.py",
    "relay_engine/seats.py",
    "relay_engine/strings.py",
    "relay_engine/supersede.py",
    "relay_engine/version.py",
)


def _stage_roster(destination):
    for relative in EXPECTED_ROSTER:
        source = TOOLS / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _independent_fingerprint(tools_dir):
    digest = hashlib.sha256()
    digest.update(b"adt-engine-fp/1\0")
    for relative in EXPECTED_ROSTER:
        content = (tools_dir / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _invoke_rules(tools_dir):
    environment = os.environ.copy()
    prior = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.fspath(tools_dir)
    if prior:
        environment["PYTHONPATH"] += os.pathsep + prior
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    return subprocess.run(
        [sys.executable, "-c", "import relay_engine.rules"],
        cwd=tools_dir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class TestVersion(unittest.TestCase):
    def assert_reason(self, tools_dir, reason):
        with self.assertRaises(FingerprintError) as raised:
            fingerprint(tools_dir)
        self.assertEqual(str(raised.exception), reason)

    def test_kit_version_and_roster_match_canonical_universe(self):
        actual = {"relay"}
        for path in (TOOLS / "relay_engine").rglob("*"):
            relative = path.relative_to(TOOLS)
            if "__pycache__" in relative.parts:
                continue
            if relative.parts[:2] == ("relay_engine", "tests"):
                continue
            if path.suffix == ".pyc":
                continue
            if path.is_file():
                actual.add(relative.as_posix())
        self.assertEqual(KIT_VERSION, "2.9.0")
        self.assertEqual(ROSTER, EXPECTED_ROSTER)
        self.assertEqual(set(ROSTER), actual)

    def test_staged_fingerprint_is_deterministic_and_canonically_framed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            _stage_roster(first)
            _stage_roster(second)
            first_fingerprint = fingerprint(first)
            self.assertRegex(first_fingerprint, r"^[0-9a-f]{64}$")
            self.assertEqual(first_fingerprint, fingerprint(second))
            self.assertEqual(
                first_fingerprint,
                _independent_fingerprint(first),
            )

    def test_copy_oracle_ignores_tests_and_cache_before_and_after_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared" / "tools"
            bundle = root / "bundle" / "tools"
            shutil.copytree(TOOLS, shared)
            _stage_roster(bundle)
            generated_copies = []
            for plugin in ("adt-pair", "adt-orchestrator", "adt-master"):
                generated = REPO / "plugins" / plugin / "tools"
                if (generated / "relay_engine" / "version.py").is_file():
                    generated_copies.append(generated)
            copies = [TOOLS, shared]
            if len(generated_copies) == 3:
                copies.extend(generated_copies)
            else:
                copies.append(bundle)

            before = []
            after = []
            for copy in copies:
                cache = copy / "relay_engine" / "__pycache__"
                cache.mkdir(exist_ok=True)
                (cache / "ignored.pyc").write_bytes(b"ignored cache")
                before.append(fingerprint(copy))
                invoked = _invoke_rules(copy)
                self.assertEqual(invoked.returncode, 0, invoked.stderr)
                after.append(fingerprint(copy))
            self.assertEqual(len(set(before + after)), 1)

    def test_extra_direct_and_nested_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct = root / "direct"
            nested = root / "nested"
            _stage_roster(direct)
            _stage_roster(nested)
            (direct / "relay_engine" / "evil.py").write_text("evil\n")
            (nested / "relay_engine" / "sub").mkdir()
            (nested / "relay_engine" / "sub" / "x.py").write_text("x\n")
            self.assert_reason(direct, "extra-member")
            self.assert_reason(nested, "extra-member")

    def test_symlink_fifo_missing_and_unreadable_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            symlinked = root / "symlinked"
            _stage_roster(symlinked)
            symlink_path = symlinked / "relay_engine" / "rules.py"
            symlink_path.unlink()
            symlink_path.symlink_to("README.md")
            self.assert_reason(symlinked, "non-regular-member")

            fifo = root / "fifo"
            _stage_roster(fifo)
            fifo_path = fifo / "relay_engine" / "rules.py"
            fifo_path.unlink()
            os.mkfifo(fifo_path)
            self.assert_reason(fifo, "non-regular-member")

            missing = root / "missing"
            _stage_roster(missing)
            (missing / "relay_engine" / "rules.py").unlink()
            self.assert_reason(missing, "missing-member")

            unreadable = root / "unreadable"
            _stage_roster(unreadable)
            unreadable_path = unreadable / "relay_engine" / "rules.py"
            unreadable_path.chmod(0)
            try:
                self.assert_reason(unreadable, "unreadable-member")
            finally:
                unreadable_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_member_changes_rename_addition_and_removal_are_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            mutated = root / "mutated"
            _stage_roster(mutated)
            original = fingerprint(mutated)
            with (mutated / "relay_engine" / "rules.py").open("ab") as member:
                member.write(b"\n# mutation\n")
            self.assertNotEqual(fingerprint(mutated), original)

            readme = root / "readme"
            _stage_roster(readme)
            original = fingerprint(readme)
            with (readme / "relay_engine" / "README.md").open("ab") as member:
                member.write(b"\nREADME mutation\n")
            self.assertNotEqual(fingerprint(readme), original)

            renamed = root / "renamed"
            _stage_roster(renamed)
            (renamed / "relay_engine" / "rules.py").rename(
                renamed / "relay_engine" / "renamed.py"
            )
            with self.assertRaises(FingerprintError):
                fingerprint(renamed)

            added = root / "added"
            _stage_roster(added)
            (added / "relay_engine" / "added.txt").write_text("added\n")
            self.assert_reason(added, "extra-member")

            removed = root / "removed"
            _stage_roster(removed)
            (removed / "relay_engine" / "rules.py").unlink()
            self.assert_reason(removed, "missing-member")


if __name__ == "__main__":
    unittest.main()
