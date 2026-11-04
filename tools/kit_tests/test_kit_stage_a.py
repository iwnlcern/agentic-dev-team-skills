"""Kit v3 stage A tests (DD-v3-kit r2 stage A, PL-v3-kit-a).

Run: python3 -m unittest discover -s tools/kit_tests -v
"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
