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
        import os
        import resource
        def cpu_seconds():
            usages = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
            return sum(usage.ru_utime + usage.ru_stime for usage in usages)
        load_start = os.getloadavg()
        cpu_start = cpu_seconds()
        start = time.monotonic()
        self.assertEqual(mt.structural_errors(spec, harness, spec.ARMS, True), [])
        wall = time.monotonic() - start
        cpu = cpu_seconds() - cpu_start
        load_end = os.getloadavg()
        print(f'GUARD_SECONDS={wall:.6f} GUARD_CPU_SECONDS={cpu:.6f} '
              f'GUARD_LOAD_START={load_start} GUARD_LOAD_END={load_end}', flush=True)

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
        arms = [a for a in spec.ARMS if a['row'] == 'C1']
        self.assertEqual(len(arms), 2)
        context = mt.RunContext(('custom',), {}, {}, (656, 786))
        for count in (786, 785):
            obs = mt.ArmObservation(frozenset({'x'}), {}, frozenset(), {}, count - 1, 1, 1)
            with mock.patch.object(mt, 'run_arm', return_value=obs):
                reports = [mt.arm_report(arm, spec, context) for arm in arms]
            lines = [line for report in reports for line in report.lines]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(f'TOTAL={count}/786' in line for line in lines))
            incomplete = [error for report in reports for error in report.errors if 'incomplete-denominator' in error]
            self.assertEqual(len(incomplete), 0 if count == 786 else 2)
            if count == 785:
                self.assertTrue(all('incomplete-denominator=785/786' in line for line in incomplete))

    def test_main_denominator_line_names_both_populations(self):
        import io
        out = io.StringIO()
        with self.cheap_parent(), mock.patch.object(mt, 'run_workers', return_value=[]), contextlib.redirect_stdout(out):
            mt.main(['--spec', str(mt.TOOLS / 'mtbattery-spec.py'), '--only', 'C1'])
        lines = out.getvalue().splitlines()
        denominator = [line for line in lines if line.startswith('DENOMINATOR ')]
        self.assertEqual(len(denominator), 1)
        self.assertIn('786', denominator[0])
        self.assertIn('656', denominator[0])
        self.assertFalse(any(line.startswith('ARM=') for line in lines))


