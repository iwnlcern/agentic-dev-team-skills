"""Pure migration mapping, exercised only on synthetic 2.9.7 copies."""
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import relay_engine.store_import as si
from relay_engine import addresses, identity_import, schema

LEGACY_DDL = r"""
CREATE TABLE relays(
  seq INTEGER PRIMARY KEY,
  submission_id TEXT, stamp TEXT NOT NULL, rendered_path TEXT UNIQUE NOT NULL,
  admits_against_seq INTEGER REFERENCES relays(seq),
  phase TEXT NOT NULL, role TEXT NOT NULL, dispatch_id TEXT NOT NULL,
  parent_dispatch_id TEXT, run_id TEXT, from_seat TEXT NOT NULL,
  to_seats TEXT, cc_seats TEXT, status TEXT, subject TEXT,
  headers_json TEXT NOT NULL, body BLOB NOT NULL,
  body_sha256 TEXT NOT NULL, content_hash TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('daemon','hand','adopted')),
  occupancy_ref INTEGER, advisories_json TEXT,
  UNIQUE(from_seat, submission_id));
CREATE UNIQUE INDEX ux_relays_stamp_daemon ON relays(stamp) WHERE origin='daemon';
CREATE TABLE seat_events(seq INTEGER PRIMARY KEY, address TEXT NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('declared','booted','occupied','replaced','offline','stood-down')),
  occupant_id TEXT, key_id TEXT,
  boot_relay_seq INTEGER REFERENCES relays(seq),
  cause_seq INTEGER, detail TEXT);
CREATE TABLE cycle_events(seq INTEGER PRIMARY KEY, dispatch_id TEXT NOT NULL,
  event TEXT NOT NULL CHECK(event IN ('open','close','reopen')),
  commissioning_seq INTEGER, cause_seq INTEGER);
CREATE TABLE supersession_edges(seq INTEGER PRIMARY KEY, target_seq INTEGER NOT NULL,
  ruling_seq INTEGER NOT NULL, source TEXT NOT NULL CHECK(source IN ('local','adopted')),
  applied INTEGER NOT NULL);
CREATE TABLE projection_events(seq INTEGER PRIMARY KEY, relay_seq INTEGER REFERENCES relays(seq),
  target TEXT NOT NULL CHECK(target IN ('relay','index','seats','foreign-path')),
  event TEXT NOT NULL CHECK(event IN ('rendered','missing','modified','conflict','divergence','failed','repaired')),
  digest TEXT, detail TEXT,
  CHECK((target='relay') = (relay_seq IS NOT NULL)));
CREATE TABLE migration_events(seq INTEGER PRIMARY KEY,
  event TEXT NOT NULL CHECK(event IN ('epoch-open','census','cutover','rollback')), digest TEXT, detail TEXT);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, commissioned_by TEXT NOT NULL);
CREATE TABLE commissions(child_run_id TEXT PRIMARY KEY,
  dispatch_seq INTEGER NOT NULL REFERENCES relays(seq),
  dispatch_content_digest TEXT NOT NULL, record_digest TEXT NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS admission_judgments(
  relay_seq INTEGER PRIMARY KEY REFERENCES relays(seq),
  status TEXT NOT NULL CHECK(status IN ('judged','exempt','failed')),
  rules_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admission_findings(
  relay_seq INTEGER NOT NULL REFERENCES admission_judgments(relay_seq),
  ord INTEGER NOT NULL, check_id TEXT NOT NULL,
  lint_severity TEXT NOT NULL CHECK(lint_severity IN ('error','warn')),
  operands_json TEXT NOT NULL, params_json TEXT NOT NULL, message TEXT NOT NULL,
  PRIMARY KEY(relay_seq,ord));
CREATE TABLE IF NOT EXISTS roster(
  address TEXT PRIMARY KEY, role TEXT NOT NULL, declared_by TEXT NOT NULL,
  declared_at_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS routed_notices(
  relay_seq INTEGER PRIMARY KEY REFERENCES relays(seq),
  notice_seq INTEGER NOT NULL UNIQUE REFERENCES relays(seq));
"""


