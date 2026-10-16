"""One atomic and explicitly linked notice for each unknown-address submission."""
import importlib.util
from pathlib import Path
import subprocess
import time
from unittest import mock

from relay_engine import daemon, judging, ledger as store, reconcile, roster, rules, seats
from relay_engine.envelope import parse_draft
from relay_engine.tests.test_judging import body
from relay_engine.tests.test_roster import RosterFixture


class TestRoutedNotice(RosterFixture):
    def unknown(self):
        return body('SITREP', target='missing.planner', extra='')

    def row(self,path):
        return self.ledger.execute('SELECT seq,stamp,body FROM relays WHERE rendered_path=?',(path,)).fetchone()

    def test_unknown_address_admitted_with_finding_and_notice(self):
        result=self.submit_raw(self.unknown())
        finding=next(f for f in result['findings'] if f['id']=='routing.unknown-address')
        self.assertEqual(finding['params']['addresses'],['missing.planner'])
        self.assertEqual(finding['params']['notice'],'filed')
        notice=Path(self.root.path,result['routed_notice']).read_text()
        self.assertIn('TO: p.planner\n',notice);self.assertIn('IN_REPLY_TO: '+result['path'],notice)
        self.assertIn('not an instruction',notice)
        roster.declare(self.ledger,self.root,[('missing.planner','planner')],self.tag)
        self.assertEqual(self.submit_raw(body('SITREP',target='missing.planner',ident='declared'),'declared')['findings'],[])
        seats.register(self.ledger,self.root,'registered.planner','Planner')
        self.assertEqual(self.submit_raw(body('SITREP',target='registered.planner',ident='registered'),'registered')['findings'],[])

    def test_unknown_address_deduplicated_across_fields(self):
        raw=self.unknown().replace(b'CC: r.orchestrator-reviewer',b'CC: missing.planner, another.planner')
        result=self.submit_raw(raw)
        unknown=[f for f in result['findings'] if f['id']=='routing.unknown-address']
        self.assertEqual(len(unknown),1)
        self.assertEqual(unknown[0]['params']['addresses'],['another.planner','missing.planner'])
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM routed_notices').fetchone()[0],1)

    def test_no_top_seat_gives_finding_with_notice_none(self):
        self.ledger.execute("DELETE FROM meta WHERE key='top_seat'")
        result=self.submit_raw(self.unknown())
        self.assertNotIn('routed_notice',result)
        self.assertEqual(result['findings'][0]['params']['notice'],'none-no-top-seat')
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM routed_notices').fetchone()[0],0)

    def test_replay_returns_same_notice_and_makes_no_second(self):
        first=self.submit_raw(self.unknown());second=self.submit_raw(self.unknown())
        self.assertEqual(first['routed_notice'],second['routed_notice'])
        self.assertTrue(second['duplicate'])
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM relays').fetchone()[0],2)

    def test_failure_after_notice_insert_before_commit_leaves_nothing(self):
        real=store.admit
        def failing(*args,**kwargs):return real(*args,**kwargs,fault='after-effects')
        with mock.patch.object(daemon,'admit',side_effect=failing),self.assertRaises(store.InjectedFault):
            self.submit_raw(self.unknown())
        for table in ('relays','admission_judgments','admission_findings','routed_notices'):
            self.assertEqual(self.ledger.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)

    def test_crash_between_commit_and_render_leaves_one_notice(self):
        real=store.admit
        def failing(*args,**kwargs):return real(*args,**kwargs,fault='post-commit-pre-response')
        with mock.patch.object(daemon,'admit',side_effect=failing),self.assertRaises(store.InjectedFault):
            self.submit_raw(self.unknown())
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM relays').fetchone()[0],2)
        reconcile.render_all(self.ledger,self.root)
        replay=self.submit_raw(self.unknown())
        self.assertTrue(replay['duplicate']);self.assertTrue(Path(self.root.path,replay['routed_notice']).is_file())
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM routed_notices').fetchone()[0],1)

    def test_notice_takes_the_next_allocated_stamp(self):
        result=self.submit_raw(self.unknown());original=self.row(result['path']);notice=self.row(result['routed_notice'])
        self.assertEqual(store._parse_stamp(notice[1])-store._parse_stamp(original[1]),store.timedelta(seconds=1))
        self.assertEqual(notice[0],original[0]+1)

    def test_notice_row_is_exempt_and_linked(self):
        result=self.submit_raw(self.unknown());original=self.row(result['path']);notice=self.row(result['routed_notice'])
        self.assertEqual(store.judgment_for(self.ledger,notice[0]),('exempt',()))
        self.assertEqual(list(self.ledger.execute('SELECT relay_seq,notice_seq FROM routed_notices')),[(original[0],notice[0])])
        with self.assertRaises(ValueError):
            raw=self.unknown();store.insert_engine_relay(self.ledger,self.root,parse_draft(raw.decode()),raw,clock=time.time)

    def test_seat_named_like_notice_is_ordinary(self):
        # Use the base registration implementation against the upgraded tables.
        src=subprocess.check_output(['git','show','cbc8f07:tools/relay_engine/seats.py'],cwd=Path(__file__).parents[3]).decode()
        module=type(seats)('old_seats');exec(compile(src,'base-seats.py','exec'),module.__dict__)
        registration=module.register(self.ledger,self.root,'r-engine-notice.planner','Planner')
        tag=Path(self.root.path,registration['key_path']).read_text().strip()
        raw=body('SITREP','r-engine-notice.planner',ident='ordinary')
        result=self.submit_raw(raw,tag=tag)
        self.assertEqual(store.judgment_for(self.ledger,self.row(result['path'])[0])[0],'judged')
        self.assertEqual(self.ledger.execute('SELECT count(*) FROM routed_notices WHERE notice_seq=?',(self.row(result['path'])[0],)).fetchone()[0],0)
        code,out,err=self.command(['seat','declare','r-engine-notice.planner','--role','planner','--key',self.key])
        self.assertEqual(code,0);self.assertIn('caution',err.lower())

    def test_notice_envelope_lints_clean_in_both_linters(self):
        result=self.submit_raw(self.unknown());path=Path(self.root.path,result['routed_notice'])
        standalone=Path(__file__).parents[2]/'relay-lint.py'
        self.assertEqual(rules.lint_file(path,freshness=False).errors,[])
        check=subprocess.run(['python3',str(standalone),str(path),'--no-freshness'],capture_output=True,text=True)
        self.assertEqual(check.returncode,0,check.stdout+check.stderr)
