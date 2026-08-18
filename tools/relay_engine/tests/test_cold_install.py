import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from relay_engine.tests.test_e2e_chain import (
    PYTHON, RelayProcess, ruled_manifest, run_chain,
)
from relay_engine.tests.test_identity_matrix import cid_did
from relay_engine.version import KIT_VERSION, ROSTER, fingerprint


def _write_log(path, records):
    body = (json.dumps(records, sort_keys=True, separators=(",", ":")) +
            "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC |
                         os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("zero-progress cold-install log write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TestColdInstall(unittest.TestCase):
    def test_sanitized_install_runs_exact_e2e_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary)
            install_root = outside / "install"
            install_root.mkdir()
            manifest = ruled_manifest()
            destinations = set()
            for source, relative in manifest:
                self.assertNotIn(relative, destinations)
                destinations.add(relative)
                destination = install_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                shutil.copymode(source, destination)
                self.assertEqual(destination.read_bytes(), source.read_bytes())
                self.assertEqual(
                    os.path.commonpath((install_root.resolve(),
                                        destination.resolve())),
                    os.fspath(install_root.resolve()))
            self.assertTrue(set(ROSTER).issubset(destinations))
            self.assertFalse(any(
                part in {"tests", "fixtures", "__pycache__"}
                for destination in destinations
                for part in Path(destination).parts
            ))

            home = outside / "home"
            home.mkdir()
            supplied_results = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
            results_root = Path(supplied_results) if supplied_results else (
                outside / "results")
            results_root.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            sanitized = [
                "/usr/bin/env", "-i",
                "HOME=" + os.fspath(home),
                "PATH=" + os.fspath(Path(PYTHON).parent),
                "PYTHONPATH=",
                "RELAY_ENGINE_RESULTS_ROOT=" +
                os.fspath(results_root.resolve()),
            ]
            if "RELAY_ENGINE_MATRIX_RUN" in os.environ:
                sanitized.append("RELAY_ENGINE_MATRIX_RUN=" + os.environ[
                    "RELAY_ENGINE_MATRIX_RUN"])
            sanitized.append(PYTHON)

            probe = subprocess.run(
                sanitized + ["-c",
                 ("import os, pathlib, relay_engine; "
                  "root=pathlib.Path.cwd().resolve(); "
                  "assert pathlib.Path(relay_engine.__file__).resolve()"
                  ".is_relative_to(root); "
                  "assert os.environ['RELAY_ENGINE_RESULTS_ROOT']==%r") %
                 os.fspath(results_root.resolve())],
                cwd=install_root, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(probe.returncode, 0, probe.stderr)

            version = subprocess.run(
                sanitized + [install_root / "relay", "version"],
                cwd=install_root, env=environment, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(
                json.loads(version.stdout),
                {
                    "fingerprint": fingerprint(
                        Path(__file__).parents[2]),
                    "install": os.path.realpath(install_root),
                    "kit": KIT_VERSION,
                },
            )

            root = outside / "relay-root"
            root.mkdir()
            process = RelayProcess(
                sanitized + [install_root / "relay"], environment,
                install_root)
            cid, did = cid_did(install_root, install_root)
            result = run_chain(process, root, cid, did)
            self.assertEqual(result["bypass"]["origin"], "hand")
            self.assertTrue(all(record["status"] == 0
                                for record in process.log))
            _write_log(results_root / "v29-engine-cold-install.log", {
                "install_root": os.fspath(install_root),
                "manifest": sorted(destinations),
                "module_probe": probe.returncode,
                "commands": process.log,
                "status": "pass",
            })
            self.assertTrue(
                (results_root / "v29-engine-cold-install.log").is_file())


if __name__ == "__main__":
    unittest.main()
