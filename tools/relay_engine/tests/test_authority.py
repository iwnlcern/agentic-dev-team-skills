"""Current-use authority is derived from current record and approval lineage."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import time
import unittest
from unittest import mock

from relay_engine import authority, cli, errors, judging, ledger, rules
from relay_engine.tests.test_ledger import LedgerFixture, WALL
from relay_engine.tests.test_judging import body, envelope
from relay_engine.tests.test_merge_claim import _standalone


class TestAuthority(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.initialize()
        ledger.establish_run_identity(self.ledger,"r")
        self.number = 0
        self.files = {}

    def add(self, phase='IMPL', sender='p.planner', target='p.implementer',
            ident=None, extra='', content='DISPATCH IMPL', **kwargs):
        self.number += 1
        raw = body(phase, sender, target, ident or f'row-{self.number}', extra, '\n'+content)
        admitted = ledger.admit(self.ledger, self.root, ledger._server_envelope(raw), raw,
                                f'sid-{self.number}', clock=lambda: WALL+self.number, **kwargs)
        return admitted

    def artifact(self, kind, stem, data=b'original'):
        path = Path(self.root.path)/kind/(stem+'.md')
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        self.files[stem] = path
        field = 'PLAN' if kind=='plans' else 'DESIGN'
        return f'{field}_ARTIFACT: {stem}\n{field}_SHA256: {hashlib.sha256(data).hexdigest()}'

    def base(self, design=False):
        parent = ''
        if design:
            dd = self.artifact('designs','DD-base')
            self.design = self.add('DESIGN', ident='design', extra='DESIGN_DOC_ID: DD\n'+dd, content='')
            self.add('DESIGN-REVIEW', 'p.implementer', 'p.planner', 'design-review',
                     'PARENT_DISPATCH_ID: design\nDESIGN_DOC_ID: DD\nDESIGN_REVIEW_VERDICT: approve\n'+dd, '')
            parent = 'PARENT_DISPATCH_ID: design-review\nDESIGN_LOCK_ID: DD\nDESIGN_RECORD_KIND: design-doc\n'
        self.plan_decl = self.artifact('plans','PL-base')
        self.plan = self.add('PLAN',ident='plan',extra=parent+'PLAN_LOCK_ID: PL\n'+self.plan_decl,content='')
        self.add('PLAN-REVIEW','p.implementer','p.planner','review',
                 'PARENT_DISPATCH_ID: plan\nPLAN_LOCK_ID: PL\n'+self.plan_decl+'\nVERDICT: approve', '')
        self.grant = self.add(ident='grant',extra='PARENT_DISPATCH_ID: review\nPLAN_LOCK_ID: PL')
        return self.grant

    def amendment(self, kind='PLAN', stem=None, review=True, reviewer='p.implementer',
                  verdict='approve', data=b'amendment'):
        stem = stem or ('PL-base-erratum-1' if kind=='PLAN' else 'DD-base-erratum-1')
        decl = self.artifact('plans' if kind=='PLAN' else 'designs',stem,data)
        lock = 'PLAN_LOCK_ID: PL' if kind=='PLAN' else 'DESIGN_DOC_ID: DD'
        ident = f'amend-{self.number+1}'
        carrier = self.add(kind,ident=ident,extra=lock+'\n'+decl,content='')
        reviewed = None
        if review:
            reviewed = self.review_amend(kind,ident,decl,reviewer,verdict)
        return carrier, reviewed, decl

    def review_amend(self,kind,ident,decl,sender='p.implementer',verdict='approve'):
        return self.add(kind+'-REVIEW',sender,'p.planner',
                        extra=f'PARENT_DISPATCH_ID: {ident}\n{kind}_REVIEW_VERDICT: {verdict}\n'+decl,content='')

    def check(self,grant=None,seat='p.implementer',action='implement'):
        return authority.check(self.ledger,self.root,(grant or self.grant).rendered_path,seat,action)

    def denied(self,reason,grant=None,**kwargs):
        result=self.check(grant,**kwargs)
        self.assertFalse(result['allowed'],result)
        self.assertIn('authority.'+reason,result['reasons'])
        return result

    def test_valid_grant_unchanged_artifact_allows(self):
        self.base()
        result=self.check()
        self.assertEqual(set(result),{'allowed','reasons','relay','seat','action','checked_at','external'})
        self.assertTrue(result['allowed'],result)
        self.assertEqual(result['reasons'],[])
        self.assertRegex(result['checked_at'],r'^\d{8}-\d{6}$')
        with mock.patch.object(authority,'authorizations',wraps=authority.authorizations) as evidence:
            self.check()
        evidence.assert_called_once_with(self.ledger,'p.implementer','implement')

    def test_artifact_amended_inside_approval_lineage_allows(self):
        self.base();self.amendment()
        self.assertTrue(self.check()['allowed'])

    def test_artifact_changed_with_foreign_acknowledgment_denies(self):
        self.base();decl=self.artifact('plans','PL-base',b'edited')
        self.add('SITREP','foreign.planner',extra=decl,content='acknowledged')
        self.denied('artifact-changed')

    def test_grant_to_another_seat_denies(self):
        self.grant=self.add(sender='operator',target='p.planner')
        self.denied('not-addressee')

    def test_grant_to_a_different_implementer_denies(self):
        self.grant=self.add(sender='operator',target='other.implementer')
        self.denied('not-addressee')

    def test_self_grant_denies(self):
        self.grant=self.add(sender='p.implementer')
        self.denied('issuer-not-permitted')

    def test_token_beside_denial_denies(self):
        self.grant=self.add('MERGE-GATE','operator',extra='MERGE_AUTHORIZATION: denied',content='DISPATCH MERGE')
        self.denied('denied',action='merge')
        self.grant=self.add(sender='operator',extra='VERDICT: denied')
        self.denied('denied')

    def test_conflicting_verdicts_deny(self):
        self.grant=self.add('MERGE-GATE','operator',extra='MERGE_AUTHORIZATION: approved\nVERDICT: denied',content='DISPATCH MERGE')
        self.denied('verdict-conflict',action='merge')

    def test_nonheader_verdict_is_inert_and_body_only_review_does_not_approve(self):
        for whitespace in (' ', '\t'):
            with self.subTest(whitespace=whitespace):
                direct=self.add(sender='operator',extra=f'VERDICT{whitespace}: denied')
                self.assertEqual(self.check(direct)['reasons'],[])
            with self.subTest(review_whitespace=whitespace):
                ident=f'plan-{self.number}'
                self.add('PLAN',ident=ident,content='')
                review_id=f'review-{self.number}'
                self.add('PLAN-REVIEW','p.implementer','p.planner',review_id,
                         f'PARENT_DISPATCH_ID: {ident}',
                         f'VERDICT: approve\nVERDICT{whitespace}: denied')
                grant=self.add(extra=f'PARENT_DISPATCH_ID: {review_id}')
                self.denied('lineage-incomplete',grant)

    def test_legacy_artifact_locks_are_verified_at_use(self):
        for action,phase,token in (('implement','IMPL','DISPATCH IMPL'),
                                   ('merge','MERGE-GATE','DISPATCH MERGE')):
            for field,directory in (('PLAN_LOCK_ID','plans'),('DESIGN_LOCK_ID','designs')):
                for prefix in ('sha256 ', ''):
                    with self.subTest(action=action,field=field,prefix=prefix):
                        self.artifact(directory,'legacy')
                        digest=hashlib.sha256(b'original').hexdigest()
                        grant=self.add(phase,'operator',
                            extra=f'{field}: {directory}/legacy.md @ {prefix}{digest}',content=token)
                        with mock.patch.object(authority,'read_artifact',wraps=authority.read_artifact) as read:
                            self.assertTrue(self.check(grant,action=action)['allowed'])
                        read.assert_called_once()
                        self.files['legacy'].write_bytes(b'edited')
                        self.denied('artifact-changed',grant,action=action)
                        self.files['legacy'].unlink()
                        self.denied('could-not-evaluate',grant,action=action)

    def test_legacy_artifact_lock_without_usable_digest_refuses(self):
        self.artifact('plans','legacy')
        for value in ('plans/legacy.md','plans/legacy.md @ sha256 invalid'):
            with self.subTest(value=value):
                grant=self.add(sender='operator',extra='PLAN_LOCK_ID: '+value)
                self.denied('could-not-evaluate',grant)

    def test_conflicting_phase_denies_grant_and_required_lineage(self):
        for action,phase,token in (('implement','IMPL','DISPATCH IMPL'),
                                   ('merge','MERGE-GATE','DISPATCH MERGE')):
            with self.subTest(action=action):
                valid=self.add(phase,'operator',content=token+'\n\nPHASE: '+phase)
                self.assertTrue(self.check(valid,action=action)['allowed'])
                invalid=self.add(phase,'operator',content=token+'\n\nPHASE: SITREP')
                self.denied('header-conflict',invalid,action=action)
        self.add('PLAN',ident='conflict-plan',content='PHASE: SITREP')
        self.add('PLAN-REVIEW','p.implementer','p.planner','conflict-review',
                 'PARENT_DISPATCH_ID: conflict-plan','VERDICT: approve')
        grant=self.add(extra='PARENT_DISPATCH_ID: conflict-review')
        self.denied('header-conflict',grant)

    def test_quoted_token_is_inert(self):
        for content in ('`DISPATCH IMPL`','```\nDISPATCH IMPL\n```','> DISPATCH IMPL'):
            with self.subTest(content=content):
                self.grant=self.add(sender='operator',content=content)
                self.denied('shape-absent')

    def test_no_grant_is_shape_absent(self):
        self.grant=self.add('SITREP','operator',content='consider implementation')
        self.denied('shape-absent')

    def test_authority_on_engine_authored_relay_is_denied(self):
        self.grant=self.add(sender='operator')
        self.ledger.execute("INSERT INTO seat_events(seq,address,event,boot_relay_seq) VALUES(1,'p.planner','booted',?)",(self.grant.seq,))
        self.denied('issuer-not-permitted')
        notice=self.add(sender='operator')
        self.ledger.execute('INSERT INTO routed_notices VALUES(?,?)',(self.grant.seq,notice.seq))
        self.denied('issuer-not-permitted',notice)

    def test_direct_operator_grant_allows_without_pair_chain(self):
        for sender in ('operator','orchestrator','r.orchestrator-planner'):
            for action,phase,token in (('implement','IMPL','DISPATCH IMPL'),('merge','MERGE-GATE','DISPATCH MERGE')):
                with self.subTest(sender=sender,action=action):
                    self.grant=self.add(phase,sender,content=token)
                    self.assertTrue(self.check(action=action)['allowed'])

    def test_pair_planner_dispatch_requires_review_and_plan(self):
        self.grant=self.add()
        self.denied('lineage-incomplete')
        self.grant=self.add('MERGE-GATE',content='DISPATCH MERGE')
        self.denied('issuer-not-permitted',action='merge')

    def test_superseded_or_closed_cycle_denies(self):
        self.base()
        self.ledger.execute("INSERT INTO supersession_edges(seq,target_seq,ruling_seq,source,applied) VALUES(1,?,?, 'local',1)",(self.plan.seq,self.grant.seq))
        self.denied('superseded')
        other=self.add(sender='operator',ident='closed')
        self.ledger.execute("INSERT INTO cycle_events(seq,dispatch_id,event,cause_seq) VALUES(1,'closed','close',?)",(other.seq,))
        self.denied('cycle-closed',other)

    def test_conflicting_critical_headers_deny(self):
        for extra in ('TO: other.implementer','DISPATCH_ID: other','FROM: other.planner',
                      'PLAN_LOCK_ID: one\nPLAN_LOCK_ID: two'):
            with self.subTest(extra=extra):
                # Imported history can contain headers fresh admission rejects.
                from types import SimpleNamespace
                self.number += 1
                raw=body('IMPL','operator',ident=f'legacy-{self.number}',extra=extra,content='DISPATCH IMPL')
                path=f'legacy-{self.number}.md'
                self.ledger.execute("INSERT INTO relays(seq,submission_id,stamp,rendered_path,phase,role,dispatch_id,from_seat,headers_json,body,body_sha256,content_hash,origin,advisories_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.number,f'legacy-{self.number}',f'20261013-12000{self.number}',path,'IMPL','Pair Planner',f'legacy-{self.number}','operator','{}',raw,hashlib.sha256(raw).hexdigest(),'a'*64,'hand','[]'))
                self.grant=SimpleNamespace(rendered_path=path)
                self.denied('header-conflict')

    def test_unreadable_artifact_exits_two(self):
        self.base();self.files['PL-base'].unlink()
        result=self.denied('could-not-evaluate')
        with mock.patch('relay_engine.cli.client.request',return_value=result), mock.patch('relay_engine.cli._command_root',return_value=self.root.path), contextlib.redirect_stdout(io.StringIO()) as output:
            code=cli.main(['authority','check',self.grant.rendered_path,'--seat','p.implementer','--action','implement'])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(output.getvalue()),result)

    def test_unknown_relay_path_is_command_error(self):
        with self.assertRaises(errors.EngineError) as caught:
            authority.check(self.ledger,self.root,'missing.md','p.implementer','implement')
        self.assertEqual(caught.exception.code,'E-HEADER')

    def test_unrelated_stored_findings_do_not_deny(self):
        self.base()
        self.ledger.execute('BEGIN')
        ledger.record_judgment(self.ledger,self.grant.seq,'judged','a'*64,[rules.Finding('file.lint','error',self.grant.rendered_path,(),{},'unrelated')])
        self.ledger.commit()
        self.assertTrue(self.check()['allowed'])

    def test_plan_and_design_errata_both_eligible_all_files_match_allows(self):
        self.base(True);self.amendment();self.amendment('DESIGN')
        self.assertTrue(self.check()['allowed'])

    def test_edited_erratum_file_denies(self):
        self.base(True);self.amendment();self.amendment('DESIGN')
        self.files['PL-base-erratum-1'].write_bytes(b'edited')
        self.denied('artifact-changed')

    def test_edited_base_design_denies(self):
        self.base(True);self.amendment();self.amendment('DESIGN')
        self.files['DD-base'].write_bytes(b'edited')
        self.denied('artifact-changed')

    def test_approved_reissue_is_denied_under_standing_dispatch(self):
        self.base();self.amendment(stem='PL-base',data=b'reissued')
        self.denied('artifact-changed')

    def test_reissue_allowed_under_its_own_dispatch(self):
        self.base();carrier,review,_=self.amendment(stem='PL-base',data=b'reissued')
        ident=self.ledger.execute('SELECT dispatch_id FROM relays WHERE seq=?',(review.seq,)).fetchone()[0]
        new=self.add(extra='PARENT_DISPATCH_ID: '+ident)
        self.assertTrue(self.check(new)['allowed'])
        self.denied('artifact-changed')

    def test_base_edit_with_unreviewed_or_foreign_reviewed_declaration_denies(self):
        self.base();self.amendment(stem='PL-base',review=False,data=b'changed')
        self.denied('artifact-changed')
        self.amendment(stem='PL-base',reviewer='other.implementer',data=b'changed')
        self.denied('artifact-changed')

    def test_unreviewed_erratum_carrier_does_not_join_the_set(self):
        self.base();self.amendment(review=False)
        self.files['PL-base-erratum-1'].unlink()
        self.assertTrue(self.check()['allowed'])

    def test_unrelated_locator_is_ignored(self):
        self.base();self.amendment(stem='unrelated')
        self.files['unrelated'].unlink()
        self.assertTrue(self.check()['allowed'])

    def test_effective_review_is_selected_before_verdict(self):
        self.base();carrier,_,decl=self.amendment()
        self.files['PL-base-erratum-1'].write_bytes(b'edited')
        self.denied('artifact-changed')
        ident=self.ledger.execute('SELECT dispatch_id FROM relays WHERE seq=?',(carrier.seq,)).fetchone()[0]
        self.review_amend('PLAN',ident,decl,verdict='must-revise')
        self.assertTrue(self.check()['allowed'])
        self.amendment(data=b'edited')
        self.assertTrue(self.check()['allowed'])

    def test_review_before_carrier_or_for_other_digest_is_not_effective(self):
        self.base()
        decl=self.artifact('plans','PL-base-erratum-1')
        self.review_amend('PLAN','future',decl)
        self.add('PLAN',ident='future',extra='PLAN_LOCK_ID: PL\n'+decl,content='')
        wrong=decl.replace(hashlib.sha256(b'original').hexdigest(),'0'*64)
        self.review_amend('PLAN','future',wrong)
        self.files['PL-base-erratum-1'].unlink()
        self.assertTrue(self.check()['allowed'])

    def test_superseded_approving_review_makes_carrier_ineligible(self):
        self.base();carrier,review,_=self.amendment()
        self.ledger.execute("INSERT INTO supersession_edges(seq,target_seq,ruling_seq,source,applied) VALUES(1,?,?, 'local',1)",(review.seq,self.grant.seq))
        self.files['PL-base-erratum-1'].unlink()
        self.assertTrue(self.check()['allowed'])

    def test_each_member_of_required_set_is_hashed(self):
        self.base(True);self.amendment();self.amendment('DESIGN')
        with mock.patch.object(authority,'read_artifact',wraps=authority.read_artifact) as read:
            self.assertTrue(self.check()['allowed'])
        self.assertEqual(read.call_count,4)
        self.assertEqual({call.args[1] for call in read.call_args_list},
                         {'plans/PL-base.md','plans/PL-base-erratum-1.md',
                          'designs/DD-base.md','designs/DD-base-erratum-1.md'})

    def test_pair_role_aliases_remain_own_pair(self):
        self.add('PLAN','p.pair-planner','p.pair-implementer','alias-plan',content='')
        self.add('PLAN-REVIEW','p.pair-implementer','p.pair-planner','alias-review',
                 'PARENT_DISPATCH_ID: alias-plan'+'\nVERDICT: approve', '')
        self.grant=self.add('IMPL','p.pair-planner','p.pair-implementer',extra='PARENT_DISPATCH_ID: alias-review')
        self.assertTrue(self.check(seat='p.pair-implementer')['allowed'])

    def test_cross_root_design_uses_pinned_lineage_and_shared_budget(self):
        from relay_engine.tests import xroot_narrow_corpus as corpus
        self.enterContext(corpus.budget(rules))
        import tempfile
        import xrootfixgen
        from relay_engine import paths
        with tempfile.TemporaryDirectory() as foreign:
            def complete_review(repo):
                path = repo/'relays'/xrootfixgen.REVIEW_NAME
                path.write_text(path.read_text()+'RUN_ID: fixture\nSUBJECT: foreign review\n')
            repo,commit,digest=xrootfixgen._foreign_repo(Path(foreign), complete_review)
            xrootfixgen._init_repo(Path(self.root.path))
            fields=xrootfixgen._plan_fields(repo,commit,digest)
            extra='\n'.join(k+': '+v for k,v in fields if k.startswith('DESIGN_'))
            self.add('PLAN',ident='external-plan',extra=extra,content='')
            self.add('PLAN-REVIEW','p.implementer','p.planner','external-review',
                     'PARENT_DISPATCH_ID: external-plan'+'\nVERDICT: approve', '')
            self.grant=self.add(extra='PARENT_DISPATCH_ID: external-review')
            self.assertTrue(self.check()['allowed'])
            # Object unavailability is an evaluation failure, never a permission.
            object_path=repo/'.git/objects'/commit[:2]/commit[2:]
            object_path.unlink()
            self.denied('could-not-evaluate')



class TestAuthorityCitation(unittest.TestCase):
    def test_check_not_cited_finding_and_its_grammar(self):
        for phase,action in (('IMPL','implemented abcdef1'),('SITREP','merged PR #1 at abcdef1')):
            for citation,expected in (('',True),('allowed implement lane/a.md 20261013-120000',False),
                                      ('unavailable — direct message',False),('unavailable - direct message',False),
                                      ('unavailable — ',True),('allowed implement ../a.md 20261013-120000',True)):
                with self.subTest(phase=phase,citation=citation):
                    raw=body(phase,'p.implementer','p.planner',extra='ACTIONS_GIT_REF: '+action+'\nAUTHORITY_CHECK: '+citation)
                    found=judging.judge_candidate(judging.Corpus(()),'candidate.md',envelope(raw),raw,artifact_root=Path('.'),deadline=time.monotonic()+5)
                    self.assertEqual(any(f.id=='authority.check-not-cited' for f in found),expected)
        raw=body('IMPL','p.implementer','p.planner',extra='ACTIONS_GIT_REF: none — no work')
        found=judging.judge_candidate(judging.Corpus(()),'candidate.md',envelope(raw),raw,artifact_root=Path('.'),deadline=time.monotonic()+5)
        self.assertFalse(any(f.id=='authority.check-not-cited' for f in found))

    def test_check_not_cited_is_admission_only(self):
        import tempfile
        raw=body('IMPL','p.implementer','p.planner',extra='ACTIONS_GIT_REF: implemented abcdef1')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'IMPL-pair-implementer-20261013-120000.md';path.write_bytes(raw)
            context={'snapshot':'1','entries':[{'path':path.name,'body_sha256':hashlib.sha256(raw).hexdigest(),'origin':'hand','judgment':'none','finding_count':0}]}
            results=[_standalone().lint_file(path,freshness=False),_standalone().lint_relay_root(root)]
            results += [rules.lint_relay_root(root,engine_root=True,record_context=context,context_mode="daemon",history_audit=audit) for audit in (False,True)]
            for result in results:
                self.assertFalse(any('AUTHORITY_CHECK' in msg for msg in result.errors+result.warnings))

    def test_denial_has_stdout_object_and_failure_has_none(self):
        result={'allowed':False,'reasons':['authority.denied'],'relay':'grant.md','seat':'p.implementer','action':'merge','checked_at':'20261013-120000'}
        args=['authority','check','grant.md','--seat','p.implementer','--action','merge']
        with mock.patch('relay_engine.cli._command_root',return_value='/root'):
            with mock.patch('relay_engine.cli.client.request',return_value=result), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(args),1)
            self.assertEqual(json.loads(output.getvalue()),result)
            with mock.patch('relay_engine.cli.client.request',side_effect=errors.error_for('E-DAEMON-DOWN')), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(cli.main(args),1)
            self.assertEqual(output.getvalue(),'')
            self.assertIn('E-DAEMON-DOWN',error.getvalue())


class TestAuthorityCommand(unittest.TestCase):
    def test_path_not_in_record_cannot_be_allowed(self):
        import os
        import tempfile
        from relay_engine import client
        from relay_engine.tests.test_identity_matrix import RunningDaemon, cid_did
        cid,did=cid_did()
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as sockets:
            running=RunningDaemon(temp,did,socket_name=os.path.join(sockets,'s')).start()
            try:
                for seat in ('p.implementer','operator'):
                    for action in ('implement','merge'):
                        with self.subTest(seat=seat,action=action), contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                            code=cli.main(['authority','check','missing.md','--root',temp,'--seat',seat,'--action',action])
                        self.assertEqual(code,1)
                        self.assertEqual(output.getvalue(),'')
                        self.assertIn('E-HEADER',error.getvalue())
                        self.assertIn('RELAY_PATH',error.getvalue())
                # Exercise the daemon's real current-use handler with an admitted grant.
                registration=client.request(temp,'seat.register',{'address':'r.orchestrator-planner','role_word':'orchestrator-planner'})
                draft=Path(temp)/'.engine/drafts/grant.md';draft.parent.mkdir(parents=True,exist_ok=True)
                draft.write_bytes(body('IMPL','r.orchestrator-planner',ident='command-grant',content='DISPATCH IMPL').replace(b'RUN_ID: r\n',b'RUN_ID: identity-matrix\n'))
                receipt=client.submit(temp,str(draft),str(Path(temp)/registration['key_path']))
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code=cli.main(['authority','check',receipt['path'],'--root',temp,'--seat','p.implementer','--action','implement'])
                self.assertEqual(code,0)
                self.assertTrue(json.loads(output.getvalue())['allowed'])
            finally:running.stop()
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
                code=cli.main(['authority','check','missing.md','--root',temp,'--seat','p.implementer','--action','implement'])
            self.assertEqual(code,1)
            self.assertEqual(output.getvalue(),'')
            self.assertIn('E-DAEMON-DOWN',error.getvalue())