class TestMutationWorkers(unittest.TestCase):
    # These controls catch shared scratch, completion-order output, retries,
    # corrupt/missing records, and source writes across real process boundaries.
    @contextlib.contextmanager
    def workers(self, mode='normal'):
        import sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = root / 'worker.py'
            script.write_text('''import json, os, sys, time
from pathlib import Path
name, mode, root = sys.argv[1:]
with (Path(root) / (name + '.starts')).open('a') as stream:
    stream.write('start\\n')
if mode == 'crash' and name == '0':
    sys.exit(3)
if mode == 'malformed':
    print('MTBATTERY_REPORT {broken')
    sys.exit(0)
time.sleep((4-int(name)) * 0.02)
line = name
if mode == 'tmp':
    assert os.environ['TMPDIR'] == os.environ['TEMP'] == os.environ['TMP']
    assert Path(os.environ['TMPDIR']).is_dir()
    fd = os.fstat(0)
    line = json.dumps([os.environ['TMPDIR'], os.environ['PYTHONDONTWRITEBYTECODE'], os.getpgrp(), [fd.st_dev, fd.st_ino]])
if mode == 'write':
    (Path(root) / 'source' / 'leak').write_text('unexpected')
print('MTBATTERY_REPORT ' + json.dumps(dict(name=name, lines=['ARM=' + name + ' ' + line], errors=[], completed=True)))
''')
            arms = [{'name': str(i)} for i in range(4)]
            command = lambda name: [sys.executable, '-B', str(script), name, mode, str(root)]
            yield root, arms, command

    def test_start_order_declared_and_seeded(self):
        self.assertEqual(mt.start_order(spec.ARMS, None), list(spec.ARMS))
        first = mt.start_order(spec.ARMS, 297)
        self.assertEqual(first, mt.start_order(spec.ARMS, 297))
        self.assertCountEqual(first, spec.ARMS)
        self.assertNotEqual(first, list(spec.ARMS))

    def test_parse_jobs_default_and_bounds(self):
        import io, os
        argv = ['--spec', 'spec.py']
        self.assertEqual(mt.parse_args(argv).jobs, os.cpu_count())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mt.parse_args(argv + ['--jobs', '0'])

    def test_run_workers_output_independent_of_jobs_and_order(self):
        with self.workers() as (root, arms, command):
            expected = [mt.ArmReport(str(i), (f'ARM={i} {i}',), (), True) for i in range(4)]
            for jobs, seed in [(1, None), (3, None), (3, 1), (3, 2)]:
                self.assertEqual(mt.run_workers(arms, jobs, seed, root/'context', root, command), expected)

    def test_run_workers_give_each_worker_its_own_tmpdir(self):
        import json, os
        with self.workers('tmp') as (root, arms, command):
            before = mt.tree_digest(root)
            reports = mt.run_workers(arms[:3], 3, None, root/'context', root, command)
            values = [json.loads(report.lines[0].split(' ', 1)[1]) for report in reports]
            self.assertEqual(len({v[0] for v in values}), 3)
            for path, no_bytecode, group, descriptor in values:
                fd = os.fstat(0)
                self.assertEqual(descriptor, [fd.st_dev, fd.st_ino])
                self.assertEqual(Path(path).parent, root)
                self.assertFalse(Path(path).exists())
                self.assertEqual(no_bytecode, '1')
                self.assertEqual(group, os.getpgrp())
            self.assertEqual(mt.tree_digest(root)['worker.py'], before['worker.py'])

    def test_worker_without_record_is_hard_fail_started_once(self):
        with self.workers('crash') as (root, arms, command):
            reports = mt.run_workers(arms, 3, None, root/'context', root, command)
            self.assertFalse(reports[0].completed)
            self.assertIn('HARD-FAIL', reports[0].lines[0])
            self.assertIn('0', reports[0].lines[0])
            self.assertIn('3', reports[0].lines[0])
            self.assertEqual(len(reports[0].errors), 1)
            self.assertEqual((root/'0.starts').read_text(), 'start\n')
            self.assertTrue(all(r.completed for r in reports[1:]))

    def test_malformed_record_is_hard_fail(self):
        with self.workers('malformed') as (root, arms, command):
            reports = mt.run_workers(arms[:1], 1, None, root/'context', root, command)
            self.assertFalse(reports[0].completed)
            self.assertEqual(len(reports[0].errors), 1)

    def test_worker_report_equals_in_process_report(self):
        import io, json
        with TestMutationPopulations().tree() as (tools, arm):
            hp = tools/'check-relay-lint-fixtures.py'
            hp.write_text(hp.read_text() + '\nFIXTURES = Path(__file__).parent\nA5_CWD = {}\nA5_SEAM = {}\n')
            arm['kills'] = ['first']
            sp = tools/'spec.py';sp.write_text('ARMS = '+repr([arm])+'\nRAISES = {}\n')
            context = mt.RunContext(mt.DEFAULT_SUITE, {}, {}, (2, 3))
            run_file = tools.parent/'context'
            mt.write_run_file(run_file, context)
            self.assertEqual(mt.read_run_file(run_file), context)
            with mock.patch.object(mt, 'TOOLS', tools):
                expected = mt.arm_report(arm, SimpleNamespace(RAISES={}), context)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(mt.worker_main(sp, arm['name'], run_file), 0)
            lines = output.getvalue().splitlines();self.assertEqual(len(lines), 1)
            record = json.loads(lines[0].removeprefix('MTBATTERY_REPORT '))
            self.assertEqual(record, {'name': expected.name, 'lines': list(expected.lines),
                                     'errors': list(expected.errors), 'completed': expected.completed})
            self.assertTrue(expected.completed)
            self.assertEqual(expected.errors, ())

    def test_tree_digest_names_added_changed_and_removed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw);(root/'changed').write_text('a');(root/'removed').write_text('b')
            before = mt.tree_digest(root)
            self.assertEqual(mt.digest_changes(before, mt.tree_digest(root)), [])
            (root/'changed').write_text('z');(root/'removed').unlink()
            (root/'__pycache__').mkdir();(root/'__pycache__/x.pyc').write_bytes(b'x')
            changes = mt.digest_changes(before, mt.tree_digest(root))
            self.assertEqual(len(changes), 3)
            for path in ('changed', 'removed', '__pycache__/x.pyc'):
                self.assertTrue(any(path in c for c in changes))

    def test_real_worker_source_write_is_detected(self):
        with self.workers('write') as (root, arms, command):
            source = root/'source';source.mkdir();before = mt.tree_digest(source)
            mt.run_workers(arms[:1], 1, None, root/'context', root, command)
            changes = mt.digest_changes(before, mt.tree_digest(source))
            self.assertEqual(len(changes), 1);self.assertIn('leak', changes[0])

    def test_main_run_lines_bracket_the_run(self):
        import io, sys
        reports = [mt.ArmReport('dispatch-impl-false', ('ARM=dispatch-impl-false first-report',), (), True),
                   mt.ArmReport('dispatch-impl-true', ('ARM=dispatch-impl-true second-report',), (), True)]
        out = io.StringIO()
        with TestMutationPopulations().cheap_parent(), mock.patch.object(mt, 'run_workers', return_value=reports), contextlib.redirect_stdout(out):
            code = mt.main(['--spec', str(mt.TOOLS/'mtbattery-spec.py'), '--only', 'C1'])
        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('RUN '));self.assertIn(sys.executable, lines[0])
        self.assertIn('jobs=', lines[0]);self.assertIn('order=declared', lines[0])
        self.assertTrue(lines[-1].startswith('RUN '));self.assertIn('wall_seconds=', lines[-1])
        self.assertLess(lines.index('ARM=dispatch-impl-false first-report'), lines.index('ARM=dispatch-impl-true second-report'))

    def test_main_preflight_starts_no_worker(self):
        import io
        out = io.StringIO()
        with mock.patch.object(mt, 'structural_errors', return_value=['G3 seeded']), \
             mock.patch.object(mt, 'run_workers', side_effect=AssertionError('worker started')), contextlib.redirect_stdout(out):
            self.assertEqual(mt.main(['--spec', str(mt.TOOLS/'mtbattery-spec.py')]), 7)
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].startswith('RUN '));self.assertTrue(lines[-1].startswith('RUN '))

    def test_main_reports_a_source_change(self):
        import io
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            def write(*args, **kwargs):
                (root/'leaked').write_text('oops')
                return []
            out = io.StringIO()
            def load(name, path):
                return spec if name == 'mtbattery_spec' else harness
            with TestMutationPopulations().cheap_parent(), mock.patch.object(mt, 'TOOLS', root), \
                 mock.patch.object(mt, 'load_module', side_effect=load), \
                 mock.patch.object(mt, 'run_workers', side_effect=write), contextlib.redirect_stdout(out):
                self.assertEqual(mt.main(['--spec', str(Path(__file__).parents[2]/'mtbattery-spec.py'), '--only', 'C1']), 7)
            self.assertTrue(any(line.startswith('ERROR ') and 'leaked' in line for line in out.getvalue().splitlines()))

    def test_arm_report_uses_context_populations(self):
        context = mt.RunContext(mt.DEFAULT_SUITE, {}, {}, (656, 786))
        arm = {'name': 'standin', 'row': 'C8a', 'function': 'f', 'kills': ['x']}
        obs = mt.ArmObservation(frozenset({'x'}), {}, frozenset(), {}, 655, 1, 1)
        with mock.patch.object(mt, 'suite_populations', side_effect=AssertionError('recount')), \
             mock.patch.object(mt, 'suite_denominator', side_effect=AssertionError('recount')), \
             mock.patch.object(mt, 'run_arm', return_value=obs) as run:
            report = mt.arm_report(arm, SimpleNamespace(RAISES={}), context)
        self.assertTrue(report.completed);self.assertEqual(report.errors, ())
        self.assertEqual(run.call_args.args[3], 656)


