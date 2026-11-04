"""Kit v3 stage A tests (DD-v3-kit r2 stage A, PL-v3-kit-a).

Run: python3 -m unittest discover -s tools/kit_tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGINS = ROOT / "plugins"
TIERS = ("adt-pair", "adt-orchestrator", "adt-master")


class KitTreeCase(unittest.TestCase):
    """Reads canonical sources and their generated plugin copies."""

    @staticmethod
    def canonical_text(rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    @staticmethod
    def installed_texts(rel: str) -> dict[str, str]:
        """Tier -> text of plugins/<tier>/<rel> for each tier that ships rel."""
        texts = {}
        for tier in TIERS:
            path = PLUGINS / tier / rel
            if path.is_file():
                texts[tier] = path.read_text(encoding="utf-8")
        return texts

    @staticmethod
    def installed_shared_copies(name: str) -> dict[str, str]:
        """plugins-relative path -> text for every installed copy of shared/<name>."""
        return {
            path.relative_to(PLUGINS).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(PLUGINS.glob(f"*/skills/*/{name}"))
        }

    @staticmethod
    def shipped_text_files() -> dict[str, str]:
        """Repo-relative path -> text for every Markdown file in canonical skills/ and shared/ and in plugins/*/skills/."""
        paths = [
            *(ROOT / "skills").rglob("*.md"),
            *(ROOT / "shared").rglob("*.md"),
            *PLUGINS.glob("*/skills/**/*.md"),
        ]
        return {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(paths)
            if path.is_file()
        }


class TestWrongSentences(KitTreeCase):
    """A1: the three wrong shipped sentences are gone and their replacements name the deciding code."""

    WRONG = (
        "travel with the tree",
        "role word from the address grammar",
        "one-time `/hooks` re-trust",
    )

    def test_wrong_sentences_corrected(self):
        texts = self.shipped_text_files()
        texts["CHANGELOG.md"] = self.canonical_text("CHANGELOG.md")
        for phrase in self.WRONG:
            hits = [path for path, text in texts.items() if phrase in text]
            self.assertEqual(hits, [], f"wrong sentence {phrase!r} still shipped in {hits}")

        setup = {"skills/sprint-doc-setup/SKILL.md": self.canonical_text("skills/sprint-doc-setup/SKILL.md")}
        setup.update(self.installed_texts("skills/sprint-doc-setup/SKILL.md"))
        self.assertEqual(len(setup), 1 + len(TIERS))
        for where, text in setup.items():
            for code in ("read_lint_context", "notice_effect", "_relative", "relay-monitor.py"):
                self.assertIn(code, text, f"{where}: moved-root sentence does not name {code}")

        protocol = {"shared/protocol.md": self.canonical_text("shared/protocol.md")}
        protocol.update(self.installed_shared_copies("protocol.md"))
        self.assertGreater(len(protocol), 1)
        for where, text in protocol.items():
            self.assertIn("`_role_slug`", text, f"{where}: filename role sentence does not name _role_slug")

        changelog = self.canonical_text("CHANGELOG.md")
        self.assertIn("after every kit update", changelog)

    def test_named_code_exists(self):
        """Every function the corrected sentences name exists where they say."""
        cases = (
            ("tools/relay_engine/daemon.py", "def read_lint_context("),
            ("tools/relay_engine/roster.py", "def notice_effect("),
            ("tools/relay_engine/client.py", "def _relative("),
            ("tools/relay_engine/ledger.py", "def _role_slug("),
            ("tools/adapters/relay-monitor.py", "def bind_from_receipt("),
        )
        for rel, needle in cases:
            self.assertIn(needle, self.canonical_text(rel), f"{rel} lacks {needle}")


BASE_COMMIT = "5f313bfa14a5e7a2a6efc9c1dac92be4356830af"

ALL_ROLES = ("Prove a claim by running it, and cite the command and its output.",)
PLANNER = ("task sizing", "file structure", "spec coverage", "interface consistency")
IMPLEMENTER = (
    "Write the failing test first",
    "Reproduce a bug before fixing it",
    "Work in a separate git worktree",
    "push the branch, open the PR and stop",
)
REVIEWER = ("A blocking finding cites an executed check",)

PRACTICES: dict[str, tuple[str, ...]] = {
    "pair-planner": ALL_ROLES + PLANNER,
    "orchestrator-planner": ALL_ROLES + PLANNER,
    "master-planner": ALL_ROLES + PLANNER,
    "domain-planner": ALL_ROLES + PLANNER,
    "pair-implementer": ALL_ROLES + IMPLEMENTER,
    "orchestrator-reviewer": ALL_ROLES + REVIEWER,
    "master-reviewer": ALL_ROLES + REVIEWER,
    "domain-reviewer": ALL_ROLES + REVIEWER,
}

PROCEDURE_NAMES = ("writing-plans", "executing-plans", "using-git-worktrees", "finishing-a-development-branch")
VENDORED_BRAINSTORMING = "vendor/obra/superpowers/brainstorming.md"


