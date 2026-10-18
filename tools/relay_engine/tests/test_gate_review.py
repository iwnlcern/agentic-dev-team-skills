"""B8 checkpoint controls: independent expected answers across lint surfaces."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

from relay_engine import rules
from relay_engine.tests.test_parity import _standalone
from relay_engine.tests import test_finding_tags

PRE_B8_BASE = '36a246639fbb92893a2a103104bbffdbd037d4e7'
REPO = Path(__file__).resolve().parents[3]
FAMILY = 'review.gate-not-heard'
PLAN = 'B8 plan\n'
DIGEST = hashlib.sha256(PLAN.encode()).hexdigest()
DISPOSITION = 'operator-in-chat 20261018-120000 — "Proceed with this head"'
REASONS = ('run-pair-ambiguous', 'grant-owner-mismatch', 'no-gate-record',
           'gate-record-ambiguous', 'no-request', 'request-ambiguous',
           'no-reply', 'reply-ambiguous', 'gate-record-invalid', 'grant-invalid',
           'request-invalid', 'reply-invalid', 'binding-mismatch', 'must-revise',
           'held-without-disposition')


def pre_b8_module(path):
    source = subprocess.check_output(['git', 'show', PRE_B8_BASE + ':' + path], cwd=REPO, text=True)
    name = '_b8_base_' + path.replace('/', '_').replace('.', '_')
    module = types.ModuleType(name)
    module.__file__ = str(REPO / path)
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


def row(ident, sender, target, role, phase='MERGE-GATE', authority='report-only', parent=None, **payload):
    fields = dict(ROLE=role, PHASE=phase, AUTHORITY=authority, DISPATCH_ID=ident,
                  RUN_ID='b8', CEREMONY_TIER='small', EVIDENCE_TARGET='E2',
                  HUMAN_GATE_REQUIRED='no', FROM=sender, TO=target,
                  CC='a.orchestrator-reviewer', SUBJECT='checkpoint test',
                  FINAL_GIT_STATUS_SHORT='clean')
    if parent:
        fields['PARENT_DISPATCH_ID'] = parent
    fields.update(payload)
    return dict(fields=fields, body='', path=None)


def scenario():
    adoption = row('adopt', 'a.orchestrator-planner', 'a.orchestrator-reviewer',
                   'Orchestrator Planner', phase='SITREP', GATE_REVIEW='on')
    gate = row('gate', 'p.planner', 'a.orchestrator-planner', 'Pair Planner',
               PLAN_LOCK_ID='PL-b8', PLAN_SHA256=DIGEST, PLAN_ARTIFACT='P')
    request = row('request', 'a.orchestrator-planner', 'a.orchestrator-reviewer',
                  'Orchestrator Planner', parent='gate', GATE_HEAD='abc123',
                  GATE_BASE='def456', PLAN_LOCK_ID='PL-b8', PLAN_SHA256=DIGEST, PLAN_ARTIFACT='P')
    reply = row('reply', 'a.orchestrator-reviewer', 'a.orchestrator-planner',
                'Orchestrator Reviewer', authority='review-only', parent='request',
                GATE_REVIEW_VERDICT='approve', REVIEWED_HEAD='abc123', REVIEWED_BASE='def456')
    grant = row('grant', 'a.orchestrator-planner', 'p.implementer',
                'Orchestrator Planner', authority='merge-gated', parent='gate',
                MERGE_HEAD='abc123', MERGE_BASE='def456')
    grant['body'] = 'DISPATCH MERGE\n'
    return [adoption, gate, request, reply, grant]


def cases():
    result = []
    def add(name, rows, reason=None, operands=('gate', 'request', 'reply'), disposition='absent', dirty=False, admission='same'):
        rows = copy.deepcopy(rows)
        for i, item in enumerate(rows):
            if item['path'] is None:
                item['path'] = f'{i + 1:02}-{item["fields"]["DISPATCH_ID"]}.md'
        result.append(dict(name=name, rows=rows, reason=reason, operands=operands,
                           disposition=disposition, dirty=dirty,
                           admission=reason if admission == 'same' else admission))
    def foreign(item):
        item['fields']['FROM'] = item['fields']['FROM'].replace('a.', 'b.')
        item['fields']['TO'] = item['fields']['TO'].replace('a.', 'b.')
    for alias in (False, True):
        rows = scenario()
        if alias: rows[-1]['fields']['FROM'] = 'orchestrator'
        suffix = '-alias' if alias else ''
        add('S1'+suffix, rows)
        other = copy.deepcopy(rows[2]); other['fields']['DISPATCH_ID'] = 'foreign-request'; foreign(other)
        add('S7'+suffix, rows[:-1]+[other, rows[-1]])
        for item in rows[2:4]: foreign(item)
        add('W16'+suffix, rows, 'no-request', ('gate',))
        rows = scenario()
        if alias: rows[-1]['fields']['FROM'] = 'orchestrator'
        other = copy.deepcopy(rows[0]); other['fields']['DISPATCH_ID'] = 'other-adoption'; foreign(other)
        add('W14'+suffix, rows[:-1]+[other,rows[-1]], 'run-pair-ambiguous', ())
    rows = scenario(); rows[3]['fields']['GATE_REVIEW_VERDICT'] = 'human-decision-required'; rows[-1]['fields']['GATE_DISPOSITION'] = DISPOSITION
    add('S2', rows, disposition='well-formed')
    rows = scenario(); rows[-1]['fields']['FROM'] = 'operator'; rows[-1]['fields']['ROLE'] = 'Operator'
    add('S3', rows[:2]+[rows[-1]])
    waiver = row('waiver','operator','a.orchestrator-planner','Operator',phase='SITREP',ORCH_REVIEW_WAIVER='yes')
    rows = scenario(); add('S4', rows[:2]+[waiver,rows[-1]])
    rows = scenario(); add('S5-after', [rows[1],rows[-1],rows[0]])
    rows = scenario(); rows[0]['path']='left/02-same.md'; rows[-1]['path']='right/02-same.md'; rows[1]['path']='01-gate.md'
    add('S5-tie', rows[:2]+[rows[-1]], admission='no-request')
    rows = scenario(); add('S6', rows[1:])
    for variant in ('off','duplicate','fenced'):
        rows=scenario()
        if variant=='off': rows[0]['fields']['GATE_REVIEW']='off'
        elif variant=='duplicate': rows[0]['body']='GATE_REVIEW: on\n'
        else:
            del rows[0]['fields']['GATE_REVIEW']; rows[0]['body']='```text\nGATE_REVIEW: on\n```\n'
        add('S8-'+variant,rows[:2]+[rows[-1]])
    rows=scenario(); rows[0]['fields']['TO']='operator'; add('S9',rows[:2]+[rows[-1]])
    rows=scenario(); add('W1',rows[:3]+[rows[-1]],'no-reply',('gate','request'))
    rows=scenario(); rows[3]['fields']['GATE_REVIEW_VERDICT']='must-revise'; add('W2',rows,'must-revise')
    rows=scenario(); newer=copy.deepcopy(rows[3]); newer['fields'].update(DISPATCH_ID='reply2',GATE_REVIEW_VERDICT='must-revise')
    add('W3',rows[:-1]+[newer,rows[-1]],'must-revise',('gate','request','reply2'))
    for sender,role in [('v297-lint.implementer','Pair Implementer'),('b.orchestrator-reviewer','Orchestrator Reviewer')]:
        rows=scenario(); rows[3]['fields'].update(FROM=sender,ROLE=role)
        add('W4-'+sender,rows,'no-reply',('gate','request'))
    rows=scenario(); rows[2]['fields']['TO']='operator'; add('W5',rows,'no-request',('gate',))
    rows=scenario(); rows[3]['fields']['PARENT_DISPATCH_ID']='elsewhere'; add('W6',rows,'no-reply',('gate','request'))
    for name,index,key,reason in [('W7-request',2,'GATE_HEAD','request-invalid'),('W7-reply',3,'REVIEWED_BASE','reply-invalid'),('W10a-lock',2,'PLAN_LOCK_ID','request-invalid'),('W10a-sha',2,'PLAN_SHA256','request-invalid'),('W18',1,'PLAN_LOCK_ID','gate-record-invalid'),('W19',4,'MERGE_BASE','grant-invalid'),('W20-absent',2,'PLAN_ARTIFACT','request-invalid')]:
        rows=scenario(); del rows[index]['fields'][key]; add(name,rows,reason,dirty=name=='W20-absent')
    rows=scenario(); rows[3]['body']='GATE_REVIEW_VERDICT: approve\n'; add('W7-duplicate',rows,'reply-invalid')
    rows=scenario(); newer=copy.deepcopy(rows[3]); newer['fields']['DISPATCH_ID']='reply2'; del newer['fields']['REVIEWED_HEAD']
    add('W8',rows[:-1]+[newer,rows[-1]],'reply-invalid',('gate','request','reply2'))
    rows=scenario(); newer=copy.deepcopy(rows[2]); newer['fields']['DISPATCH_ID']='request2'; del newer['fields']['GATE_BASE']
    add('W9',rows[:-1]+[newer,rows[-1]],'no-reply',('gate','request2'))
    for key,value,dirty in [('PLAN_LOCK_ID','PL-other',False),('PLAN_SHA256','0'*64,True)]:
        rows=scenario(); rows[2]['fields'][key]=value; add('W10-'+key,rows,'binding-mismatch',dirty=dirty)
    for key,value,label in [('MERGE_HEAD','a-new-head','head'),('MERGE_HEAD','rebased-same-tree','rebase'),('MERGE_HEAD','ABC123','case'),('MERGE_BASE','different-base','base')]:
        rows=scenario(); rows[-1]['fields'][key]=value; add('W11-'+label,rows,'binding-mismatch')
    for label,value in [('absent',None),('stamp',DISPOSITION.replace('20261018','20260230')),('empty',DISPOSITION.replace('Proceed with this head','')),('dash',DISPOSITION.replace('—','-')),('quotes',DISPOSITION.replace('"','“')),('duplicate',DISPOSITION)]:
        rows=scenario(); rows[3]['fields']['GATE_REVIEW_VERDICT']='human-decision-required'
        if value: rows[-1]['fields']['GATE_DISPOSITION']=value
        if label=='duplicate': rows[-1]['body']+='GATE_DISPOSITION: '+DISPOSITION+'\n'
        add('W12-'+label,rows,'held-without-disposition',disposition='absent' if value is None else 'malformed')
    rows=scenario(); rows[3]['fields']['GATE_REVIEW_VERDICT']='human-decision-required'; rows[-1]['fields'].update(GATE_DISPOSITION=DISPOSITION,MERGE_HEAD='changed')
    add('W13',rows,'binding-mismatch',disposition='well-formed')
    rows=scenario(); rows[-1]['fields']['FROM']='b.orchestrator-planner'; add('W15',rows,'grant-owner-mismatch',())
    for parent in ('missing','grant'):
        rows=scenario(); rows[-1]['fields']['PARENT_DISPATCH_ID']=parent; add('W17-'+parent,rows,'no-gate-record',())
    rows=scenario()
    rows[1]['fields'].update(FROM='operator', TO='p.implementer', ROLE='Operator', AUTHORITY='merge-gated',
                             MERGE_HEAD='abc123', MERGE_BASE='def456')
    rows[1]['body']='DISPATCH MERGE\n'
    add('W17-earlier-grant',rows,'no-gate-record',())
    rows=scenario(); rows[2]['body']='PLAN_ARTIFACT: P\n'; add('W20-duplicate',rows,'request-invalid')
    rows=scenario(); add('W21',rows[:2]+[rows[-1],waiver],'no-request',('gate',))
    rows=scenario(); newer=copy.deepcopy(rows[2]); newer['fields']['DISPATCH_ID']='request2'; rows[2]['path']='left/03-same.md'; newer['path']='right/03-same.md'; rows[3]['fields']['PARENT_DISPATCH_ID']='request2'
    add('W22-request',rows[:3]+[newer]+rows[3:],'request-ambiguous',('gate',),admission=None)
    rows=scenario(); newer=copy.deepcopy(rows[3]); newer['fields']['DISPATCH_ID']='reply2'; rows[3]['fields']['GATE_REVIEW_VERDICT']='must-revise'; rows[3]['path']='left/04-same.md'; newer['path']='right/04-same.md'
    add('W22-reply',rows[:-1]+[newer,rows[-1]],'reply-ambiguous',('gate','request'),admission=None)
    rows=scenario(); rows[3]['fields']['GATE_REVIEW_VERDICT']='human-decision-required'; add('W23',rows,'held-without-disposition')
    return result


CASES = cases()


def text_for(item):
    return '\n'.join(f'{k}: {v}' for k,v in item['fields'].items())+'\n\n'+item['body']


def materialize(case, root):
    root.mkdir(parents=True, exist_ok=True)
    (root.parent.parent/'plans').mkdir(exist_ok=True)
    (root.parent.parent/'plans/P.md').write_text(PLAN)
    for item in case['rows']:
        path=root/item['path']; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(text_for(item))


def message(subject, reason, disposition):
    return f'{subject}: review.gate-not-heard: orchestrator merge grant after GATE_REVIEW adoption has no heard reviewer checkpoint ({reason}; disposition {disposition})'


class TestGateReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live=_standalone()
        cls.old_rules=pre_b8_module('tools/relay_engine/rules.py')
        cls.old_live=pre_b8_module('tools/relay-lint.py')

    def test_reasons_vocabulary_and_order(self):
        self.assertEqual(getattr(rules,'GATE_REVIEW_REASONS',None),REASONS)

    def test_family_declared(self):
        self.assertIn(FAMILY,rules.FAMILY_IDS)
        self.assertEqual(rules.FAMILY_IDS,test_finding_tags.FAMILIES)

    def test_hand_sweep_case_table(self):
        for case in CASES:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'.relays/b8'; materialize(case,root)
                found=[f for f in rules.lint_relay_root(root).findings if f.id==FAMILY]
                if case['reason'] is None:
                    self.assertEqual(found,[]); continue
                paths={r['fields']['DISPATCH_ID']:r['path'] for r in case['rows']}
                want=rules.Finding(FAMILY,'warn',paths['grant'],tuple(sorted(paths[k] for k in case['operands'])),
                                   dict(reason=case['reason'],disposition=case['disposition']),message(paths['grant'],case['reason'],case['disposition']))
                self.assertEqual(found,[want])

    def test_standalone_case_table(self):
        for case in CASES:
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'.relays/b8'; materialize(case,root)
                engine=rules.lint_relay_root(root); live=self.live.lint_relay_root(root)
                self.assertEqual((engine.errors,engine.warnings,engine.ok),(live.errors,live.warnings,live.ok))

    def test_hand_legs_preserve_base(self):
        for case in CASES:
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'.relays/b8'; materialize(case,root)
                for new,old in ((rules,self.old_rules),(self.live,self.old_live)):
                    actual=new.lint_relay_root(root); baseline=old.lint_relay_root(root)
                    self.assertEqual(actual.errors,baseline.errors)
                    self.assertEqual(actual.ok,baseline.ok)
                    self.assertEqual([w for w in actual.warnings if FAMILY not in w],baseline.warnings)
                    if not case['dirty']: self.assertEqual(actual.errors,[])

    def test_existing_fixture_roots_silent(self):
        roots=[REPO/'tools/relay-lint-fixtures']
        for directory in ('corpora','record-root'):
            roots.extend(p for p in (REPO/'tools/relay-engine-fixtures'/directory).iterdir() if p.is_dir())
        for root in roots:
            for module in (rules,self.live):
                self.assertFalse(any(FAMILY in w for w in module.lint_relay_root(root).warnings))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'.relays/b8'
            root.mkdir(parents=True)
            source=REPO/'tools/relay-lint-fixtures/orch-review/A1-no-reviewer-no-waiver/01-merge.md'
            (root/source.name).write_bytes(source.read_bytes())
            declaration=scenario()[0]; declaration['fields'].update(FROM='run.orchestrator-planner',TO='run.orchestrator-reviewer')
            (root/'00-adopt.md').write_text(text_for(declaration))
            for module,old in ((rules,self.old_rules),(self.live,self.old_live)):
                actual=module.lint_relay_root(root)
                self.assertEqual(actual.errors,old.lint_relay_root(root).errors)
                self.assertEqual([w for w in actual.warnings if FAMILY in w],[message(source.name,'no-gate-record','absent')])

    def test_existing_parity_rows_byte_equal_to_pre_b8_base(self):
        path='tools/relay-engine-fixtures/parity/golden-verdicts.json'
        base=json.loads(subprocess.check_output(['git','show',PRE_B8_BASE+':'+path],cwd=REPO))
        actual=json.loads((REPO/path).read_text())
        def same(candidate):
            return candidate['verdicts']==base['verdicts'] and candidate['manifest_sha256']==base['manifest_sha256']
        self.assertTrue(same(actual))
        changed=copy.deepcopy(actual)
        group=next(iter(changed['verdicts'].values())); row=next(iter(group.values())); row['warnings'].append('planted change')
        self.assertFalse(same(changed))


# Real daemon/CLI transport keeps these tests independent of the helper's caller.
import io
import os
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from relay_engine import cli, client, daemon, judging, ledger
from relay_engine.envelope import parse_draft
from relay_engine.paths import Root, ensure_engine_dir
from relay_engine.tests.test_identity_matrix import cid_did


class ManagedRoot:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b8-managed-')
        self.root = Path(self.temp.name)/'.relays/b8'
        self.root.mkdir(parents=True)
        (self.root.parent.parent/'plans').mkdir()
        (self.root.parent.parent/'plans/P.md').write_text(PLAN)
        with Root(self.root) as root:
            fd = ensure_engine_dir(root.dirfd)
            try:
                connection = ledger.init_schema(fd, root.path)
                connection.execute("INSERT INTO meta(key,value) VALUES('run_id','b8')")
                connection.close()
            finally:
                os.close(fd)
        read_fd, write_fd = os.pipe()
        _, did = cid_did()
        self.thread = threading.Thread(target=daemon.start, args=(str(self.root),),
            kwargs=dict(ready_fd=write_fd, socket_override=str(Path(self.temp.name)/'s'), did=did))
        self.thread.start()
        try:
            if os.read(read_fd, 1) != b'R':
                raise AssertionError('daemon did not become ready')
        finally:
            os.close(read_fd)
        self.keys = {}
        self.paths = {}
        return self

    def __exit__(self, *exc):
        try:
            client.request(str(self.root), 'daemon.stop', {})
        finally:
            self.thread.join(10)
            self.temp.cleanup()
        if self.thread.is_alive():
            raise AssertionError('daemon did not stop')

    def command(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(args)
        if code:
            raise AssertionError((args, code, out.getvalue(), err.getvalue()))
        return json.loads(out.getvalue())

    def submit(self, item):
        fields = item['fields']; sender = fields['FROM']; ident = fields['DISPATCH_ID']
        if sender not in self.keys:
            registered = self.command(['seat','register',sender,'--role',fields['ROLE'],'--root',str(self.root)])
            self.keys[sender] = registered['key_path']
        path = '.engine/drafts/' + ident + '.md'
        target=self.root/path; target.parent.mkdir(parents=True,exist_ok=True); target.write_text(text_for(item))
        receipt = self.command(['submit',path,'--key',self.keys[sender],'--root',str(self.root)])
        self.paths[ident] = receipt['path']
        return receipt

    def stored(self, ident):
        with Root(self.root) as root:
            fd=ensure_engine_dir(root.dirfd)
            try:
                connection=ledger.open_ledger(fd)
                try:
                    seq=connection.execute('SELECT seq FROM relays WHERE rendered_path=?',(self.paths[ident],)).fetchone()[0]
                    return ledger.judgment_for(connection,seq)
                finally:
                    connection.close()
            finally:
                os.close(fd)

    def sweep(self, audit=False):
        args=['lint','--relay-root',str(self.root)]
        if audit: args.append('--history-audit')
        return self.command(args)[str(self.root)]


class TestGateAdmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_rules=pre_b8_module('tools/relay_engine/rules.py')

    def direct(self, case, root):
        materialize(case,root)
        grant_index=next(i for i,r in enumerate(case['rows']) if r['fields']['DISPATCH_ID']=='grant')
        grant=case['rows'][grant_index]; raw=text_for(grant).encode()
        corpus=judging.Corpus(tuple((i+1,r['path'],text_for(r).encode()) for i,r in enumerate(case['rows'][:grant_index])))
        found=judging.judge_candidate(corpus,grant['path'],parse_draft(raw.decode()),raw,artifact_root=root,deadline=time.monotonic()+10)
        return [f for f in found if f.id==FAMILY]

    def assert_answer(self, found, case, paths=None):
        reason=case['admission']
        if reason is None:
            self.assertEqual(found,[]); return
        self.assertEqual(len(found),1)
        self.assertEqual(found[0].lint_severity,'warn')
        self.assertEqual(found[0].params,dict(reason=reason,disposition=case['disposition']))
        if paths is not None:
            self.assertEqual(found[0].subject,paths['grant'])
            self.assertEqual(found[0].message,message(paths['grant'],reason,case['disposition']))

    def test_direct_admission_case_table(self):
        for case in CASES:
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'.relays/b8'; found=self.direct(case,root)
                self.assert_answer(found,case,{r['fields']['DISPATCH_ID']:r['path'] for r in case['rows']})
                if case['admission']==case['reason']:
                    expected=[f for f in rules.lint_relay_root(root).findings if f.id==FAMILY]
                    self.assertEqual(found,expected)

    def test_managed_admission_case_table(self):
        for case in CASES:
            with self.subTest(case=case['name']),ManagedRoot() as managed:
                for item in case['rows']: managed.submit(item)
                status,findings=managed.stored('grant')
                self.assertEqual(status,'judged')
                found=[f for f in findings if f.id==FAMILY]
                self.assert_answer(found,case,managed.paths)
                if not case['dirty']:
                    self.assertFalse([f for f in findings if f.lint_severity=='error'])

    def test_managed_sweep_suppresses_and_history_audit_shows(self):
        for case in CASES:
            with self.subTest(case=case['name']),ManagedRoot() as managed:
                for item in case['rows']: managed.submit(item)
                plain=managed.sweep(); history=managed.sweep(audit=True)
                self.assertFalse([w for w in plain['warnings'] if FAMILY in w])
                expected=[] if case['admission'] is None else ['history-audit: '+message(managed.paths['grant'],case['admission'],case['disposition'])]
                self.assertEqual([w for w in history['warnings'] if FAMILY in w],expected)
                context=client.lint_context(str(managed.root))
                projections=cli._index_projection_digests(managed.root)
                baseline=self.old_rules.lint_relay_root(managed.root,engine_root=True,
                    record_context=context,context_mode='daemon',projection_digests=projections)
                self.assertEqual(plain['errors'],baseline.errors)

    def test_record_known_gate_record_warns_at_admission(self):
        case=next(c for c in CASES if c['name']=='W23')
        with ManagedRoot() as managed:
            for item in case['rows'][:-1]: managed.submit(item)
            self.assertIn(managed.paths['gate'],{e['path'] for e in client.lint_context(str(managed.root))['entries']})
            managed.submit(case['rows'][-1])
            status,findings=managed.stored('grant')
            self.assertEqual(status,'judged')
            self.assert_answer([f for f in findings if f.id==FAMILY],case,managed.paths)

    def test_tie_contexts_differ_as_declared(self):
        for case in CASES:
            if not (case['name'].startswith('W22') or case['name']=='S5-tie'): continue
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)/'.relays/b8'
                self.assert_answer(self.direct(case,root),case)
                sweep=[f.params['reason'] for f in rules.lint_relay_root(root).findings if f.id==FAMILY]
                self.assertEqual(sweep,[] if case['reason'] is None else [case['reason']])
