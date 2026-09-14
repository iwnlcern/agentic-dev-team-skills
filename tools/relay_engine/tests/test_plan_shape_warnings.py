import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from relay_engine import rules

TOOLS = Path(__file__).parents[2]
SCRIPT = TOOLS / 'relay-lint.py'
CONTROL = TOOLS / 'relay-engine-fixtures/plan-shape/PL-v291-b3-20260816.md'


def _standalone():
    spec = importlib.util.spec_from_file_location('v29_frozen_relay_lint', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules():
    return (rules, _standalone())


def _relay(body='', *, dispatch='x-plan', sender='qi.planner', phase='PLAN', locator=None, digest=None):
    fields = [f'ROLE: Planner', f'PHASE: {phase}', 'AUTHORITY: plan-only',
              f'DISPATCH_ID: {dispatch}', 'CEREMONY_TIER: medium',
              'EVIDENCE_TARGET: E2', 'HUMAN_GATE_REQUIRED: no', f'FROM: {sender}',
              'TO: qi.implementer', 'FINAL_GIT_STATUS_SHORT: none — fixture only']
    if locator is not None:
        fields.append(f'PLAN_ARTIFACT: {locator}')
    if digest is not None:
        fields.append(f'PLAN_SHA256: {digest}')
    return '\n'.join(fields) + '\n\n' + body + '\n'


def _file(root, text, name='01-plan.md'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestPlanShapeWarnings(unittest.TestCase):
    @staticmethod
    def _raw_inline_body(crlf=False):
        headers = b'ROLE: Planner\r\nPHASE: PLAN\r\nFROM: qi.planner\r\n'
        if crlf:
            return headers + (b'x' * 64 + b'\r\n') * 993
        headers = headers.replace(b'\r\n', b'\n')
        return headers + b'x' * (65500 - len(headers)) + b'\xff' * 30

    def test_inline_body_measures_raw_bytes_crlf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'PLAN-pair-planner-20260914-000000.md'
            body = self._raw_inline_body(crlf=True)
            path.write_bytes(body)
            self.assertEqual(len(body), 65584)
            self.assertEqual(len(path.read_text().encode()), 64588)
            for module in _modules():
                with self.subTest(module=module.__name__):
                    warnings = module.lint_file(path, artifact_root=root).warnings
                    self.assertIn('plan shape: no PLAN_ARTIFACT declared; the relay body was measured', warnings)
                    self.assertIn('plan shape: relay body over threshold: bytes 65584 > 65536', warnings)

    def test_inline_body_invalid_utf8_counts_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'PLAN-pair-planner-20260914-000000.md'
            body = self._raw_inline_body()
            path.write_bytes(body)
            self.assertEqual(len(body), 65530)
            self.assertEqual(len(body.decode('utf-8', errors='replace').encode()), 65590)
            for module in _modules():
                with self.subTest(module=module.__name__):
                    warnings = module.lint_file(path, artifact_root=root).warnings
                    self.assertFalse(any('bytes ' in warning for warning in warnings), warnings)

    def _raw_record_result(self, root, body):
        path = root / 'PLAN-pair-planner-20260914-000000.md'
        path.write_bytes(body)
        context = {'snapshot': '1', 'entries': [{
            'path': path.name, 'body_sha256': '0' * 64, 'origin': 'daemon'}]}
        result = rules.lint_relay_root(
            root, engine_root=True, record_context=context, context_mode='daemon')
        self.assertTrue(any('digest-mismatch' in finding
                            for finding in result.errors + result.warnings), result)
        return result

    def test_engine_record_path_supplies_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._raw_record_result(Path(temporary), self._raw_inline_body())
            self.assertFalse(any('bytes ' in warning for warning in result.warnings), result.warnings)

    def test_engine_record_path_crlf_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._raw_record_result(Path(temporary), self._raw_inline_body(crlf=True))
            self.assertTrue(any('plan shape: relay body over threshold: bytes 65584 > 65536'
                                in warning for warning in result.warnings), result.warnings)

    def test_corpus_script_prints_six_measures(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / 'empty.md'
            empty.write_bytes(b'')
            completed = subprocess.run(
                [sys.executable, str(TOOLS / 'measure-plan-shape.py'), str(CONTROL), str(empty)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.splitlines(), [
                f'{CONTROL} 455 166 0.36 18 46035 1086 b7c5525e43af -',
                f'{empty} 0 0 0.00 0 0 0 e3b0c44298fc -'])

    def test_corpus_script_reports_unlisted_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            listing = root / 'listing.md'
            listing.write_text('| corpus | path | a | b | c | d | e | 0 | 000000000000 | - |\n')
            extra = root / 'extra.md'
            extra.write_bytes(b'')
            completed = subprocess.run(
                [sys.executable, str(TOOLS / 'measure-plan-shape.py'),
                 '--expect', str(listing), str(extra)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.splitlines(), [f'UNLISTED {extra}'])
            listed = root / 'listed.md'
            listed.write_bytes(b'')
            listing.write_text(
                f'| sample | {listed} | a | b | c | d | e | 0 | e3b0c44298fc | block |\n')
            continued = subprocess.run(
                [sys.executable, str(TOOLS / 'measure-plan-shape.py'),
                 '--expect', str(listing), str(extra), str(listed)],
                capture_output=True, text=True)
            self.assertEqual(continued.returncode, 1, continued.stderr)
            self.assertEqual(continued.stdout.splitlines(),
                             [f'UNLISTED {extra}', f'MISMATCH {listed}'])

    def _cli_draft(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / 'relays'
        draft = _file(root, _relay('x' * 66000), '.engine/drafts/qi.planner/draft.md')
        return temp, root, draft

    def _engine_draft(self, root, draft, with_root=True):
        args = [sys.executable, str(TOOLS / 'relay'), 'lint']
        if with_root:
            args += ['--relay-root', str(root)]
        completed = subprocess.run(args + [str(draft)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        return json.loads(completed.stdout)[str(draft)]

    def _standalone_draft(self, root, draft, with_root=True):
        args = [sys.executable, str(SCRIPT)]
        if with_root:
            args += ['--relay-root', str(root)]
        completed = subprocess.run(args + [str(draft)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn(f'ERROR {draft}: filename carries no YYYYMMDD-HHMMSS timestamp', completed.stdout)
        prefix = f'WARN {draft}: '
        return [line[len(prefix):] for line in completed.stdout.splitlines() if line.startswith(prefix)]

    def test_engine_cli_root_pass_through(self):
        temp, root, draft = self._cli_draft()
        with temp:
            result = self._engine_draft(root, draft)
            self.assertIn('plan shape: relay body over threshold: bytes 66217 > 65536, longest line 66000 > 8000', result['warnings'])
            self.assertTrue(any('filename carries no YYYYMMDD-HHMMSS timestamp' in error for error in result['errors']))
            self.assertFalse(any('plan shape' in line for line in self._engine_draft(root, draft, False)['warnings']))

    def test_standalone_cli_root_pass_through(self):
        temp, root, draft = self._cli_draft()
        with temp:
            warnings = self._standalone_draft(root, draft)
            self.assertIn('plan shape: relay body over threshold: bytes 66217 > 65536, longest line 66000 > 8000', warnings)
            self.assertFalse(any('plan shape' in line for line in self._standalone_draft(root, draft, False)))

    def test_both_clis_agree_on_the_draft(self):
        temp, root, draft = self._cli_draft()
        with temp:
            engine = [line for line in self._engine_draft(root, draft)['warnings'] if line.startswith('plan shape: relay body over threshold:')]
            standalone = [line for line in self._standalone_draft(root, draft) if line.startswith('plan shape: relay body over threshold:')]
            self.assertEqual(len(engine), 1)
            self.assertEqual(engine, standalone)

    def test_measure_matches_a5_control(self):
        env = os.environ.get('RELAY_A5_CONTROL')
        path = Path(env) if env else CONTROL
        self.assertTrue(path.is_file(), f'missing control: RELAY_A5_CONTROL={env!r}; fixture={CONTROL}')
        data = path.read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), 'b7c5525e43af5a68b9466e3d3bcedaf7092431004999b46026e206b64b289c30')
        for module in _modules():
            self.assertEqual(tuple(module.plan_shape_measure(data)), (455, 166, 18, 46035, 1086))

    def test_measure_backtick_info_opener_is_content(self):
        for module in _modules():
            self.assertEqual(module.plan_shape_measure(b'```bad`info\ninside\n').fenced, 0)

    def test_measure_pseudo_closer_with_suffix_stays_inside(self):
        for module in _modules():
            self.assertEqual(module.plan_shape_measure(b'```\na\n  ``` suffix\nb\n```\n').fenced, 3)

    def test_measure_unclosed_fence_runs_to_eof(self):
        for module in _modules():
            shape = module.plan_shape_measure(b'~~~\na\nb\n')
            self.assertEqual((shape.lines, shape.fenced, shape.largest_block), (3, 2, 2))

    def test_measure_shorter_closer_stays_inside(self):
        for module in _modules():
            shape = module.plan_shape_measure(b'````\na\n```\nb\n````\n')
            self.assertEqual((shape.lines, shape.fenced, shape.largest_block), (5, 3, 3))

    def test_measure_crlf_and_trailing_newline(self):
        for module in _modules():
            self.assertEqual(module.plan_shape_measure(b'a\r\nb\r\n').lines, 2)
            self.assertEqual(module.plan_shape_measure(b'a\nb\n').lines, 2)
            self.assertEqual(module.plan_shape_measure('a\u2028b\nc\n'.encode()).lines, 2)
            self.assertEqual(module.plan_shape_measure('a\x85b\rc\n'.encode()).lines, 2)

    def test_measure_invalid_utf8_never_raises(self):
        for module in _modules():
            self.assertEqual(module.plan_shape_measure(b'a\xff\n').byte_count, 3)

    def test_ratio_is_integer_relation(self):
        for module in _modules():
            shape = module.PlanShape
            self.assertIn('ratio', [x[0] for x in module.plan_shape_exceeded(shape(401, 201, 0, 0, 0))])
            self.assertNotIn('ratio', [x[0] for x in module.plan_shape_exceeded(shape(402, 201, 0, 0, 0))])
            self.assertNotIn('ratio', [x[0] for x in module.plan_shape_exceeded(shape(400, 201, 0, 0, 0))])
            self.assertNotIn('fenced', [x[0] for x in module.plan_shape_exceeded(shape(400, 400, 0, 0, 0))])

    def test_exceeded_order_and_display(self):
        for module in _modules():
            got = module.plan_shape_exceeded(module.PlanShape(1501, 801, 81, 65537, 8001))
            self.assertEqual([x[0] for x in got], ['block', 'fenced', 'ratio', 'lines', 'bytes', 'longest line'])
            self.assertEqual(got[2], ('ratio', '0.53', '0.50'))

    def test_ordinal_grammar(self):
        valid = {'x-plan': 1, 'x-plan-7': 7, 'x-plan-999': 999}
        invalid = ('x-plan-review-3', 'x-plan-i2', 'x-plan-r450', 'x-plan-20260913', 'x-plan-1-corrected', 'x-plan-08', 'x-plan-1000')
        for module in _modules():
            for value, expected in valid.items():
                self.assertEqual(module.plan_revision_ordinal(value), expected)
            for value in invalid:
                self.assertIsNone(module.plan_revision_ordinal(value))

    def test_ordinal_warning_at_eight_only_for_pair_planner_plan(self):
        for module in _modules():
            fields = {'PHASE': 'PLAN', 'FROM': 'qi.planner', 'DISPATCH_ID': 'x-plan-8'}
            self.assertEqual(module.plan_revision_ordinal_warning(fields), 'plan revision ordinal 8 declared by DISPATCH_ID; the corpus median is 5')
            self.assertIsNone(module.plan_revision_ordinal_warning({**fields, 'DISPATCH_ID': 'x-plan-7'}))
            self.assertIsNone(module.plan_revision_ordinal_warning({**fields, 'FROM': 'v29.orchestrator-planner'}))
            self.assertIsNone(module.plan_revision_ordinal_warning({**fields, 'PHASE': 'PLAN-REVIEW'}))

    def test_ordinal_warning_without_root_in_both_implementations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = _file(Path(temp), _relay(dispatch='x-plan-8'))
            for module in _modules():
                warnings = module.lint_file(path).warnings
                self.assertIn('plan revision ordinal 8 declared by DISPATCH_ID; the corpus median is 5', warnings)
                self.assertFalse(any('plan shape' in x for x in warnings))

    def test_shape_warning_needs_root_and_applicability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _file(root, 'x' * 66000, 'plans/target.md')
            plan = _file(root, _relay(locator='target'))
            other = _file(root, _relay(sender='v29.orchestrator-planner', locator='target'), '02-plan.md')
            for module in _modules():
                self.assertTrue(any(x.startswith('plan shape: plans/target.md over threshold: bytes 66000 > 65536') for x in module.lint_file(plan, artifact_root=root).warnings))
                self.assertFalse(any('plan shape' in x for x in module.lint_file(plan).warnings))
                self.assertFalse(any('plan shape' in x for x in module.lint_file(other, artifact_root=root).warnings))

    def test_shape_missing_locator_measures_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay('x' * 66000))
            for module in _modules():
                warnings = module.plan_shape_warnings(path, path.read_text(), module.header_fields(path.read_text()), root)
                self.assertEqual(warnings[0], 'plan shape: no PLAN_ARTIFACT declared; the relay body was measured')
                self.assertTrue(warnings[1].startswith('plan shape: relay body over threshold: bytes '))

    def test_shape_unresolved_locator_measures_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay('short', locator='missing'))
            for module in _modules():
                self.assertEqual(module.plan_shape_warnings(path, path.read_text(), module.header_fields(path.read_text()), root), ["plan shape: PLAN_ARTIFACT 'missing' resolves at no probe root; the relay body was measured"])

    def test_shape_probe_order_and_display(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / 'a/b'
            plan = _file(root, _relay(locator='target'))
            root_artifact = _file(root, 'x' * 66000, 'plans/target.md')
            _file(base, 'short', 'plans/target.md')
            for module in _modules():
                w = module.plan_shape_warnings(plan, plan.read_text(), module.header_fields(plan.read_text()), root)
                self.assertTrue(any(x.startswith('plan shape: plans/target.md over threshold: bytes ') for x in w))
            _file(base, 'x' * 66000, 'plans/target.md')
            original_read_bytes = Path.read_bytes

            def unreadable(candidate):
                if candidate == root_artifact:
                    raise PermissionError('controlled unreadable root probe')
                return original_read_bytes(candidate)

            seen = []
            original_cwd = Path.cwd()
            try:
                for directory in (base / 'cwd-one', base / 'cwd-two'):
                    directory.mkdir()
                    os.chdir(directory)
                    with mock.patch.object(Path, 'read_bytes', unreadable):
                        for module in _modules():
                            w = module.plan_shape_warnings(plan, plan.read_text(), module.header_fields(plan.read_text()), root)
                            self.assertEqual(len(w), 1)
                            self.assertTrue(w[0].startswith('plan shape: ../../plans/target.md over threshold: bytes '))
                            seen.append(w[0])
            finally:
                os.chdir(original_cwd)
            self.assertEqual(len(set(seen)), 1)

    def test_probe_stat_permission_error_falls_back_to_body(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay(locator='target'))
            for module in _modules():
                with self.subTest(module=module.__name__):
                    with mock.patch.object(Path, 'is_file', side_effect=PermissionError('denied')):
                        warnings = module.plan_shape_warnings(path, path.read_text(),
                            module.header_fields(path.read_text()), root)
                    self.assertEqual(warnings, ["plan shape: PLAN_ARTIFACT 'target' resolves at no probe root; the relay body was measured"])

    def test_probe_unreadable_parent_and_long_stem_do_not_raise(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'relays'
            root.mkdir()
            plans = root / 'plans'
            plans.mkdir()
            plans.chmod(0)
            try:
                for stem in ('target', 'x' * 4096):
                    path = _file(root, _relay(locator=stem))
                    for module in _modules():
                        with self.subTest(module=module.__name__, stem_length=len(stem)):
                            warnings = module.plan_shape_warnings(path, path.read_text(),
                                module.header_fields(path.read_text()), root)
                            self.assertEqual(warnings, [f"plan shape: PLAN_ARTIFACT {stem!r} resolves at no probe root; the relay body was measured"])
            finally:
                plans.chmod(0o700)

    def test_cwd_probe_uses_exact_display(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / 'relays'
            path = _file(root, _relay(locator='target'))
            cwd = base / 'cwd'
            _file(cwd, ('x' * 99 + '\n') * 660, 'plans/target.md')
            previous = Path.cwd()
            try:
                os.chdir(cwd)
                for module in _modules():
                    warnings = module.plan_shape_warnings(path, path.read_text(),
                        module.header_fields(path.read_text()), root)
                    self.assertEqual(warnings, ['plan shape: ./plans/target.md over threshold: bytes 66000 > 65536'])
            finally:
                os.chdir(previous)

    def test_template_mode_suppresses_shape_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay('x' * 66000))
            for module in _modules():
                self.assertFalse(any('plan shape' in warning for warning in
                    module.lint_file(path, artifact_root=root, template_mode=True).warnings))

    def test_shape_digest_field_never_changes_measurement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _file(root, 'x' * 66000, 'plans/target.md')
            for module in _modules():
                found = []
                for digest in (None, 'bad', '0' * 64):
                    path = _file(root, _relay(locator='target', digest=digest))
                    result = module.lint_file(path, artifact_root=root)
                    found.append([x for x in result.warnings if 'over threshold' in x])
                    if digest == 'bad':
                        self.assertTrue(any('not a 64-hex lowercase sha256 digest' in e for e in result.errors))
                self.assertEqual(found[0], found[1])
                self.assertEqual(found[1], found[2])

    def test_root_mode_strict_attributes_per_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay('x' * 66000))
            for module in _modules():
                warnings = module.lint_relay_root(root).warnings
                self.assertEqual(len([x for x in warnings if 'over threshold' in x]), 1)
                self.assertTrue(any(x.startswith(path.name + ': plan shape: relay body over threshold: bytes ') for x in warnings))

    def test_root_mode_record_known_relay_stays_suppressed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = _file(root, _relay('x' * 66000))
            context = {'snapshot': '1', 'entries': [{'path': path.name, 'body_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'origin': 'daemon'}]}
            result = rules.lint_relay_root(root, engine_root=True, record_context=context, context_mode='daemon')
            self.assertFalse(any('plan shape' in x for x in result.warnings))
            self.assertIn('engine-root sweep: 1 record-known relays not re-judged; context source: daemon', result.warnings)
            self.assertTrue(any('over threshold' in x for x in rules.lint_file(path, artifact_root=root).warnings))

    def test_standalone_main_passes_root_into_file_lint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'relays'
            _file(root, _relay('short'))
            path = _file(Path(temp), _relay('x' * 66000), 'draft.md')
            module = _standalone()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                module.main(['--no-freshness', '--relay-root', str(root), str(path)])
            self.assertIn(f'WARN {path}: plan shape: relay body over threshold: bytes ', out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                module.main(['--no-freshness', str(path)])
            self.assertNotIn('plan shape', out.getvalue())

    def test_no_new_error_kind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _file(root, 'x' * 66000, 'plans/target.md')
            cases = [
                (_relay('short'), []),
                (_relay('x' * 66000), []),
                (_relay('short', locator='missing'), []),
                (_relay('short', locator='target'), []),
                (_relay('short', dispatch='x-plan-8'), []),
                (_relay('short', locator='target', digest='bad'),
                 ["PLAN_SHA256 value 'bad' is not a 64-hex lowercase sha256 digest"]),
                (_relay('short', locator='target', digest='0' * 64), []),
            ]
            for module in _modules():
                for index, (text, expected) in enumerate(cases):
                    path = _file(root, text, f'{index:02d}-plan.md')
                    self.assertEqual(module.lint_file(path, artifact_root=root).errors, expected)
                template = _file(root, _relay('x' * 66000), '20-template.md')
                self.assertEqual(module.lint_file(template, template_mode=True, artifact_root=root).errors, [])