class TestMutationDiagnostics(unittest.TestCase):
    def test_unstable_report_retains_both_observations(self):
        import json
        first = mt.ArmObservation(frozenset({'fixture'}), {'fixture': 'ValueError'},
                                  frozenset({'fixture', 'hidden'}), {'fixture': 'ValueError'}, 1, 1, 1)
        second = replace(first, raises={'fixture': 'TypeError'}, universal_raises={'fixture': 'TypeError'})
        arm = {'name': 'standin', 'row': 'C8a', 'function': 'f', 'kills': ['fixture']}
        context = mt.RunContext(mt.DEFAULT_SUITE, {}, {}, (2, 3))
        with mock.patch.object(mt, 'run_arm', side_effect=[first, second]) as observe:
            report = mt.arm_report(arm, SimpleNamespace(RAISES={}), context)
        self.assertEqual(observe.call_count, 2)
        self.assertFalse(report.completed)
        self.assertEqual(len(report.errors), 1)
        self.assertTrue(report.errors[0].startswith('UNSTABLE standin '))
        self.assertIn(' observations=', report.errors[0])
        records = json.loads(report.errors[0].split(' observations=', 1)[1])
        self.assertEqual(records, [
            {'kills': ['fixture'], 'raises': {'fixture': 'ValueError'},
             'universal_kills': ['fixture', 'hidden'], 'universal_raises': {'fixture': 'ValueError'},
             'passed': 1, 'failed': 1, 'exit': 1},
            {'kills': ['fixture'], 'raises': {'fixture': 'TypeError'},
             'universal_kills': ['fixture', 'hidden'], 'universal_raises': {'fixture': 'TypeError'},
             'passed': 1, 'failed': 1, 'exit': 1},
        ])


class TestFoldValidation(unittest.TestCase):
    # M2: malformed yet syntactically valid records must never earn completion.
    def test_parent_rejects_invalid_record_shapes(self):
        import json
        records = [
            dict(name='a', lines=[], errors=[], completed=True),
            dict(name='a', lines=['ARM=forged ROW=C1'], errors=[], completed=True),
            dict(name='a', lines=['ARM=a one', 'ARM=a two'], errors=[], completed=True),
            dict(name='a', lines=[], errors=[], completed=False),
            dict(name='wrong', lines=['ARM=a ok'], errors=[], completed=True),
        ]
        for record in records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                mt.decode_report(mt.REPORT_MARKER + json.dumps(record), 'a')
        with self.assertRaises(ValueError):
            mt.decode_report('', 'a')

    def parent(self, reports, extra=(), hook=None):
        import io
        out = io.StringIO()
        def workers(*args, **kwargs):
            if hook:
                hook(*args, **kwargs)
            return reports
        with TestMutationPopulations().cheap_parent(), \
                mock.patch.object(mt, 'run_workers', side_effect=workers), contextlib.redirect_stdout(out):
            code = mt.main(['--spec', str(Path(__file__).parents[2]/'mtbattery-spec.py'),
                            '--only', 'C1', *extra])
        return code, out.getvalue()

    def reports(self):
        return [mt.ArmReport(name, (f'ARM={name} valid',), (), True)
                for name in ('dispatch-impl-false', 'dispatch-impl-true')]

    def test_only_incomplete_is_not_success(self):
        reports = self.reports()
        reports[0] = replace(reports[0], completed=False, lines=())
        code, output = self.parent(reports)
        self.assertEqual(code, 7)
        self.assertIn('completed=1', output)

    def test_unknown_only_row_stops_before_workers(self):
        def unexpected(*args, **kwargs):
            self.fail('worker started for unknown selection')
        code, output = self.parent(self.reports(), ['--only', 'NOSUCHROW'], unexpected)
        self.assertEqual(code, 7)
        self.assertIn('NOSUCHROW', output)

    def test_digest_covers_changes_during_first_read(self):
        with tempfile.TemporaryDirectory() as raw:
            tools = Path(raw)
            path = tools/'changed';path.write_text('before')
            def load(name, source):
                path.write_text('after')
                return spec if name == 'mtbattery_spec' else harness
            with mock.patch.object(mt, 'TOOLS', tools), mock.patch.object(mt, 'load_module', side_effect=load):
                code, output = self.parent(self.reports())
        self.assertEqual(code, 7)
        self.assertIn('source tools changed changed', output)

    def test_full_population_unmutated_control(self):
        import sys
        # Real custom suite process; parent/arm orchestration stays real except
        # expensive fixture preflight and the independently exercised workers.
        for outcome, count, status, expected in [('FAIL', 2, 1, 7), ('PASS', 2, 0, 0),
                                                 ('PASS', 1, 0, 7), ('PASS', 2, 3, 7)]:
            with self.subTest(outcome=outcome, count=count, status=status), tempfile.TemporaryDirectory() as raw:
                tools = Path(raw);(tools/'source').write_text('unmutated')
                h = SimpleNamespace(EXPECTED=[('file', 'a', 0), ('file', 'b', 0)], EXPECTED_ERROR_SET={})
                def load(name, source):
                    return spec if name == 'mtbattery_spec' else h
                command = [sys.executable, '-B', '-c',
                           f"from pathlib import Path; assert Path('tools/source').read_text() == 'unmutated'; "
                           f"print('a: expected=0 observed={int(outcome == 'FAIL')} {outcome}'); "
                           + ("print('b: expected=0 observed=0 PASS'); " if count == 2 else '')
                           + f'raise SystemExit({status})']
                with mock.patch.object(mt, 'TOOLS', tools), mock.patch.object(mt, 'load_module', side_effect=load), \
                     mock.patch.object(mt, 'xroot_registration_count', return_value=0):
                    # cheap_parent supplies130; use a tiny full population of132.
                    h.EXPECTED = h.EXPECTED[:2]
                    # Override only the count boundary within the parent helper.
                    old = TestMutationPopulations.cheap_parent
                    @contextlib.contextmanager
                    def cheap(instance):
                        with old(instance), mock.patch.object(mt, 'xroot_registration_count', return_value=0):
                            yield
                    with mock.patch.object(TestMutationPopulations, 'cheap_parent', cheap):
                        code, output = self.parent(self.reports(), ['--', *command])
                self.assertEqual(code, expected, output)
                if outcome == 'FAIL':
                    self.assertIn('a', output)
                    self.assertIn('control', output.lower())


