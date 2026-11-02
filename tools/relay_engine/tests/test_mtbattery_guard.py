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


class TestMutationRunner(unittest.TestCase):
    def output(self, raised=True, label='fixture', outcome='FAIL'):
        marker = f'{mt.RAISE_MARKER}IndexError\n' if raised else ''
        return marker + f'{label}: expected=0 observed=1 {outcome}\n'

    def test_proxy_isolates_a_raising_lint_call(self):
        import io
        def fail(*args):
            raise IndexError('seeded')
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = mt.LintProxy(SimpleNamespace(lint_file=fail)).lint_file('x')
        self.assertFalse(result.ok)
        self.assertEqual(stdout.getvalue(), f'{mt.RAISE_MARKER}IndexError\n')
        self.assertEqual(stderr.getvalue(), '')

    def test_proxy_wraps_only_lint_calls(self):
        def fail():
            raise ValueError('outside')
        proxy = mt.LintProxy(SimpleNamespace(other=fail, constant=42))
        self.assertEqual(proxy.constant, 42)
        with self.assertRaises(ValueError):
            proxy.other()

    def test_proxy_leaves_internal_calls_unwrapped(self):
        import io
        def fail():
            raise IndexError('inner')
        lint = SimpleNamespace(lint_file=fail)
        lint.lint_relay_root = lambda: lint.lint_file()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            mt.LintProxy(lint).lint_relay_root()
        self.assertEqual(stdout.getvalue().count(mt.RAISE_MARKER), 1)

    def test_parse_assigns_marker_to_next_result_line(self):
        result = mt.parse_suite_output(self.output() + 'other: expected=0 observed=0 PASS\n', 1)
        self.assertEqual(result.raised, {'fixture': 'IndexError'})
        self.assertEqual((result.failed, result.passed), (1, 1))

    def test_raised_label_is_a_kill_even_when_its_line_passes(self):
        suite = mt.parse_suite_output(self.output(outcome='PASS'), 0)
        self.assertEqual(suite.raised, {'fixture': 'IndexError'})
        # Inject only external observations; run_arm still builds the actual kill union.
        with mock.patch.object(mt, 'observe_suite', return_value=suite), \
                mock.patch.object(mt, 'exact_error_sets', return_value={('file', 'fixture'): mt.Raised('IndexError')}):
            actual = mt.run_arm(spec.ARMS[0], mt.DEFAULT_SUITE, {('file', 'fixture'): frozenset()})
        self.assertIn('fixture', actual.kills)

    def test_parse_marker_on_xroot_label(self):
        self.assertEqual(mt.parse_suite_output(self.output(label='xroot/X'), 1).raised, {'xroot/X': 'IndexError'})

    def test_parse_marker_without_result_line_is_a_hard_stop(self):
        with self.assertRaises(mt.RunnerError):
            mt.parse_suite_output(f'{mt.RAISE_MARKER}ValueError\n', 1)

    def test_parse_strips_root_and_index_prefixes(self):
        output = '--relay-root X: expected=0 observed=1 FAIL\n--index Y: expected=0 observed=1 FAIL\n'
        self.assertEqual(mt.parse_suite_output(output, 1).fail_labels, frozenset({'X', 'Y'}))

    def test_parse_carries_returncode(self):
        self.assertEqual(mt.parse_suite_output('x: expected=0 observed=0 PASS\n', 1).exit, 1)

    def test_runner_completion_checks(self):
        mt.check_runner_completion(f'{mt.RUNNER_DONE_MARKER}1\n', 1)
        for output in ('', f'{mt.RUNNER_FAILED_MARKER}RuntimeError\n', f'{mt.RUNNER_DONE_MARKER}0\n', f'{mt.RUNNER_DONE_MARKER}1\n' * 2):
            with self.subTest(output=output), self.assertRaises(mt.RunnerError):
                mt.check_runner_completion(output, 1)

    @contextlib.contextmanager
    def tree(self, rows=2, truncated=False, outside=False, raises=False):
        with tempfile.TemporaryDirectory() as raw:
            tools = Path(raw) / 'tools'
            tools.mkdir()
            (tools / 'relay-lint.py').write_text('''from types import SimpleNamespace

def lint_file(target):
    bad = target == 'bad'
    if bad and RAISE:
        raise IndexError('lint')
    return SimpleNamespace(ok=not bad, errors=['bad'] if bad else [], warnings=[])
'''.replace('RAISE', str(raises)))
            # The mutant flips False to True in this single expression.
            arm = {'name': 'standin', 'row': 'C8a', 'function': 'lint_file', 'node': 'Compare', 'original': "target == 'bad'", 'replacement': "target == 'first'"}
            (tools / 'check-relay-lint-fixtures.py').write_text('''import importlib.util
from pathlib import Path
EXPECTED = [('file', name, 0) for name in NAMES]
def load_linter():
    spec = importlib.util.spec_from_file_location('standin_lint', Path(__file__).with_name('relay-lint.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
def xroot_cases(path):
    return []
def main():
    lint = load_linter()
    failed = False
    for kind, rel, expected in EXPECTED[:LIMIT]:
        result = lint.lint_file(rel)
        observed = 0 if result.ok else 1
        ok = expected == observed
        failed |= not ok
        print(f"{rel}: expected={expected} observed={observed} {'PASS' if ok else 'FAIL'}")
    if OUTSIDE:
        raise RuntimeError('after all lines')
    return int(failed)
'''.replace('NAMES', repr(['first', 'second', 'third'][:rows])).replace('LIMIT', '1' if truncated else 'None').replace('OUTSIDE', str(outside)))
            yield tools, arm

    def test_observe_suite_complete_stand_in_keeps_exit_1(self):
        with self.tree() as (tools, arm):
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual((obs.exit, obs.denominator), (1, 2))
        self.assertEqual(obs.fail_labels, frozenset({'first'}))

    def test_observe_suite_truncated_without_marker(self):
        with self.tree(truncated=True) as (tools, arm), self.assertRaises(mt.IncompleteSuiteError) as caught:
            mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertIn('1/2', str(caught.exception))

    def test_observe_suite_runner_failure_outside_lint_call(self):
        with self.tree(outside=True) as (tools, arm), self.assertRaises(mt.RunnerError):
            mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)

    def test_observe_suite_uses_the_tree_population(self):
        with self.tree(rows=3) as (tools, arm):
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual(obs.denominator, 3)
        self.assertEqual(obs.passed + obs.failed, 3)

    def test_observe_suite_isolates_lint_raise(self):
        with self.tree(raises=True) as (tools, arm):
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual(obs.raised, {'first': 'IndexError'})
        self.assertEqual((obs.passed, obs.failed), (1, 1))

    def test_visible_labels_excludes_xroot_for_non_xr_arms(self):
        for row, expected in [('C8a', {'static'}), ('XR8', {'static', 'xroot/X'})]:
            self.assertEqual(mt.visible_labels({'row': row}, {'static', 'xroot/X'}), frozenset(expected))

    def test_static_raise_mismatch(self):
        self.assertEqual(mt.static_raise_mismatch({'x': 'ValueError'}, {'x': 'ValueError', 'xroot/X': 'IndexError'}), [])
        self.assertTrue(any('y' in s for s in mt.static_raise_mismatch({}, {'y': 'IndexError'})))

    def observation(self, kills=('x',), universal=('x',)):
        return mt.ArmObservation(frozenset(kills), {}, frozenset(universal), {}, 1, 1, 1)

    def test_classify_match_observes_once(self):
        value = self.observation()
        observer = mock.Mock(return_value=value)
        self.assertEqual(mt.classify_arm({'name': 'arm'}, value.kills, {}, {}, observer), (value, []))
        self.assertEqual(observer.call_count, 1)

    def test_classify_repeated_mismatch_returns_the_observation(self):
        value = self.observation()
        observer = mock.Mock(return_value=value)
        self.assertEqual(mt.classify_arm({'name': 'arm'}, frozenset(), {}, {}, observer), (value, []))
        self.assertEqual(observer.call_count, 2)

    def test_classify_discharged_count_control_is_not_a_mismatch(self):
        value = self.observation(universal=('x', 'control'))
        observer = mock.Mock(return_value=value)
        self.assertEqual(mt.classify_arm({'name': 'arm'}, value.kills, {}, {'arm': {'control': 'x'}}, observer), (value, []))
        self.assertEqual(observer.call_count, 1)

    def test_classify_alternating_observation_is_unstable(self):
        observer = mock.Mock(side_effect=[self.observation(), self.observation(kills=('x', 'moved'))])
        value, errors = mt.classify_arm({'name': 'arm'}, frozenset(), {}, {}, observer)
        self.assertIsNone(value)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith('UNSTABLE arm'))
        self.assertIn('moved', errors[0])

    def test_runner_command_uses_invoking_battery(self):
        self.assertIn(str(Path(mt.__file__).resolve()), mt.DEFAULT_SUITE)

    def test_main_preflight_exits_7_before_any_arm(self):
        import io
        stdout = io.StringIO()
        with mock.patch.object(mt, 'structural_errors', return_value=['G3 seeded']), \
                mock.patch.object(mt, 'run_arm', side_effect=AssertionError('arm must not run')) as run, contextlib.redirect_stdout(stdout):
            self.assertEqual(mt.main(['--spec', str(mt.TOOLS / 'mtbattery-spec.py')]), 7)
        self.assertEqual(run.call_count, 0)
        self.assertIn('G3 seeded', stdout.getvalue())
        self.assertNotIn('ARM=', stdout.getvalue())

    def test_main_passes_selection_to_preflight(self):
        import io
        with mock.patch.object(mt, 'structural_errors', return_value=['G3 seeded']) as guard, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mt.main(['--spec', str(mt.TOOLS / 'mtbattery-spec.py'), '--only', 'XR8']), 7)
        self.assertEqual(len(guard.call_args.args[2]), 2)
        self.assertTrue(all('XR8' in arm.get('rows', (arm['row'],)) for arm in guard.call_args.args[2]))
        self.assertFalse(guard.call_args.args[3])


