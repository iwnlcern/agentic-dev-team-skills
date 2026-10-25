"""B9 older ancestor edges: integrity without historical permission gates."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from relay_engine import authority, rules
from relay_engine.tests import test_authority_fold5 as fixtures
from relay_engine.tests import test_authority_fold10 as fold10
from relay_engine.tests import xroot_cache_cases as cc
from relay_engine.tests.test_judging import body
import xrootfixgen as xf

C = 'authority.could-not-evaluate'
L = 'authority.lineage-incomplete'
A = 'authority.artifact-changed'
H = 'authority.header-conflict'


class TestAuthorityB9Ancestors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='authority-b9-ancestors-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.case = cc.make(self.home/'nearest')
        self.older = cc.make(self.home/'older')

    @contextmanager
    def chain(self, ancestors, *, nearest=None):
        """Real admitted ledger rows, oldest first; explicit parent overrides allowed."""
        f = fixtures.TestAuthorityFold5()
        f.setUp()
        try:
            xf._init_repo(Path(f.root.path))
            previous = ''
            carriers = []
            for index, spec in enumerate(ancestors):
                phase = spec.get('phase','PLAN')
                ident = spec.get('id','ancestor-'+str(index))
                declaration = spec.get('fields',{})
                parent = spec.get('parent',previous)
                extra = '\n'.join(k+': '+v for k,v in declaration.items())
                if parent:
                    extra += '\nPARENT_DISPATCH_ID: '+parent
                extra += '\n'+spec.get('extra','')
                if spec.get('extra'):
                    # Imported history can carry conflicts rejected by fresh admission.
                    f.number += 1
                    raw=body(phase,'p.planner','p.implementer',ident,extra,'')
                    path=f'legacy-{f.number}.md'
                    f.ledger.execute("INSERT INTO relays(seq,submission_id,stamp,rendered_path,phase,role,dispatch_id,from_seat,to_seats,parent_dispatch_id,headers_json,body,body_sha256,content_hash,origin,advisories_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f.number,f'legacy-{f.number}',f'20261013-12000{f.number}',path,phase,'Pair Planner',ident,'p.planner','["p.implementer"]',parent,'{}',raw,hashlib.sha256(raw).hexdigest(),'a'*64,'hand','[]'))
                    carriers.append(SimpleNamespace(seq=f.number,rendered_path=path))
                else:
                    carriers.append(f.add(phase,ident=ident,extra=extra,content=spec.get('content','')))
                previous = ident
            pin = f.artifact('plans','PL-nearest')
            fields = self.case.declaration if nearest is None else nearest
            extra = '\n'.join(k+': '+v for k,v in fields.items())
            extra += '\nPLAN_LOCK_ID: PL\n'+pin
            if previous:
                extra += '\nPARENT_DISPATCH_ID: '+previous
            plan = f.add('PLAN',ident='nearest',extra=extra,content='')
            f.add('PLAN-REVIEW','p.implementer','p.planner','review',
                  'PARENT_DISPATCH_ID: nearest\nPLAN_LOCK_ID: PL\n'+pin+'\nVERDICT: approve','')
            grant = f.add(ident='grant',extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
            yield f,grant,carriers,plan
        finally:
            f.tearDown()

    def test_valid_plan_bad_ancestor_edge_refuses(self):
        # Dropping older edges incorrectly allows this valid nearest PLAN.
        for fields,reason,axis in ((dict(self.older.declaration,DESIGN_SHA256='0'*64),A,'byte'),
                                  (dict(self.older.declaration,DESIGN_SOURCE_COMMIT='bad'),C,'commit')):
            with self.subTest(axis=axis),self.chain([{'fields':fields}]) as (f,g,rows,_):
                answer=f.check(g)
                self.assertFalse(answer['allowed'])
                self.assertEqual([reason],answer['reasons'])
                self.assertEqual(1,len(answer['external']))
                entry=answer['external'][0]
                self.assertEqual(axis,entry['axis'])
                self.assertEqual([rows[0].rendered_path],entry['relays'])
                self.assertEqual(fields['DESIGN_SOURCE_COMMIT'],entry['commit'])

    def test_valid_ancestor_edge_allows(self):
        with self.chain([{'fields':self.older.declaration}]) as (f,g,_,_), \
             mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify:
            answer=f.check(g)
            self.assertTrue(answer['allowed'],answer)
            self.assertEqual([],answer['external'])
            self.assertEqual(2,verify.call_count)

    def test_repeated_ancestor_edge_verified_once_listed_with_both(self):
        bad=dict(self.older.declaration,DESIGN_SHA256='0'*64)
        with self.chain([{'fields':bad},{'fields':bad}]) as (f,g,rows,_), \
             mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify:
            answer=f.check(g)
            self.assertEqual([A],answer['reasons'])
            self.assertEqual(2,verify.call_count)  # nearest plus one distinct ancestor identity
            self.assertEqual(1,len(answer['external']))
            self.assertEqual([r.rendered_path for r in reversed(rows)],answer['external'][0]['relays'])

    def test_self_parent_and_repeated_id_cycle_stop(self):
        for specs in ([{'id':'self','parent':'self','fields':self.older.declaration}],
                      [{'id':'repeat','parent':'repeat','fields':self.older.declaration},
                       {'id':'repeat','parent':'repeat','fields':self.older.declaration}]):
            with self.subTest(specs=specs),self.chain(specs) as (f,g,carriers,plan):
                rows=tuple(authority.authorizations(f.ledger,'p.implementer','implement'))
                start=next(r for r in rows if r.seq==plan.seq)
                walked,conflict=authority._walked_ancestors(f.ledger,rows,start)
                self.assertFalse(conflict)
                self.assertEqual([carriers[0].seq],[r.seq for r in walked])
                self.assertTrue(f.check(g)['allowed'])

    def test_conflicting_ancestor_header_conflict_stops_walk(self):
        # Conflicting parent or declaration values cannot choose either gate or older edges.
        for key,value in (('DESIGN_DOC_ID','other'),('DESIGN_SOURCE_COMMIT','bad'),('PARENT_DISPATCH_ID','missing')):
            bad=dict(self.older.declaration,DESIGN_SHA256='0'*64)
            conflict=dict(self.older.declaration,PLAN_SOURCE_REPO=str(self.older.repository),
                          PLAN_SOURCE_COMMIT=self.older.commit,PLAN_LOCK_ID='missing.md')
            specs=[{'fields':bad},{'fields':conflict,'extra':key+': '+value}]
            with self.subTest(key=key),self.chain(specs) as (f,g,_,_), \
                 mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify, \
                 mock.patch.object(rules,'xroot_plan_gate',wraps=rules.xroot_plan_gate) as gate:
                answer=f.check(g)
                self.assertEqual([H],answer['reasons'])
                self.assertEqual([],answer['external'])
                self.assertEqual(1,verify.call_count)
                self.assertEqual(0,gate.call_count)

    def test_ancestor_control_character_lineage_incomplete(self):
        with self.chain([{'fields':self.older.declaration,'content':'prose\f tail'}]) as (f,g,_,_):
            answer=f.check(g)
            self.assertEqual([L],answer['reasons'])
            self.assertEqual([],answer['external'])

    def test_superseded_and_closed_valid_ancestors_allow(self):
        historical=dict(self.older.declaration,PLAN_ARTIFACT='PL-missing',PLAN_SHA256='0'*64)
        with self.chain([{'fields':historical}]) as (f,g,rows,_):
            f.ledger.execute("INSERT INTO supersession_edges(seq,target_seq,ruling_seq,source,applied) VALUES(1,?,?, 'local',1)",(rows[0].seq,g.seq))
            f.ledger.execute("INSERT INTO cycle_events(seq,dispatch_id,event,cause_seq) VALUES(1,'ancestor-0','close',?)",(rows[0].seq,))
            with mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify:
                answer=f.check(g)
                self.assertTrue(answer['allowed'],answer)
                self.assertEqual([],answer['reasons'])
                self.assertEqual(2,verify.call_count)

    def test_non_plan_ancestor_carriers(self):
        for plan_source in (False,True):
            fields=dict(self.older.declaration,DESIGN_SHA256='0'*64)
            if plan_source:
                fields.update(PLAN_SOURCE_REPO=str(self.older.repository),PLAN_SOURCE_COMMIT=self.older.commit,
                              PLAN_LOCK_ID='missing.md')
            with self.subTest(plan_source=plan_source),self.chain([{'phase':'SITREP','fields':fields}]) as (f,g,rows,_), \
                 mock.patch.object(rules,'xroot_verify',wraps=rules.xroot_verify) as verify:
                answer=f.check(g)
                self.assertEqual([C] if plan_source else [],answer['reasons'])
                self.assertEqual(1,verify.call_count)
                self.assertEqual(1 if plan_source else 0,len(answer['external']))
                if plan_source:
                    self.assertEqual('plan-source',answer['external'][0]['gate'])
                    self.assertEqual([rows[0].rendered_path],answer['external'][0]['relays'])
                    self.assertEqual({'key':'PLAN_LOCK_ID','path':'missing.md'},answer['external'][0]['lock'])

    def test_local_design_lineage_is_not_consumed_twice(self):
        f=fixtures.TestAuthorityFold5()
        f.setUp()
        self.addCleanup(f.tearDown)
        xf._init_repo(Path(f.root.path))
        old=f.add('SITREP',ident='old',extra='PLAN_SOURCE_REPO: '+str(self.older.repository)+
                  '\nPLAN_SOURCE_COMMIT: '+self.older.commit+'\nPLAN_LOCK_ID: missing.md',content='')
        design=f.artifact('designs','DD-local')
        f.add('DESIGN',ident='design',extra='PARENT_DISPATCH_ID: old\nDESIGN_DOC_ID: DD\n'+design+
              '\nPLAN_SOURCE_REPO: '+str(self.case.repository)+'\nPLAN_SOURCE_COMMIT: '+self.case.commit+
              '\nPLAN_LOCK_ID: designs/d1.md',content='')
        f.add('DESIGN-REVIEW','p.implementer','p.planner','design-review',
              'PARENT_DISPATCH_ID: design\nDESIGN_DOC_ID: DD\nVERDICT: approve\n'+design,'')
        pin=f.artifact('plans','PL-local')
        f.add('PLAN',ident='plan',extra='PARENT_DISPATCH_ID: design-review\nDESIGN_LOCK_ID: DD\nPLAN_LOCK_ID: PL\n'+pin,content='')
        f.add('PLAN-REVIEW','p.implementer','p.planner','review',
              'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\nVERDICT: approve\n'+pin,'')
        grant=f.add(ident='grant',extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
        with mock.patch.object(rules,'xroot_plan_gate',wraps=rules.xroot_plan_gate) as gate:
            answer=f.check(grant)
        self.assertEqual([C],answer['reasons'])
        self.assertEqual(2,gate.call_count)
        self.assertEqual(1,len(answer['external']))
        self.assertEqual([old.rendered_path],answer['external'][0]['relays'])

    def test_direct_issuer_unchanged(self):
        # Reuse the actual direct-path foreign fixtures and preserved control assertions.
        fixture=fold10.TestAuthorityFold10()
        fixture.test_direct_issuer_preserves_old_foreign_behavior()

    def deadline_chain(self, refused):
        late=cc.make(self.home/'late')
        middle=dict(self.older.declaration)
        if refused:
            middle['DESIGN_SHA256']='0'*64
        original=rules.xroot_git
        deadlines=[]
        def git(repo,*args,**kwargs):
            current=rules._XROOT_BUDGET.get()
            if current:
                deadlines.append(current[0])
            if Path(repo).resolve()==late.repository.resolve():
                rules._XROOT_BUDGET.set((0,current[1]))
                kwargs['deadline']=0
            return original(repo,*args,**kwargs)
        with self.chain([{'fields':late.declaration},{'fields':middle}]) as (f,g,rows,_), \
             mock.patch.object(rules,'xroot_git',side_effect=git):
            answer=f.check(g)
        self.assertFalse(answer['allowed'])
        self.assertEqual(sorted([A,C]) if refused else [C],answer['reasons'])
        self.assertEqual(2 if refused else 1,len(answer['external']))
        self.assertEqual('capacity',answer['external'][-1]['axis'])
        self.assertEqual([rows[0].rendered_path],answer['external'][-1]['relays'])
        self.assertEqual(1,len(set(d for d in deadlines if d)))
        if refused:
            self.assertEqual('byte',answer['external'][0]['axis'])
            self.assertEqual([rows[1].rendered_path],answer['external'][0]['relays'])

    def test_ancestor_walk_deadline_never_allows(self):
        self.deadline_chain(False)

    def test_ancestor_walk_deadline_after_refused_edge(self):
        self.deadline_chain(True)