class TestFoldVerdicts(unittest.TestCase):
    def test_verdict_errors_reach_parent_exit(self):
        arm = {'name': 'dispatch-impl-false', 'row': 'C1', 'function': 'f', 'kills': ['x']}
        context = mt.RunContext(mt.DEFAULT_SUITE, {}, {}, (2, 3))
        good = mt.ArmObservation(frozenset({'x'}), {}, frozenset({'x'}), {}, 1, 1, 1)
        cases = [('O3', replace(good, kills=frozenset({'y'}), universal_kills=frozenset({'y'}))),
                 ('raises', replace(good, raises={'x': 'ValueError'})),
                 ('universal-exact-minus-registered', replace(good, universal_kills=frozenset({'x', 'extra'}))),
                 ('survivor', replace(good, kills=frozenset(), universal_kills=frozenset()))]
        for tag, observed in cases:
            with self.subTest(tag=tag), mock.patch.object(mt, 'run_arm', return_value=observed):
                report = mt.arm_report(arm, SimpleNamespace(RAISES={}), context)
                self.assertTrue(any(tag in error for error in report.errors), report)
                reports = TestFoldValidation().reports();reports[0] = report
                code, output = TestFoldValidation().parent(reports)
                self.assertEqual(code, 7)
                self.assertIn(tag, output)
        reports = TestFoldValidation().reports()
        reports[0] = replace(reports[0], errors=('worker evidence failed',))
        code, output = TestFoldValidation().parent(reports)
        self.assertEqual(code, 7);self.assertIn('worker evidence failed', output)

    def test_arm_report_exceptions_are_errors(self):
        arm = {'name': 'a', 'row': 'C1', 'function': 'f', 'kills': ['x']}
        context = mt.RunContext(mt.DEFAULT_SUITE, {}, {}, (2, 3))
        for exc, tag in [(mt.IncompleteSuiteError(1, 2), 'incomplete'), (RuntimeError('seeded'), 'seeded')]:
            with self.subTest(tag=tag), mock.patch.object(mt, 'run_arm', side_effect=exc):
                report = mt.arm_report(arm, SimpleNamespace(RAISES={}), context)
                self.assertFalse(report.completed)
                self.assertTrue(any(tag in error for error in report.errors), report)

    def test_guard_composes_all_four_checks(self):
        key = next(iter(observations))
        changed = {**observations, key: replace(observations[key], ok=not observations[key].ok)}
        for tag in ('G1', 'G2', 'G3', 'G4'):
            arms = [spec.ARMS[0]]
            local_spec = spec
            first, second = observations, observations
            if tag == 'G1': first = second = changed
            if tag == 'G2': second = changed
            if tag == 'G3': arms = [{**arms[0], 'original': 'missing-anchor-fold'}]
            if tag == 'G4':
                local_spec = SimpleNamespace(**{**vars(spec), 'KILLS': {**spec.KILLS, spec.ARMS[0]['name']: ['missing-fold-label']}})
            with self.subTest(tag=tag), mock.patch.object(mt, 'lint_observations', side_effect=[first, second]), \
                 mock.patch.object(mt, 'xroot_labels', return_value=labels):
                errors = mt.structural_errors(local_spec, harness, arms, False)
                self.assertTrue(any(error.startswith(tag+' ') for error in errors), errors)

    def test_main_passes_jobs_and_shuffle(self):
        seen = []
        def workers(arms, jobs, seed, *args, **kwargs):
            seen.append((jobs, seed))
        code, _ = TestFoldValidation().parent(TestFoldValidation().reports(), ['--jobs', '2', '--shuffle', '297'], workers)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(2, 297)])

    def test_parent_preserves_nonalphabetic_declared_order(self):
        import io
        arms = [a for a in spec.ARMS if a['row'] == 'C1'][::-1]
        local_spec = SimpleNamespace(**{**vars(spec), 'ARMS': arms})
        def load(name, path):
            return local_spec if name == 'mtbattery_spec' else harness
        reports = TestFoldValidation().reports()[::-1]
        with mock.patch.object(mt, 'load_module', side_effect=load):
            code, output = TestFoldValidation().parent(reports)
        self.assertEqual(code, 0)
        self.assertLess(output.index('ARM=dispatch-impl-true '), output.index('ARM=dispatch-impl-false '))

    def test_workers_apply_shuffle_and_job_bounds(self):
        import json, sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw);script = root/'worker.py';events = root/'events'
            script.write_text('''import json, os, sys, time
name, events = sys.argv[1:]
def record(kind):
    fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, (kind + ' ' + name + '\\n').encode())
    finally: os.close(fd)
record('start')
time.sleep(0.15)
record('end')
print('MTBATTERY_REPORT ' + json.dumps(dict(name=name, lines=['ARM=' + name + ' ok'], errors=[], completed=True)))
''')
            arms = [{'name': name} for name in ('z', 'a', 'q', 'b')]
            command = lambda name: [sys.executable, '-B', str(script), name, str(events)]
            for jobs, seed in [(1, 297), (2, 297)]:
                if events.exists(): events.unlink()
                reports = mt.run_workers(arms, jobs, seed, root/'context', root, command)
                self.assertEqual([r.name for r in reports], ['z', 'a', 'q', 'b'])
                active = peak = 0;starts = []
                for line in events.read_text().splitlines():
                    kind, name = line.split()
                    active += 1 if kind == 'start' else -1
                    if kind == 'start': starts.append(name)
                    peak = max(peak, active)
                    self.assertLessEqual(active, jobs)
                self.assertEqual(active, 0)
                if jobs == 1:
                    self.assertEqual(starts, [a['name'] for a in mt.start_order(arms, seed)])
                else:
                    self.assertGreater(peak, 1)


