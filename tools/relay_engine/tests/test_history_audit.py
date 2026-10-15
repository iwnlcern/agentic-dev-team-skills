from relay_engine.tests.test_judgment_store import base_module
"""Managed sweeps report integrity; historical judgments are explicit audits."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from relay_engine import client, daemon, rules
from relay_engine.ledger import open_ledger, record_judgment
from relay_engine.rules import Finding
from relay_engine.tests.capture_order_baseline import collect, snapshot
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_lint_context import ContextDaemonCase, DID, _entry

TOOLS=Path(__file__).parents[2]
FIXTURE=TOOLS/'relay-engine-fixtures/record-root/history-only'


class TestHistoryAudit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for source in FIXTURE.rglob('*.md'):
            target=self.root/source.relative_to(FIXTURE);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        self.context={'snapshot':'2','entries':[{'path':str(p.relative_to(self.root)), 'body_sha256':hashlib.sha256(p.read_bytes()).hexdigest(), 'origin':'hand', 'judgment':'none','finding_count':0} for p in sorted(self.root.rglob('*.md'))]}

    def lint(self,**kwargs):
        return rules.lint_relay_root(self.root,engine_root=True,record_context=self.context,context_mode='daemon',**kwargs)

    def test_default_sweep_drops_structural_findings_on_record_known_subjects(self):
        self.assertEqual(self.lint().errors,[])
        strict=rules.lint_relay_root(self.root)
        self.assertTrue(strict.errors)
        self.assertTrue(any('DISPATCH IMPL parent' in e for e in strict.errors))

    def test_history_audit_lists_exactly_the_base_findings(self):
        baseline=json.loads((FIXTURE/'base-cbc8f07.json').read_text())
        result=self.lint(history_audit=True)
        self.assertEqual(result.errors,[])
        expected=sorted(baseline['errors']+baseline['warnings'])
        actual=sorted(w.removeprefix('history-audit: ') for w in result.warnings if w.startswith('history-audit: '))
        self.assertEqual(actual,expected)
        self.assertTrue(actual)

    def test_suppression_uses_subject_not_message_text(self):
        for p in self.root.rglob('*.md'):p.unlink()
        parent=self.root/'SITREP-pair-planner-20261013-120000.md';parent.write_bytes(body('SITREP',ident=parent.name))
        report=self.root/'IMPL-pair-implementer-20261013-120001.md';report.write_bytes(body('IMPL','p.implementer','p.planner',extra='PARENT_DISPATCH_ID: '+parent.name+'\nPLAN_LOCK_ID: '+parent.name+'/missing.md\nACTIONS_GIT_REF: implemented abcdef1'))
        self.context['entries']=[{'path':parent.name,'body_sha256':hashlib.sha256(parent.read_bytes()).hexdigest(),'origin':'hand','judgment':'none','finding_count':0}]
        result=self.lint()
        self.assertTrue(any(parent.name in e and report.name in e for e in result.errors))
        self.assertTrue(any(f.subject==report.name and f.id=='lineage.impl-report-parent' for f in result.findings))

    def test_outside_record_subject_still_reported(self):
        self.context['entries']=[e for e in self.context['entries'] if 'PLAN-REVIEW' in e['path']]
        self.assertTrue(any('DISPATCH IMPL parent' in e for e in self.lint().errors))

    def test_integrity_foreign_and_unknown_still_error(self):
        self.context['entries'][0]['body_sha256']='0'*64
        (self.root/'foreign.bin').write_bytes(b'foreign')
        result=self.lint()
        self.assertTrue(any('record-integrity: digest-mismatch' in e for e in result.errors))
        self.assertTrue(any('outside-the-record: foreign entry: foreign.bin' in e for e in result.errors))

    def test_admission_summary_counts_and_equation(self):
        for p in self.root.rglob('*.md'):p.unlink()
        self.context['entries']=[]
        # Includes old rows, clean, findings, failed, boot, imported, old-writer.
        statuses=[('none',0),('judged',0),('judged',2),('failed',0),('exempt',0),('none',0),('none',0)]
        for i,(status,count) in enumerate(statuses):
            path=self.root/f'SITREP-pair-planner-20261013-12000{i}.md';path.write_bytes(body('SITREP',ident=f'row-{i}'))
            self.context['entries'].append({'path':path.name,'body_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'origin':'hand','judgment':status,'finding_count':count})
        self.assertIn('engine-root admission: 2 judged at admission, 1 with findings, 1 judging failed, 3 never judged, 1 engine-authored',self.lint().warnings)
        self.assertEqual(sum(status==s for status,_ in statuses for s in ('judged','failed','none','exempt')),len(statuses))

    def test_hand_root_sweep_unchanged(self):
        baseline=json.loads((TOOLS/'relay-engine-fixtures/judging/base-order-cbc8f07.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);snapshot(root)
            self.assertEqual(collect(rules,root,baseline['roots']),collect(base_module('rules'),root,baseline['roots']))


class TestJudgmentContext(ContextDaemonCase):
    def populate(self):
        entries=[_entry(f'row-{i}.md') for i in range(4)]
        self.insert_entries(entries)
        fd=os.open(Path(self.root_name,'.engine'),os.O_RDONLY|os.O_DIRECTORY)
        ledger=open_ledger(fd);os.close(fd)
        try:
            ledger.execute('BEGIN')
            record_judgment(ledger,1,'judged','a'*64,())
            record_judgment(ledger,2,'judged','a'*64,[Finding('file.lint','error','row-1.md',(),{},'bad')])
            record_judgment(ledger,3,'failed','a'*64,())
            ledger.commit()
        finally:ledger.close()

    def test_live_and_stopped_context_agree_on_judgment(self):
        self.populate()
        live=client.lint_context(self.root_name,cid=DID)
        self.assertEqual([(e['judgment'],e['finding_count']) for e in live['entries']],[('judged',0),('judged',1),('failed',0),('none',0)])
        self.running.stop()
        self.assertEqual(daemon.read_lint_context(self.root_name),live)

    def test_judgment_metadata_rejects_malformed_types(self):
        valid = {'path': 'row.md', 'body_sha256': 'a'*64, 'origin': 'hand',
                 'judgment': 'none', 'finding_count': 0}
        self.assertEqual(client._context_entry(valid), valid)
        for fields in ({'judgment': []}, {'judgment': 'unknown'},
                       {'finding_count': True}, {'finding_count': -1},
                       {'judgment': 'failed', 'finding_count': 1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                client._context_entry({**valid, **fields})

    def test_stopped_read_of_base_ledger_reports_none(self):
        self.insert_entries([_entry('old.md')]);self.running.stop()
        fd=os.open(Path(self.root_name,'.engine'),os.O_RDONLY|os.O_DIRECTORY)
        ledger=open_ledger(fd);os.close(fd)
        try:
            ledger.execute('DROP TABLE admission_findings');ledger.execute('DROP TABLE admission_judgments')
        finally:ledger.close()
        entries=daemon.read_lint_context(self.root_name)['entries']
        self.assertEqual([(e['judgment'],e['finding_count']) for e in entries],[('none',0)])
