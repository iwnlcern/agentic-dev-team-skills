"""Measure real scratch-daemon submits; never mutate the source relay root.

Run explicitly, outside unittest discovery. Timing misses are retained data.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import cProfile
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pstats
import queue
import random
import re
import shutil
import statistics
import tempfile
import threading
import time

from relay_engine import client, daemon, judging, ledger, render, rules, version
from relay_engine.envelope import REQUIRED_FIELDS, parse_draft
from relay_engine.paths import Root, ensure_engine_dir

SEED = 297013
TOOLS = Path(__file__).resolve().parents[2]


@contextmanager
def scratch_daemon(root, socket_path, run_id, top_seat=None):
    """Real framed daemon, held in a thread so timing can wrap its judge only."""
    read_fd, write_fd = os.pipe()
    failures = []
    def serve():
        try:
            daemon.start(str(root), ready_fd=write_fd, socket_override=str(socket_path),
                         run_id=run_id, top_seat=top_seat,
                         top_role='orchestrator-planner' if top_seat else None)
        except BaseException as exc:
            failures.append(exc)
            try: os.close(write_fd)
            except OSError: pass
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        ready = os.read(read_fd, 1)
    finally:
        os.close(read_fd)
    if ready != b'R':
        worker.join(5)
        raise RuntimeError('scratch daemon did not become ready', failures)
    try:
        yield
    finally:
        if worker.is_alive():
            client.request(str(root), 'daemon.stop', {}, timeout=30)
            worker.join(30)
        if worker.is_alive():
            raise RuntimeError('scratch daemon did not stop')
        if failures:
            raise failures[0]


def snapshot(source):
    rows = []
    for path in sorted(source.rglob('*.md')):
        relative = path.relative_to(source)
        if '.engine' in relative.parts or path.name in {'INDEX.md', 'SEATS.md'} or not path.is_file():
            continue
        raw = path.read_bytes()
        fields = rules.header_fields(raw.decode('utf-8'))
        if fields.get('PHASE'):
            rows.append((relative.as_posix(), raw))
    if not rows:
        raise ValueError('source has no relay files')
    return rows


def commissioned_chains(fixture, count):
    originals = [(p.name, p.read_text()) for p in sorted(fixture.glob('0[1-5]-*.md'))]
    if len(originals) != 5:
        raise ValueError('five-carrier CM77 fixture required')
    rows = []
    # Legacy numeric fixture names carry no timestamp. Give its five ordered
    # carriers a fixed synthetic base, then shift each whole lineage by k min.
    baseline = datetime(2026, 8, 1, 12)
    for k in range(1, count+1):
        suffix = f'-k{k:02}'
        mapping = {}
        for _, text in originals:
            fields = rules.header_fields(text)
            for key in ('DISPATCH_ID', 'PARENT_DISPATCH_ID', 'COMMISSION_ID',
                        'COMMISSION_SCOPE', 'CHARTER_DOC_ID', 'DESIGN_DOC_ID',
                        'DESIGN_LOCK_ID', 'PLAN_LOCK_ID'):
                if fields.get(key): mapping[fields[key]] = fields[key]+suffix
            for key in ('FROM', 'TO', 'CC', 'COMMISSION_TO'):
                for address in rules.split_addresses(fields.get(key)):
                    if '.' in address:
                        owner, role = address.rsplit('.', 1)
                        mapping[address] = owner+suffix+'.'+role
        pattern = re.compile('|'.join(re.escape(value) for value in sorted(mapping,key=len,reverse=True)))
        for index, (name, text) in enumerate(originals):
            text = pattern.sub(lambda match: mapping[match[0]], text)
            stamp = (baseline+timedelta(minutes=k,seconds=index)).strftime('%Y%m%d-%H%M%S')
            rows.append((f'commission-k{k:02}/{name[:-3]}-{stamp}.md',text.encode()))
    return rows


def ingest(root_path, rows, run_id):
    """Import a captured historical population without re-judging its rows."""
    with Root(root_path) as root:
        fd = ensure_engine_dir(root.dirfd)
        db = ledger.init_schema(fd, root.path)
        os.close(fd)
        try:
            ledger.establish_run_identity(db, run_id)
            db.execute('BEGIN')
            addresses = set()
            for seq,(relative,raw) in enumerate(rows,1):
                path = root_path/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(raw)
                fields = rules.header_fields(raw.decode())
                stamp = re.search(r'\d{8}-\d{6}',Path(relative).name)
                stamp = stamp[0] if stamp else (datetime(2026,1,1)+timedelta(seconds=seq)).strftime('%Y%m%d-%H%M%S')
                targets = rules.split_addresses(fields.get('TO'))
                copied = rules.split_addresses(fields.get('CC'))
                addresses.update([fields.get('FROM',''),*targets,*copied])
                digest = hashlib.sha256(raw).hexdigest()
                db.execute('INSERT INTO relays(seq,submission_id,stamp,rendered_path,phase,role,dispatch_id,parent_dispatch_id,run_id,from_seat,to_seats,cc_seats,status,subject,headers_json,body,body_sha256,content_hash,origin,advisories_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (seq,f'capture-{seq}',stamp,relative,fields.get('PHASE',''),fields.get('ROLE',''),
                     fields.get('DISPATCH_ID',f'capture-{seq}'),fields.get('PARENT_DISPATCH_ID'),run_id,
                     fields.get('FROM','capture.planner'),json.dumps(targets),json.dumps(copied),
                     fields.get('STATUS'),fields.get('SUBJECT','captured history'),json.dumps(fields),
                     raw,digest,digest,'hand','[]'))
            for address in sorted(addresses-{''}):
                db.execute('INSERT INTO roster VALUES(?,?,?,?)',
                           (address.lower(),rules.address_role(address) or 'planner','capture',len(rows)))
            db.commit()
            render.render_index(root,render.index_rows(db),10,ledger.epoch_state(db))
        finally:
            db.close()


def phase_samples(rows, count):
    by_phase = defaultdict(list)
    for relative,raw in rows:
        fields=rules.header_fields(raw.decode())
        if fields.get('PHASE') in rules.PHASE_AUTHORITY_ALLOWED:
            by_phase[fields['PHASE']].append((relative,raw))
    total=sum(map(len,by_phase.values()))
    allocation={phase:math.floor(count*len(items)/total) for phase,items in by_phase.items()}
    order=sorted(by_phase,key=lambda phase:(-(count*len(by_phase[phase])/total-allocation[phase]),phase))
    for phase in order[:count-sum(allocation.values())]: allocation[phase]+=1
    randomizer=random.Random(SEED)
    selected=[randomizer.choice(by_phase[phase]) for phase in sorted(allocation) for _ in range(allocation[phase])]
    randomizer.shuffle(selected)
    return selected,dict(sorted(allocation.items()))


def submission(raw, run_id, number):
    text=raw.decode()
    fields=rules.header_fields(text)
    values={key:fields.get(key,'') for key in REQUIRED_FIELDS}
    values.update(DISPATCH_ID=f'latency-{number:04}',RUN_ID=run_id,
                  SUBJECT=f'latency sample {number}',CEREMONY_TIER='medium',
                  EVIDENCE_TARGET='E2',HUMAN_GATE_REQUIRED='no')
    values['AUTHORITY']=fields.get('AUTHORITY') or sorted(rules.PHASE_AUTHORITY_ALLOWED[fields['PHASE']])[0]
    # Preserve the sampled role, addressees and structural content; only the
    # submission identity and required envelope defaults are newly authored.
    remainder='\n'.join(line for line in text.splitlines()
                        if not any(re.match(r'^'+key+r':',line) for key in REQUIRED_FIELDS))
    result=('\n'.join(key+': '+values[key] for key in REQUIRED_FIELDS)+'\n\n'+remainder+'\n').encode()
    parse_draft(result.decode())
    return result


def distribution(values):
    ordered=sorted(values)
    return {'median':statistics.median(ordered),'p95':ordered[math.ceil(.95*len(ordered))-1],
            'maximum':max(ordered)}


def run(args):
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    prefix='workload-'+args.workload
    for suffix in ('counts.json','samples.csv','summary.json'):
        if (out/(prefix+'-'+suffix)).exists():raise FileExistsError('refuse to overwrite a capture')
    captured=snapshot(args.source_root)
    source_pairs=[(path,hashlib.sha256(raw).hexdigest()) for path,raw in captured]
    rows=list(captured)
    if args.workload=='B':rows+=commissioned_chains(args.chain_fixture,args.lineages)
    rows.sort(key=lambda row:rules.relay_order_key(Path(row[0])))
    pairs=sorted((path,hashlib.sha256(raw).hexdigest()) for path,raw in rows)
    snapshot_id=hashlib.sha256(json.dumps(pairs,separators=(',',':')).encode()).hexdigest()
    selected,allocation=phase_samples(rows,args.submits)
    fields=[rules.header_fields(raw.decode()) for _,raw in rows]
    counts={'source_root':str(args.source_root.resolve()),'snapshot_identity':snapshot_id,
            'snapshot_pairs':pairs,'source_snapshot_pairs':source_pairs,
            'relay_count':len(rows),'lock_consumer_count':sum(bool(f.get('DESIGN_LOCK_ID') or f.get('PLAN_LOCK_ID')) for f in fields),
            'cross_root_declaration_count':sum(any(f.get(k) for k in (*rules.XROOT_TRIGGERS,'PLAN_SOURCE_REPO','PLAN_SOURCE_COMMIT')) for f in fields),
            'commission_carrier_count':sum(bool(f.get('COMMISSION_ID')) for f in fields),
            'lineage_count':args.lineages if args.workload=='B' else 0,'chain_depth':5 if args.workload=='B' else 0,
            'capture_time':datetime.now(timezone.utc).isoformat(),'engine_fingerprint':version.fingerprint(TOOLS),
            'seed':SEED,'phase_mix':dict(Counter(f.get('PHASE') for f in fields)), 'sample_phase_mix':allocation}
    samples=[];timings=queue.Queue();real_judge=judging.judge_candidate;real_admit=daemon.admit
    def timed_admit(*positional,**keyword):
        judge=keyword.get('judge')
        if judge is not None:
            def timed(context):
                started=time.perf_counter()
                try:return judge(context)
                finally:timings.put((time.perf_counter()-started)*1000)
            keyword['judge']=timed
        return real_admit(*positional,**keyword)
    with tempfile.TemporaryDirectory(prefix='v297-latency-') as temp:
        scratch=Path(temp);sprint=scratch/'sprint';root=sprint/'.relays/latency';root.mkdir(parents=True)
        # Keep artifact paths relative to the sprint; exclude relay substrates
        # and result captures, which are not governing artifact inputs.
        for member in args.artifact_tree.iterdir():
            if member.name in {'.relays','results','.git'}:continue
            target=sprint/member.name
            if member.is_dir():shutil.copytree(member,target,symlinks=True)
            elif member.is_file():shutil.copy2(member,target)
        ingest(root,rows,'latency')
        try:
            daemon.admit=timed_admit
            with scratch_daemon(root,scratch/'daemon.sock','latency'):
                registrations={}
                prepared=[]
                for number,(_,raw) in enumerate(selected,1):
                    draft=submission(raw,'latency',number)
                    envelope=parse_draft(draft.decode())
                    if envelope.from_seat not in registrations:
                        registrations[envelope.from_seat]=client.request(str(root),'seat.register',
                            {'address':envelope.from_seat,'role_word':envelope.role})['key_path']
                    path=root/f'.engine/drafts/latency/{number:04}.md';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(draft)
                    prepared.append((path,envelope,registrations[envelope.from_seat]))
                for number,(path,envelope,key) in enumerate(prepared,1):
                    started=time.perf_counter()
                    receipt=client.submit(str(root),str(path),str(root/key),timeout=60)
                    elapsed=(time.perf_counter()-started)*1000
                    samples.append({'phase':envelope.phase,'whole_submit_ms':elapsed,'judging_ms':timings.get(timeout=1),
                                    'finding_count':len(receipt['findings']),'relay':receipt['path']})
                    if 'judging.failed' in receipt['advisories']:raise RuntimeError('judging failed in measured submit')
                    if number%25==0:print(f'{args.workload}: {number}/{args.submits}',flush=True)
        finally:daemon.admit=real_admit
        whole=distribution([s['whole_submit_ms'] for s in samples]);judgment=distribution([s['judging_ms'] for s in samples])
        summary={'whole_submit_ms':whole,'judging_ms':judgment,'intent_met':judgment['median']<=100 and judgment['maximum']<=500,
                 'sample_count':len(samples),'external_budget_seconds':5}
        if not summary['intent_met']:
            slowest=max(samples,key=lambda sample:sample['judging_ms'])
            fd=os.open(root/'.engine',os.O_RDONLY|os.O_DIRECTORY);db=ledger.open_ledger(fd);os.close(fd)
            try:
                seq,raw=db.execute('SELECT seq,body FROM relays WHERE rendered_path=?',(slowest['relay'],)).fetchone()
                corpus=judging.corpus_before(db,seq)
            finally:db.close()
            profiler=cProfile.Profile();profiler.enable()
            real_judge(corpus,slowest['relay'],parse_draft(raw.decode()),raw,artifact_root=root,deadline=time.monotonic()+5)
            profiler.disable();stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumulative').print_stats(35)
            summary['slowest_submit']=slowest;summary['slowest_profile']=stream.getvalue()
    (out/(prefix+'-counts.json')).write_text(json.dumps(counts,indent=2)+'\n')
    with (out/(prefix+'-samples.csv')).open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['phase','whole_submit_ms','judging_ms','finding_count','relay']);writer.writeheader();writer.writerows(samples)
    (out/(prefix+'-summary.json')).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload',choices=('A','B'),required=True)
    parser.add_argument('--source-root',type=Path,required=True)
    parser.add_argument('--artifact-tree',type=Path,required=True)
    parser.add_argument('--chain-fixture',type=Path,default=TOOLS/'relay-lint-fixtures/mastertier/CM77-valid-chain')
    parser.add_argument('--lineages',type=int,default=50)
    parser.add_argument('--submits',type=int,default=200)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    if args.submits<1 or args.lineages<1:parser.error('counts must be positive')
    run(args)


if __name__=='__main__':main()
