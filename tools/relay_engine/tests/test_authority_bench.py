"""Small isolated witness for the seeded Authority query workloads."""
import ast
from collections import Counter
from contextlib import closing
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from relay_engine import authority


class TestBench(unittest.TestCase):
    def setUp(self):
        self.bench = importlib.import_module('relay_engine.authority_bench')
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def make(self, name='store'):
        root = self.root / name
        stats = self.bench.seed(root, relays=500, seats=8)
        return root, stats

    def connection(self, root):
        return closing(sqlite3.connect(root / 'store.sqlite3'))

    def test_seed_counts(self):
        _, stats = self.make()
        self.assertEqual(stats.counts, {'relays': 500, 'implement_slips': 32, 'merge_slips': 10,
                                       'claims': 55, 'snapshots': 94, 'findings': 5, 'seats': 8})
        self.assertEqual(stats.seat_allocation, {'held': 1, 'no_slip': 1, 'with_slips': 6})
        self.assertEqual(stats.ratios['implement_slips'], (1129, 17596))
        with self.assertRaises(ValueError):
            self.bench.seed(self.root / 'unwritten', relays=500, seats=7)

    def test_seed_store_is_valid(self):
        root, _ = self.make()
        with self.connection(root) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            records = [json.loads(row[0]) for row in conn.execute(
                "SELECT binds FROM slips WHERE state = 'live' AND kind = 'implement'")]
        for binds in records:
            for item in binds['digests'] + [lock for c in binds['claims'] for lock in c['locks']]:
                repo, _, file = item['artifact'].partition(':')
                data = (root / 'copies' / repo / file).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), item.get('digest', item.get('content_sha256')))

    def test_seed_profile(self):
        root, stats = self.make()
        sizes = Counter(p.stat().st_size for p in (root / 'copies').glob('*/*') if p.name.startswith('fixture-'))
        self.assertEqual(stats.file_size_counts, sizes)
        self.assertEqual(sizes, {4500: 112, 14000: 280, 60000: 84, 90000: 56, 420000: 28})
        with self.connection(root) as conn:
            held, = conn.execute('SELECT project_id FROM restore_cancels WHERE released_at IS NULL').fetchone()
            found = conn.execute("SELECT seat_id, project_id, binds FROM slips WHERE state='live' AND kind='implement'").fetchall()
        self.assertEqual(len(found), 18)
        self.assertNotIn(held, {row[1] for row in found})
        self.assertEqual(set(Counter(row[0] for row in found).values()), {3})
        self.assertEqual({len(json.loads(row[2])['claims']) for row in found}, {1, 2, 4})

    def test_seed_is_deterministic(self):
        a, _ = self.make('a')
        b, _ = self.make('b')
        with self.connection(a) as ca, self.connection(b) as cb:
            self.assertEqual(list(ca.iterdump()), list(cb.iterdump()))
        self.assertEqual({p.relative_to(a): hashlib.sha256(p.read_bytes()).digest()
                          for p in (a / 'copies').glob('*/*')},
                         {p.relative_to(b): hashlib.sha256(p.read_bytes()).digest()
                          for p in (b / 'copies').glob('*/*')})

    def measure(self):
        root, stats = self.make()
        with self.connection(root) as conn:
            return self.bench.measure(conn, queries=200, warmup=0), stats

    def test_measure_shape_recorded(self):
        got, stats = self.measure()
        self.assertEqual(got.seeded_file_size_counts, stats.file_size_counts)
        self.assertEqual(got.standing_fanout, stats.standing_fanout)
        self.assertEqual(got.stratum_counts['standing'], {'live': 120, 'empty': 40, 'held': 40})
        self.assertEqual(got.stratum_counts['scope'], {'bound': 80, 'unbound': 60, 'path': 40, 'other': 20})
        self.assertEqual(got.stratum_counts['act'], {'bound': 60, 'unbound': 30, 'pending': 30,
                                                     'objected': 20, 'changed': 20, 'other': 20, 'missing': 20})
        total = sum(got.timed_file_size_counts.values())
        self.assertGreater(total, 0)
        for size, share in ((4500, 4), (14000, 10), (60000, 3), (90000, 2), (420000, 1)):
            self.assertLessEqual(abs(got.timed_file_size_counts.get(size, 0) - total * share / 20), 1)

    def test_timed_size_mix_is_what_is_hashed(self):
        entries = Counter()
        observed = []
        original_entry = authority.act_entry
        original_digest = authority.working_copy_digest
        def entry(conn, seat_id, effect, now_ms):
            observed.clear()
            answer = original_entry(conn, seat_id, effect, now_ms)
            if answer.outcome == 'allow' or answer.code in (
                    'authority.verification-pending', 'authority.verification-objected'):
                entries.update(observed)
            return answer

        def digest(path, algorithm='sha256'):
            observed.append(Path(path).stat().st_size)
            return original_digest(path, algorithm)

        with mock.patch.object(authority, 'act_entry', side_effect=entry), \
             mock.patch.object(authority, 'working_copy_digest', side_effect=digest):
            got, _ = self.measure()
        self.assertTrue(entries)
        self.assertEqual(got.timed_file_size_counts, entries)

    def test_measure_strata(self):
        counts = Counter()
        original = authority.act_entry
        def entry(conn, seat_id, effect, now_ms):
            answer = original(conn, seat_id, effect, now_ms)
            counts[answer.outcome, answer.code] += 1
            return answer
        with mock.patch.object(authority, 'act_entry', side_effect=entry):
            self.measure()
        self.assertEqual(counts, {('allow', None): 90, ('held', 'authority.verification-pending'): 30,
                                  ('held', 'authority.verification-objected'): 20,
                                  ('held', 'authority.plan-changed'): 20, ('deny', 'authority.slip-invalid'): 20,
                                  ('deny', 'authority.slip-missing'): 20})

    def test_measure_times_production_entry(self):
        with mock.patch.object(authority, 'act_entry', wraps=authority.act_entry) as entry, \
             mock.patch.object(authority, 'working_copy_digest', wraps=authority.working_copy_digest) as digest:
            self.measure()
        self.assertEqual(entry.call_count, 200)
        self.assertGreater(digest.call_count, 140)

    def test_verdict_rule(self):
        for wall, cpu, want in ((9, 9, 'within-bound'), (12, 11, 'over-bound'), (20, 6, 'inconclusive'),
                                (9, 2, 'within-bound'), (10, 10, 'within-bound'), (10.001, 10, 'inconclusive'),
                                (12, 10, 'inconclusive'), (12, 10.001, 'over-bound')):
            with self.subTest(wall=wall, cpu=cpu):
                self.assertEqual(self.bench.verdict(wall, cpu), want)

    def test_kind_verdicts_recorded(self):
        names = iter(['within-bound', 'inconclusive', 'over-bound'])
        with mock.patch.object(self.bench, 'verdict', side_effect=lambda *args: next(names)):
            got, _ = self.measure()
        self.assertEqual(got.kind_verdicts, {'standing': 'within-bound', 'scope': 'inconclusive', 'act': 'over-bound'})
        self.assertEqual(got.verdict, 'over-bound')

    def test_result_states_limits(self):
        got, _ = self.measure()
        for phrase in ('slowed core', 'file reads', 'busy run', 'proxy', 'this seed'):
            self.assertIn(phrase, got.limits)
        self.assertEqual(set(got.clocks), {'standing', 'scope', 'act'})
        for record in got.clocks.values():
            self.assertEqual(set(record), {'wall_p50_ns', 'wall_p99_ns', 'cpu_p50_ns',
                                           'cpu_p99_ns', 'wall_cpu_p99_ratio'})
        self.assertEqual(len(got.load_start), 3)
        self.assertEqual(len(got.load_end), 3)
        self.assertGreater(got.logical_cpus, 0)

    def argv(self):
        return ['--store', str(self.root / 'store'), '--relays', '500', '--seats', '8', '--queries', '200',
                '--out', str(self.root / 'result.json')]

    def test_main_exit_codes(self):
        for label, expected in (('within-bound', 0), ('over-bound', 1), ('inconclusive', 3)):
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir=self.root) as path:
                args = self.argv()
                args[1], args[-1] = str(Path(path) / 'store'), str(Path(path) / 'out')
                with mock.patch.object(self.bench, 'verdict', return_value=label), \
                     mock.patch.object(self.bench, '_warmup_count', return_value=0):
                    self.assertEqual(self.bench.main(args), expected)
                self.assertEqual(json.loads(Path(args[-1]).read_bytes())['verdict'], label)

    def test_main_refuses_low_disk(self):
        with mock.patch.object(self.bench, '_available_bytes', return_value=4 * 1024 ** 3):
            self.assertEqual(self.bench.main(self.argv()), 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_main_writes_result_only_at_end(self):
        def interrupted(*args, **kwargs):
            self.assertFalse((self.root / 'result.json').exists())
            raise KeyboardInterrupt
        with mock.patch.object(self.bench, 'measure', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.bench.main(self.argv())
        self.assertFalse((self.root / 'result.json').exists())

    def test_bench_imports_no_network_or_daemon(self):
        tree = ast.parse(Path(self.bench.__file__).read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.update(alias.name for alias in node.names)
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
        self.assertFalse({'socket', 'http', 'urllib', 'daemon'} & {name.split('.')[-1] for name in imports})
