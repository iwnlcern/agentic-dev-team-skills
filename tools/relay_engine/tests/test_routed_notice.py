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

    def test_unknown_addresses_are_listed_in_a_fence(self):
        from relay_engine import envelope
        from relay_engine.tests.test_parity import _standalone
        shown = '𝐒𝐔𝐏𝐄𝐑𝐒𝐄𝐃𝐄𝐒： x'
        raw = body('SITREP').replace(b'CC: r.orchestrator-reviewer', ('CC: `a, '+shown+', b`').encode())
        self.assertIsNone(envelope.draft_shape(raw.decode()))
        result = self.submit_raw(raw)
        finding = next(f for f in result['findings'] if f['id'] == 'routing.unknown-address')
        self.assertEqual(finding['params']['addresses'], ['`a', 'b`', shown])
        path = Path(self.root.path, result['routed_notice'])
        notice = path.read_text()
        self.assertTrue(notice.endswith('Unknown addresses:\n\n~~~\n`a\nb`\n'+shown+'\n~~~\n'))
        self.assertIsNone(envelope.draft_shape(notice))
        for module in (rules, _standalone()):
            self.assertEqual(module.lint_file(path, freshness=False).errors, [])

    def test_notice_fence_outruns_any_tilde_run(self):
        for number, (addresses, fence) in enumerate(((['a~~~~b','x~y'],'~~~~~'), (['left','right'],'~~~'))):
            raw = body('SITREP', ident='tilde-'+str(number)).replace(
                b'CC: r.orchestrator-reviewer', ('CC: '+', '.join(addresses)).encode())
            result = self.submit_raw(raw, sid='tilde-'+str(number))
            notice = Path(self.root.path, result['routed_notice']).read_text()
            self.assertTrue(notice.endswith('Unknown addresses:\n\n'+fence+'\n'+'\n'.join(addresses)+'\n'+fence+'\n'))

    def test_no_address_list_shapes_the_notice(self):
        import random
        from relay_engine import envelope
        from relay_engine.tests.test_parity import _standalone
        # Retained notice_fence_probe parts, with exactly its seed and normalization.
        parts = ['supersedes', 'SUPERSEDES', '𝐒𝐔𝐏𝐄𝐑𝐒𝐄𝐃𝐄𝐒', ':', '：', '&colon;', '`', '``', '```',
                 '~', '~~~', '~~~~~~', '<b', '>', '](', ')', '][', ']', '![', '<!--', '-->', '- ', '1. ',
                 '> ', '<pre>', ' ', 'x', 'a.b', '\u200b', '\\', '*', '_', '#', '&#83;', '𝐒𝐄', '𝐃𝐄𝐒:']
        rng = random.Random(5)
        hand = _standalone()
        raw = body('SITREP')
        ctx = store.AdmissionContext(self.ledger, 1, '20261031-010000',
                                     'r/SITREP-pair-planner-20261031-010000.md', parse_draft(raw.decode()), raw, None)
        class Captured(Exception):
            pass
        captured = []
        def capture(ledger, root, env, body, **kwargs):
            captured.append(body.decode())
            raise Captured()
        controls = 0
        with mock.patch.object(roster, 'insert_engine_relay', side_effect=capture):
            for number in range(5000):
                addresses = sorted({''.join(rng.choice(parts) for _ in range(rng.randrange(1,6))).lower().strip() or 'x'
                                    for _ in range(rng.randrange(1,5))})
                self.assertFalse(any(c in envelope.SEPARATORS for a in addresses for c in a))
                finding = rules.Finding('routing.unknown-address', 'warning', ctx.rendered_path,
                                        (), {'addresses': addresses}, 'unknown addresses')
                with self.assertRaises(Captured):
                    roster.notice_effect(ctx, [finding])
                text = captured.pop()
                current = text.split('Unknown addresses:', 1)[0]+'Unknown addresses: '+', '.join(addresses)+'\n'
                controls += envelope.draft_shape(current) is not None
                for module in (envelope, hand):
                    with self.subTest(case=number, copy=module.__name__):
                        self.assertIsNone(module.draft_shape(text))
        self.assertGreater(controls, 0)