class TestMutationPopulations(unittest.TestCase):
    # A wrong population branch must either leak xroot results into static runs
    # or falsely reject a complete custom/full run.
    @contextlib.contextmanager
    def tree(self, **kwargs):
        with TestMutationRunner().tree(**kwargs) as (tools, arm):
            path = tools / 'check-relay-lint-fixtures.py'
            source = path.read_text().replace('def xroot_cases(path):\n    return []',
                "def xroot_cases(path):\n    return ['xroot/one']\ndef xroot_generator_contract_errors():\n    return []")
            source = source.replace('    if OUTSIDE:', '    if OUTSIDE:')
            source = source.replace('    return int(failed)',
                "    for label in xroot_cases(Path('.')):\n        print(f'{label}: expected=0 observed=1 FAIL')\n        failed = True\n    return int(failed)")
            path.write_text(source)
            yield tools, arm

    def test_reads_xroot_agrees_with_visible_labels_for_every_spec_arm(self):
        self.assertEqual(sum(mt.reads_xroot(a) for a in spec.ARMS), 39)
        for arm in spec.ARMS:
            self.assertEqual(bool(mt.visible_labels(arm, {'xroot/x'})), mt.reads_xroot(arm))

    def test_static_mode_omits_xroot_lines(self):
        with self.tree() as (tools, arm):
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual(obs.denominator, 2)
        self.assertEqual(obs.fail_labels, frozenset({'first'}))
        self.assertEqual((obs.passed, obs.failed), (1, 1))

    def test_static_mode_missing_static_line_is_incomplete(self):
        with self.tree(truncated=True) as (tools, arm), self.assertRaises(mt.IncompleteSuiteError) as caught:
            mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual(str(caught.exception), '1/2')

    def test_xr_arm_runs_the_whole_suite(self):
        with self.tree() as (tools, arm):
            arm['row'] = 'XR8'
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
        self.assertEqual(obs.denominator, 3)
        self.assertEqual(obs.fail_labels, frozenset({'first', 'xroot/one'}))
        self.assertEqual((obs.passed, obs.failed), (1, 2))

    def test_static_runner_leaves_the_harness_file_and_proxy(self):
        with self.tree(raises=True) as (tools, arm):
            path = tools / 'check-relay-lint-fixtures.py'
            before = path.read_bytes()
            obs = mt.observe_suite(arm, tools, mt.DEFAULT_SUITE)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(obs.raised, {'first': 'IndexError'})
        self.assertEqual(obs.denominator, 2)

    def test_custom_command_unchanged_for_non_xr_arm(self):
        self.assertEqual(mt.suite_command({'row': 'C8a'}, ('custom',)), ('custom',))
        self.assertEqual(mt.suite_command({'row': 'XR8'}, mt.DEFAULT_SUITE), mt.DEFAULT_SUITE)

    def test_arm_population_rule(self):
        for row, command, expected in [('C8a', mt.DEFAULT_SUITE, 656), ('C8a', ('custom',), 786),
                                       ('XR8', mt.DEFAULT_SUITE, 786), ('XR8', ('custom',), 786)]:
            self.assertEqual(mt.arm_population({'row': row}, command, (656, 786)), expected)

    @contextlib.contextmanager
    def cheap_parent(self):
        # Isolate reporting from the already-tested expensive preflight/oracles.
        with mock.patch.object(mt, 'structural_errors', return_value=[]), \
             mock.patch.object(mt, 'xroot_registration_count', return_value=130), \
             mock.patch.object(mt, 'exact_error_sets', return_value={}), \
             mock.patch.object(mt, 'validate_count_controls', return_value=({}, [])), \
             mock.patch.object(mt, 'generated_and_registered_by_kind', return_value=({}, {})):
            yield

    def test_custom_command_full_population_is_complete(self):
        import io
        for count in (786, 785):
            out = io.StringIO()
            obs = mt.ArmObservation(frozenset({'x'}), {}, frozenset(), {}, count - 1, 1, 1)
            with self.cheap_parent(), mock.patch.object(mt, 'run_arm', return_value=obs), contextlib.redirect_stdout(out):
                mt.main(['--spec', str(mt.TOOLS / 'mtbattery-spec.py'), '--only', 'C1', '--', 'custom'])
            lines = out.getvalue().splitlines()
            arms = [line for line in lines if line.startswith('ARM=')]
            self.assertEqual(len(arms), 2)
            self.assertTrue(all(f'TOTAL={count}/786' in line for line in arms))
            incomplete = [line for line in lines if 'incomplete-denominator' in line]
            self.assertEqual(len(incomplete), 0 if count == 786 else 2)
            if count == 785:
                self.assertTrue(all('incomplete-denominator=785/786' in line for line in incomplete))

    def test_main_denominator_line_names_both_populations(self):
        import io
        out = io.StringIO()
        with self.cheap_parent(), contextlib.redirect_stdout(out):
            mt.main(['--spec', str(mt.TOOLS / 'mtbattery-spec.py'), '--only', 'no-arm'])
        lines = out.getvalue().splitlines()
        denominator = [line for line in lines if line.startswith('DENOMINATOR ')]
        self.assertEqual(len(denominator), 1)
        self.assertIn('786', denominator[0])
        self.assertIn('656', denominator[0])
        self.assertFalse(any(line.startswith('ARM=') for line in lines))
