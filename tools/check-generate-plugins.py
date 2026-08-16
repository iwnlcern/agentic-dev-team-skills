#!/usr/bin/env python3
"""Behavioral acceptance checks for the canonical plugin generator."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "generate-plugins.py"
PLUGINS_ROOT = ROOT / "plugins"
PYTHON = "python3"
KIT_VERSION = "2.9.0"
MANIFEST_PATH = "PROVENANCE.json"

PLUGINS = {
    "adt-pair": {"pair-planner", "pair-implementer", "design-grill", "sprint-doc-setup"},
    "adt-orchestrator": {
        "pair-planner",
        "pair-implementer",
        "design-grill",
        "orchestrator-planner",
        "orchestrator-reviewer",
        "sprint-doc-setup",
    },
    "adt-master": {
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
    },
}
ROLE_SKILLS = {
    "pair-planner",
    "pair-implementer",
    "orchestrator-planner",
    "orchestrator-reviewer",
    "master-planner",
    "master-reviewer",
    "domain-planner",
    "domain-reviewer",
}
REVIEW_ASSETS = {
    "review-panels.md",
    "reviewer-spawn-prompts.md",
    "design-request-template.md",
}
REVIEW_SKILLS = {"pair-planner", "orchestrator-planner"}
MASTER_SKILLS = {"domain-planner", "domain-reviewer", "master-planner", "master-reviewer"}
LOCKED_SHARED_ASSETS = {
    "protocol.md": frozenset(ROLE_SKILLS),
    "harness-codex.md": frozenset(ROLE_SKILLS),
    "review-panels.md": frozenset({"pair-planner", "orchestrator-planner"}),
    "reviewer-spawn-prompts.md": frozenset({"pair-planner", "orchestrator-planner"}),
    "design-request-template.md": frozenset({"pair-planner", "orchestrator-planner"}),
    "master-tier-boundary.md": frozenset(MASTER_SKILLS),
}

HOST_ADAPTATION_TEXT = """## Host adaptation

