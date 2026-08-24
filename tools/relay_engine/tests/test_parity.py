import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from relay_engine import rules
from relay_engine.envelope import REQUIRED_FIELDS


TOOLS = Path(__file__).parents[2]
REPO = TOOLS.parent
FIXTURES = TOOLS / "relay-engine-fixtures"
PARITY = FIXTURES / "parity"
MANIFEST = PARITY / "corpus-manifest.json"
GOLDEN = PARITY / "golden-verdicts.json"
SCRIPT = TOOLS / "relay-lint.py"


def _standalone():
    spec = importlib.util.spec_from_file_location(
        "v29_frozen_relay_lint", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _population():
    roots = [TOOLS / "relay-lint-fixtures", FIXTURES / "corpora"]
    return sorted(path for root in roots for path in root.rglob("*")
                  if path.is_file())


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verdict(result):
    return {"errors": sorted(result.errors),
            "warnings": sorted(result.warnings),
            "exit": 1 if result.errors else 0}


def _collect(module):
    single = {}
    for path in _population():
        if path.suffix == ".md":
            single[str(path.relative_to(REPO))] = _verdict(
                module.lint_file(path, freshness=False))
    root_paths = [TOOLS / "relay-lint-fixtures"]
    root_paths.extend(sorted(
        path for path in (FIXTURES / "corpora").iterdir()
        if path.is_dir()))
    roots = {str(path.relative_to(REPO)): _verdict(
        module.lint_relay_root(path)) for path in root_paths}
    indexes = {str(path.relative_to(REPO)): _verdict(
        module.lint_relay_index(path)) for path in _population()
        if path.name == "INDEX.md"}
    return {"single": single, "root": roots, "index": indexes}


def generate_artifacts():
    PARITY.mkdir(parents=True, exist_ok=True)
    files = {str(path.relative_to(REPO)): _digest(path)
             for path in _population()}
    manifest = {"count": len(files), "files": files}
    MANIFEST.write_text(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")) + "\n")
    golden = {
        "producer_sha256": _digest(SCRIPT),
        "manifest_sha256": _digest(MANIFEST),
        "verdicts": _collect(_standalone()),
    }
    GOLDEN.write_text(json.dumps(
        golden, sort_keys=True, separators=(",", ":")) + "\n")


class TestParity(unittest.TestCase):
    def test_manifest_is_complete_and_content_bound(self):
        manifest = json.loads(MANIFEST.read_text())
        actual = {str(path.relative_to(REPO)): _digest(path)
                  for path in _population()}
        self.assertTrue(actual)
        self.assertEqual(manifest, {"count": len(actual), "files": actual})

    def test_native_required_fields_are_submission_contract(self):
        self.assertEqual(tuple(rules.REQUIRED_FIELDS),
                         tuple(REQUIRED_FIELDS))

    def test_native_blocked_script_leg_matches_frozen_golden(self):
        source = Path(rules.__file__).read_text()
        self.assertNotIn("importlib", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("relay-lint.py", source)
        with tempfile.TemporaryDirectory() as isolated:
            self.assertFalse(Path(isolated, "relay-lint.py").exists())
            actual = _collect(rules)
        frozen = json.loads(GOLDEN.read_text())
        self.assertEqual(_digest(SCRIPT), frozen["producer_sha256"])
        self.assertEqual(_digest(MANIFEST), frozen["manifest_sha256"])
        self.assertEqual(actual, frozen["verdicts"])

    def test_live_secondary_comparison_all_modes(self):
        live = _collect(_standalone())
        native = _collect(rules)
        self.assertEqual(native, live)
        root = os.environ.get("RELAY_ENGINE_RESULTS_ROOT")
        if root:
            destination = Path(root, "v29-engine-parity-verdicts.json")
            destination.write_text(json.dumps({
                "manifest_sha256": _digest(MANIFEST),
                "producer_sha256": _digest(SCRIPT),
                "status": "PASS", "verdicts": native,
            }, sort_keys=True, separators=(",", ":")) + "\n")

    def test_live_secondary_comparison_explicit_order_inversion(self):
        source = TOOLS / "relay-lint-fixtures" / "t11closure" / \
            "R32-singleton-unstamped-candidate"
        with tempfile.TemporaryDirectory() as isolated:
            fixture = Path(isolated, "R32-singleton-unstamped-candidate")
            shutil.copytree(source, fixture)
            for stamp, path in enumerate(
                    sorted(fixture.glob("*.md"), reverse=True), start=1):
                os.utime(path, ns=(stamp, stamp))
            native = _verdict(rules.lint_relay_root(fixture))
            live = _verdict(_standalone().lint_relay_root(fixture))
        self.assertEqual(native, live)


if os.environ.get("RELAY_REGENERATE_PARITY") == "1":
    generate_artifacts()


if __name__ == "__main__":
    unittest.main()
