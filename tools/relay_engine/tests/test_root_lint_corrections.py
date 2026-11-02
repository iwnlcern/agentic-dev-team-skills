"""Hand-root population and path spelling obey the same root boundary."""
import contextlib
import datetime
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from relay_engine import rules
from relay_engine.tests.test_draft_shape import draft
from relay_engine.tests.test_parity import _standalone
from relay_engine.tests.test_rules_index import INDEX_TEXT


@contextlib.contextmanager
def cwd(path):
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


def verdict(result):
    return result.errors, result.warnings


class TestRootLintCorrections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hand = _standalone()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sprint = Path(self.temp.name) / 'sprint-é'
        self.root = self.sprint / '.relays/r'
        self.root.mkdir(parents=True)
        (self.root / 'lane').mkdir()
        (self.sprint / 'plans').mkdir()
        (self.sprint / 'designs').mkdir()
        self.path = self.root / 'SITREP-pair-planner-20261026-010000.md'
        self.path.write_text(draft())
        self.hidden = self.root / '.engine/drafts/p.planner/SITREP-pair-planner-20261026-010001.md'

    def consumer(self):
        text = draft('IMPL', extra='PARENT_DISPATCH_ID: shape\nACTIONS_GIT_REF: implemented abcdef1\n')
        text = text.replace('\nDISPATCH_ID: shape\n', '\nDISPATCH_ID: report\n')
        text = text.replace('ROLE: Pair Planner', 'ROLE: Pair Implementer')
        text = text.replace('AUTHORITY: report-only', 'AUTHORITY: implementation')
        text = text.replace('FROM: p.planner', 'FROM: p.implementer').replace('TO: p.implementer', 'TO: p.planner')
        (self.root / 'IMPL-pair-implementer-20261026-020000.md').write_text(text)

    def hidden_copy(self):
        self.hidden.parent.mkdir(parents=True)
        self.hidden.write_bytes(self.path.read_bytes())

    def test_hand_root_walk_skips_engine_drafts(self):
        self.consumer()
        self.hidden_copy()
        for module in (self.hand, rules):
            result = module.lint_relay_root(self.root)
            self.assertFalse(any('held by 2 relays' in e for e in result.errors))
            self.assertFalse(any('.engine/drafts/' in e for e in result.errors + result.warnings))

    def test_a_second_filed_holder_still_conflicts(self):
        self.consumer()
        second = self.root / 'SITREP-pair-planner-20261026-010001.md'
        second.write_bytes(self.path.read_bytes())
        for module in (self.hand, rules):
            self.assertTrue(any('held by 2 relays' in e for e in module.lint_relay_root(self.root).errors))

    def test_engine_root_mode_is_unchanged(self):
        self.consumer()
        # Planner reading v297-b2-task5-reading-1: only the engine exposes this mode.
        before = verdict(rules.lint_relay_root(self.root, engine_root=True))
        self.hidden_copy()
        self.assertEqual(verdict(rules.lint_relay_root(self.root, engine_root=True)), before)

    def test_projection_digest_keys_follow_the_canonical_root(self):
        index = self.root / 'INDEX.md'
        index.write_text(INDEX_TEXT)
        digest = hashlib.sha256(index.read_bytes()).hexdigest()
        alias = Path(self.temp.name) / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        canonical = self.root.resolve()
        frozen = datetime.datetime(2026, 8, 16, 12, 0, 0)
        with patch.object(rules, 'clock_now', return_value=frozen):
            expected = verdict(rules.lint_relay_root(
                canonical, projection_digests={canonical / 'INDEX.md': digest}))
            actual = verdict(rules.lint_relay_root(
                alias, projection_digests={alias / 'INDEX.md': digest}))
            self.assertFalse(any('ahead of the' in e for e in expected[0]))
            self.assertEqual(actual, expected)
            for label, key, value in (
                    ('foreign directory', other / 'INDEX.md', digest),
                    ('foreign symlink', other / 'INDEX.md', digest),
                    ('wrong digest', alias / 'INDEX.md', '0' * 64)):
                with self.subTest(control=label):
                    if label == 'foreign symlink':
                        key.symlink_to(index)
                    result = rules.lint_relay_root(alias, projection_digests={key: value})
                    self.assertTrue(any('ahead of the' in e for e in result.errors))

    def spellings(self):
        link = Path(self.temp.name) / 'linked-root'
        link.symlink_to(self.root, target_is_directory=True)
        return ((self.sprint, self.root), (self.sprint, str(self.root) + '/'),
                (self.root, Path('.')), (self.root, './'),
                (self.root.parent, Path('r')), (self.root / 'lane', Path('..')),
                (self.sprint, link))

    def check_spellings(self, missing):
        plan = self.sprint / 'plans/PL-check.md'
        raw = b'plan content\n'
        if not missing:
            plan.write_bytes(raw)
        self.path.write_text(draft(extra='PLAN_ARTIFACT: PL-check\nPLAN_SHA256: '
                                   + hashlib.sha256(raw).hexdigest() + '\n'))
        cases = self.spellings()
        for module in (self.hand, rules):
            expected = verdict(module.lint_relay_root(self.root))
            self.assertEqual(any('no artifact resolves' in e for e in expected[0]), missing)
            for working_dir, spelling in cases:
                with self.subTest(module=module.__name__, spelling=str(spelling)):
                    with cwd(working_dir):
                        actual = verdict(module.lint_relay_root(spelling))
                    self.assertEqual(actual, expected)

    def test_root_spellings_agree(self):
        self.check_spellings(missing=False)

    def test_missing_artifact_fails_in_every_spelling(self):
        self.check_spellings(missing=True)