Claude Code is the default host and has no adaptation file.
If your harness is something else, read the matching `harness-<host>.md` beside this protocol before acting — `harness-codex.md` for OpenAI Codex.
Model-specific files (`model-<family>.md`) follow the same pattern and ship empty: one may be added only when it cites a reproducible failure, and it is deleted when the failure stops reproducing.
Host and model files map protocol mechanics onto a host; they never change the protocol, and on conflict the protocol wins.
"""

CODEX_INSTALL_TEXT = """- Codex (and any `.agents/skills` host): copy `plugins/adt-<tier>/skills/*` into `.agents/skills/` (project) or `~/.agents/skills/` (user), and `plugins/adt-<tier>/tools/` to `~/.agents/skills/tools/`; `~/.codex/skills` remains a working deprecated destination.
  Manual copy is the deliberate v2.9 distribution choice for this host, and it needs no manifest (filesystem discovery: https://learn.chatgpt.com/docs/build-skills).
  Codex plugin distribution does exist and is what current first-party guidance recommends for reusable distribution (https://developers.openai.com/codex/skills); it remains out of scope for v2.9 by operator decision — no request recorded — not because the host lacks the route.
"""

PDC_CODEX_FORWARD_POINTER = (
    "For Codex, `.agents/skills` is the current convention; "
    "`~/.codex/skills` remains a working deprecated destination."
)
LOCKED_TOOLS_SET = (
    "relay-lint.py",
    "check-relay-lint-fixtures.py",
    "check-timestamp-drift.py",
    "relay-lint-fixtures",
    "adapters",
)
EXACT_INVENTORY_SENTINELS = {
    "adt-orchestrator": {
        "skills/orchestrator-planner/handoff-templates.md",
        "skills/orchestrator-planner/orchestration-moves.md",
        "skills/orchestrator-planner/sitrep-reconciliation.md",
        "tools/check-relay-lint-fixtures.py",
        "tools/check-timestamp-drift.py",
    },
    "adt-master": {
        "skills/orchestrator-planner/handoff-templates.md",
        "skills/orchestrator-planner/orchestration-moves.md",
        "skills/orchestrator-planner/sitrep-reconciliation.md",
        "skills/master-planner/charter-template.md",
        "tools/check-relay-lint-fixtures.py",
        "tools/check-timestamp-drift.py",
    },
}
VOCABULARY = (
    ("Master Planner", "t-x.master-planner"),
    ("Master Reviewer", "t-x.master-reviewer"),
    ("Domain Planner", "t-x.domain-planner"),
    ("Domain Reviewer", "t-x.domain-reviewer"),
    ("Pair Planner", "t-x.pair-planner"),
    ("Pair Implementer", "t-x.pair-implementer"),
    ("Planner", "t-x.planner"),
)
RETIRED_M01_COMPOUND_PLACEHOLDERS = (
    "<id/hash/title>",
    "<design id/title to create>",
    "<design id/title/hash",
    "<approved design id/title/hash>",
    "<approved design doc id/hash",
    "<plan id/title to create>",
    "<locked plan id/title/hash>",
)


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stdout + result.stderr).strip()


def run_generator(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, str(root / "tools" / "generate-plugins.py"), *arguments],
        cwd=root,
        text=True,
        capture_output=True,
    )


def expect_success(result: subprocess.CompletedProcess[str], context: str) -> None:
    expect(result.returncode == 0, f"{context} exited {result.returncode}: {output(result)}")


def expect_drift(result: subprocess.CompletedProcess[str], path: str) -> None:
    expect(result.returncode == 1, f"drift for {path} exited {result.returncode}: {output(result)}")
    expect(f"different: {path}" in output(result), f"drift for {path} was not reported: {output(result)}")


def file_map(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def check_retired_m01_compound_placeholders() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "shared", "skills", "plugins"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")
    hits: list[str] = []
    for raw_relative in tracked:
        if not raw_relative:
            continue
        relative = Path(os.fsdecode(raw_relative))
        parts = relative.parts
        in_shipped_surface = (
            parts[0] in {"shared", "skills"}
            or (len(parts) >= 3 and parts[0] == "plugins" and parts[2] == "skills")
        )
        if not in_shipped_surface:
            continue
        for line_number, line in enumerate((ROOT / relative).read_text(encoding="utf-8").splitlines(), 1):
            for pattern in RETIRED_M01_COMPOUND_PLACEHOLDERS:
                if pattern in line:
                    hits.append(f"{relative.as_posix()}:{line_number}: {pattern}")
    expect(
        not hits,
        "retired M01 compound placeholders found:\n" + "\n".join(hits),
    )


def canonical_files(source: Path, destination: str) -> set[str]:
    expect(source.exists(), f"canonical input is missing: {source.relative_to(ROOT)}")
    if source.is_file():
        return {destination}
    return {
        (Path(destination) / path.relative_to(source)).as_posix()
        for path in source.rglob("*")
        if path.is_file()
    }


def canonical_source_map() -> dict[str, str]:
    """Derive every generated path-to-source edge without importing the generator."""
    sources: dict[str, str] = {}

    def record(plugin: str, source: Path, destination: Path) -> None:
        generated = (Path(plugin) / destination).as_posix()
        canonical = source.relative_to(ROOT).as_posix()
        expect(generated not in sources, f"duplicate independently derived provenance path: {generated}")
        sources[generated] = canonical

    def record_tree(plugin: str, source: Path, destination: Path) -> None:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                record(plugin, path, destination / path.relative_to(source))

    for plugin, skills in PLUGINS.items():
        for skill in skills:
            record_tree(plugin, ROOT / "skills" / skill, Path("skills") / skill)
        for asset, recipients in LOCKED_SHARED_ASSETS.items():
            for skill in skills & recipients:
                record(plugin, ROOT / "shared" / asset, Path("skills") / skill / asset)
        for tool in LOCKED_TOOLS_SET:
            source = ROOT / "tools" / tool
            destination = Path("tools") / tool
            if source.is_dir():
                record_tree(plugin, source, destination)
            else:
                record(plugin, source, destination)
        record_tree(plugin, ROOT / "vendor", Path("vendor"))
        record(plugin, ROOT / "LICENSE", Path("LICENSE"))
        record_tree(plugin, ROOT / "LICENSES", Path("LICENSES"))
        sources[f"{plugin}/.claude-plugin/plugin.json"] = "tools/generate-plugins.py"
    return sources


def expected_plugin_files(plugin: str) -> set[str]:
    """Derive a plugin's locked file inventory from canonical inputs, not the generator."""
    expected = {".claude-plugin/plugin.json"}
    skills = PLUGINS[plugin]
    for skill in skills:
        expected |= canonical_files(ROOT / "skills" / skill, f"skills/{skill}")
    for asset, recipients in LOCKED_SHARED_ASSETS.items():
        for skill in skills & recipients:
            source = ROOT / "shared" / asset
            expect(source.is_file(), f"canonical shared asset is missing: shared/{asset}")
            expected.add(f"skills/{skill}/{asset}")
    for tool in LOCKED_TOOLS_SET:
        expected |= canonical_files(ROOT / "tools" / tool, f"tools/{tool}")
    expected |= canonical_files(ROOT / "vendor" / "mattpocock", "vendor/mattpocock")
    expected |= canonical_files(ROOT / "LICENSE", "LICENSE")
    expected |= canonical_files(ROOT / "LICENSES", "LICENSES")
    return expected


def assert_exact_plugin_file_inventory(plugin: str, plugin_root: Path) -> None:
    expected = expected_plugin_files(plugin)
    sentinels = EXACT_INVENTORY_SENTINELS.get(plugin, set())
    expect(sentinels <= expected, f"{plugin} expected inventory omits sentinels: {sorted(sentinels - expected)}")
    actual = {
        path.relative_to(plugin_root).as_posix()
        for path in plugin_root.rglob("*")
        if path.is_file()
    }
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    expect(
        not missing and not extra,
        f"{plugin} exact file inventory differs: missing: {missing}; extra: {extra}",
    )


def copy_generator_inputs(destination: Path) -> Path:
    destination.mkdir(exist_ok=True)
    fixture = destination / "repo"
    fixture.mkdir()
    for name in ("skills", "shared", "tools", "vendor", "LICENSE", "LICENSES"):
        source = ROOT / name
        target = fixture / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)
    return fixture


def copy_repository_fixture(destination: Path) -> Path:
    fixture = copy_generator_inputs(destination)
    shutil.copytree(PLUGINS_ROOT, fixture / "plugins")
    return fixture


def check_determinism() -> None:
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-") as temporary:
        temp_root = Path(temporary)
        first = copy_generator_inputs(temp_root / "first")
        second = copy_generator_inputs(temp_root / "second")
        expect_success(run_generator(first), "first generation")
        expect_success(run_generator(second), "second generation")
        expect(file_map(first / "plugins") == file_map(second / "plugins"), "two isolated generations differ")


def check_clean() -> None:
    expect_success(run_generator(ROOT, "--check"), "clean --check")


def check_manifest_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-") as temporary:
        fixture = copy_repository_fixture(Path(temporary))
        manifest = fixture / "plugins" / "PROVENANCE.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        expect_drift(run_generator(fixture, "--check"), "PROVENANCE.json")
        expect_success(run_generator(fixture), "manifest mutation regeneration")
        expect_success(run_generator(fixture, "--check"), "manifest mutation regenerated --check")


def check_banner_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-") as temporary:
        fixture = copy_repository_fixture(Path(temporary))
        relative = "adt-pair/skills/pair-planner/SKILL.md"
        generated = fixture / "plugins" / relative
        text = generated.read_text(encoding="utf-8")
        expect(text.startswith("---\n"), "target frontmatter is not at byte zero")
        closing = text.find("\n---\n", 4)
        expect(closing >= 0, "target lacks a closing frontmatter delimiter")
        banner_position = text.find("<!-- GENERATED by tools/generate-plugins.py", closing)
        expect(banner_position > closing, "target lacks a generated banner after frontmatter")
        generated.write_text(text.replace("kit v2.9.0", "kit v0.0.0", 1), encoding="utf-8")
        expect_drift(run_generator(fixture, "--check"), relative)


def check_body_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-") as temporary:
        fixture = copy_repository_fixture(Path(temporary))
        relative = "adt-pair/skills/pair-planner/SKILL.md"
        generated = fixture / "plugins" / relative
        text = generated.read_text(encoding="utf-8")
        expect("name: pair-planner" in text, "target lacks expected generated skill body")
        generated.write_text(text.replace("name: pair-planner", "name: altered-planner", 1), encoding="utf-8")
        expect_drift(run_generator(fixture, "--check"), relative)


def check_strict_json() -> None:
    json_files = sorted(PLUGINS_ROOT.rglob("*.json"))
    expect(json_files, "no generated JSON files found")
    for path in json_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise AssertionError(f"invalid JSON in {path.relative_to(ROOT)}: {error}") from error
        if path.name == "plugin.json":
            expect(
                set(payload) == {"author", "description", "keywords", "license", "name", "version"},
                f"plugin manifest schema differs in {path.relative_to(ROOT)}",
            )
            expect(
                payload["author"] == {"name": "Jack Li"},
                f"plugin manifest author differs in {path.relative_to(ROOT)}: {payload['author']!r}",
            )


def frontmatter_name(path: Path) -> str:
    data = path.read_bytes()
    expect(data.startswith(b"---\n"), f"{path.relative_to(ROOT)} does not begin with YAML frontmatter")
    closing = data.find(b"\n---\n", 4)
    expect(closing >= 0, f"{path.relative_to(ROOT)} has no closing YAML frontmatter delimiter")
    block = data[4:closing].decode("utf-8")
    expect("\n\n" not in block, f"{path.relative_to(ROOT)} frontmatter contains a blank line")
    fields: dict[str, str] = {}
    for line in block.splitlines():
        key, separator, value = line.partition(":")
        expect(bool(separator), f"{path.relative_to(ROOT)} frontmatter line lacks a colon: {line!r}")
        key = key.strip()
        expect(bool(key), f"{path.relative_to(ROOT)} frontmatter contains an empty key")
        expect(key not in fields, f"{path.relative_to(ROOT)} frontmatter repeats key {key!r}")
        fields[key] = value.strip().strip("'\"")
    expect(fields.get("name"), f"{path.relative_to(ROOT)} frontmatter has no name")
    expect(fields.get("description"), f"{path.relative_to(ROOT)} frontmatter has no description")
    return fields["name"]


def check_skill_frontmatter() -> None:
    canonical_skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
    installed_skill_files = sorted(PLUGINS_ROOT.glob("*/**/SKILL.md"))
    expect(canonical_skill_files, "no canonical SKILL.md files found")
    expect(installed_skill_files, "no installed SKILL.md files found")
    for path in canonical_skill_files + installed_skill_files:
        name = frontmatter_name(path)
        expect(
            name == path.parent.name,
            f"{path.relative_to(ROOT)} frontmatter name {name!r} does not match directory {path.parent.name!r}",
        )


def expected_bannered_markdown(source: str, data: bytes) -> bytes:
    banner = (
        f"<!-- GENERATED by tools/generate-plugins.py from {source} — kit v{KIT_VERSION}. "
        "Do not edit; edit the canonical source. -->\n\n"
    ).encode("utf-8")
    return banner + data


def check_r1_harness_copies() -> None:
    harness = ROOT / "shared" / "harness-codex.md"
    expect(harness.is_file(), "R1 canonical shared/harness-codex.md is missing")
    expected_harness = expected_bannered_markdown("shared/harness-codex.md", harness.read_bytes())
    for plugin, skills in PLUGINS.items():
        for skill in skills & ROLE_SKILLS:
            installed = PLUGINS_ROOT / plugin / "skills" / skill / "harness-codex.md"
            expect(installed.is_file(), f"R1 {plugin}/{skill} lacks harness-codex.md")
            expect(
                installed.read_bytes() == expected_harness,
                f"R1 {plugin}/{skill} harness-codex.md differs from canonical bannered bytes",
            )
        for skill in skills - ROLE_SKILLS:
            installed = PLUGINS_ROOT / plugin / "skills" / skill / "harness-codex.md"
            expect(not installed.exists(), f"R1 {plugin}/{skill} unexpectedly carries harness-codex.md")


def check_r3_protocol_content() -> None:
    protocol = (ROOT / "shared" / "protocol.md").read_text(encoding="utf-8")
    expect(protocol.count(HOST_ADAPTATION_TEXT) == 1, "R3 protocol lacks the exact host-adaptation section")


def check_r4_readme_content() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expect(readme.count(CODEX_INSTALL_TEXT) == 1, "R4 README lacks the exact Codex install text")


def check_r5_pdc_pointer() -> None:
    pdc = (
        ROOT
        / "docs/sprints/2026-08-03-v2.9-release/results/v29-split-rename-sitrep-pdc.md"
    ).read_text(encoding="utf-8")
    expect(pdc.count(PDC_CODEX_FORWARD_POINTER) == 1, "R5 pdc template lacks the Codex forward pointer")


def validate_provenance(entries: object, plugins_root: Path) -> None:
    expect(isinstance(entries, list), "PROVENANCE.json is not an array")
    expected_sources = canonical_source_map()
    on_disk = {
        path.relative_to(plugins_root).as_posix()
        for path in plugins_root.rglob("*")
        if path.is_file() and path.relative_to(plugins_root).as_posix() != MANIFEST_PATH
    }
    paths: list[str] = []
    for entry in entries:
        expect(isinstance(entry, dict), f"provenance entry is not an object: {entry!r}")
        expect(
            set(entry) == {"kit_version", "path", "sha256", "source"},
            f"provenance entry keys differ: {entry!r}",
        )
        path = entry["path"]
        expect(isinstance(path, str), f"provenance path is not a string: {path!r}")
        expect(path != MANIFEST_PATH, "PROVENANCE.json records itself")
        paths.append(path)
        expect(path in expected_sources, f"provenance path is not independently derivable: {path}")
        expect(entry["source"] == expected_sources[path], f"provenance source differs for {path}")
        expect(entry["kit_version"] == KIT_VERSION, f"provenance kit version differs for {path}")
        generated = plugins_root / path
        expect(generated.is_file(), f"provenance path is absent on disk: {path}")
        digest = hashlib.sha256(generated.read_bytes()).hexdigest()
        expect(entry["sha256"] == digest, f"provenance hash differs for {path}")
    expect(len(paths) == len(set(paths)), "PROVENANCE.json contains duplicate paths")
    expect(set(paths) == on_disk, "provenance paths differ from on-disk files minus the manifest")
    expect(set(paths) == set(expected_sources), "provenance paths differ from independently derived inputs")


def check_provenance() -> None:
    manifest = PLUGINS_ROOT / MANIFEST_PATH
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    validate_provenance(entries, PLUGINS_ROOT)

    sabotaged = [dict(entry) for entry in entries]
    expect(sabotaged, "cannot run provenance sabotage against an empty manifest")
    sabotaged[0]["sha256"] = "0" * 64
    try:
        validate_provenance(sabotaged, PLUGINS_ROOT)
    except AssertionError as error:
        expect("provenance hash differs" in str(error), f"hash sabotage failed for the wrong reason: {error}")
    else:
        raise AssertionError("provenance hash sabotage was not rejected")


def check_verbatim_copies() -> None:
    for plugin in PLUGINS:
        plugin_root = PLUGINS_ROOT / plugin
        for tool in LOCKED_TOOLS_SET:
            canonical = ROOT / "tools" / tool
            installed = plugin_root / "tools" / tool
            if canonical.is_dir():
                expect(file_map(canonical) == file_map(installed), f"{plugin} tool tree differs byte-for-byte: {tool}")
            else:
                expect(canonical.read_bytes() == installed.read_bytes(), f"{plugin} tool differs byte-for-byte: {tool}")
        expect(
            file_map(ROOT / "vendor") == file_map(plugin_root / "vendor"),
            f"{plugin} vendor tree is not copied byte-for-byte",
        )


def check_inventory() -> None:
    actual_plugins = {path.name for path in PLUGINS_ROOT.iterdir() if path.is_dir()}
    expect(actual_plugins == set(PLUGINS), f"plugin inventory differs: expected {set(PLUGINS)}, got {actual_plugins}")
    for plugin, expected_skills in PLUGINS.items():
        actual_skills = {path.name for path in (PLUGINS_ROOT / plugin / "skills").iterdir() if path.is_dir()}
        expect(
            actual_skills == expected_skills,
            f"{plugin} skill inventory differs: missing {expected_skills - actual_skills}, extra {actual_skills - expected_skills}",
        )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected_rows = (
        "| `adt-pair` | `pair-planner`, `pair-implementer`, `design-grill`, `sprint-doc-setup` |",
        "| `adt-orchestrator` | Everything in `adt-pair`, plus `orchestrator-planner`, `orchestrator-reviewer` |",
        "| `adt-master` | Everything in `adt-orchestrator`, plus `domain-planner`, `domain-reviewer`, `master-planner`, `master-reviewer` |",
    )
    for row in expected_rows:
        expect(readme.count(row) == 1, f"README lacks exact locked inventory row: {row}")


def check_adjacency() -> None:
    for plugin, skills in PLUGINS.items():
        plugin_root = PLUGINS_ROOT / plugin
        assert_exact_plugin_file_inventory(plugin, plugin_root)
        for skill in skills:
            skill_root = plugin_root / "skills" / skill
            if skill in ROLE_SKILLS:
                expect((skill_root / "protocol.md").is_file(), f"{plugin}/{skill} lacks protocol.md")
            else:
                expect(not (skill_root / "protocol.md").exists(), f"{plugin}/{skill} unexpectedly carries protocol.md")
            if skill in REVIEW_SKILLS:
                for asset in REVIEW_ASSETS:
                    expect((skill_root / asset).is_file(), f"{plugin}/{skill} lacks {asset}")
            if skill in MASTER_SKILLS:
                expect(plugin == "adt-master", f"{plugin}/{skill} is a master-only skill outside adt-master")
                expect((skill_root / "master-tier-boundary.md").is_file(), f"{plugin}/{skill} lacks master-tier-boundary.md")
        for required in (
            "tools/relay-lint.py",
            "vendor/mattpocock/grill-me/SKILL.md",
            "LICENSE",
        ):
            expect((plugin_root / required).is_file(), f"{plugin} lacks file {required}")
        for required in ("tools/relay-lint-fixtures", "tools/adapters", "LICENSES"):
            expect((plugin_root / required).is_dir(), f"{plugin} lacks directory {required}")
    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-exact-inventory-") as temporary:
        fixture = Path(temporary) / "adt-orchestrator"
        missing = "skills/orchestrator-planner/handoff-templates.md"
        shutil.copytree(PLUGINS_ROOT / "adt-orchestrator", fixture)
        (fixture / missing).unlink()
        try:
            assert_exact_plugin_file_inventory("adt-orchestrator", fixture)
        except AssertionError as error:
            expect(f"missing: ['{missing}']" in str(error), f"omitted inventory file was not diagnosed: {error}")
        else:
            raise AssertionError("exact inventory did not fail for a missing non-adjacent skill asset")


def install_tier(plugin: str, scratch_base: Path) -> Path:
    scratch = scratch_base / plugin
    plugin_root = PLUGINS_ROOT / plugin
    shutil.copytree(plugin_root / "skills", scratch / "skills")
    shutil.copytree(plugin_root / "tools", scratch / "tools")
    shutil.copytree(plugin_root / "vendor", scratch / "vendor")
    shutil.copytree(plugin_root / "LICENSES", scratch / "LICENSES")
    shutil.copy2(plugin_root / "LICENSE", scratch / "LICENSE")
    return scratch


def run_linter(linter: Path, relay: Path, empty_cwd: Path, *, no_freshness: bool = False) -> subprocess.CompletedProcess[str]:
    arguments = [PYTHON, str(linter), str(relay)]
    if no_freshness:
        arguments.append("--no-freshness")
    return subprocess.run(arguments, cwd=empty_cwd, text=True, capture_output=True)


def check_cold_install(scratch_base: Path) -> dict[str, Path]:
    installations: dict[str, Path] = {}
    for plugin, skills in PLUGINS.items():
        scratch = install_tier(plugin, scratch_base)
        installations[plugin] = scratch
        for skill in skills:
            skill_root = scratch / "skills" / skill
            if skill in ROLE_SKILLS:
                expect((skill_root / "protocol.md").is_file(), f"{plugin} cold install lacks {skill}/protocol.md")
            else:
                expect(not (skill_root / "protocol.md").exists(), f"{plugin} cold install gives {skill} protocol.md")
            if skill in REVIEW_SKILLS:
                for asset in REVIEW_ASSETS:
                    expect((skill_root / asset).is_file(), f"{plugin} cold install lacks {skill}/{asset}")
            if skill in MASTER_SKILLS:
                expect((skill_root / "master-tier-boundary.md").is_file(), f"{plugin} cold install lacks {skill}/master-tier-boundary.md")
        expect((scratch / "vendor" / "mattpocock" / "grill-me" / "SKILL.md").is_file(), f"{plugin} cold install lacks vendored grill-me")
        expect((scratch / "LICENSES" / "mattpocock-skills-MIT.txt").is_file(), f"{plugin} cold install lacks vendored MIT license")
        expect((scratch / "LICENSE").is_file(), f"{plugin} cold install lacks kit license")
        linter = scratch / "tools" / "relay-lint.py"
        fixture = scratch / "tools" / "relay-lint-fixtures" / "claude" / "A1-valid-audit.md"
        expect(linter.is_file(), f"{plugin} cold install lacks tools/relay-lint.py")
        expect(fixture.is_file(), f"{plugin} cold install lacks claude/A1-valid-audit.md")
        empty_cwd = scratch_base / f"empty-cwd-{plugin}"
        empty_cwd.mkdir()
        expect_success(run_linter(linter, fixture, empty_cwd, no_freshness=True), f"{plugin} cold-install linter")
        self_test = subprocess.run(
            [PYTHON, str(scratch / "tools" / "check-relay-lint-fixtures.py")],
            cwd=empty_cwd,
            text=True,
            capture_output=True,
        )
        expect_success(self_test, f"{plugin} shipped relay-fixture self-test")
    return installations


def relay_text(role: str, address: str) -> str:
    return "\n".join(
        (
            f"ROLE: {role}",
            "PHASE: AUDIT",
            "AUTHORITY: read-only",
            "DISPATCH_ID: task-7-vocabulary",
            "CEREMONY_TIER: small",
            "EVIDENCE_TARGET: E1",
            "HUMAN_GATE_REQUIRED: no",
            f"FROM: {address}",
            "TO: operator",
            "",
            "FINAL_GIT_STATUS_SHORT: none — clean tree",
            "",
        )
    )


def check_protocol_linter_coherence(installations: dict[str, Path], scratch_base: Path) -> None:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    for plugin, scratch in installations.items():
        for skill in PLUGINS[plugin] & ROLE_SKILLS:
            protocol = (scratch / "skills" / skill / "protocol.md").read_text(encoding="utf-8")
            for role, address in VOCABULARY:
                suffix = address.rsplit(".", 1)[1]
                expect(role in protocol, f"{plugin} {skill} protocol lacks ROLE vocabulary {role}")
                expect(f"`{suffix}`" in protocol, f"{plugin} {skill} protocol lacks address vocabulary {suffix}")
        relay_root = scratch / "acceptance-relays"
        relay_root.mkdir()
        empty_cwd = scratch_base / f"coherence-empty-cwd-{plugin}"
        empty_cwd.mkdir()
        linter = scratch / "tools" / "relay-lint.py"
        for role, address in VOCABULARY:
            filename_role = role.lower().replace(" ", "-")
            relay = relay_root / f"AUDIT-{filename_role}-{stamp}.md"
            relay.write_text(relay_text(role, address), encoding="utf-8")
            expect_success(run_linter(linter, relay, empty_cwd), f"{plugin} vocabulary {role}/{address}")


def check_no_json_banner() -> None:
    for path in sorted(PLUGINS_ROOT.rglob("*.json")):
        first = path.read_bytes()[:1]
        expect(first not in {b"<", b"#"}, f"comment banner found in {path.relative_to(ROOT)}")


def check_claude_plugin_validation() -> None:
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    expect(bool(claude), "Claude Code validator not found; set CLAUDE_BIN to its executable")
    for plugin in sorted(PLUGINS):
        result = subprocess.run(
            [str(claude), "plugin", "validate", str(PLUGINS_ROOT / plugin)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        expect_success(result, f"{plugin} Claude plugin validation")
        validation_output = output(result).lower()
        expect("warning" not in validation_output, f"{plugin} Claude plugin validation emitted warnings: {output(result)}")
        expect("error" not in validation_output, f"{plugin} Claude plugin validation emitted errors: {output(result)}")


def run_check(name: str, check: Callable[[], object]) -> tuple[bool, object | None]:
    try:
        result = check()
    except Exception as error:
        print(f"FAIL {name}: {error}")
        return False, None
    print(f"PASS {name}")
    return True, result


def main() -> int:
    if not GENERATOR.is_file():
        print("FAIL generator present: generate-plugins.py not found")
        return 1

    failures = 0
    for name, check in (
        ("determinism", check_determinism),
        ("clean --check", check_clean),
        ("retired M01 compound placeholders", check_retired_m01_compound_placeholders),
        ("manifest mutation", check_manifest_mutation),
        ("banner mutation", check_banner_mutation),
        ("body mutation", check_body_mutation),
        ("strict JSON", check_strict_json),
        ("installed skill frontmatter", check_skill_frontmatter),
        ("R1 harness copies", check_r1_harness_copies),
        ("R3 protocol content", check_r3_protocol_content),
        ("R4 README content", check_r4_readme_content),
        ("R5 pdc forward pointer", check_r5_pdc_pointer),
        ("independent provenance", check_provenance),
        ("verbatim detection and vendor copies", check_verbatim_copies),
        ("inventory", check_inventory),
        ("adjacency", check_adjacency),
    ):
        passed, _ = run_check(name, check)
        if not passed:
            failures += 1

    with tempfile.TemporaryDirectory(prefix="check-generate-plugins-cold-install-") as temporary:
        scratch_base = Path(temporary)
        cold_install_passed, installations = run_check("cold install", lambda: check_cold_install(scratch_base))
        no_banner_passed, _ = run_check("no banner in comment-free formats", check_no_json_banner)
        validator_passed, _ = run_check("per-plugin Claude validation", check_claude_plugin_validation)
        if not no_banner_passed:
            failures += 1
        if not validator_passed:
            failures += 1
        if not cold_install_passed:
            failures += 1
            print("FAIL protocol/linter coherence: cold install did not produce scratch installations")
            failures += 1
        else:
            coherence_passed, _ = run_check(
                "protocol/linter coherence",
                lambda: check_protocol_linter_coherence(installations, scratch_base),
            )
            if not coherence_passed:
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
