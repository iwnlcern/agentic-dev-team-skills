"""Structural drift controls; semantic kill drift still needs the full battery."""
import contextlib
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import mtbattery as mt

PC10 = 'plancontract/PC10-draft-explicit-root/.engine/drafts/v29-x.planner/draft-plan.md'


def setUpModule():
    global harness, spec, observations, labels
    harness = mt.load_module('guard_harness', mt.TOOLS / 'check-relay-lint-fixtures.py')
    spec = mt.load_module('guard_spec', mt.TOOLS / 'mtbattery-spec.py')
    observations = mt.lint_observations(harness)
    labels = mt.xroot_labels(harness)


class TestMutationGuard(unittest.TestCase):
    def assert_channels(self, messages, rel, channels):
        self.assertEqual(len(messages), 1, messages)
        self.assertTrue(messages[0].startswith('G1 '), messages)
        self.assertIn(rel, messages[0])
        for channel in ('exit', 'error-set', 'error-count', 'warning', 'raised'):
            self.assertEqual(channel in messages[0].split(' channels=')[-1].split(','),
                             channel in channels, messages)

    def changed(self, key, **fields):
        result = dict(observations)
        result[key] = replace(result[key], **fields)
        return result

    @contextlib.contextmanager
    def wrong_file_root(self, root):
        original = mt.prepare_lint_call
        def prepare(lint, h, kind, rel, target):
            if kind == 'file-root':
                return lambda: lint.lint_relay_root(target) if root else lint.lint_file(target)
            return original(lint, h, kind, rel, target)
        with mock.patch.object(mt, 'prepare_lint_call', side_effect=prepare):
            yield

    @contextlib.contextmanager
    def raising_file(self, rel, exception):
        lint = harness.load_linter()
        original = lint.lint_file
        def call(target, *args, **kwargs):
            if target == harness.FIXTURES / rel:
                raise exception('seeded lint-call failure')
            return original(target, *args, **kwargs)
        with mock.patch.object(lint, 'lint_file', side_effect=call), \
                mock.patch.object(harness, 'load_linter', return_value=lint):
            yield

    def test_guard_green_on_head(self):
        start = time.monotonic()
        self.assertEqual(mt.structural_errors(spec, harness, spec.ARMS, True), [])
        print(f'GUARD_SECONDS={time.monotonic() - start:.6f}', flush=True)

    def test_exact_error_sets_derives_from_observations(self):
        actual = mt.exact_error_sets(harness)
        self.assertEqual(set(actual), set(observations))
        for key, value in actual.items():
            self.assertIsInstance(value, frozenset)
            self.assertEqual(value, frozenset(observations[key].errors))

    def test_g1_exit_channel(self):
        key = next(iter(observations))
        self.assert_channels(mt.g1_agreement(harness, self.changed(key, ok=not observations[key].ok)), key[1], {'exit'})

    def test_g1_error_multiset_channel(self):
        key = next(k for k, v in observations.items() if v.errors and k[1] in harness.EXPECTED_ERROR_SET and k[1] not in harness.EXPECTED_ERROR_COUNT)
        self.assert_channels(mt.g1_agreement(harness, self.changed(key, errors=observations[key].errors + observations[key].errors[:1])), key[1], {'error-set'})

    def test_g1_error_count_channel(self):
        key = next(k for k in observations if k[1] == harness.C13_CM79C)
        self.assertEqual(len(observations[key].errors), 2)
        self.assert_channels(mt.g1_agreement(harness, self.changed(key, errors=observations[key].errors[:1])), key[1], {'error-count'})

    def test_g1_warning_channel(self):
        self.assert_channels(mt.g1_agreement(harness, self.changed(('file-root', PC10), warnings=())), PC10, {'warning'})

    def test_g1_control_file_root_routed_to_root(self):
        with self.wrong_file_root(True):
            self.assert_channels(mt.g1_agreement(harness, mt.lint_observations(harness)), PC10, {'exit', 'warning'})

    def test_g1_control_file_root_without_artifact_root(self):
        with self.wrong_file_root(False):
            self.assert_channels(mt.g1_agreement(harness, mt.lint_observations(harness)), PC10, {'warning'})

    def test_g1_control_extra_expected_table(self):
        with mock.patch.object(harness, 'EXPECTED_EXTRA', {}, create=True):
            self.assertIn('EXPECTED_EXTRA', '\n'.join(mt.g1_agreement(harness, observations)))

    def test_g1_unknown_kind(self):
        with mock.patch.object(harness, 'EXPECTED', [*harness.EXPECTED, ('nonsense', 'x.md', 0)]):
            with self.assertRaises(mt.RegistrationStop) as caught:
                mt.lint_observations(harness)
            self.assertIsInstance(caught.exception.__cause__, mt.UnknownKindError)
            self.assertIn('nonsense', str(caught.exception))
            self.assertIn('x.md', str(caught.exception))
            errors = mt.structural_errors(spec, harness, spec.ARMS, True)
            self.assertTrue(any(e.startswith('G1 ') and 'nonsense' in e and 'x.md' in e for e in errors))

    def test_g1_baseline_raise(self):
        key = next(k for k in observations if k[0] == 'file')
        with self.raising_file(key[1], ValueError):
            errors = mt.g1_agreement(harness, mt.lint_observations(harness))
        self.assertTrue(any(key[1] in e and 'raised' in e for e in errors))

    def test_g2_control_path_embedding(self):
        with tempfile.TemporaryDirectory() as raw, self.wrong_file_root(True):
            tools = Path(raw) / 'tools'
            shutil.copytree(mt.TOOLS, tools)
            copied = mt.load_module('guard_copied', tools / 'check-relay-lint-fixtures.py')
            errors = mt.g2_path_independence(mt.lint_observations(harness), mt.lint_observations(copied))
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(errors[0].startswith('G2 '))
        self.assertIn(PC10, errors[0])

    def test_g3_control_unmatched_anchor(self):
        arm = {**spec.ARMS[0], 'original': 'no_matching_anchor_abcdef'}
        errors = mt.g3_anchors(spec, [arm], False)
        self.assertTrue(any(arm['name'] in e and e.startswith('G3 ') for e in errors))

    def test_g4_control_unregistered_label(self):
        arm = spec.ARMS[0]['name']
        kills = {**spec.KILLS, arm: [*spec.KILLS[arm], 'no/such-label']}
        standin = SimpleNamespace(**{**vars(spec), 'KILLS': kills, 'RAISES': {arm: {'no/raise-label': 'ValueError'}}})
        errors = mt.g4_declarations(standin, harness, labels)
        for label in ('no/such-label', 'no/raise-label'):
            self.assertTrue(any(arm in e and label in e and e.startswith('G4 ') for e in errors), errors)

    def test_empty_populations_are_red(self):
        with mock.patch.object(harness, 'EXPECTED', []):
            errors = mt.g1_agreement(harness, {})
            self.assertEqual(len(errors), 1, errors)
            self.assertTrue(errors[0].startswith('G1 '))
        self.assertEqual(len(mt.g2_path_independence({}, {})), 1)
        self.assertTrue(mt.g2_path_independence({}, {})[0].startswith('G2 '))
        self.assertEqual(len(mt.g3_anchors(spec, [], True)), 1)
        self.assertTrue(mt.g3_anchors(spec, [], True)[0].startswith('G3 '))
        with mock.patch.object(spec, 'KILLS', {}):
            errors = mt.g4_declarations(spec, harness, labels)
            self.assertEqual(len(errors), 1, errors)
            self.assertTrue(errors[0].startswith('G4 '))

    def check_isolation(self, key, exception):
        with self.raising_file(key[1], exception):
            actual = mt.lint_observations(harness)
            exact = mt.exact_error_sets(harness)
        self.assertEqual(actual[key].raised, exception.__name__)
        self.assertNotIsInstance(exact[key], frozenset)
        self.assertIn(exception.__name__, str(exact[key]))
        self.assertEqual({k: v for k, v in actual.items() if k != key}, {k: v for k, v in observations.items() if k != key})

    def test_universal_isolation_records_the_raise(self):
        self.check_isolation(next(k for k in observations if k[0] == 'file'), ValueError)

    def test_seam_entry_failure_is_not_a_kill(self):
        with mock.patch.object(harness, 'a5_private_seam', side_effect=RuntimeError('seam')):
            with self.assertRaises(mt.RegistrationStop) as caught:
                mt.lint_observations(harness)
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertTrue(any(rel in str(caught.exception) for rel in harness.A5_SEAM))

    def test_missing_file_root_entry_is_a_hard_stop(self):
        with mock.patch.object(harness, 'FILE_ROOT_OF', {}):
            with self.assertRaises(mt.RegistrationStop) as caught:
                mt.lint_observations(harness)
            self.assertIsInstance(caught.exception.__cause__, KeyError)
            self.assertIn(PC10, str(caught.exception))
            self.assertTrue(any(e.startswith('G1 ') and PC10 in e for e in mt.structural_errors(spec, harness, spec.ARMS, True)))

    def test_lint_call_raising_the_same_type_is_a_kill(self):
        self.check_isolation(('file-root', PC10), KeyError)