def _base_text(rel: str) -> str:
    return subprocess.run(
        ["git", "show", f"{BASE_COMMIT}:{rel}"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def _description(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("description: "):
            return line[len("description: "):]
    raise AssertionError("no description line")


class TestSuperpowersOut(KitTreeCase):
    """A2: no Superpowers dependency; each role skill states its own practices."""

    def test_no_superpowers_dependency(self):
        offenders = []
        for path, text in self.shipped_text_files().items():
            for number, line in enumerate(text.splitlines(), 1):
                lowered = line.lower()
                if "superpowers" in lowered and VENDORED_BRAINSTORMING not in line:
                    offenders.append(f"{path}:{number}: superpowers")
                for name in PROCEDURE_NAMES:
                    if name in line:
                        offenders.append(f"{path}:{number}: {name}")
                if "`brainstorming`" in line:
                    offenders.append(f"{path}:{number}: `brainstorming`")
        self.assertEqual(offenders, [])

    def test_kept_practices_in_every_role_copy(self):
        for skill, phrases in PRACTICES.items():
            rel = f"skills/{skill}/SKILL.md"
            copies = {rel: self.canonical_text(rel)}
            variant = ROOT / "skills" / "variants" / skill / "SKILL.md"
            if variant.is_file():
                copies[f"skills/variants/{skill}/SKILL.md"] = variant.read_text(encoding="utf-8")
            installed = self.installed_texts(rel)
            self.assertTrue(installed, f"{skill} is not installed in any tier")
            copies.update({f"plugins/{tier}/{rel}": text for tier, text in installed.items()})
            for where, text in copies.items():
                for phrase in phrases:
                    self.assertIn(phrase, text, f"{where} lacks practice {phrase!r}")

    def test_descriptions_keep_use_when(self):
        suffix = " Requires Superpowers."
        edited = 0
        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            rel = path.relative_to(ROOT).as_posix()
            base = _description(_base_text(rel))
            now = _description(path.read_text(encoding="utf-8"))
            self.assertNotIn("Superpowers", now, rel)
            if base.endswith(suffix):
                edited += 1
                self.assertEqual(now, base[: -len(suffix)], rel)
            else:
                self.assertEqual(now, base, rel)
        self.assertEqual(edited, 9)


PLANNER_SKILLS = ("orchestrator-planner", "pair-planner", "master-planner", "domain-planner")
UPSTREAM_COMMIT = "bb92a77741419a4ab5f06e711a283343f1ada0c3"
VENDOR_LICENSE = "LICENSES/obra-superpowers-MIT.txt"


class TestVendoredBrainstorming(KitTreeCase):
    """A2: a trimmed, attributed brainstorming reference that Planner skills read by plugin-root path."""

    def test_vendored_brainstorming_marked_modified(self):
        text = self.canonical_text(VENDORED_BRAINSTORMING)
        head = "\n".join(text.splitlines()[:12])
        for needle in ("Superpowers 7.0.0", UPSTREAM_COMMIT, "github.com/obra/superpowers", "modified"):
            self.assertIn(needle, head)
        license_text = self.canonical_text(VENDOR_LICENSE)
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2025 Jesse Vincent", license_text)
        lowered = text.lower()
        for dropped in ("visual companion", "builder", "spike", "docs/superpowers", "writing-plans"):
            self.assertNotIn(dropped, lowered, f"trimmed copy still contains {dropped!r}")

    def test_vendored_brainstorming_keeps_the_conversation(self):
        text = self.canonical_text(VENDORED_BRAINSTORMING)
        for kept in (
            "Each message ends with one question",
            "Play back as you go",
            "Mark anything you're guessing as your guess",
            "Think wide privately",
            "Offer recon",
            "Get them describing",
            "everyday words",
            "analogy",
        ):
            self.assertIn(kept, text)

    def test_planner_skills_name_an_existing_vendored_path(self):
        for skill in PLANNER_SKILLS:
            rel = f"skills/{skill}/SKILL.md"
            copies = {rel: self.canonical_text(rel)}
            copies.update({f"plugins/{tier}/{rel}": text for tier, text in self.installed_texts(rel).items()})
            for where, text in copies.items():
                self.assertIn(f"`{VENDORED_BRAINSTORMING}`", text, f"{where} does not name the vendored file")
                self.assertNotIn(f"]({VENDORED_BRAINSTORMING}", text, where)
                self.assertNotIn("/vendor/obra/", text.replace(f"`{VENDORED_BRAINSTORMING}`", ""), where)
            for tier in self.installed_texts(rel):
                self.assertTrue((PLUGINS / tier / VENDORED_BRAINSTORMING).is_file(), f"{tier} lacks the vendored file")
                self.assertTrue((PLUGINS / tier / VENDOR_LICENSE).is_file(), f"{tier} lacks the vendored licence")

    def test_readme_says_superpowers_not_needed(self):
        readme = self.canonical_text("README.md")
        requirements = readme.split("## Requirements", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Superpowers is not needed", requirements)
        self.assertIn("3,192 bytes", requirements)
        license_section = readme.split("## License", 1)[1].split("\n## ", 1)[0]
        for needle in ("vendor/mattpocock/grill-me/", VENDORED_BRAINSTORMING, "LICENSES/mattpocock-skills-MIT.txt", VENDOR_LICENSE):
            self.assertIn(needle, license_section)



LINK = re.compile(r"\]\(([^)\s]+)\)")


def dead_relative_links(paths) -> list[str]:
    """file:line target for every relative Markdown link whose target file does not exist."""
    dead = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in LINK.finditer(line):
                target = match.group(1)
                if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                file_part = target.split("#", 1)[0]
                if file_part and not (path.parent / file_part).exists():
                    dead.append(f"{path}:{number} {target}")
    return dead


class TestItem12(KitTreeCase):
    """A3: every relative link in canonical, derived and installed skills resolves."""

    def test_relative_links_resolve(self):
        paths = sorted(
            path
            for path in [*(ROOT / "skills").rglob("*.md"), *PLUGINS.glob("*/skills/**/*.md")]
            if path.is_file()
        )
        self.assertGreater(len(paths), 50)
        self.assertEqual(dead_relative_links(paths), [])

    def test_dead_link_control_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "fixture.md"
            (Path(temporary) / "present.md").write_text("x\n", encoding="utf-8")
            fixture.write_text("[ok](present.md) [gone](../missing/SKILL.md#a) [web](https://example.com)\n", encoding="utf-8")
            self.assertEqual(dead_relative_links([fixture]), [f"{fixture}:1 ../missing/SKILL.md#a"])


def _generator():
    tools = str(ROOT / "tools")
    if sys.path[:1] != [tools]:
        sys.path.insert(0, tools)
    spec = importlib.util.spec_from_file_location("kit_generate_plugins", ROOT / "tools" / "generate-plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(sys.modules["relay_engine.version"].__file__).resolve().is_relative_to(ROOT), "relay_engine imported from another tree"
    return module


def _png_chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    chunks, position = [], 8
    while position < len(png):
        (length,) = struct.unpack(">I", png[position:position + 4])
        kind = png[position + 4:position + 8]
        chunks.append((kind, png[position + 8:position + 8 + length]))
        position += 12 + length
    return chunks


class TestIcon(KitTreeCase):
    """A4: one generated paper-airplane PNG, identical everywhere, found by the validator."""

    @classmethod
    def setUpClass(cls):
        cls.png = _generator().render_icon_png()

    def test_icon_deterministic_and_valid(self):
        self.assertEqual(self.png, _generator().render_icon_png())
        chunks = _png_chunks(self.png)
        self.assertEqual([kind for kind, _ in chunks], [b"IHDR", b"PLTE", b"IDAT", b"IEND"])
        width, height, depth, colour = struct.unpack(">IIBB", chunks[0][1][:10])
        self.assertEqual((width, height, depth, colour), (1024, 1024, 8, 3))
        self.assertLess(len(self.png), 2_000_000)
        for tier in TIERS:
            self.assertEqual((PLUGINS / tier / ".claude-plugin" / "icon.png").read_bytes(), self.png, tier)

    def test_icon_bytes_independent_of_compressor(self):
        stream = b"".join(data for kind, data in _png_chunks(self.png) if kind == b"IDAT")
        self.assertEqual(stream[:2], b"\x78\x01")
        position, blocks = 2, 0
        while True:
            header = stream[position]
            self.assertEqual((header >> 1) & 3, 0, f"block {blocks} is not stored")
            length, complement = struct.unpack("<HH", stream[position + 1:position + 5])
            self.assertEqual(length ^ 0xFFFF, complement)
            position += 5 + length
            blocks += 1
            if header & 1:
                break
        self.assertEqual(len(stream), position + 4)
        self.assertGreater(blocks, 1)

    def test_icon_clears_validator(self):
        selected = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
        self.assertTrue(selected, "Claude Code validator not found; set CLAUDE_BIN")
        claude = shutil.which(str(selected))
        self.assertTrue(claude, f"Claude Code validator is not executable: {selected}")
        version = subprocess.run([claude, "--version"], capture_output=True, text=True, check=True).stdout.strip()
        print(f"validator: {claude} {version}")
        for tier in TIERS:
            result = subprocess.run(
                [claude, "plugin", "validate", "--json", str(PLUGINS / tier)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            findings = []

            def walk(value):
                if isinstance(value, dict):
                    if "code" in value or "severity" in value:
                        findings.append(value)
                    for item in value.values():
                        walk(item)
                elif isinstance(value, list):
                    for item in value:
                        walk(item)

            walk(json.loads(result.stdout))
            self.assertNotIn("ICON_MISSING", [f.get("code") for f in findings], tier)
            self.assertEqual([f for f in findings if f.get("severity") == "error"], [], tier)

if __name__ == "__main__":
    unittest.main()
