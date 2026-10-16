"""Capture unsorted root diagnostics from the pinned fixture population."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

BASE = "cbc8f071cd593af4c48994ec30b0775b3c919ad6"
REPO = Path(__file__).resolve().parents[3]
FIXTURE_PATHS = ("tools/relay-lint-fixtures", "tools/relay-engine-fixtures")


def snapshot(destination):
    archive = subprocess.check_output(["git", "archive", BASE, *FIXTURE_PATHS], cwd=REPO)
    import io
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(destination, filter="data")


def root_list(root):
    paths = ["tools/relay-lint-fixtures"]
    for parent in ("tools/relay-engine-fixtures/corpora", "tools/relay-engine-fixtures/record-root"):
        paths.extend(str(p.relative_to(root)) for p in sorted((root / parent).iterdir()) if p.is_dir())
    return paths


def collect(module, root, paths):
    results = {}
    for name in paths:
        result = module.lint_relay_root(root / name)
        results[name] = {key: [msg.replace(str(root), "<fixture-root>") for msg in getattr(result, key)]
                         for key in ("errors", "warnings")}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rules", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("order_baseline_rules", args.rules)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        snapshot(root)
        paths = root_list(root)
        result = {"source_commit": BASE, "rules_sha256": hashlib.sha256(args.rules.read_bytes()).hexdigest(),
                  "roots": paths, "results": collect(module, root, paths)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