# After a killed worker the battery promises no immediate reap: the suite
# runner notices its parent's death on stop_scope's `while not
# finished.wait(0.05):` poll and then tears down, so each suite process is
# gone within one watcher poll plus STOP_GRACE.
WATCH_POLL_SECONDS = 0.05
REAP_BOUND = mt.STOP_GRACE + WATCH_POLL_SECONDS


class TestFoldLifecycle(unittest.TestCase):
    # Child processes exercise signal delivery, descriptor inheritance and
    # partial output across the actual worker scheduler, not mocked futures.
    @contextlib.contextmanager
    def driver(self, mode='block', hold_teardown=False):
        import sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            worker = root/'worker.py'
            worker.write_text('''import json, os, sys, time, signal
from pathlib import Path
import mtbattery as mt
name, mode, root = sys.argv[1:]
root = Path(root)
staged = root / (name + '.pid.' + str(os.getpid()) + '.tmp'); staged.write_text(str(os.getpid())); os.replace(staged, root / (name + '.pid'))
if mode == 'ordered':
    if name == 'z':
        while not (root/'release').exists(): time.sleep(0.01)
    elif name == 'b':
        while not (root/'last').exists(): time.sleep(0.01)
else:
    if name == 'a':
        with mt.stop_scope(watch_parent=True):
            result = mt.managed_run([sys.executable, '-B', mt.__file__, '--suite-runner', str(root/'harness.py')])
            if result.returncode: raise SystemExit(7)
print('MTBATTERY_REPORT ' + json.dumps(dict(name=name, lines=['ARM=' + name + ' ok'], errors=[], completed=True)), flush=True)
''')
            # A held teardown keeps the suite runner alive at its exit until
            # `release` exists, so it is certain to outlive the driver.
            hold = '''import atexit, time
def hold_teardown():
    end = time.monotonic() + 20
    while not (root/'release').exists() and time.monotonic() < end: time.sleep(0.01)
atexit.register(hold_teardown)
''' if hold_teardown else ''
            (root/'harness.py').write_text("""import os, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
root = Path(__file__).parent
""" + hold + """staged = root/('child.pid.' + str(os.getpid()) + '.tmp'); staged.write_text(str(os.getpid())); os.replace(staged, root/'child.pid')
def load_linter(): return SimpleNamespace()
def main():
    subprocess.run([sys.executable, '-c', "import os,time; from pathlib import Path; p = Path(" + repr(str(root/'grandchild.pid')) + "); t = p.with_name(p.name + '.' + str(os.getpid()) + '.tmp'); t.write_text(str(os.getpid())); os.replace(t, p); time.sleep(60)"])
    return 0
""")
            driver = root/'driver.py'
            driver.write_text('''import os, sys
from pathlib import Path
import mtbattery as mt
root = Path(sys.argv[1]); mode = sys.argv[2]
arms = [{'name': name} for name in ('z', 'a', 'b')]
def command(name):
    with (root/'starts').open('a') as f: f.write(name + '\\n')
    return [sys.executable, '-B', str(root/'worker.py'), name, mode, str(root)]
mt.emit('RUN start')
try:
    with mt.stop_scope():
        reports = mt.run_workers(arms, 1 if mode == 'block' else 3, None, root/'context', root, command,
                                 on_report=lambda report: [mt.emit(line) for line in report.lines])
    code = 7 if any(r.errors for r in reports) else 0
finally:
    mt.emit('RUN end')
raise SystemExit(code)
''')
            yield root, [sys.executable, '-B', str(driver), str(root), mode]

    def wait_for(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(0.02)
        self.fail('child did not reach the synchronization point')

    def wait_for_pid(self, path, timeout=8):
        # A pid file's existence is not its pid: wait until its text parses.
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try: text = Path(path).read_text().strip()
            except FileNotFoundError: text = ''
            if text.isascii() and text.isdigit() and int(text) > 0: return int(text)
            time.sleep(0.02)
        self.fail('child did not reach the synchronization point')

    def wait_until_gone(self, pid, bound=REAP_BOUND):
        import os
        end = time.monotonic() + bound
        while True:
            try: os.kill(pid, 0)
            except ProcessLookupError: return
            if time.monotonic() >= end: break
            time.sleep(0.02)
        self.fail(f'suite process {pid} not gone within {bound:.2f} s (watcher poll plus STOP_GRACE)')

    def test_stop_forwards_to_child_and_cancels_queue(self):
        import os, signal, subprocess
        for sig, group in [(signal.SIGTERM, False), (signal.SIGINT, True)]:
            with self.subTest(signal=sig, group=group), self.driver() as (root, command):
                with (root/'output').open('w+') as out:
                    process = subprocess.Popen(command, stdout=out, stderr=out, start_new_session=True)
                    try:
                        self.wait_for(lambda: (root/'grandchild.pid').exists() or process.poll() is not None)
                        self.assertIsNone(process.poll(), (root/'output').read_text())
                        pids = [self.wait_for_pid(root/name) for name in ('grandchild.pid', 'a.pid', 'child.pid')]
                        os.killpg(process.pid, sig) if group else os.kill(process.pid, sig)
                        self.assertEqual(process.wait(timeout=10), 7)
                        self.assertEqual((root/'starts').read_text().splitlines(), ['z', 'a'])
                        output = (root/'output').read_text()
                        self.assertIn('ARM=z ok', output)
                        self.assertIn('UNOBSERVED ARM=a', output)
                        self.assertIn('UNOBSERVED ARM=b', output)
                        self.assertTrue(output.rstrip().endswith('RUN end'), output)
                        self.assertFalse(list(root.glob('arm-*')))
                        for pid in pids:
                            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                    finally:
                        if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def test_ordered_prefix_flushes_before_last_finishes(self):
        import os, signal, subprocess
        with self.driver('ordered') as (root, command), (root/'output').open('w+') as out:
            process = subprocess.Popen(command, stdout=out, stderr=out, start_new_session=True)
            try:
                self.wait_for(lambda: (root/'b.pid').exists() or process.poll() is not None)
                self.assertIsNone(process.poll(), (root/'output').read_text())
                self.assertNotIn('ARM=a ok', (root/'output').read_text())
                (root/'release').touch()
                self.wait_for(lambda: 'ARM=a ok' in (root/'output').read_text())
                output = (root/'output').read_text()
                self.assertLess(output.index('ARM=z ok'), output.index('ARM=a ok'))
                self.assertNotIn('ARM=b ok', output)
                (root/'last').touch()
                self.assertEqual(process.wait(timeout=8), 0)
            finally:
                if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def run_parent_death(self, publish):
        # The parent starts a suite runner, publishes its pid by `publish`
        # and is killed; the runner must notice the death and exit.
        import os, signal, subprocess, sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); harness_path = root/'harness.py'; parent = root/'parent.py'
            harness_path.write_text('''import time
from types import SimpleNamespace
def load_linter(): return SimpleNamespace(lint_file=lambda path: None)
def main():
    while True:
        load_linter().lint_file('x')
        time.sleep(0.02)
''')
            parent.write_text('''import os, subprocess, sys, time
from pathlib import Path
p = subprocess.Popen([sys.executable, '-B', sys.argv[1], '--suite-runner', sys.argv[2]], env={**os.environ, 'MTBATTERY_PARENT_PID': str(os.getpid())})
''' + publish + '''
time.sleep(60)
''')
            with (root/'output').open('w+') as out:
                process = subprocess.Popen([sys.executable, '-B', str(parent), mt.__file__, str(harness_path), str(root/'suite.pid')], stdout=out, stderr=out, start_new_session=True)
                try:
                    pid = self.wait_for_pid(root/'suite.pid')
                    os.kill(process.pid, signal.SIGKILL);process.wait()
                    def gone():
                        try: os.kill(pid, 0)
                        except ProcessLookupError: return True
                        return False
                    self.wait_for(gone)
                finally:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()

    def test_suite_runner_notices_parent_death(self):
        self.run_parent_death("staged = Path(sys.argv[3] + '.' + str(os.getpid()) + '.tmp'); staged.write_text(str(p.pid)); os.replace(staged, sys.argv[3])")

    def test_parent_death_survives_slow_pid_write(self):
        # Create-then-write on purpose: suite.pid exists empty for 0.5 s,
        # the window in which a reader of an existing file finds no pid.
        self.run_parent_death("Path(sys.argv[3]).touch(); time.sleep(0.5); staged = Path(sys.argv[3] + '.' + str(os.getpid()) + '.tmp'); staged.write_text(str(p.pid)); os.replace(staged, sys.argv[3])")

    def test_wait_for_pid_waits_through_an_empty_file(self):
        import subprocess, sys, threading
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); path = root/'slow.pid'
            # Create-then-write on purpose: the child writes only once `go`
            # exists, so the helper is certain to meet the empty file first.
            child = subprocess.Popen([sys.executable, '-c', "import os,sys,time; from pathlib import Path; p = Path(sys.argv[1]); go = Path(sys.argv[2]); p.touch()\nwhile not go.exists(): time.sleep(0.01)\np.write_text(str(os.getpid())); time.sleep(60)", str(path), str(root/'go')])
            try:
                self.wait_for(path.exists)
                self.assertEqual(path.read_text(), '')
                with self.assertRaises(AssertionError) as caught: self.wait_for_pid(path, timeout=0.1)
                self.assertEqual(str(caught.exception), 'child did not reach the synchronization point')
                release = threading.Timer(0.3, (root/'go').touch)
                release.start()
                try: self.assertEqual(self.wait_for_pid(path), child.pid)
                finally: release.join()
            finally:
                child.kill(); child.wait()

    def test_wait_for_pid_fails_on_a_file_that_stays_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)/'empty.pid'; path.touch()
            with self.assertRaises(AssertionError) as caught: self.wait_for_pid(path, timeout=0.2)
            self.assertEqual(str(caught.exception), 'child did not reach the synchronization point')

    def test_killed_worker_keeps_signal_and_reaps_suite(self):
        import os, signal, subprocess
        with self.driver() as (root, command), (root/'output').open('w+') as out:
            process = subprocess.Popen(command, stdout=out, stderr=out, start_new_session=True)
            try:
                grandchild = self.wait_for_pid(root/'grandchild.pid')
                child = self.wait_for_pid(root/'child.pid')
                os.kill(self.wait_for_pid(root/'a.pid'), signal.SIGKILL)
                self.assertEqual(process.wait(timeout=8), 7)
                output = (root/'output').read_text()
                self.assertIn('SIGKILL', output)
                for pid in (child, grandchild):
                    self.wait_until_gone(pid)
                self.assertNotIn('Errno 66', output)
                self.assertIn('ARM=b ok', output)
                self.assertFalse(list(root.glob('arm-*')))
            finally:
                if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def test_reap_check_waits_out_a_held_teardown(self):
        # The driver does not wait for its worker's descendants, so a suite
        # runner held at its exit is still alive when the driver exits 7.
        import os, signal, subprocess
        with self.driver(hold_teardown=True) as (root, command), (root/'output').open('w+') as out:
            process = subprocess.Popen(command, stdout=out, stderr=out, start_new_session=True)
            try:
                grandchild = self.wait_for_pid(root/'grandchild.pid')
                child = self.wait_for_pid(root/'child.pid')
                os.kill(self.wait_for_pid(root/'a.pid'), signal.SIGKILL)
                self.assertEqual(process.wait(timeout=8), 7)
                os.kill(child, 0)  # held: the suite runner outlives the driver's exit
                (root/'release').touch()
                for pid in (child, grandchild):
                    self.wait_until_gone(pid)
            finally:
                (root/'release').touch()
                if process.poll() is None: os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def test_wait_until_gone_fails_on_a_live_process(self):
        import subprocess
        child = subprocess.Popen(['sleep', '60'])
        try:
            with self.assertRaises(AssertionError) as caught: self.wait_until_gone(child.pid, bound=0.2)
            self.assertEqual(str(caught.exception),
                             f'suite process {child.pid} not gone within 0.20 s (watcher poll plus STOP_GRACE)')
        finally:
            child.kill(); child.wait()
        # Control: once the child is killed and waited, the helper returns at once.
        self.wait_until_gone(child.pid, bound=0.2)

    def test_worker_entry_unwinds_owned_subprocess(self):
        import os, signal, subprocess, sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw);driver = root/'driver.py'
            driver.write_text('''import subprocess, sys
import mtbattery as mt
def work(*args):
    subprocess.run([sys.executable, '-c', "from pathlib import Path; import os,time; p = Path(" + repr(sys.argv[1]) + "); t = p.with_name(p.name + '.' + str(os.getpid()) + '.tmp'); t.write_text(str(os.getpid())); os.replace(t, p); time.sleep(60)"])
    return 0
mt.worker_main = work
raise SystemExit(mt.main(['--spec', 'unused', '--worker-arm', 'standin', '--run-file', 'unused']))
''')
            with (root/'output').open('w+') as out:
                process = subprocess.Popen([sys.executable, '-B', str(driver), str(root/'child.pid')], stdout=out, stderr=out, start_new_session=True)
                try:
                    child = self.wait_for_pid(root/'child.pid')
                    os.kill(process.pid, signal.SIGTERM)
                    self.assertNotEqual(process.wait(timeout=7), 0)
                    with self.assertRaises(ProcessLookupError): os.kill(child, 0)
                finally:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()


