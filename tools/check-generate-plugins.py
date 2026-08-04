#!/usr/bin/env python3
"""Behavioral checks for the canonical plugin generator."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "generate-plugins.py"
PLUGINS = {
    "adt-pair": ("pair-planner", "pair-implementer", "design-grill"),
    "adt-orchestrator": (
        "pair-planner",
        "pair-implementer",
        "design-grill",
        "orchestrator-planner",
        "orchestrator-reviewer",
        "sprint-doc-setup",
    ),
    "adt-master": (
        "pair-planner",
        "pair-implementer",
        "design-grill",
        "orchestrator-planner",
        "orchestrator-reviewer",
        "sprint-doc-setup",
        "domain-planner",
        "domain-reviewer",
        "master-planner",
        "master-reviewer",
    ),
}
PLUGIN_METADATA = {
    "adt-pair": {
        "author": "Jack Li",
        "description": "Pair-tier planning and implementation skills for governed software work.",
        "keywords": ["agents", "pair", "planning", "review", "relays"],
        "license": "Apache-2.0",
        "name": "adt-pair",
        "version": "2.9.0",
    },
    "adt-orchestrator": {
        "author": "Jack Li",
        "description": "Nested pair and orchestration skills for governed software work.",
        "keywords": ["agents", "orchestration", "planning", "review", "relays"],
        "license": "Apache-2.0",
        "name": "adt-orchestrator",
        "version": "2.9.0",
    },
    "adt-master": {
        "author": "Jack Li",
        "description": "Nested pair, orchestration, domain, and master planning skills.",
        "keywords": ["agents", "domain", "master", "orchestration", "relays"],
        "license": "Apache-2.0",
        "name": "adt-master",
        "version": "2.9.0",
    },
}
SHARED_RECIPIENTS = {
    "protocol.md": set().union(*PLUGINS.values()),
    "review-panels.md": {"pair-planner", "orchestrator-planner"},
    "reviewer-spawn-prompts.md": {"pair-planner", "orchestrator-planner"},
    "design-request-template.md": {"pair-planner", "orchestrator-planner"},
    "master-tier-boundary.md": {
        "domain-planner",
        "domain-reviewer",
        "master-planner",
        "master-reviewer",
    },
}


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def run_generator_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "tools" / "generate-plugins.py"), "--check"],
        cwd=root,
        text=True,
        capture_output=True,
    )


def expected_source(path: str) -> str:
    parts = path.split("/")
    if parts[1:3] == [".claude-plugin", "plugin.json"]:
        return "tools/generate-plugins.py"
    if parts[1] == "skills":
        skill, remainder = parts[2], parts[3:]
        if len(remainder) == 1 and skill in SHARED_RECIPIENTS.get(remainder[0], set()):
            return f"shared/{remainder[0]}"
        return "skills/" + skill + "/" + "/".join(remainder)
    if parts[1] == "tools":
        return "/".join(parts[1:])
    if parts[1] == "vendor":
        return "/".join(parts[1:])
    if parts[1] == "LICENSE":
        return "LICENSE"
    if parts[1] == "LICENSES":
        return "/".join(parts[1:])
    raise AssertionError(f"unexpected generated path: {path}")


def copy_check_fixture(destination: Path) -> Path:
    fixture = destination / "repo"
    fixture.mkdir()
    for name in ("skills", "shared", "tools", "vendor", "LICENSE", "LICENSES", "plugins"):
        source = ROOT / name
        target = fixture / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return fixture


def expect_drift(result: subprocess.CompletedProcess[str], category: str, path: str) -> None:
    output = result.stdout + result.stderr
    expect(result.returncode == 1, f"{category}-path drift did not exit 1: {output}")
    expect(f"{category}: {path}" in output, f"{category}-path drift was not reported: {output}")


def check_drift_reporting() -> None:
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-") as temporary:
        fixture = copy_check_fixture(Path(temporary))
        missing_path = "adt-pair/skills/pair-planner/SKILL.md"
        missing_target = fixture / "plugins" / missing_path
        missing_target.unlink()
        expect_drift(run_generator_check(fixture), "missing", missing_path)

        extra_path = "adt-pair/extra-plugin-file.txt"
        (fixture / "plugins" / extra_path).write_text("extra\n", encoding="utf-8")
        expect_drift(run_generator_check(fixture), "extra", extra_path)

        differing_path = "adt-pair/skills/pair-planner/protocol.md"
        differing_target = fixture / "plugins" / differing_path
        differing_target.write_bytes(differing_target.read_bytes() + b"\n")
        expect_drift(run_generator_check(fixture), "different", differing_path)


def main() -> int:
    if not GENERATOR.is_file():
        fail("generate-plugins.py not found")

    result = run_generator_check(ROOT)
    expect(result.returncode == 0, result.stdout + result.stderr)

    plugins_root = ROOT / "plugins"
    expected_root_entries = set(PLUGINS) | {"PROVENANCE.json"}
    expect(
        {path.name for path in plugins_root.iterdir()} == expected_root_entries,
        "plugins/ root inventory differs from the three generated bundles and manifest",
    )

    for plugin, skills in PLUGINS.items():
        plugin_root = plugins_root / plugin
        manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
        manifest_text = manifest_path.read_text(encoding="utf-8")
        expect(not manifest_text.startswith("<!-- GENERATED"), f"{manifest_path} has a banner")
        manifest = json.loads(manifest_text)
        expected_manifest = PLUGIN_METADATA[plugin]
        expect(set(manifest) == set(expected_manifest), f"{manifest_path} has the wrong schema")
        expect(manifest == expected_manifest, f"{manifest_path} has the wrong metadata")
        expect(
            manifest_text == json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n",
            f"{manifest_path} is not sorted-key JSON with one trailing newline",
        )
        expect(
            "provisional" not in manifest["description"].lower(),
            f"{manifest_path} carries a provisional disclosure",
        )
        expect(
            {path.name for path in (plugin_root / "skills").iterdir()} == set(skills),
            f"{plugin} skill inventory is wrong",
        )
        for skill in skills:
            skill_file = plugin_root / "skills" / skill / "SKILL.md"
            expect(skill_file.is_file(), f"{skill_file} is missing")
            expect(
                skill_file.read_text(encoding="utf-8").startswith("<!-- GENERATED by tools/generate-plugins.py from skills/"),
                f"{skill_file} lacks its markdown provenance banner",
            )

    protocol = plugins_root / "adt-pair" / "skills" / "pair-planner" / "protocol.md"
    expect(protocol.is_file(), f"{protocol} is missing")
    expect(
        protocol.read_text(encoding="utf-8").startswith(
            "<!-- GENERATED by tools/generate-plugins.py from shared/protocol.md — kit v2.9.0. Do not edit; edit the canonical source. -->\n\n"
        ),
        f"{protocol} has the wrong banner",
    )

    provenance_path = plugins_root / "PROVENANCE.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    paths = [entry["path"] for entry in provenance]
    expect(paths == sorted(paths), "PROVENANCE.json paths are not sorted")
    expect(
        [(entry["path"], entry["source"]) for entry in provenance]
        == sorted((entry["path"], entry["source"]) for entry in provenance),
        "PROVENANCE.json path/source pairs are not ordered",
    )
    expect("PROVENANCE.json" not in paths, "PROVENANCE.json hashes itself")
    generated_paths = sorted(
        path.relative_to(plugins_root).as_posix()
        for path in plugins_root.rglob("*")
        if path.is_file() and path != provenance_path
    )
    expect(paths == generated_paths, "PROVENANCE.json does not inventory every generated file")
    for entry in provenance:
        generated = plugins_root / entry["path"]
        expect(
            set(entry) == {"path", "source", "sha256", "kit_version"},
            f"{entry['path']} has the wrong provenance schema",
        )
        expect(
            entry["source"] == expected_source(entry["path"]),
            f"{entry['path']} has the wrong canonical source",
        )
        expect(
            (ROOT / entry["source"]).is_file(),
            f"{entry['path']} source does not resolve to a canonical file",
        )
        expect(entry["kit_version"] == "2.9.0", f"{entry['path']} has the wrong kit version")
        expect(
            entry["sha256"] == hashlib.sha256(generated.read_bytes()).hexdigest(),
            f"{entry['path']} has the wrong sha256",
        )
    check_drift_reporting()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
