"""Agreement compares full findings; temporal exceptions have explicit results."""
import json
from pathlib import Path
import tempfile
import time
import unittest

from relay_engine import judging, rules
from relay_engine.tests.test_judging import envelope

ROOT = Path(__file__).resolve().parents[2] / 'relay-engine-fixtures/judging'


class TestJudgingAgreement(unittest.TestCase):
    def cases(self):
        return [json.loads(p.read_text()) for p in sorted(ROOT.glob('*/*.json'))]

    def materialize(self, case, root):
        for row in case['relays']:
            (root / row['path']).write_text(row['text'])
        for name, contents in case['artifacts'].items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)

    def candidate(self, case, root, index):
        row = case['relays'][index]
        corpus = judging.Corpus(tuple((i+1,r['path'],r['text'].encode()) for i,r in enumerate(case['relays'][:index])))
        return judging.judge_candidate(corpus,row['path'],envelope(row['text'].encode()),row['text'].encode(),artifact_root=root,deadline=time.monotonic()+5)

    def test_every_fixture_relay_is_in_exactly_one_partition(self):
        seen = set()
        for case in self.cases():
            # Derive order inversion from actual input paths, not the label.
            paths = [r['path'] for r in case['relays']]
            self.assertEqual(case['order_divergent'], paths != sorted(paths))
            self.assertEqual(judging.partition_of(case), case['partition'])
            for row in case['relays']:
                self.assertNotIn((case['case'], row['path']), seen)
                seen.add((case['case'], row['path']))
        self.assertEqual(len(seen), sum(len(c['relays']) for c in self.cases()))
        self.assertGreater(len(seen), 20)

    def test_aligned_partition_agrees_with_sweep(self):
        for case in self.cases():
            if judging.partition_of(case) != 'aligned': continue
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);self.materialize(case,root)
                sweep=rules.lint_relay_root(root)
                for index,row in enumerate(case['relays']):
                    subject=row['path']
                    expected=[f for f in sweep.findings if f.subject==subject]
                    per_file=rules.lint_file(root/subject,freshness=False,artifact_root=root)
                    for severity,messages in (('error',per_file.errors),('warn',per_file.warnings)):
                        expected.extend(rules.Finding('file.lint',severity,subject,(),{},f'{subject}: {message}') for message in messages)
                    expected=tuple(sorted(expected,key=lambda f:(f.id,f.operands,f.message)))
                    self.assertEqual(self.candidate(case,root,index),expected,subject)

    def assert_temporal_cases(self,partition):
        count=0
        for case in self.cases():
            if judging.partition_of(case)!=partition:continue
            count+=1
            with self.subTest(case=case['case']), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);self.materialize(case,root)
                index=next(i for i,r in enumerate(case['relays']) if r['path']==case['focus'])
                admission=[f.message for f in self.candidate(case,root,index) if f.id==case['family']]
                sweep=[f.message for f in rules.lint_relay_root(root).findings if f.id==case['family'] and f.subject==case['focus']]
                for actual,wanted in ((admission,case['admission_messages']),(sweep,case['sweep_messages'])):
                    self.assertEqual(len(actual),len(wanted),(actual,wanted))
                    for message,fragment in zip(actual,wanted):self.assertIn(fragment,message)
        self.assertGreater(count,0)

    def test_future_sensitive_cases_assert_both_results(self):
        self.assert_temporal_cases('future-sensitive')

    def test_order_divergent_cases_assert_both_results(self):
        self.assert_temporal_cases('order-divergent')
        self.assertEqual(judging.partition_of({'order_divergent':True,'future_sensitive':True}),'order-divergent')
