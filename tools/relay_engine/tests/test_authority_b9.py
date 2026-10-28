"""B9 reason transitions, complete external evidence, and per-gate identities."""
from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from relay_engine import authority, rules
from relay_engine.tests import xroot_b9_cases as cases, xroot_cache_cases as cc
from relay_engine.tests import xroot_narrow_corpus as corpus
from relay_engine.tests.test_authority_fold5 import TestAuthorityFold5
from relay_engine.tests import test_xroot_deadline as deadline_cases
import xrootfixgen as xf

C = 'authority.could-not-evaluate'
L = 'authority.lineage-incomplete'
A = 'authority.artifact-changed'
ENTRY_KEYS = {'relays','gate','reason','axis','detail','repository','commit','root','progress'}


class TestAuthorityB9(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(authority, '_xroot_reason'), 'B9 reason mapping is missing')
        self.temp = tempfile.TemporaryDirectory(prefix='authority-b9-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.case = cc.make(self.home/'valid')
        # These two tests own a scripted finite clock; other assertions decide
        # semantic answers and must not be replaced by machine-load deadlines.
        if self._testMethodName not in {'test_external_deadline_detail_wording',
                                        'test_post_review_deadline_never_allows'}:
            self.enterContext(corpus.budget(rules))

    @contextmanager
    def direct_chain(self, declarations):
        fixture = TestAuthorityFold5()
        fixture.setUp()
        try:
            xf._init_repo(Path(fixture.root.path))
            parent, carriers = '', []
            for index, declaration in enumerate(declarations):
                extra = '\n'.join(k+': '+v for k,v in declaration.items())
                if parent:
                    extra += '\nPARENT_DISPATCH_ID: '+parent
                parent = 'plan-'+str(index)
                carriers.append(fixture.add('PLAN', ident=parent, extra=extra, content=''))
            grant = fixture.add('MERGE-GATE', 'operator', ident='grant',
                                extra='PARENT_DISPATCH_ID: '+parent, content='DISPATCH MERGE')
            yield fixture, grant, carriers
        finally:
            fixture.tearDown()

    def entry(self, value, declaration, axis, reason, *, gate='cross-root-design'):
        self.assertEqual(ENTRY_KEYS | ({'lock'} if gate=='plan-source' else set()), set(value))
        self.assertEqual(axis, value['axis'])
        self.assertEqual(reason, value['reason'])
        self.assertEqual(gate, value['gate'])
        prefix = 'DESIGN_SOURCE_' if gate=='cross-root-design' else 'PLAN_SOURCE_'
        self.assertEqual(declaration.get(prefix+'REPO'), value['repository'])
        self.assertEqual(declaration.get(prefix+'COMMIT'), value['commit'])
        self.assertEqual(declaration.get(prefix+'ROOT') if gate=='cross-root-design' else None, value['root'])
        self.assertTrue(value['relays'])
        self.assertIsInstance(value['detail'], str)
        if axis=='capacity':
            self.assertEqual({'stopped_at','relays_read','trees_read','bytes_read','bound_seconds','limits'}, set(value['progress']))
            self.assertEqual(5, value['progress']['bound_seconds'])
            self.assertEqual({'trees','entries','relays','relay_bytes'}, set(value['progress']['limits']))
        else:
            self.assertIsNone(value['progress'])

    @contextmanager
    def fault(self, label, case):
        if label=='T1b-timeout-population-blob':
            original = rules.XrootBatchReader
            oid = xf._git(case.repository, 'rev-parse', case.commit+':'+cc.R)
            class Expired(original):
                def request(self, spec):
                    if spec==oid+'^{blob}':
                        self.deadline = time.monotonic()-1
                    return super().request(spec)
            with mock.patch.object(rules, 'XrootBatchReader', Expired):
                yield
        elif label.startswith('T'):
            with cases.deadline_fault(case, rules):
                yield
        else:
            yield

    def test_transition_table_pair_and_direct(self):
        axes = {
            'control-allow':None, 'T1-timeout-blob':'capacity', 'T1b-timeout-population-blob':'capacity',
            'T2-timeout-tree':'capacity', 'T3-timeout-commit':'capacity',
            'L1-no-origin':'authority-population', 'L2-no-review':'authority-parent',
            'L3-owner-mismatch':'authority-owner', 'L4-verdict-must-revise':'authority-verdict',
            'L5-ordering-review-earlier':'ordering', 'L6-ambiguity-two-origins':'ambiguity',
            'L7-orchestrator-peer':None, 'L8-origin-no-timestamp':'ordering',
            'L9-review-wrong-authority':'authority-population',
            'G1-symlink':None, 'G2-non-utf8-blob':None, 'G3-non-utf8-name':None,
            'G4-phase-conflict-unrelated':None, 'G5-depth-257':'repository', 'G6-gitlink-md':None,
            'D1-repo-missing':'repository', 'D3-commit-short':'commit', 'D4-commit-absent':'commit',
            'D5-root-missing':'authority-population', 'D6-path-missing':'byte',
            'D7-digest-mismatch':'byte', 'D8-digest-malformed':'byte', 'D9-root-dotdot':'declaration',
        }
        baseline = json.loads((Path(__file__).parents[2]/'relay-engine-fixtures/xroot/baseline-b8d87eb.json').read_text())
        changed = {'T1b-timeout-population-blob','L2-no-review','L3-owner-mismatch',
                   'L4-verdict-must-revise','L5-ordering-review-earlier','L7-orchestrator-peer',
                   'L8-origin-no-timestamp','G1-symlink','G2-non-utf8-blob','G3-non-utf8-name',
                   'G4-phase-conflict-unrelated','G6-gitlink-md'}
        lineage = {'authority-population','authority-parent','authority-owner','authority-verdict','ordering','ambiguity'}
        for label, case in cases.materialize(self.home/'matrix', only=axes).items():
            axis = axes[label]
            expected = [] if axis is None else [A if label=='D7-digest-mismatch' else L if axis in lineage else C]
            with cc.authority_fixture(case.declaration) as (fixture, pair, direct):
                for path, grant, action in (('pair',pair,'implement'),('direct',direct,'merge')):
                    with self.subTest(case=label,path=path), corpus.budget(rules), \
                         rules.xroot_fact_cache(rules.XrootFactCache()), self.fault(label,case):
                        answer = fixture.check(grant,action=action)
                    self.assertEqual(expected,answer['reasons'],label)
                    self.assertEqual(not expected,answer['allowed'])
                    self.assertEqual(label in changed, baseline['cases'][label]['paths'][path]['reasons'] != expected)
                    self.assertEqual(0 if axis is None else 1,len(answer['external']))
                    if axis:
                        self.entry(answer['external'][0],case.declaration,axis,expected[0])

    def test_capacity_and_narrow_transition_rows_pair_and_direct(self):
        repo = self.case.repository
        tiny = xf._literal_object(repo,'blob',b'PHASE: SITREP\n')
        big = xf._literal_object(repo,'blob',b'x'*(1024*1024))
        leaf = cc._object_tree(repo,{'n.txt':(b'100644',tiny)})
        rows = []
        for candidate in (False,True):
            prefix = 'DESIGN-' if candidate else 'SITREP-'
            count = dict(cc.files(), **{f'relays/f{i//150:03d}/{prefix}{i:06d}.md':(b'100644',tiny) for i in range(19999)})
            rows.append(('count-'+str(candidate),cc.pin(repo,count),'capacity' if candidate else None,'XROOT_MAX_RELAYS'))
            size = dict(cc.files(), **{f'relays/big/{prefix}{i:06d}.md':(b'100644',big) for i in range(300)})
            rows.append(('bytes-'+str(candidate),cc.pin(repo,size),'capacity' if candidate else None,'XROOT_MAX_RELAY_BYTES'))
        trees = dict(cc.files(), **{f'relays/t{i:05d}':(b'40000',leaf) for i in range(20000)})
        rows.append(('trees',cc.pin(repo,trees),'capacity','XROOT_MAX_TREES'))
        entries = dict(cc.files(), **{f'relays/n{i:06d}.txt':(b'100644',tiny) for i in range(200001)})
        rows.append(('entries',cc.pin(repo,entries),'capacity','XROOT_MAX_ENTRIES'))
        complete = dict(cc.files(), **{f'relays/f{i//150:03d}/SITREP-{i:06d}.md':(b'100644',tiny) for i in range(5000)})
        rows.append(('complete',cc.pin(repo,complete),None,None))
        relay_tree = xf._git(repo,'rev-parse',self.case.commit+':relays')
        raw = xf._git(repo,'cat-file','tree',relay_tree,text=False)
        malformed_name = xf._literal_object(repo,'tree',raw+xf._tree_entry(b'100644',b'DESIGN-\xff.md',tiny))
        name_case=cc.pin(repo,{'relays':(b'40000',malformed_name),'designs/d1.md':b'design'})
        rows.append(('name',name_case,'authority-population',None))
        baseline = json.loads((Path(__file__).parents[2]/'relay-engine-fixtures/xroot/baseline-b8d87eb.json').read_text())
        old_rows={'count-False':'C1-over-count-20001','count-True':'C1-over-count-20001',
                  'bytes-False':'C2-over-bytes-300MiB','bytes-True':'C2-over-bytes-300MiB',
                  'trees':'M1-over-trees-20001','entries':'M2-over-entries-200001',
                  'complete':'B1-complete-within-envelope','name':'G3-non-utf8-name',
                  'symlink':'G1-symlink','utf8':'G2-non-utf8-blob','phase':'G4-phase-conflict-unrelated','gitlink':'G6-gitlink-md'}
        changed={'count-False','count-True','bytes-True','entries','complete'}
        # Candidate twins of every R181 hygiene move remain refusals.
        for name, content, axis in (
            ('symlink',(b'120000',tiny),'authority-population'),
            ('utf8',b'\xff','authority-population'),
            ('phase','PHASE: DESIGN\nPHASE: SITREP\n','authority-population'),
            ('gitlink',(b'160000','2'*40),'authority-population')):
            rows.append((name,cc.pin(repo,dict(cc.files(),**{'relays/DESIGN-'+name+'.md':content})),axis,None))
        for label,case,axis,limit in rows:
            with cc.authority_fixture(case.declaration) as (fixture,pair,direct):
                for path,grant,action in (('pair',pair,'implement'),('direct',direct,'merge')):
                    with self.subTest(case=label,path=path),corpus.budget(rules):
                        answer=fixture.check(grant,action=action)
                    expected=[] if axis is None else [C if axis=='capacity' else L]
                    self.assertEqual(expected,answer['reasons'])
                    self.assertEqual(label in changed,baseline['cases'][old_rows[label]]['paths'][path]['reasons'] != expected)
                    self.assertEqual(not expected,answer['allowed'])
                    self.assertEqual(0 if axis is None else 1,len(answer['external']))
                    if axis:
                        self.entry(answer['external'][0],case.declaration,axis,expected[0])
                        if limit:self.assertIn(limit,answer['external'][0]['detail'])

    def test_capacity_edge_does_not_erase_other_edge(self):
        missing=cc.make(self.home/'no-review',{cc.O:cc.DESIGN,'designs/d1.md':b'design'})
        bad=dict(self.case.declaration,DESIGN_SHA256='0'*64)
        for other,reason in ((missing.declaration,L),(bad,A)):
            for declarations in ((other,self.case.declaration),(self.case.declaration,other)):
                with self.subTest(reason=reason,order=[d['DESIGN_SHA256'] for d in declarations]), \
                     self.direct_chain(declarations) as (fixture,grant,carriers), \
                     mock.patch.object(rules,'XROOT_MAX_RELAYS',1):
                    answer=fixture.check(grant,action='merge')
                    self.assertEqual(sorted([C,reason]),answer['reasons'])
                    self.assertEqual(2,len(answer['external']))
                    self.assertEqual({C,reason},{e['reason'] for e in answer['external']})
                    self.assertEqual([[r.rendered_path] for r in reversed(carriers)],[e['relays'] for e in answer['external']])

    def test_passing_edge_then_capacity_edge(self):
        late=cc.make(self.home/'late')
        original=rules.xroot_git
        def git(repo,*args,**kwargs):
            if Path(repo).resolve()==late.repository.resolve():kwargs['deadline']=0
            return original(repo,*args,**kwargs)
        with self.direct_chain((late.declaration,self.case.declaration)) as (fixture,grant,carriers), \
             mock.patch.object(rules,'xroot_git',side_effect=git):
            answer=fixture.check(grant,action='merge')
        self.assertEqual([C],answer['reasons'])
        self.assertEqual(1,len(answer['external']))
        self.entry(answer['external'][0],late.declaration,'capacity',C)
        self.assertEqual([carriers[0].rendered_path],answer['external'][0]['relays'])

    def test_missing_field_external_entry(self):
        for value in (None,''):
            declaration=dict(self.case.declaration)
            if value is None:
                del declaration['DESIGN_SOURCE_COMMIT']
            else:
                declaration['DESIGN_SOURCE_COMMIT']=value
            with self.subTest(declared=value),cc.authority_fixture(declaration) as (fixture,pair,direct):
                for grant,action in ((pair,'implement'),(direct,'merge')):
                    answer=fixture.check(grant,action=action)
                    self.assertEqual([C],answer['reasons'])
                    self.assertEqual(1,len(answer['external']))
                    self.entry(answer['external'][0],declaration,'declaration',C)
                    self.assertEqual('missing DESIGN_SOURCE_COMMIT',answer['external'][0]['detail'])

    def test_review_approval_and_control_character_axes(self):
        for axis,graph in (
            ('review-approval',dict(cc.files(),**{cc.R:cc.REVIEW+'VERDICT: must-revise\n'})),
            ('foreign-control-character',dict(cc.files(),**{'relays/DESIGN-older-20260801-103000.md':cc.REVIEW+'\nprose\f tail'}))):
            case=cc.pin(self.case.repository,graph)
            with self.subTest(axis=axis),cc.authority_fixture(case.declaration) as (fixture,pair,direct):
                answer=fixture.check(pair)
                self.assertEqual([L],answer['reasons'])
                self.assertEqual(1,len(answer['external']))
                self.entry(answer['external'][0],case.declaration,axis,L)
                direct_answer=fixture.check(direct,action='merge')
                self.assertTrue(direct_answer['allowed'])
                self.assertEqual([],direct_answer['external'])

    def test_design_identity_shared_plan_source_differs(self):
        other=cc.make(self.home/'other')
        good=dict(self.case.declaration,PLAN_SOURCE_REPO=str(self.case.repository),PLAN_SOURCE_COMMIT=self.case.commit,PLAN_LOCK_ID='designs/d1.md')
        bad=dict(good,PLAN_SOURCE_REPO=str(other.repository),PLAN_SOURCE_COMMIT=other.commit,PLAN_LOCK_ID='plans/missing.md')
        with self.direct_chain((bad,good)) as (fixture,grant,carriers), \
             mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify, \
             mock.patch.object(rules,'xroot_plan_gate',wraps=rules.xroot_plan_gate) as plan:
            answer=fixture.check(grant,action='merge')
            self.assertEqual(1,verify.call_count)
            self.assertEqual(2,plan.call_count)
        self.assertEqual([C],answer['reasons'])
        self.assertEqual(1,len(answer['external']))
        self.entry(answer['external'][0],bad,'byte',C,gate='plan-source')
        self.assertEqual([carriers[0].rendered_path],answer['external'][0]['relays'])
        self.assertEqual({'key':'PLAN_LOCK_ID','path':'plans/missing.md'},answer['external'][0]['lock'])

    def test_plan_source_same_repo_commit_different_lock(self):
        # DD r11 erratum 1 section 2 row 1 (R229): digest mismatch is artifact-changed.
        for key in ('DESIGN_LOCK_ID','PLAN_LOCK_ID'):
            for fault in ('path','digest'):
                good=dict(self.case.declaration,PLAN_SOURCE_REPO=str(self.case.repository),PLAN_SOURCE_COMMIT=self.case.commit)
                good[key]='designs/d1.md'
                bad=dict(good)
                bad[key]='plans/missing.md' if fault=='path' else 'designs/d1.md @ '+'0'*64
                for order in ((bad,good),(good,bad)):
                    with self.subTest(key=key,fault=fault,order=order[0][key]),self.direct_chain(order) as (fixture,grant,carriers):
                        answer=fixture.check(grant,action='merge')
                    reason=C if fault=='path' else A
                    self.assertEqual([reason],answer['reasons'])
                    self.assertEqual(1,len(answer['external']))
                    self.entry(answer['external'][0],bad,'byte',reason,gate='plan-source')
                    index=0 if order[0] is bad else 1
                    self.assertEqual([carriers[index].rendered_path],answer['external'][0]['relays'])
                    self.assertEqual({'key':key,'path':bad[key].partition(' @ ')[0]},answer['external'][0]['lock'])

    def test_external_empty_when_allowed(self):
        with cc.authority_fixture(self.case.declaration) as (fixture,pair,direct):
            for grant,action in ((pair,'implement'),(direct,'merge')):
                answer=fixture.check(grant,action=action)
                self.assertTrue(answer['allowed'])
                self.assertEqual([],answer['external'])
                self.assertEqual({'allowed','reasons','relay','seat','action','checked_at','external'},set(answer))

    def test_external_deadline_detail_wording(self):
        control=deadline_cases.TestXrootDeadline()
        control.setUp()
        self.addCleanup(control.doCleanups)
        tail='this is a time limit on a cold read, not a lineage fault; a large root can reach it on a busy machine'
        label='relays at '+control.case.commit[:12]
        with cc.authority_fixture(control.case.declaration) as (fixture,pair,direct):
            for stage in ('classification','classification-query','batch','event','limit'):
                expected=(f'Git object query timed out at the 5 s bound during {"classification" if stage=="classification-query" else "rev-parse"}; '+tail
                          if stage.startswith('classification') else f'foreign read of {label} stopped at the 5 s bound at tree relays, after 0 relays and 0 trees; '+tail)
                if stage=='limit':expected=label+' exceeds the supported XROOT_MAX_RELAYS of 1; this is a capacity limit, not a lineage gap'
                for path,grant,action in (('pair',pair,'implement'),('direct',direct,'merge')):
                    with self.subTest(stage=stage,path=path),rules.xroot_fact_cache(rules.XrootFactCache()), \
                         control.injected_deadline(rules,stage),mock.patch.object(rules,'XROOT_MAX_RELAYS',1 if stage=='limit' else 20000):
                        answer=fixture.check(grant,action=action)
                    self.assertEqual([C],answer['reasons'])
                    self.assertEqual(1,len(answer['external']))
                    self.entry(answer['external'][0],control.case.declaration,'capacity',C)
                    self.assertEqual(expected,answer['external'][0]['detail'])
                    self.assertNotRegex(answer['external'][0]['detail'],r'retry|again')

    def test_post_review_deadline_never_allows(self):
        now=[0]
        original=authority._approved
        def approved(item):
            answer=original(item)
            if isinstance(item,rules.XrootRelay):now[0]=6
            return answer
        with cc.authority_fixture(self.case.declaration) as (fixture,pair,_), \
             mock.patch.object(rules.time,'monotonic',side_effect=lambda:now[0]), \
             mock.patch.object(authority,'_approved',side_effect=approved):
            answer=fixture.check(pair)
        self.assertEqual([C],answer['reasons'])
        self.assertFalse(answer['allowed'])
        self.assertEqual(1,len(answer['external']))
        self.entry(answer['external'][0],self.case.declaration,'capacity',C)

    def test_cache_differential_final_shape(self):
        from relay_engine.tests.test_xroot_cache_differential import TestXrootCacheDifferential
        fixture=TestXrootCacheDifferential()
        fixture.setUp()
        original=cc.whole_answers
        def complete(*args,**kwargs):
            answers=original(*args,**kwargs)
            for answer in answers.values():
                for entry in answer['external']:
                    keys=ENTRY_KEYS | ({'lock'} if entry['gate']=='plan-source' else set())
                    self.assertEqual(entry,{key:entry[key] for key in keys})
                    if entry['axis']=='capacity':
                        keys={'stopped_at','relays_read','trees_read','bytes_read','bound_seconds','limits'}
                        self.assertEqual(entry['progress'],{key:entry['progress'][key] for key in keys})
            return answers
        try:
            with mock.patch.object(cc,'whole_answers',side_effect=complete):
                fixture.run_sequence(rules.XROOT_CACHE_BOUND,require_external=True)
        finally:
            fixture.doCleanups()