class TestMapping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.number = 0

    def source(self, rows, *, run='m-3', top=None, commissions=(), canonical='/x/project/relays'):
        self.number += 1
        root = self.directory / str(self.number)
        root.mkdir()
        conn = sqlite3.connect(root / 'ledger.db')
        try:
            conn.executescript(LEGACY_DDL)
            conn.executemany('INSERT INTO meta VALUES (?,?)',
                [('root_uuid', f'00000000-0000-4000-8000-{self.number:012d}'), ('run_id', run)]
                + ([] if canonical is None else [('canonical_root', canonical)])
                + ([] if top is None else [('top_seat', top)]))
            teams = {run} | {p.split('.')[0] for r in rows for p in
                [r.get('sender', 'm-3.planner')] + r.get('to', []) + r.get('cc', [])
                if '.' in p and p.count('.') == 1 and '@' not in p}
            conn.executemany('INSERT INTO runs VALUES (?,?)', [(t, 'operator') for t in sorted(teams)])
            for n, row in enumerate(rows, 1):
                sender = row.get('sender', 'm-3.planner')
                phase = row.get('phase', 'SITREP')
                headers = {'FROM': sender, 'TO': ', '.join(row.get('to', [])),
                           'CC': ', '.join(row.get('cc', [])), **row.get('headers', {})}
                encoded = ('# synthetic\n\nPHASE: ' + phase + '\n' +
                    '\n'.join(k + ': ' + v for k, v in headers.items()) + '\n\n').encode()
                body = encoded + row.get('payload', b'fixture\n')
                relative = row.get('path', f'{run}/r{n}.md')
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(row.get('rendered', body))
                if 'mtime' in row:
                    os.utime(target, (row['mtime'], row['mtime']))
                conn.execute('INSERT INTO relays (seq,stamp,rendered_path,phase,role,dispatch_id,'
                    'run_id,from_seat,to_seats,cc_seats,status,subject,headers_json,body,'
                    'body_sha256,content_hash,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (n,row.get('stamp','20261115-120000'),relative,phase,'Pair Planner',f'd{n}',
                     run,sender,json.dumps(row.get('to',[])),json.dumps(row.get('cc',[])),
                     row.get('status'),row.get('subject',f'row {n}'),json.dumps(headers),body,
                     hashlib.sha256(body).hexdigest(),'0'*64,'hand'))
            conn.executemany('INSERT INTO commissions VALUES (?,?,?,?)', commissions)
            conn.execute("INSERT INTO seat_events (seq,address,event) VALUES (1,?,'occupied')", (run+'.planner',))
            conn.commit()
        finally:
            conn.close()
        self.write_manifest(root)
        before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        result = si.read_source(root)
        self.assertEqual(before, {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()})
        self.assertEqual(result.run_id, run)
        return result

    def test_created_at_unique_and_ordered(self):
        # Break: ranking by input order, repeating timestamps or accepting rank 1000.
        a = self.source([{'subject':'a1','payload':b'a1\n'}, {'subject':'a2','payload':b'a2\n'}])
        b = self.source([{'subject':'b1','payload':b'b1\n'}])
        plan = si.plan_relays([a,b])
        self.assertEqual([r.subject for r in plan.relays], ['a1','a2','b1'])
        times = [r.created_at for r in plan.relays]
        self.assertEqual(times, list(range(times[0],times[0]+3)))
        self.assertIsInstance(si.created_at_ms('20261115-120000',1000,None), si.Listed)
        self.assertIsInstance(si.created_at_ms('20261115-120000',-1,None), si.Listed)
        zone = ZoneInfo('America/Los_Angeles')
        standard = datetime(2026,11,1,1,10,tzinfo=zone,fold=1).timestamp()
        daylight = datetime(2026,11,1,1,50,tzinfo=zone,fold=0).timestamp()
        folded = self.source([
            {'subject':'standard','stamp':'20261101-011000','mtime':standard},
            {'subject':'daylight','stamp':'20261101-015000','mtime':daylight}])
        ordered = si.plan_relays([folded])
        self.assertEqual([r.subject for r in ordered.relays], ['daylight','standard'])
        self.assertEqual([r.created_at for r in ordered.relays],
                         [int(daylight*1000),int(standard*1000)])

    def test_repeated_hour(self):
        # Break: choosing one fold regardless of the rendered file's timestamp.
        local = datetime(2026,11,1,1,30,tzinfo=ZoneInfo('America/Los_Angeles'))
        first = local.timestamp()
        second = local.replace(fold=1).timestamp()
        self.assertEqual(si.created_at_ms('20261101-013000',0,first), int(first*1000))
        self.assertEqual(si.created_at_ms('20261101-013000',1,second), int(second*1000)+1)
        self.assertIsInstance(si.created_at_ms('20261101-013000',0,first+20), si.Listed)
        p=si.plan_relays([self.source([{'stamp':'20261101-013000','mtime':first+20}])])
        self.assertTrue(si.Report(p).unresolved())

    def test_boot_and_verdicts(self):
        # Break: retaining BOOT or accepting a verdict outside the kind's set.
        p=si.plan_relays([self.source([{'phase':'BOOT'},
            {'phase':'PLAN-REVIEW','headers':{'PLAN_REVIEW_VERDICT':'approve'}},
            {'phase':'PLAN-REVIEW','headers':{'PLAN_REVIEW_VERDICT':'merge-ready'}}])])
        self.assertEqual([r.kind for r in p.relays], ['SITREP','PLAN-REVIEW','PLAN-REVIEW'])
        self.assertEqual([r.verdict for r in p.relays], [None,'approve',None])
        self.assertEqual(p.relays[0].provenance[0].original_phase,'BOOT')
        self.assertEqual(p.relays[2].provenance[0].headers['PLAN_REVIEW_VERDICT'],'merge-ready')

    def test_duplicates_once(self):
        # Break: treating a cross-root copy as new work or losing its provenance.
        row={'path':'m-3/same.md'}
        p=si.plan_relays([self.source([row]),self.source([row])])
        self.assertEqual(len(p.relays),1)
        self.assertEqual(len(p.relays[0].provenance),2)
        self.assertEqual(si.Report(p).counts['deduplicated'],1)

    def test_body_must_equal_source(self):
        # Break: trusting a copied file that differs from the source record.
        p=si.plan_relays([self.source([{'rendered':b'changed'}])])
        self.assertEqual(p.relays,())
        self.assertTrue(si.Report(p).unresolved())
        self.assertIn('source bytes differ',si.Report(p).render())

    def test_parties_once(self):
        # Break: retaining duplicate TO/CC, a none party or merging namespaces.
        src=self.source([{'to':['m-3.planner','none'],'cc':['m-3.planner','s4-floor.implementer']}])
        p=si.plan_relays([src]);r=p.relays[0]
        self.assertEqual(r.to,('m-3.planner',))
        self.assertEqual(r.cc,('s4-floor.implementer',))
        self.assertGreater(len(p.listed),0)
        ids=si.plan_identities([src],{'m-3':'finished','s4-floor':'running'})
        self.assertEqual({x.name for x in ids.instances},{'m-3','s4-floor'})

    def test_alias_coalesces(self):
        # Break: importing aliases as separate seats or display-only senders.
        src=self.source([{'sender':'m-3.pair-planner'},{'sender':'m-3.planner'}])
        ids=si.plan_identities([src],{'m-3':'running'});plan=si.plan_relays([src])
        self.assertEqual([(s.team_instance_id,s.role) for s in ids.seats],[(ids.instances[0].id,'planner')])
        self.assertEqual([r.sender for r in plan.relays],['m-3.planner','m-3.planner'])
        self.assertEqual(plan.relays[0].provenance[0].headers['FROM'],'m-3.pair-planner')
        self.assertEqual(ids.aliases,{('m-3','pair-planner'):1})
        self.assertEqual(si.Report(plan,ids).unresolved(),[])

    def test_aliases_never_reach_identity_step(self):
        # Break: forwarding retired spelling into Platform's strict role column.
        src=self.source([{'sender':'m-3.pair-planner'}])
        ids=si.plan_identities([src],{'m-3':'running'})
        self.assertTrue(all(s.role in addresses.ROLE_WORDS for s in ids.seats))
        conn=sqlite3.connect(':memory:',isolation_level=None)
        try:
            schema.init_v3(conn);conn.execute('BEGIN')
            identity_import.import_identities_step(conn,project_id='00000000-0000-7000-8000-000000000001',
                project_name='moved',created_at=1,instances=ids.instances,seats=ids.seats,top_seat_id=ids.top_seat_id)
            conn.rollback();conn.execute('BEGIN')
            bad=replace(ids.seats[0],role='pair-planner')
            with self.assertRaises(identity_import.IdentityImportRefused) as caught:
                identity_import.import_identities_step(conn,project_id='00000000-0000-7000-8000-000000000001',
                    project_name='moved',created_at=1,instances=ids.instances,seats=[bad],top_seat_id=ids.top_seat_id)
            self.assertEqual((caught.exception.reason,caught.exception.item),('invalid_role','planner'))
            conn.rollback()
        finally:conn.close()

    def test_sender_must_be_seat_or_operator(self):
        # Break: coercing an unknown role into a live sender.
        src=self.source([{'sender':'m-3.captain'}])
        p=si.plan_relays([src]);ids=si.plan_identities([src],{'m-3':'running'})
        self.assertTrue(si.Report(p,ids).unresolved())
        self.assertFalse(ids.seats)
        self.assertIsNone(si.canonical_role('captain'))
        self.assertEqual(si.canonical_role('pair-implementer'),('implementer','pair-implementer'))

    def test_parent_spelling(self):
        # Break: always choosing the first spelling rather than first resolution.
        src=self.source([{'path':'m-3/a.md'}, {'path':'m-3/b.md'},
            {'headers':{'IN_REPLY_TO':'missing.md, m-3/b.md, m-3/a.md'}},
            {'headers':{'IN_REPLY_TO':'m-3/a.md, m-3/b.md, missing.md'}},
            {'headers':{'IN_REPLY_TO':'missing.md'}}])
        p=si.plan_relays([src]);self.assertEqual((p.relays[2].parent,p.relays[2].parent_spelling),(1,'m-3/b.md'))
        self.assertEqual((p.relays[3].parent,p.relays[3].parent_spelling),(0,'m-3/a.md'))
        self.assertEqual((p.relays[4].parent,p.relays[4].parent_spelling),(None,None))
        self.assertEqual(p.relays[2].provenance[0].headers['IN_REPLY_TO'],'missing.md, m-3/b.md, m-3/a.md')
        self.assertEqual(si.Report(p).unresolved(), [])

    def test_answers_entries(self):
        # Break S2-M17: accepting a reply to a relay the sender never received.
        src=self.source([{'path':'m-3/parent.md'},
            {'path':'m-3/cc.md','cc':['m-3.planner']},
            {'path':'m-3/not.md','to':['m-3.implementer']},
            {'headers':{'IN_REPLY_TO':'m-3/parent.md, m-3/cc.md, m-3/parent.md, m-3/not.md, missing.md'}},
            {'path':'m-3/to.md','to':['m-3.planner']},
            {'headers':{'IN_REPLY_TO':'m-3/parent.md, m-3/to.md'}}])
        p=si.plan_relays([src])
        self.assertEqual(p.relays[3].answers,(1,))
        self.assertEqual(si.Report(p).answers_listed(),[(3,'m-3/not.md','not-received'),(3,'missing.md','unresolved')])
        self.assertEqual(p.relays[5].answers,(4,))
        self.assertEqual(si.Report(p).unresolved(),[])

    def test_foreign_recipient_display_only(self):
        # Break: making a foreign recipient a local seat or refusing its relay.
        src=self.source([{'to':['harness.master.orchestrator-planner']}])
        p=si.plan_relays([src]);ids=si.plan_identities([src],{'m-3':'running'})
        self.assertEqual(p.relays[0].to,())
        self.assertEqual(p.relays[0].provenance[0].foreign,('harness.master.orchestrator-planner',))
        self.assertEqual({i.name for i in ids.instances},{'m-3'})
        self.assertEqual(si.Report(p,ids).unresolved(),[])

    def test_identities_and_top_seat(self):
        # Break: selecting our own top seat or ignoring running/finished choices.
        src=self.source([{'sender':'master.master-planner'}, {'sender':'m-3.planner'}],run='master')
        ids=si.plan_identities([src],{'m-3':'finished'})
        self.assertEqual(ids.top_seat_id,next(s.id for s in ids.seats if s.role=='master-planner'))
        self.assertEqual(next(i for i in ids.instances if i.name=='m-3').closed_at,
                         next(i for i in ids.instances if i.name=='m-3').opened_at)
        pair=self.source([{}]);only=si.plan_identities([pair],{'m-3':'running'})
        self.assertEqual(only.top_seat_id,identity_import.derive_top_seat(only.instances,only.seats))
        closed=si.plan_identities([pair],{'m-3':'finished'});self.assertIsNone(closed.top_seat_id)
        two=self.source([{'sender':'a.planner'},{'sender':'b.planner'}],run='a')
        self.assertTrue(si.Report(si.plan_relays([two]),si.plan_identities([two],{'a':'running','b':'running'})).unresolved())
        mismatch=self.source([{}],top='m-3.implementer')
        self.assertTrue(si.Report(si.plan_relays([mismatch]),si.plan_identities([mismatch],{'m-3':'running'})).unresolved())

    def test_report_is_computed(self):
        # Break: returning fixed success or dropping source audit records.
        p=si.plan_relays([self.source([{}, {'rendered':b'bad'}])]);report=si.Report(p,store_id='s',project_id='p')
        self.assertEqual(report.counts['imported'],1)
        self.assertEqual(report.counts['refused'],1)
        self.assertTrue(report.unresolved())
        rendered=report.render();self.assertIn('1 imported',rendered);self.assertIn('source bytes differ',rendered)
        self.assertIn('occupied',rendered);self.assertIn('s',rendered);self.assertIn('p',rendered)
        with self.assertRaises(ValueError):si.read_source(self.directory/'absent')

    def test_identity_ids_include_source_roots(self):
        # Break: ids collide for equal namespaces/times in different projects.
        source = self.source([{}])
        other = replace(source, root_uuid='00000000-0000-4000-8000-ffffffffffff')
        first = si.plan_identities([source], {'m-3':'running'})
        repeated = si.plan_identities([source], {'m-3':'running'})
        different = si.plan_identities([other], {'m-3':'running'})
        self.assertNotEqual(first.instances[0].id, different.instances[0].id)
        self.assertNotEqual(first.seats[0].id, different.seats[0].id)
        self.assertEqual(first.instances, repeated.instances)
        self.assertEqual(first.seats, repeated.seats)

    def test_blocking_and_shown(self):
        # Break: making documentary links block, or hiding structural refusals.
        src = self.source([
            {'headers':{'IN_REPLY_TO':'missing-parent.md'}},
            {'path':'m-3/not-received.md','to':['m-3.implementer']},
            {'headers':{'IN_REPLY_TO':'m-3/r1.md, m-3/not-received.md, missing-answer.md'}}])
        plan = si.plan_relays([src]); report = si.Report(plan)
        self.assertTrue(report.shown())
        self.assertEqual(report.unresolved(), [])
        self.assertEqual(report.answers_listed(),
                         [(0,'missing-parent.md','unresolved'),
                          (2,'m-3/not-received.md','not-received'),
                          (2,'missing-answer.md','unresolved')])
        for rows in ([{'rendered':b'changed'}], [{'sender':'m-3.qa'}],
                     [{'stamp':'20261101-013000','mtime':0}],
                     [{'payload':str(n).encode()} for n in range(1001)]):
            with self.subTest(rows=len(rows)):
                self.assertTrue(si.Report(si.plan_relays([self.source(rows)])).unresolved())
        undecided = self.source([{}])
        identities = si.plan_identities([undecided], {})
        self.assertTrue(si.Report(si.plan_relays([undecided]), identities).unresolved())
        two = self.source([{'sender':'a.planner'},{'sender':'b.planner'}],run='a')
        identities = si.plan_identities([two], {'a':'running','b':'running'})
        self.assertTrue(si.Report(si.plan_relays([two]), identities).unresolved())

    def test_cross_root_resolution(self):
        # Break: last root wins a path, or ambiguous candidates become parents.
        a = self.source([{'path':'m-3/shared.md','payload':b'root one'},
                         {'stamp':'20261115-120001','headers':{'IN_REPLY_TO':'m-3/shared.md'}}])
        b = self.source([{'path':'m-3/shared.md','payload':b'root two'},
                         {'stamp':'20261115-120001','payload':b'reply two',
                          'headers':{'IN_REPLY_TO':'m-3/shared.md'}}])
        c = self.source([{'stamp':'20261115-120001','payload':b'reply three',
                          'headers':{'IN_REPLY_TO':'m-3/shared.md'}}])
        p = si.plan_relays([a,b,c])
        self.assertEqual(p.relays[2].parent, 0)
        self.assertEqual(p.relays[3].parent, 1)
        self.assertIsNone(p.relays[4].parent)
        report = si.Report(p)
        self.assertIn((4,'m-3/shared.md','ambiguous'), report.answers_listed())
        self.assertEqual(report.unresolved(), [])
        self.assertTrue(any(a.root_uuid in item and b.root_uuid in item
                            for item in report.shown()))
        # One distinct cross-root document resolves, and equal copies dedupe.
        unique = si.plan_relays([a,c])
        self.assertEqual(unique.relays[-1].parent, 0)
        same = self.source([{'path':'m-3/shared.md','payload':b'root one'}])
        dedup = si.plan_relays([a,same,c])
        self.assertEqual(dedup.relays[-1].parent, 0)
        self.assertEqual(dedup.deduplicated, 1)

    def test_level_from_roles(self):
        # Break: refusing mixed-level historical seats instead of using highest.
        mixed = self.source([{'sender':'master.master-planner'},
                             {'sender':'master.orchestrator-planner'}],run='master')
        ids = si.plan_identities([mixed], {})
        self.assertEqual([(i.name,i.level) for i in ids.instances], [('master','master')])
        self.assertEqual(si.Report(si.plan_relays([mixed]),ids).unresolved(), [])
        pair = self.source([{'sender':'team.orchestrator-planner'},
                            {'sender':'team.planner'}],run='team',top='team.orchestrator-planner')
        ids = si.plan_identities([pair], {'team':'running'})
        self.assertEqual([(i.name,i.level) for i in ids.instances], [('team','orchestrator')])
        self.assertEqual(ids.top_seat_id, next(s.id for s in ids.seats if s.role=='orchestrator-planner'))
        self.assertEqual(si.Report(si.plan_relays([pair]),ids).unresolved(), [])
        empty = self.source([],run='empty')
        ids = si.plan_identities([empty], {'empty':'finished'})
        self.assertTrue(any('team level unresolved: empty' in item
                            for item in si.Report(si.plan_relays([empty]),ids).unresolved()))

    def test_display_only_forms_no_instance(self):
        # Break S2-M21: turning a display token's namespace into identity evidence.
        src = self.source([{'to':['harness.master.orchestrator-planner']}])
        ids = si.plan_identities([src], {'m-3':'running'})
        self.assertNotIn('harness', {i.name for i in ids.instances})
        self.assertEqual(si.Report(si.plan_relays([src]),ids).unresolved(), [])

    def test_legacy_envelope_shapes(self):
        # Break: requiring a blank separator or losing its original bytes.
        src = self.source([{}]); row = src.relays[0]
        prefix, body = row['body'].split(b'\n\n', 2)[1:]
        header = b'# synthetic\n\n' + prefix + b'\n'
        documents = [(header+body,header,body), (header,header,b''),
                     (header+b'\n'+body,header+b'\n',body)]
        documents.append(tuple(part.replace(b'\n',b'\r\n') for part in documents[0]))
        documents.append(tuple(part.replace(b'\n',b'\r') for part in documents[0]))
        for document, expected_header, expected_body in documents:
            with self.subTest(document=document):
                edited = dict(row,body=document,rendered_bytes=document,
                              body_sha256=hashlib.sha256(document).hexdigest())
                p = si.plan_relays([replace(src,relays=(edited,))])
                self.assertEqual(p.refused,0,si.Report(p).render())
                self.assertEqual(p.relays[0].header_bytes,expected_header)
                self.assertEqual(p.relays[0].body,expected_body)
                self.assertEqual(p.relays[0].header_bytes+p.relays[0].body,document)

    def test_read_source_wal_copy(self):
        # Break S2-M20: opening the copy, changing sidecars or dropping its WAL.
        src = self.source([{'subject':'WAL witness'}])
        conn = sqlite3.connect(src.copy_dir/'ledger.db')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.row_factory = sqlite3.Row
        witness = dict(conn.execute('SELECT * FROM relays').fetchone())
        conn.execute('DELETE FROM relays'); conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        columns = ','.join(witness)
        conn.execute('INSERT INTO relays ('+columns+') VALUES ('+
                     ','.join('?' for _ in witness)+')',tuple(witness.values()))
        conn.commit()
        for include_shm in (False,True):
            with self.subTest(include_shm=include_shm):
                root = self.directory/('wal-copy-'+str(include_shm)); root.mkdir()
                for path in src.copy_dir.rglob('*'):
                    if not path.is_file() or (not include_shm and path.name=='ledger.db-shm'):
                        continue
                    target = root/path.relative_to(src.copy_dir)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(path,target)
                def snapshot():
                    return {str(p.relative_to(root)):(p.stat().st_size,hashlib.sha256(p.read_bytes()).hexdigest())
                            for p in root.rglob('*') if p.is_file()}
                self.write_manifest(root)
                before = snapshot()
                result = si.read_source(root)
                self.assertEqual([r['subject'] for r in result.relays], ['WAL witness'])
                self.assertEqual(snapshot(),before)

    def test_display_only_count(self):
        # Break: counting foreign only, counting duplicate spellings or none.
        foreign = 'harness.master.orchestrator-planner'
        src = self.source([{'to':[foreign,'m-3.ops','operator','none'],
                            'cc':[foreign,'m-3.qa']}])
        report = si.Report(si.plan_relays([src]))
        self.assertEqual(report.counts['display_only'],3)

    def write_manifest(self, root):
        # Test utility for the future Task5 copy producer, using copied bytes.
        entries = []
        for path in root.rglob('*'):
            if not path.is_file() or path == root/'.source-manifest.json':
                continue
            relative = path.relative_to(root).as_posix()
            if relative in ('ledger.db','ledger.db-wal','ledger.db-shm'):
                relative = '.engine/' + relative
            data = path.read_bytes()
            entries.append({'path':relative,'size':len(data),
                            'sha256':hashlib.sha256(data).hexdigest()})
        raw = json.dumps({'files':sorted(entries,key=lambda e:e['path'])},
                         sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        (root/'.source-manifest.json').write_bytes(raw)

    def test_dedup_reply_resolution(self):
        # Break S2-M22: using only the first root holding a duplicate reply.
        p = {'path':'p.md','payload':b'P','subject':'P','to':['m-3.planner']}
        q = dict(p,payload=b'Q',subject='Q')
        reply = {'path':'reply.md','stamp':'20261115-120001',
                 'subject':'reply','headers':{'IN_REPLY_TO':'p.md'}}
        a = self.source([p,reply]); b = self.source([q,reply])
        plan = si.plan_relays([a,b])
        self.assertIsNone(plan.relays[2].parent)
        self.assertIn((2,'p.md','ambiguous'),si.Report(plan).answers_listed())
        self.assertTrue(any(a.root_uuid in x and b.root_uuid in x for x in si.Report(plan).shown()))
        a = self.source([p,reply]); b = self.source([reply]); c = self.source([q])
        plan = si.plan_relays([a,b,c])
        self.assertIsNone(plan.relays[2].parent)
        self.assertIn((2,'p.md','ambiguous'),si.Report(plan).answers_listed())
        self.assertTrue(any(a.root_uuid in x and c.root_uuid in x for x in si.Report(plan).shown()))
        common = {'path':'common.md','subject':'common','payload':b'common'}
        reply2 = dict(reply,headers={'IN_REPLY_TO':'common.md, p.md'})
        plan = si.plan_relays([self.source([common,p,reply2]),self.source([common,q,reply2])])
        self.assertEqual(plan.relays[3].parent,0)
        self.assertEqual(plan.relays[3].answers,())
        self.assertIn((3,'p.md','ambiguous'),si.Report(plan).answers_listed())
        plan = si.plan_relays([self.source([p,reply]),self.source([p,reply])])
        self.assertEqual(plan.relays[1].parent,0)
        self.assertEqual(len(plan.relays),2)

    def test_source_manifest_checked_and_reported(self):
        # Break S2-M24: consuming ledger bytes without checking their manifest.
        src = self.source([{'subject':'manifest witness'}])
        conn = sqlite3.connect(src.copy_dir/'ledger.db')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA wal_autocheckpoint=0')
            conn.execute("UPDATE meta SET value=value || '/new' WHERE key='canonical_root'")
            conn.commit()
            originals = {p.relative_to(src.copy_dir):p.read_bytes()
                         for p in src.copy_dir.rglob('*') if p.is_file()}
        finally:
            conn.close()
        def copied(name):
            root = self.directory/name; root.mkdir()
            for relative,data in originals.items():
                path = root/relative; path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(data)
            self.write_manifest(root)
            return root
        root = copied('manifest-wal')
        wal = root/'ledger.db-wal'; data = bytearray(wal.read_bytes()); data[-1] ^= 1
        wal.write_bytes(data)
        with self.assertRaisesRegex(ValueError,r'\.engine/ledger\.db-wal'):
            si.read_source(root)
        root = copied('manifest-shm')
        path = root/'.source-manifest.json'; manifest = json.loads(path.read_bytes())
        manifest['files'] = [e for e in manifest['files'] if e['path']!='.engine/ledger.db-shm']
        path.write_text(json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False))
        with self.assertRaisesRegex(ValueError,r'\.engine/ledger\.db-shm'):
            si.read_source(root)
        root = copied('manifest-rendered')
        (root/'m-3/r1.md').write_bytes(b'changed rendered document')
        with self.assertRaisesRegex(ValueError,r'm-3/r1\.md'):
            si.read_source(root)
        root = copied('manifest-intact'); raw = (root/'.source-manifest.json').read_bytes()
        expected = {e['path']:(e['size'],e['sha256']) for e in json.loads(raw)['files']}
        result = si.read_source(root)
        self.assertEqual(result.manifest_sha256,hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.manifest,expected)
        for name in ('ledger.db','ledger.db-wal','ledger.db-shm'):
            data = (root/name).read_bytes()
            self.assertEqual(result.manifest['.engine/'+name],(len(data),hashlib.sha256(data).hexdigest()))
        self.assertIn(f'source {result.root_uuid}: manifest sha256 {hashlib.sha256(raw).hexdigest()}, {len(expected)} files',
                      si.Report(si.plan_relays([result])).render().splitlines())

    def test_rank_after_dedupe(self):
        # Break S2-M25: duplicate occurrences consume the second's 1000 ranks.
        rows = [{'path':f'lane/{n}.md','subject':str(n),'payload':str(n).encode()}
                for n in range(600)]
        plan = si.plan_relays([self.source(rows),self.source(rows)])
        self.assertEqual(plan.refused,0)
        self.assertEqual(len(plan.relays),600)
        self.assertEqual(plan.deduplicated,600)
        times = [r.created_at for r in plan.relays]
        start = int(datetime(2026,11,15,12,tzinfo=ZoneInfo('America/Los_Angeles')).timestamp()*1000)
        self.assertEqual(times,list(range(start,start+600)))
        self.assertEqual([r.subject for r in plan.relays],[str(n) for n in range(600)])

        entries = [{'path': f'rank/{n}.md', 'subject': str(n)} for n in range(1001)]
        with self.subTest(case='P1-capacity'):
            target = entries[999]
            first = self.source([*entries[:999], dict(target, rendered=b'refused fixture')])
            eligible = self.source([target])
            plan = si.plan_relays([first, eligible])
            self.assertEqual(len(plan.relays), 1000)
            self.assertEqual([row.created_at for row in plan.relays], list(range(start, start + 1000)))
            self.assertEqual([row.subject for row in plan.relays].count('999'), 1)
            self.assertEqual(plan.relays[999].subject, '999')
            provenance = plan.relays[999].provenance
            self.assertEqual([(point.root_uuid, point.seq) for point in provenance],
                             [(eligible.root_uuid, eligible.relays[0]['seq'])])
            self.assertEqual(plan.refused, 1)
            self.assertEqual(si.Report(plan).unresolved(), ['source bytes differ: rank/999.md'])

        with self.subTest(case='P1-overflow'):
            plan = si.plan_relays([self.source(entries)])
            self.assertEqual([row.subject for row in plan.relays], [str(n) for n in range(1000)])
            self.assertEqual([row.created_at for row in plan.relays], list(range(start, start + 1000)))
            self.assertEqual(plan.refused, 1)
            self.assertEqual(si.Report(plan).unresolved(), ['rank outside 0..999: 1000'])

        gap_stamp = '20260308-023000'
        gap_finding = f'wall time does not exist: {gap_stamp}'
        with self.subTest(case='P2-gap-equal-copy'):
            target = {'path': 'gap-target.md', 'subject': 'gap target'}
            gap = self.source([dict(target, stamp=gap_stamp)])
            valid_stamp = '20260308-033000'
            peers = self.source([dict(row, stamp=valid_stamp) for row in entries[:999]])
            valid = self.source([dict(target, stamp=valid_stamp)])
            plan = si.plan_relays([gap, peers, valid])
            survivor = next(row for row in plan.relays if row.subject == 'gap target')
            self.assertEqual(survivor.created_at, 1772965800999)
            self.assertEqual([row.created_at for row in plan.relays],
                             list(range(1772965800000, 1772965801000)))
            self.assertIs(plan.relays[999], survivor)
            self.assertEqual([row.subject for row in plan.relays].count('gap target'), 1)
            self.assertEqual([(point.root_uuid, point.seq, point.stamp, point.offset)
                              for point in survivor.provenance],
                             [(valid.root_uuid, valid.relays[0]['seq'], valid_stamp, -25200)])
            self.assertEqual(plan.refused, 1)
            self.assertEqual(si.Report(plan).unresolved(), [gap_finding])

        with self.subTest(case='P2-epoch-capacity'):
            gap = self.source([{'path': 'gap-only.md', 'stamp': gap_stamp}])
            epoch = self.source([dict(row, stamp='19691231-160000') for row in entries[:1000]])
            plan = si.plan_relays([gap, epoch])
            self.assertEqual(len(plan.relays), 1000)
            self.assertEqual([row.created_at for row in plan.relays], list(range(1000)))
            self.assertEqual([row.subject for row in plan.relays], [str(n) for n in range(1000)])
            self.assertEqual(plan.refused, 1)
            self.assertEqual(si.Report(plan).unresolved(), [gap_finding])

        with self.subTest(case='P2-repeated-fallback'):
            stamp = '20261101-013000'
            fold = self.source([
                {'path': 'fallback.md', 'subject': 'fallback', 'stamp': stamp, 'mtime': 1793521820},
                {'path': 'daylight.md', 'subject': 'daylight', 'stamp': stamp, 'mtime': 1793521800},
            ])
            plan = si.plan_relays([fold])
            self.assertEqual([(row.subject, row.created_at) for row in plan.relays],
                             [('fallback', 1793521800000), ('daylight', 1793521800001)])
            self.assertEqual(si.Report(plan).unresolved(),
                             [f'repeated hour needs matching file mtime: {stamp}'])

    def test_spelling_ways(self):
        # Break S2-M23: trying shorter prefixes when the longest has no target.
        p = {'path':'lane/f.md','subject':'P','payload':b'P'}
        q = dict(p,subject='Q',payload=b'Q')
        def reply(entry, name='reply.md'):
            return {'path':name,'stamp':'20261115-120001','subject':name,
                    'headers':{'IN_REPLY_TO':entry}}
        canonical1 = '/x/pdc/master/relays'; canonical2 = '/x/bivpak/.relays/arm2'
        a = self.source([p],canonical=canonical1)
        b = self.source([q,reply('master/relays/lane/f.md')],canonical=canonical2)
        plan = si.plan_relays([a,b])
        self.assertEqual(plan.relays[2].parent,0)
        a = self.source([],canonical=canonical1)
        b = self.source([q,reply('master/relays/lane/f.md')],canonical=canonical2)
        plan = si.plan_relays([a,b])
        self.assertIsNone(plan.relays[1].parent)
        self.assertIn((1,'master/relays/lane/f.md','unresolved'),si.Report(plan).answers_listed())
        a = self.source([p],canonical='/a/x/relays')
        b = self.source([q,reply('x/relays/lane/f.md')],canonical='/b/x/relays')
        plan = si.plan_relays([a,b])
        self.assertIsNone(plan.relays[2].parent)
        self.assertIn((2,'x/relays/lane/f.md','ambiguous'),si.Report(plan).answers_listed())
        self.assertTrue(any(a.root_uuid in x and b.root_uuid in x for x in si.Report(plan).shown()))
        a = self.source([reply('master/relays/lane/x.md')],canonical='/repo/a/master/relays')
        b = self.source([dict(q,path='relays/lane/x.md')],canonical='/repo/b/master')
        plan = si.plan_relays([a,b])
        self.assertIsNone(plan.relays[1].parent)
        self.assertIn((1,'master/relays/lane/x.md','unresolved'),si.Report(plan).answers_listed())
        a = self.source([dict(p,path='arm2/DESIGN-y.md')],canonical=canonical1)
        b = self.source([dict(q,path='DESIGN-y.md'),reply('arm2/DESIGN-y.md')],canonical=canonical2)
        plan = si.plan_relays([a,b])
        self.assertEqual(plan.relays[2].parent,0)
        self.assertEqual(si.Report(plan).counts['spellings'],{('bare','resolved'):1})
        entries = ['../x/lane/f.md','lane/f.md (filed from the t1 worktree)',
                   'harness/master/relays2/lane/f.md','master/relays/lane/f.md (filed from the t1 worktree)']
        a = self.source([p],canonical=canonical1)
        b = self.source([q,reply('master/relays/lane/f.md'),reply(', '.join(entries),'annotated.md')],canonical=canonical2)
        plan = si.plan_relays([a,b]); report = si.Report(plan)
        self.assertIsNone(plan.relays[3].parent)
        self.assertEqual(report.answers_listed(),[(3,e,'unresolved') for e in entries])
        self.assertEqual(report.counts['spellings'],{('prefixed','resolved'):1,('prefixed','unresolved'):1,('neither','unresolved'):3})
        self.assertIn('spellings: neither/unresolved=3, prefixed/resolved=1, prefixed/unresolved=1',report.render().splitlines())


    def test_source_manifest_states(self):
        # T-A: copied bytes and every listed path are proved before SQLite.
        from unittest.mock import patch
        cases = ['A1','A2','A3','A4','A5','A6','A7-db','A7-side',
                 'A7-rendered','A8','A8-shm','A9-folder','A9-link','A10-missing',
                 'A10-not-json','A10-shape','A10-extra-key',
                 'A10-not-canonical','A11','A12','A12-shape','A13-order',
                 'A14-inventory-unique','A15-inventory-complete']
        for case in cases:
            with self.subTest(case=case):
                src = self.source([{'path':'p.md','subject':'checkpointed'}])
                root = src.copy_dir
                path = root/'p.md'
                manifest_file = root/'.source-manifest.json'
                expected = 'p.md'
                if case == 'A1':
                    self.assertEqual(si.read_source(root).relays[0]['subject'],'checkpointed')
                    continue
                if case == 'A2':
                    path.write_bytes(path.read_bytes()+b'x')
                elif case == 'A3':
                    data = bytearray(path.read_bytes()); data[-1] ^= 1
                    path.write_bytes(data)
                elif case == 'A4':
                    # Keep a valid older relay checkpointed in the main file.
                    # The later committed subject exists only in the WAL copy.
                    conn = sqlite3.connect(root/'ledger.db')
                    try:
                        conn.execute('PRAGMA journal_mode=WAL')
                        conn.execute('PRAGMA wal_autocheckpoint=0')
                        conn.execute("UPDATE relays SET subject='WAL witness'")
                        conn.commit()
                        snapshot = {p.relative_to(root):p.read_bytes()
                                    for p in root.rglob('*') if p.is_file()}
                    finally:
                        conn.close()
                    copied = self.directory/'missing-wal'; copied.mkdir()
                    for relative,data in snapshot.items():
                        target = copied/relative
                        target.parent.mkdir(parents=True,exist_ok=True)
                        target.write_bytes(data)
                    root = copied
                    self.write_manifest(root)
                    (root/'ledger.db-wal').unlink()
                    expected = '.engine/ledger.db-wal'
                elif case == 'A5':
                    path.unlink()
                elif case == 'A6':
                    (root/'extra.txt').write_bytes(b'unlisted')
                    expected = 'extra.txt'
                elif case == 'A7-db':
                    for listed in (True,False):
                        with self.subTest(listed=listed):
                            other = self.source([]).copy_dir
                            (other/'ledger.db').unlink()
                            if not listed:
                                self.write_manifest(other)
                            with self.assertRaisesRegex(ValueError,'ledger.db'):
                                si.read_source(other)
                    continue
                elif case == 'A7-side':
                    self.assertNotIn('.engine/ledger.db-wal',src.manifest)
                    self.assertNotIn('.engine/ledger.db-shm',src.manifest)
                    self.assertEqual(len(si.read_source(root).relays),1)
                    continue
                elif case == 'A7-rendered':
                    path.unlink(); self.write_manifest(root)
                    plan = si.plan_relays([si.read_source(root)])
                    self.assertEqual(plan.refused,1)
                    self.assertEqual(si.Report(plan).unresolved(), ['source bytes differ: p.md'])
                    self.assertNotIn('source bytes differ: p.md',si.Report(plan).shown())
                    continue
                elif case in ('A8', 'A8-shm'):
                    (root/'ledger.db').unlink()
                    sidecar = 'ledger.db-shm' if case == 'A8-shm' else 'ledger.db-wal'
                    (root/sidecar).write_bytes(b'sidecar')
                    self.write_manifest(root)
                    expected = 'ledger.db'
                elif case == 'A9-folder':
                    path.unlink(); path.mkdir()
                elif case == 'A9-link':
                    target = root/'target.txt'; target.write_bytes(path.read_bytes())
                    self.write_manifest(root)
                    path.unlink(); path.symlink_to('target.txt')
                elif case.startswith('A10'):
                    expected = '.source-manifest.json'
                    if case == 'A10-missing':
                        manifest_file.unlink()
                    elif case == 'A10-not-json':
                        manifest_file.write_bytes(b'{')
                    elif case == 'A10-shape':
                        manifest_file.write_bytes(b'[]')
                    else:
                        obj = json.loads(manifest_file.read_bytes())
                        if case == 'A10-extra-key':
                            obj['files'][0]['extra'] = True
                            manifest_file.write_text(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False))
                        else:
                            manifest_file.write_text(json.dumps(obj,indent=2))
                elif case == 'A11':
                    original = manifest_file.read_bytes()
                    for bad in ('/absolute.md','x/../p.md','duplicate'):
                        with self.subTest(path=bad):
                            obj = json.loads(original)
                            if bad == 'duplicate':
                                obj['files'].append(dict(obj['files'][0]))
                            else:
                                obj['files'][0]['path'] = bad
                            manifest_file.write_text(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False))
                            with self.assertRaisesRegex(ValueError,r'\.source-manifest\.json'):
                                si.read_source(root)
                    continue
                elif case == 'A12':
                    unused = root/'INDEX.md'; unused.write_bytes(b'index')
                    self.write_manifest(root); unused.write_bytes(b'Index')
                    expected = 'INDEX.md'
                elif case in ('A12-shape', 'A13-order', 'A14-inventory-unique'):
                    document = json.loads(manifest_file.read_bytes())
                    expected = '.source-manifest.json'
                    if case == 'A12-shape':
                        document['extra'] = []
                    elif case == 'A13-order':
                        document['files'].reverse()
                    else:
                        ledger = dict(document['files'][0], path='ledger.db')
                        document['files'].insert(1, ledger)
                        expected = 'ledger.db'
                    manifest_file.write_text(json.dumps(document, sort_keys=True,
                                                       separators=(',', ':'), ensure_ascii=False))
                elif case == 'A15-inventory-complete':
                    expected = '.engine/ledger.db'
                    stray = root / expected
                    stray.parent.mkdir()
                    stray.write_bytes(b'unlisted fixture file')
                # A4 in particular must stop before even opening a duplicate.
                with patch.object(si.sqlite3,'connect',wraps=sqlite3.connect) as connect:
                    with self.assertRaisesRegex(ValueError,__import__('re').escape(expected)):
                        si.read_source(root)
                    connect.assert_not_called()

    def test_reference_outcomes(self):
        # T-B: identity and union precede the earlier-than-reply test.
        cases = ['B1','B1-answer','B2','B2-answer','B3','B4','B5','B6',
                 'B6-answer','B7','B8','B9-earlier','B9-mixed','B9-later',
                 'B10','B11','B12','B13','B13-other','B14','B15','B15-other',
                 'B10-answer','B11-none','B11-one','B16']
        def document(name,when='early',path='p.md'):
            return {'path':path,'subject':name,'payload':name.encode(),
                    'stamp':'20261115-12000'+('0' if when=='early' else '2'),
                    'to':['m-3.planner']}
        def exercise(case,variant=None):
            answer = case.endswith('-answer')
            base = case.removesuffix('-answer')
            entry = 'p.md'
            reply = {'path':'reply.md','subject':'reply',
                     'stamp':'20261115-120001','headers':{}}
            groups = [[],[],[]]
            canonicals = ['/repo/own/alpha','/repo/other/beta','/repo/third/gamma']
            outcome = 'unresolved'; chosen = None; ambiguous_roots = ()
            if base in ('B1','B2'):
                groups[0].append(document('X','early' if base=='B1' else 'late'))
                if base=='B2': groups[1].append(document('Y'))
                if base=='B1': outcome,chosen='resolved','X'
            elif base in ('B3','B4'):
                groups[1].append(document('X','early' if base=='B3' else 'late'))
                if base=='B3': outcome,chosen='resolved','X'
            elif base in ('B5','B6','B7'):
                groups[1].append(document('X','late' if base=='B7' else 'early'))
                groups[2].append(document('Y','early' if base=='B5' else 'late'))
                outcome='ambiguous'; ambiguous_roots=(1,2)
            elif base=='B8':
                common = document('X',variant)
                groups[0].append(common); groups[1].append(common)
                if variant=='early': outcome,chosen='resolved','X'
            elif base.startswith('B9'):
                groups[0].append(document('X','late' if base=='B9-later' else 'early'))
                groups[1].append(document('Y','early' if base=='B9-earlier' else 'late'))
                outcome='ambiguous'; ambiguous_roots=(0,1)
            elif base=='B10':
                entry='beta/p.md'
                if answer:
                    variant = 'early'
                if variant != 'absent': groups[1].append(document('X',variant))
                groups[0].append(document('Y'))
                if variant=='early': outcome,chosen='resolved','X'
            elif base in ('B11', 'B11-none', 'B11-one'):
                canonicals[1]='/one/shared/relays'; canonicals[2]='/two/shared/relays'
                entry='shared/relays/p.md'
                if base != 'B11-none':
                    groups[1].append(document('X'))
                if base == 'B11':
                    groups[2].append(document('Y'))
                outcome='ambiguous'; ambiguous_roots=(1,2)
            elif base=='B12':
                common=document('X'); groups[1].append(common); groups[2].append(common)
                outcome,chosen='resolved','X'
            elif base in ('B13','B13-other'):
                entry='reply.md'
                if base=='B13-other':groups[1].append(document('Y',path='reply.md'))
            elif base=='B14':
                entry='unknown.md'
            elif base in ('B15','B15-other'):
                groups[0].append(document('X','late'))
                groups[0].append(document('P',path='parent.md'))
                if base=='B15-other':groups[1].append(document('Y'))
            elif base == 'B16':
                canonicals[1] = None
                groups[1].append(document('X'))
                entry = f'{self.number + 2}/p.md'
            if answer:
                groups[0].append(document('P',path='parent.md'))
            entries = (['parent.md',entry] if answer else
                       [entry,'parent.md'] if base.startswith('B15') else [entry])
            reply['headers']={'IN_REPLY_TO':', '.join(entries)}
            groups[0].append(reply)
            if base=='B8' or base.startswith('B9'):groups[1].append(reply)
            sources=[self.source(rows,canonical=canonical)
                     for rows,canonical in zip(groups,canonicals)]
            plan=si.plan_relays(sources); report=si.Report(plan)
            if base == 'B16':
                self.assertEqual(entry, f'{sources[1].copy_dir.name}/p.md')
                self.assertNotIn('canonical_root', sources[1].meta)
                self.assertEqual(plan.spellings, {('neither', 'unresolved'): 1})
            index=next(i for i,r in enumerate(plan.relays) if r.subject=='reply')
            mapped=plan.relays[index]
            by_subject={r.subject:i for i,r in enumerate(plan.relays)}
            expected_parent=by_subject['P'] if answer or base.startswith('B15') else by_subject.get(chosen)
            self.assertEqual(mapped.parent,expected_parent)
            self.assertEqual(mapped.answers,(by_subject[chosen],) if answer and chosen else ())
            listed=report.answers_listed()
            if outcome=='resolved':
                self.assertFalse(any(i==index and spelling==entry for i,spelling,_ in listed))
            else:
                self.assertIn((index,entry,outcome),listed)
            for n in ambiguous_roots:
                self.assertTrue(any(sources[n].root_uuid in item and entry in item
                                    for item in report.shown()))
            if base=='B12':self.assertEqual(sum(r.subject=='X' for r in plan.relays),1)
        def exercise_refused(case, answer):
            entry = 'p.md'
            reply = {'path': 'reply.md', 'subject': 'reply', 'stamp': '20261115-120001'}
            groups = [[], [], []]
            refused_path = entry
            outcome = 'unresolved'
            selected = None

            if case == 'B17-refused-own':
                groups[0].append(dict(document('X'), rendered=b'refused fixture'))
                groups[1].append(document('Y'))
            elif case == 'B18-refused-exact':
                entry = refused_path = 'beta/p.md'
                groups[0].append(dict(document('X', path=entry), rendered=b'refused fixture'))
                groups[1].append(document('Y'))
            elif case in ('B19-mixed-earlier', 'B20-mixed-later'):
                groups[0].append(dict(document('X'), rendered=b'refused fixture'))
                if case == 'B19-mixed-earlier':
                    groups[1].append(document('X'))
                    outcome = 'resolved'
                    selected = 'X'
                else:
                    groups[1].append(document('X', 'late'))
            elif case == 'B21-mixed-reply-union':
                groups[0].append(document('X'))
                groups[1].append(document('Y'))
                refused_path = reply['path']
                outcome = 'ambiguous'
            elif case == 'B22-mixed-distinct':
                groups[1].append(dict(document('X'), rendered=b'refused fixture'))
                groups[2].append(document('Y'))
                outcome = 'ambiguous'
            else:
                self.fail(f'Unknown refused reference case {case}')

            if answer:
                groups[0].append(document('P', path='parent.md'))
            reply['headers'] = {'IN_REPLY_TO': f'parent.md, {entry}' if answer else entry}
            groups[0].append(reply)
            if case == 'B21-mixed-reply-union':
                groups[1].append(dict(reply, rendered=b'refused fixture'))

            sources = [self.source(rows, canonical=canonical) for rows, canonical in zip(
                groups, ('/repo/own/alpha', '/repo/other/beta', '/repo/third/gamma'))]
            plan = si.plan_relays(sources)
            report = si.Report(plan)
            by_subject = {row.subject: index for index, row in enumerate(plan.relays)}
            reply_index = by_subject['reply']
            mapped = plan.relays[reply_index]
            target = by_subject[selected] if selected else None
            self.assertEqual(mapped.parent, by_subject['P'] if answer else target)
            self.assertEqual(mapped.answers, (target,) if answer and target is not None else ())
            expected_listed = [] if selected else [(reply_index, entry, outcome)]
            self.assertEqual([item for item in report.answers_listed() if item[0] == reply_index],
                             expected_listed)
            expected_counts = {('bare', outcome): 1}
            if answer:
                key = ('bare', 'resolved')
                expected_counts[key] = expected_counts.get(key, 0) + 1
            self.assertEqual(plan.spellings, expected_counts)
            self.assertEqual(plan.refused, 1)
            self.assertEqual(report.unresolved(), [f'source bytes differ: {refused_path}'])
            if case == 'B21-mixed-reply-union':
                self.assertEqual([point.root_uuid for point in mapped.provenance], [sources[0].root_uuid])
            if outcome == 'ambiguous':
                roots = sources[:2] if case == 'B21-mixed-reply-union' else sources[1:]
                for source in roots:
                    self.assertTrue(any(line.startswith('ambiguous reference: ')
                                        and entry in line and source.root_uuid in line
                                        for line in report.shown()))

        for case in cases:
            with self.subTest(case=case):
                if case in ('B8','B10'):
                    for variant in (('early','late') if case=='B8' else ('early','late','absent')):
                        with self.subTest(order=variant):exercise(case,variant)
                else:
                    exercise(case)

        for case in ('B17-refused-own', 'B18-refused-exact', 'B19-mixed-earlier',
                     'B20-mixed-later', 'B21-mixed-reply-union', 'B22-mixed-distinct'):
            for answer in (False, True):
                with self.subTest(case=f'{case}-answer' if answer else case):
                    exercise_refused(case, answer)

        def check_straddle(plan, order, parent, target_path, refused_path):
            self.assertEqual([row.subject for row in plan.relays], order)
            reply_index = order.index('reply')
            reply = plan.relays[reply_index]
            self.assertEqual(reply.parent, parent)
            self.assertEqual(reply.parent_spelling, 'parent.md' if parent is not None else None)
            self.assertEqual(reply.answers, ())
            self.assertEqual(plan.refused, 1)
            report = si.Report(plan)
            self.assertEqual(report.unresolved(), [f'source bytes differ: {refused_path}'])
            self.assertEqual(report.answers_listed(), [(reply_index, target_path, 'unresolved')])
            for index, row in enumerate(plan.relays):
                if row.parent is not None:
                    self.assertLess(row.parent, index)
                for answer in row.answers:
                    self.assertLess(answer, index)

        target_path = 'straddle-target.md'
        target = {'path': target_path, 'subject': 'target', 'to': ['m-3.planner']}
        parent = {'path': 'parent.md', 'subject': 'parent'}
        for answer in (False, True):
            suffix = '-answer' if answer else ''
            reference = f'parent.md, {target_path}' if answer else target_path
            reply = {'path': 'straddle-reply.md', 'subject': 'reply',
                     'headers': {'IN_REPLY_TO': reference}}
            expected_parent = 0 if answer else None
            with self.subTest(case='P1-target-straddle' + suffix):
                fold_target = dict(target, stamp='20261101-013000')
                earlier = dict(fold_target, mtime=1793521800, rendered=b'refused fixture')
                later = dict(fold_target, mtime=1793525400)
                folded_reply = dict(reply, stamp='20261101-014500', mtime=1793522700)
                earlier_rows = [earlier, folded_reply]
                order = ['target', 'reply']
                if answer:
                    earlier_rows.insert(0, dict(parent, stamp='20261101-011500', mtime=1793520900))
                    order.insert(0, 'parent')
                eligible = self.source([later])
                plan = si.plan_relays([self.source(earlier_rows), eligible])
                survivor = next(row for row in plan.relays if row.subject == 'target')
                self.assertEqual(survivor.created_at, 1793521800000)
                self.assertEqual([(point.root_uuid, point.seq, point.offset)
                                  for point in survivor.provenance],
                                 [(eligible.root_uuid, eligible.relays[0]['seq'], -28800)])
                check_straddle(plan, order, expected_parent, target_path, target_path)

            with self.subTest(case='P1-reply-straddle' + suffix):
                earlier_rows = [dict(reply, rendered=b'refused fixture'), target]
                order = ['reply', 'target']
                if answer:
                    earlier_rows.insert(0, parent)
                    order.insert(0, 'parent')
                eligible = self.source([reply])
                plan = si.plan_relays([self.source(earlier_rows), eligible])
                survivor = next(row for row in plan.relays if row.subject == 'reply')
                second = int(datetime(2026, 11, 15, 12,
                                      tzinfo=ZoneInfo('America/Los_Angeles')).timestamp())
                self.assertEqual(survivor.created_at, second * 1000 + int(answer))
                self.assertEqual([(point.root_uuid, point.seq) for point in survivor.provenance],
                                 [(eligible.root_uuid, eligible.relays[0]['seq'])])
                check_straddle(plan, order, expected_parent, target_path, reply['path'])

    def test_copy_states(self):
        # A staged copy must be validated in full before any SQLite read.
        def exercise(place, difference, use, stray=None):
            relative = {'top': 'f.md', 'nested': 'n1/n2/f.md',
                        'linked': 'ln/f.md', 'leaf-link': 'f.md'}[place]
            rows = [{'path': relative}] if use == 'used' else []
            root = self.source(rows).copy_dir
            listed = root / relative
            if use == 'unused':
                listed.parent.mkdir(parents=True, exist_ok=True)
                listed.write_bytes(b'fixture\n')
                self.write_manifest(root)
            target = listed
            expected_paths = [relative]
            if place in ('linked', 'leaf-link'):
                outside = self.directory / f'{root.name}-outside'
                outside.mkdir()
                if place == 'linked':
                    listed.parent.rename(outside / 'files')
                    listed.parent.symlink_to(outside / 'files', target_is_directory=True)
                    target = outside / 'files' / 'f.md'
                    expected_paths.append('ln')
                else:
                    target = outside / 'f.md'
                    listed.rename(target)
                    listed.symlink_to(target)
            if difference == 'bytes':
                content = target.read_bytes()
                target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
            elif difference == 'size':
                target.write_bytes(target.read_bytes() + b'x')
            elif difference == 'missing':
                target.unlink()
            elif difference == 'extra':
                (target.parent / 'g.md').write_bytes(b'unlisted')
                expected_paths.append((listed.parent / 'g.md').relative_to(root).as_posix())
            elif difference == 'type':
                target.unlink()
                os.mkfifo(target)

            blocked = None
            if stray == 'dir-link':
                outside = self.directory / f'{root.name}-outside'
                outside.mkdir()
                (root / 'dir-link').symlink_to(outside, target_is_directory=True)
            elif stray == 'fifo':
                os.mkfifo(root / 'fifo')
            elif stray == 'unreadable':
                blocked = root / 'n1'
                blocked.mkdir()
            if stray:
                expected_paths = ['n1' if stray == 'unreadable' else stray]

            scandir = os.scandir

            def scan(path):
                if blocked is not None and not isinstance(path, int) and Path(path) == blocked:
                    raise PermissionError(13, 'fixture cannot list folder', str(blocked))
                return scandir(path)

            with (patch.object(si.sqlite3, 'connect', wraps=sqlite3.connect) as connect,
                  patch.object(os, 'scandir', side_effect=scan)):
                if stray is None and difference == 'none' and place in ('top', 'nested'):
                    self.assertEqual(len(si.read_source(root).relays), len(rows))
                else:
                    expected = '|'.join(re.escape(path) for path in expected_paths)
                    with self.assertRaisesRegex(ValueError, expected):
                        si.read_source(root)
                    connect.assert_not_called()

        for place in ('top', 'nested', 'linked', 'leaf-link'):
            for difference in ('none', 'bytes', 'size', 'missing', 'extra', 'type'):
                for use in ('used', 'unused'):
                    with self.subTest(state=f'{place}/{difference}/{use}'):
                        exercise(place, difference, use)
        for stray in ('dir-link', 'fifo', 'unreadable'):
            with self.subTest(state=f'stray/{stray}'):
                exercise('top', 'none', 'unused', stray)
