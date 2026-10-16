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


def defect_observations():
    """Independent sweep populations classify shared findings, never the candidate.

    The shared surface includes file and lineage findings with all six fields.
    Admission-only authority and routing notices have separate contract tests.
    """
    from unittest import mock
    manifest=json.loads((ROOT/'defect-corpus.json').read_text())
    for relative in manifest['roots']:
        root=ROOT.parents[1]/'relay-lint-fixtures'/relative
        files=sorted((p for p in root.rglob('*.md') if p.name!='INDEX.md' and p.is_file()),key=lambda p:rules.relay_order_key(p,root))
        texts={p:rules.read(p) for p in files}
        full=rules.lint_relay_root(root)
        keys=[rules.relay_order_key(p,root) for p in files]
        for index,path in enumerate(files):
            subject=str(path.relative_to(root));prefix=files[:index+1]
            per_file=rules.lint_file(path,freshness=False,artifact_root=root)
            file_findings=[rules.Finding('file.lint',severity,subject,(),{},f'{subject}: {message}')
                           for severity,messages in (('error',per_file.errors),('warn',per_file.warnings)) for message in messages]
            def normalized(findings):
                unique={json.dumps([f.id,f.lint_severity,f.subject,f.operands,f.params,f.message],sort_keys=True):f for f in findings}
                return tuple(sorted(unique.values(),key=lambda f:(f.id,f.operands,f.message)))
            def shared(result):
                return normalized([f for f in result.findings if f.subject==subject and f.id.startswith('lineage.')]+file_findings)
            population=(prefix,{p:texts[p] for p in prefix},{p:texts[p].encode() for p in prefix},set())
            with mock.patch.object(rules,'_record_scoped_population',return_value=population):
                before=rules.lint_relay_root(root,engine_root=True,record_context={'entries':[]},context_mode='daemon')
            expected=shared(before);after=shared(full)
            divergent=any(keys[i][:2]==keys[j][:2] for i in range(index+1) for j in range(i))
            partition='order-divergent' if divergent else 'future-sensitive' if expected!=after else 'aligned'
            corpus=judging.Corpus(tuple((i+1,str(p.relative_to(root)),texts[p].encode()) for i,p in enumerate(files[:index])))
            actual=judging.judge_candidate(corpus,subject,envelope(texts[path].encode()),texts[path].encode(),artifact_root=root,deadline=time.monotonic()+5)
            actual=normalized(f for f in actual if f.id.startswith('lineage.') or f.id=='file.lint')
            yield relative+'/'+subject,partition,actual,expected,after


class TestDefectCorpusAgreement(unittest.TestCase):
    def test_defect_corpus_partition_and_full_structural_agreement(self):
        from collections import Counter
        from dataclasses import asdict
        manifest=ROOT/'defect-corpus.json'
        self.assertTrue(manifest.exists(), 'the aligned corpus must contain real defects')
        manifest=json.loads(manifest.read_text())
        base=ROOT.parents[1]/'relay-lint-fixtures'
        self.assertEqual(manifest['roots'],sorted(str(p.relative_to(base)) for family in manifest['families'] for p in (base/family).iterdir() if p.is_dir()))
        exceptions=manifest['exceptions'];seen=set();counts=Counter();families=Counter()
        for key,partition,actual,expected,after in defect_observations():
            self.assertNotIn(key,seen);seen.add(key);counts[partition]+=1
            with self.subTest(subject=key,partition=partition):
                if partition!='order-divergent':self.assertEqual(actual,expected)
                if partition=='aligned':
                    self.assertEqual(actual,after)
                    self.assertNotIn(key,exceptions)
                    families.update(f.id for f in expected)
                else:
                    recorded=exceptions[key]
                    self.assertEqual(partition,recorded['partition'])
                    self.assertEqual(json.loads(json.dumps([asdict(f) for f in actual])),recorded['admission'])
                    self.assertEqual(json.loads(json.dumps([asdict(f) for f in after])),recorded['sweep'])
        self.assertEqual(set(exceptions),{key for key,partition,*_ in defect_observations() if partition!='aligned'})
        self.assertGreater(counts['aligned'],100)
        self.assertGreater(families['lineage.lock-lifecycle'],0)
        self.assertEqual(dict(counts),manifest['counts'])