class TestFoldStopBoundaries(unittest.TestCase):
    def test_stop_during_control_names_unobserved_selection(self):
        import os, signal
        def stopped(*args):
            os.kill(os.getpid(), signal.SIGTERM)
            return ['control stopped']
        with mock.patch.object(mt, 'unmutated_control', side_effect=stopped):
            code, output = TestFoldValidation().parent([], ['--', 'standin-command'])
        self.assertEqual(code, 7)
        for name in ('dispatch-impl-false', 'dispatch-impl-true'):
            self.assertIn('UNOBSERVED ARM='+name, output)
        self.assertTrue(output.splitlines()[-1].startswith('RUN wall_seconds='))

    def test_run_directory_cleanup_preserves_worker_error(self):
        original = shutil.rmtree
        failed = []
        def remove(path, *args, **kwargs):
            if Path(path).name.startswith('mtbattery-run-'):
                failed.append(Path(path))
                raise OSError('seeded cleanup failure')
            return original(path, *args, **kwargs)
        reports = TestFoldValidation().reports()
        reports[0] = replace(reports[0], errors=('worker exit=-9 signal=SIGKILL',), completed=False)
        try:
            with mock.patch.object(mt.shutil, 'rmtree', side_effect=remove), mock.patch.object(mt, 'CLEANUP_GRACE', 0):
                code, output = TestFoldValidation().parent(reports)
            self.assertEqual(code, 7)
            self.assertIn('SIGKILL', output)
            self.assertIn('cleanup', output)
            self.assertTrue(any(str(path) in output for path in failed))
        finally:
            for path in set(failed):
                if path.exists(): original(path)

    def test_invalid_worker_records_fail_through_parent(self):
        import json, subprocess, sys
        name = 'dispatch-impl-false'
        variants = ['', mt.REPORT_MARKER+json.dumps(dict(name=name, lines=[], errors=[], completed=True)),
                    mt.REPORT_MARKER+json.dumps(dict(name=name, lines=['ARM=forged ok'], errors=[], completed=True)),
                    mt.REPORT_MARKER+json.dumps(dict(name=name, lines=[], errors=[], completed=False))]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for output in variants:
                with self.subTest(output=output):
                    reports = mt.run_workers([{'name': name}], 1, None, root/'context', root,
                        lambda _: [sys.executable, '-B', '-c', 'print('+repr(output)+')'])
                    self.assertFalse(reports[0].completed)
                    code, text = TestFoldValidation().parent(reports + TestFoldValidation().reports()[1:])
                    self.assertEqual(code, 7)
                    self.assertIn(name, text)
                    self.assertIn('HARD-FAIL', text)


class TestFoldReviewRegressions(unittest.TestCase):
    def test_cancellation_during_command_preparation_starts_no_worker(self):
        import os, signal, subprocess, sys
        launches = []
        original = subprocess.Popen
        def spawn(*args, **kwargs):
            launches.append(args[0])
            return original(*args, **kwargs)
        def command(name):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(mt._STOP.event.wait(2))
            return [sys.executable, '-B', '-c', 'pass']
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(mt.subprocess, 'Popen', side_effect=spawn):
            root = Path(raw)
            reports = mt.run_workers([{'name': 'a'}, {'name': 'b'}], 1, None, root/'context', root, command)
        self.assertEqual(launches, [])
        self.assertTrue(all(not report.completed for report in reports))

    def test_concurrent_launch_order_survives_thread_reordering(self):
        import json, sys, threading
        from concurrent.futures import ThreadPoolExecutor
        second = threading.Event()
        starts = []
        class ReorderedPool(ThreadPoolExecutor):
            def submit(self, fn, arm):
                index = getattr(self, 'submitted', 0)
                self.submitted = index + 1
                def run():
                    if index == 0:
                        # Let the second submitted thread enter run first.
                        second.wait(2)
                        time.sleep(0.05)
                    elif index == 1:
                        second.set()
                    return fn(arm)
                return super().submit(run)
        def command(name):
            starts.append(name)
            record = mt.REPORT_MARKER+json.dumps(dict(name=name, lines=['ARM='+name+' ok'], errors=[], completed=True))
            return [sys.executable, '-B', '-c', 'print('+repr(record)+')']
        arms = [{'name': name} for name in ('z', 'a', 'q', 'b')]
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(mt, 'ThreadPoolExecutor', ReorderedPool):
            root = Path(raw)
            reports = mt.run_workers(arms, 2, 297, root/'context', root, command)
        self.assertEqual(starts, ['z', 'a', 'b', 'q'])
        self.assertTrue(all(report.completed for report in reports))

    def test_parent_preflight_stop_unwinds_child_and_reports_selection(self):
        import os, signal, subprocess, sys
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw);driver = root/'driver.py'
            (root/'spec.py').write_text("ARMS = [{'name': 'a', 'row': 'C1'}]\n")
            driver.write_text('''import subprocess,sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import mtbattery as mt
root=Path(sys.argv[1])
def preflight(*args):
    subprocess.run([sys.executable, '-B', '-c', "import os,time; from pathlib import Path; p = Path(" + repr(str(root/'child.pid')) + "); t = p.with_name(p.name + '.' + str(os.getpid()) + '.tmp'); t.write_text(str(os.getpid())); os.replace(t, p); time.sleep(60)"])
    return []
with mock.patch.object(mt,'tree_digest',return_value={}), mock.patch.object(mt,'structural_errors',side_effect=preflight), mock.patch.object(mt,'load_module',return_value=SimpleNamespace(ARMS=[{'name':'a','row':'C1'}])):
    raise SystemExit(mt.main(['--spec',str(root/'spec.py')]))
''')
            with (root/'output').open('w+') as out:
                process = subprocess.Popen([sys.executable, '-B', str(driver), str(root)], stdout=out, stderr=out, start_new_session=True)
                try:
                    child = TestFoldLifecycle().wait_for_pid(root/'child.pid')
                    os.kill(process.pid, signal.SIGTERM)
                    self.assertEqual(process.wait(timeout=6), 7)
                    with self.assertRaises(ProcessLookupError): os.kill(child, 0)
                    output = (root/'output').read_text()
                    self.assertIn('UNOBSERVED ARM=a', output)
                    self.assertTrue(output.splitlines()[-1].startswith('RUN wall_seconds='))
                finally:
                    try: os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    process.wait()
