"""Isolated, synthetic v3 authority data and a bounded engine query benchmark."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
from random import Random
import shutil
import sqlite3
import sys
import tempfile
import time

from . import authority, restore_cancel, schema, slips


_GIB = 1024 ** 3
_RATIOS = {key: (n, d) for key, n, d in (
    ('implement_slips', 1129, 17596), ('merge_slips', 350, 17596),
    ('claims', 1920, 17596), ('snapshots', 3307, 17596), ('findings', 1920, 175960))}
_SIZES = (4500, 14000, 60000, 90000, 420000)
_SHARES = (4, 10, 3, 2, 1)
_SIZE_CYCLE = tuple(size for size, count in zip(_SIZES, _SHARES) for _ in range(count))
_CLAIMS = (1, 2, 2, 4)
# Each six-seat block offers 18 target slips. At 200 calls the allow targets
# are visited 9 times each across both strata, pending 10 and objected 5.
# The size columns below make the combined N = 700 reads (140,350,105,70,35).
_ALLOW = (5, 6, 9, 10, 12, 13, 14, 15, 16, 17)
_PENDING = (0, 1, 2)
_OBJECTED = (3, 4, 7, 8)
_CHANGED = (11,)
_FILE_COUNTS = {'allow': (0, 35, 10, 5, 0), 'pending': (5, 3, 1, 2, 3),
                'objected': (18, 1, 1, 1, 1), 'changed': (4, 0, 0, 3, 0)}
_TARGETS = {'allow': _ALLOW, 'pending': _PENDING, 'objected': _OBJECTED, 'changed': _CHANGED}
_SHAPES = {'standing': {'live': 60, 'empty': 20, 'held': 20},
           'scope': {'bound': 40, 'unbound': 30, 'path': 20, 'other': 10},
           'act': {'bound': 30, 'unbound': 15, 'pending': 15, 'objected': 10,
                   'changed': 10, 'other': 10, 'missing': 10}}
_FULL_READS = frozenset({'bound', 'unbound', 'pending', 'objected'})
_ACT_OUTCOMES = {
    'bound': ('allow', None), 'unbound': ('allow', None),
    'pending': ('held', 'authority.verification-pending'),
    'objected': ('held', 'authority.verification-objected'),
    'changed': ('held', 'authority.plan-changed'),
    'other': ('deny', 'authority.slip-invalid'), 'missing': ('deny', 'authority.slip-missing')}
_LIMITS = ('Counts and workload shape are a synthetic proxy, not observations of a real store. '
           'A within-bound result meets the bound on this seed only. '
           'A slowed core can read as over-bound; this is work under observed conditions, '
           'not proof a quiet host would fail. A query’s file reads and host waits can both read as '
           'inconclusive. A busy run inside the wall cap passes.')
_PAYLOAD = b'Synthetic relay for the isolated authority benchmark.\n' * 32
_PAYLOAD_HASH = hashlib.sha256(_PAYLOAD).hexdigest()


@dataclass(frozen=True)
class SeedStats:
    counts: dict[str, int]
    ratios: dict[str, tuple[int, int]]
    seat_allocation: dict[str, int]
    file_size_counts: dict[int, int]
    standing_fanout: dict[str, tuple[int, ...]]


@dataclass(frozen=True)
class Sample:
    wall_ns: int
    cpu_ns: int


@dataclass(frozen=True)
class BenchResult:
    clocks: dict[str, dict[str, int | float]]
    stratum_counts: dict[str, dict[str, int]]
    seeded_file_size_counts: dict[int, int]
    timed_file_size_counts: dict[int, int]
    standing_fanout: dict[str, tuple[int, ...]]
    calibration_ns: tuple[int, ...]
    load_start: tuple[float, float, float]
    load_end: tuple[float, float, float]
    logical_cpus: int
    kind_verdicts: dict[str, str]
    verdict: str
    limits: str


def _available_bytes(path):
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def _floor(path, gib):
    free = _available_bytes(path)
    if free < gib * _GIB:
        raise ValueError(f'{free} bytes free at {path}; need {gib} GiB')


def _profiles():
    result = {}
    for label, indices in _TARGETS.items():
        sizes = iter(size for size, count in zip(_SIZES, _FILE_COUNTS[label]) for _ in range(count))
        for index in indices:
            result[index] = (label, tuple(next(sizes) for _ in range(_CLAIMS[index % 4] + 3)))
        if next(sizes, None) is not None:
            raise ValueError('unused sizes in target profile')
    return result


class _Seed:
    def __init__(self, conn, store, rng_seed):
        self.conn, self.store = conn, store
        self.rng = Random(rng_seed)
        self.tallies = Counter()
        self.files = {}
        self.assigned = {}
        self.repos = {}
        self.teams = {}

    def add(self, table, **fields):
        self.conn.execute(f"INSERT INTO {table} ({', '.join(fields)}) VALUES "
                          f"({', '.join('?' for _ in fields)})", tuple(fields.values()))

    def bump(self, key):
        self.tallies[key] += 1
        return self.tallies[key]

    def relay(self, project, kind='SITREP', *, repo=None, bundle=None, parent=None,
              sender=None, verdict=None, needs='none'):
        n = self.bump('relays')
        relay = f'relay-{n:08d}'
        self.add('relays', id=relay, handle=n, project_id=project, kind=kind, needs=needs,
                 bundle_id=bundle, subject='Synthetic authority benchmark', parent_id=parent,
                 sender_seat_id=sender, sender_special='operator' if sender is None else None,
                 verdict=verdict, repository_id=repo, created_at=n, body_sha256=_PAYLOAD_HASH,
                 meta_sha256=hashlib.sha256(relay.encode()).hexdigest(), body_bytes=len(_PAYLOAD), header_bytes=0)
        self.add('bodies', relay_id=relay, body=_PAYLOAD)
        if parent:
            self.add('edges', id=f'parent-{relay}', from_relay_id=relay, ordinal=1, kind='parent',
                     target_type='relay', spelling=parent, target_relay_id=parent)
        return relay

    def seat(self, project, name, role='implementer'):
        team = f'team-{name}'
        self.add('team_instances', id=team, project_id=project, name=team, level='pair',
                 opened_at=1, opened_by='operator')
        self.add('seats', id=name, team_instance_id=team, role=role, state='occupied')
        self.teams[name] = team
        self.bump('seats')

    def project(self, project, count):
        self.add('projects', id=project, name=project, created_at=1)
        repos = [f'{project}-repo-{i}' for i in range(count)]
        self.repos[project] = repos
        for repo in repos:
            self.add('repositories', id=repo, project_id=project, name=repo)
            directory = self.store / 'copies' / repo
            directory.mkdir(parents=True)
            for i, size in enumerate(_SIZE_CYCLE * 4):
                name = f'fixture-{i:02d}'
                data = self.rng.randbytes(size)
                (directory / name).write_bytes(data)
                self.files[repo, i] = (f'{repo}:{name}', hashlib.sha256(data).hexdigest(), size)

    def file_refs(self, repo, sizes, slot, label):
        # Reserve the last cycle for objected digests so a record for one
        # objection cannot make an otherwise unchanged allow target held.
        used = set()
        result = []
        for n, size in enumerate(sizes):
            objection = label == 'objected' and n == len(sizes) - 3
            indices = [i for i, candidate in enumerate(_SIZE_CYCLE * 4)
                       if candidate == size and (i >= 60) == objection and i not in used]
            i = indices[(slot + n) % len(indices)]
            used.add(i)
            result.append(self.files[repo, i])
        return result

    def snapshot(self, request, file, locked_by=None, ordinal=1):
        artifact, digest, size = file
        n = self.bump('snapshots')
        edge = f'file-edge-{n}'
        self.add('edges', id=edge, from_relay_id=request, ordinal=ordinal, kind='ref', target_type='path',
                 spelling=artifact, target_path=artifact, digest_at_submit=digest)
        self.add('snapshots', id=f'snapshot-{n}', handle=n, relay_id=request, edge_id=edge,
                 artifact=artifact, content_sha256=digest, bytes_len=size, has_bytes=0,
                 locked_by=locked_by, locked_at=n if locked_by else None)
        return n

    def claim(self, project, seat, file, parent=None, bundle=None, *, status='approved', reviewer=None):
        ask = self.relay(project, 'PLAN', bundle=bundle, parent=parent, needs='review')
        claim = self.relay(project, 'PLAN-REVIEW', bundle=bundle, parent=ask,
                           sender=seat, verdict='approve')
        self.bump('claims')
        snapshot = self.snapshot(ask, file, locked_by=claim)
        request = self.relay(project, 'REVIEW', bundle=bundle, parent=claim,
                             needs='review') if reviewer else None
        artifact, digest, _ = file
        self.add('verification_requests', claim_relay_id=claim, artifact=artifact, kind='plan',
                 snapshot_handle=snapshot, snapshot_request_relay_id=ask, content_sha256=digest,
                 request_relay_id=request, reviewer_seat_id=reviewer, created_at=self.tallies['relays'])
        if status == 'approved':
            n = self.tallies['relays']
            self.add('waivers', claim_relay_id=claim, kind='plan', by_special='operator',
                     words='Synthetic approved alternative', created_at=n, after_handle=n)
        if status == 'objected':
            self.relay(project, 'REVIEW', bundle=bundle, parent=request, sender=reviewer, verdict='must-revise')
        return {'kind': 'plan', 'claim': claim, 'locks': [
            {'artifact': artifact, 'snapshot_handle': snapshot, 'request_relay_id': ask,
             'content_sha256': digest}]}

    def slip(self, project, repo, seat, *, kind='implement', target=None, reviewer=None):
        slot = self.bump('implement_slips' if kind == 'implement' else 'merge_slips')
        number = self.bump('slips')
        bundle = f'bundle-{number:08d}'
        self.add('bundles', id=bundle, project_id=project, team_instance_id=self.teams[seat],
                 name=bundle, opened_by_special='operator')
        bindings = {'claims': [], 'digests': [], 'waived': []}
        live = target is not None
        parent = None
        if target is not None:
            position, (label, sizes) = target
            files = self.file_refs(repo, sizes, slot, label)
            for n, file in enumerate(files[:-3]):
                claim = self.claim(project, seat, file, parent, bundle,
                                   status='pending' if label == 'pending' and n == 0 else 'approved')
                bindings['claims'].append(claim)
                parent = claim['claim']
            for file in files[-3:]:
                artifact, digest, _ = file
                bindings['digests'].append({'artifact': artifact, 'kind': 'plan',
                                            'reference': 'path', 'digest': digest})
            if label == 'objected':
                self.claim(project, seat, files[-3], bundle=bundle, status='objected', reviewer=reviewer)
            if label == 'changed':
                name = files[0][0].partition(':')[2]
                path = self.store / 'changed' / repo
                path.mkdir(parents=True, exist_ok=True)
                (path / name).write_bytes(b'changed lock\n')
        issued = self.relay(project, 'IMPL' if kind == 'implement' else 'MERGE-GATE',
                            repo=repo if kind == 'implement' else None,
                            bundle=bundle, parent=parent, needs='act')
        self.add('recipients', relay_id=issued, seat_id=seat, role='to')
        timestamp = self.tallies['relays']
        sid = f'slip-{number:08d}'
        self.add('slips', id=sid, handle=number, project_id=project, kind=kind, seat_id=seat,
                 issuer_special='operator', bundle_id=bundle, repository_id=repo, issued_in=issued,
                 binds=json.dumps(bindings, separators=(',', ':')), state='live' if live else 'ended',
                 issued_at=timestamp, ended_at=None if live else timestamp + 1,
                 end_reason=None if live else ('pr-closed' if kind == 'merge' else 'superseded'),
                 revision=1 if live else 2)
        self.add('slip_events', slip_id=sid, revision=1, event='issue', at=timestamp,
                 by_special='operator', detail='synthetic')
        if not live:
            self.add('slip_events', slip_id=sid, revision=2, event='end', at=timestamp + 1,
                     by_special='operator', detail='synthetic')
            return None
        ref = f'bound-{sid}'
        self.add('branch_bindings', repository_id=repo, branch=ref, seat_id=seat,
                 bundle_id=bundle, bound_at=timestamp)
        return {'seat': seat, 'repo': repo, 'bound': ref, 'position': position, 'label': label,
                'artifacts': [file[0] for file in files]}


def seed(store_dir: Path, relays: int = 50_000, seats: int = 32, rng_seed: int = 1) -> SeedStats:
    """Seed only a new store. Authority rows here are synthetic fixture data."""
    store = Path(store_dir).resolve()
    if seats < 8:
        raise ValueError('need at least 8 seats to populate every stratum')
    counts = {key: (2 * relays * n + d) // (2 * d) for key, (n, d) in _RATIOS.items()}
    held = [f'held-{i:04d}' for i in range(seats // 8)]
    empty = [f'empty-{i:04d}' for i in range(seats // 8)]
    live = [f'live-{i:04d}' for i in range(seats - len(held) - len(empty))]
    profiles = _profiles()
    expected_claims = sum(_CLAIMS[(i % 18) % 4] + int(i % 18 in _OBJECTED)
                          for i in range(3 * len(live)))
    if min(counts['implement_slips'] - 3 * len(live),
           counts['claims'] - expected_claims, counts['snapshots'] - counts['claims']) < 0:
        raise ValueError('too few relays to populate the requested seat workload')
    _floor(store, 5)
    store.mkdir()
    with closing(sqlite3.connect(store / 'store.sqlite3')) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        schema.init_v3(conn)
        state = _Seed(conn, store, rng_seed)
        targets = []
        with conn:
            for project, count in (('active-a', 3), ('active-b', 3), ('held', 1)):
                state.project(project, count)
            for seat in held:
                state.seat('held', seat)
            for i, seat in enumerate(empty):
                state.seat('active-a' if i == 0 or i % 2 == 0 else 'active-b', seat, 'reviewer')
            for i, seat in enumerate(live):
                project = 'active-a' if i % 6 < 3 else 'active-b'
                state.seat(project, seat)
                for j, repo in enumerate(state.repos[project]):
                    position = (3 * i + j) % 18
                    targets.append(state.slip(project, repo, seat, target=(position, profiles[position]),
                                              reviewer=empty[0]))
        restore_cancel.cancel_all(conn, 'held', 'fixture-restore-job', state.tallies['relays'] + 1)
        _floor(store, 3)
        active = [(entry['repo'].rsplit('-repo-', 1)[0], entry['repo'], entry['seat']) for entry in targets]
        with conn:
            for kind in ('implement', 'merge'):
                label = f'{kind}_slips'
                for i in range(counts[label] - state.tallies[label]):
                    project, repo, seat = active[i % len(active)]
                    state.slip(project, repo, seat, kind=kind)
            for i in range(counts['claims'] - state.tallies['claims']):
                project, repo, seat = active[i % len(active)]
                state.claim(project, seat, state.files[repo, i % 60], status='pending')
            for i in range(counts['snapshots'] - state.tallies['snapshots']):
                project, repo, _ = active[i % len(active)]
                request = state.relay(project, 'PLAN', needs='review')
                state.snapshot(request, state.files[repo, i % 60])
            for i in range(counts['findings']):
                project, _, seat = active[i % len(active)]
                review = state.relay(project, 'PLAN-REVIEW', sender=seat, verdict='must-revise')
                state.add('findings', id=f'finding-{i}', handle=i + 1, project_id=project,
                          raised_in=review, raiser_seat_id=seat, severity='advisory',
                          summary='Synthetic review finding', status='open')
                state.bump('findings')
        if state.tallies['relays'] > relays:
            raise ValueError('too few relays for the allocated fixture rows')
        while state.tallies['relays'] < relays:
            _floor(store, 3)
            stop = min(relays, state.tallies['relays'] + 1000)
            with conn:
                while state.tallies['relays'] < stop:
                    state.relay('active-a' if state.tallies['relays'] % 2 else 'active-b')
    realized = {key: state.tallies[key] for key in ('relays', *_RATIOS, 'seats')}
    stats = SeedStats(realized, dict(_RATIOS), {'held': len(held), 'no_slip': len(empty), 'with_slips': len(live)},
                      dict(Counter(row[2] for row in state.files.values())),
                      {seat: tuple(len(entry['artifacts']) - 3 for entry in targets if entry['seat'] == seat)
                       for seat in live})
    (store / 'profile.json').write_text(json.dumps({'stats': asdict(stats), 'targets': targets,
                                                    'held': held, 'empty': empty, 'live': live},
                                                   sort_keys=True))
    return stats


def _strata(kind, queries):
    """Round cumulative boundaries so every requested share is within one call."""
    low = 0
    boundary = 0
    for name, share in _SHAPES[kind].items():
        boundary += share
        high = (queries * boundary + 50) // 100
        yield name, high - low
        low = high


def _pools(targets, label):
    by_position = {}
    for entry in targets:
        if entry['label'] == label:
            by_position.setdefault(entry['position'], []).append(entry)
    return [by_position[position] for position in _TARGETS[label]]


def _target(pool, n):
    peers = pool[n % len(pool)]
    return peers[(n // len(pool)) % len(peers)]


def _query(store, profile, kind, name, n, pools):
    live, empty, held = (profile[group] for group in ('live', 'empty', 'held'))
    targets = profile['targets']
    if kind == 'standing':
        pool = {'live': live, 'empty': empty, 'held': held}[name]
        seat = pool[n % len(pool)]
        return partial(slips.standing, seat_id=seat, now_ms=1), ()
    label = name if kind == 'act' and name in ('pending', 'objected', 'changed') else 'allow'
    entry = _target(pools[label], n)
    seat, repo, bound = (entry[field] for field in ('seat', 'repo', 'bound'))
    if name == 'other':
        seat = next(other['seat'] for other in targets
                    if other['repo'] == repo and other['seat'] != seat)
    elif name == 'missing':
        seat = empty[0]
        repo = next(other['repo'] for other in targets if other['repo'].startswith('active-a-'))
    ref = ('unbound-query' if name in ('unbound', 'missing') else bound)
    if kind == 'scope':
        kwargs = {'path': 'src/example.py'} if name == 'path' else {'ref': ref}
        return partial(slips.scope, seat_id=seat, repository_id=repo, **kwargs), ()
    root = store / ('changed' if name == 'changed' else 'copies')
    effect = slips.Effect('commit', repo, ref, path=str(root / repo))
    reads = tuple(store / 'copies' / repo / artifact.partition(':')[2]
                  for artifact in entry['artifacts']) if name in _FULL_READS else ()
    return partial(authority.act_entry, seat_id=seat, effect=effect, now_ms=1), reads


def _sample(call):
    wall = time.perf_counter_ns()
    cpu = time.thread_time_ns()
    answer = call()
    cpu = time.thread_time_ns() - cpu
    wall = time.perf_counter_ns() - wall
    return Sample(wall, cpu), answer


def _calibrate():
    start = time.perf_counter_ns()
    total = 0
    for i in range(200_000):
        total ^= i
    return time.perf_counter_ns() - start


def _clocks(samples):
    def quantiles(values):
        values = sorted(values)
        return tuple(values[math.ceil(len(values) * q) - 1] for q in (0.50, 0.99))
    wall50, wall99 = quantiles(sample.wall_ns for sample in samples)
    cpu50, cpu99 = quantiles(sample.cpu_ns for sample in samples)
    return {'wall_p50_ns': wall50, 'wall_p99_ns': wall99,
            'cpu_p50_ns': cpu50, 'cpu_p99_ns': cpu99,
            'wall_cpu_p99_ratio': wall99 / cpu99 if cpu99 else None}


def verdict(wall_p99_ms: float, cpu_p99_ms: float) -> str:
    if wall_p99_ms <= 10:
        return 'within-bound'
    return 'over-bound' if cpu_p99_ms > 10 else 'inconclusive'


def measure(conn, queries: int = 10_000, warmup: int = 1_000) -> BenchResult:
    if queries < 1 or warmup < 0 or conn.in_transaction:
        raise ValueError('need positive queries, nonnegative warmup and an idle seeded connection')
    db = next(path for _, name, path in conn.execute('PRAGMA database_list') if name == 'main')
    if not db:
        raise ValueError('measure needs a file-backed seed')
    store = Path(db).parent
    profile = json.loads((store / 'profile.json').read_bytes())
    pools = {label: _pools(profile['targets'], label) for label in _TARGETS}
    stats = profile['stats']
    schedules, sizes = {}, Counter()
    known_sizes = {}
    for kind in _SHAPES:
        schedule = []
        for name, count in _strata(kind, queries):
            for n in range(count):
                call, files = _query(store, profile, kind, name, n, pools)
                schedule.append((name, call))
                for path in files:
                    if path not in known_sizes:
                        known_sizes[path] = path.stat().st_size
                    sizes[known_sizes[path]] += 1
        schedules[kind] = schedule

    def invoke(kind, call, timed):
        if kind == 'act':
            conn.execute('BEGIN IMMEDIATE')
        try:
            if timed:
                return _sample(partial(call, conn))
            return None, call(conn)
        finally:
            if kind == 'act' and conn.in_transaction:
                conn.rollback()

    clocks, counts, kinds, calibration = {}, {}, {}, []
    load_start = os.getloadavg()
    for kind, schedule in schedules.items():
        _floor(store, 3)
        for i in range(warmup):
            _, call = schedule[i % len(schedule)]
            invoke(kind, call, False)
        realized, samples = Counter(), []
        for n, (name, call) in enumerate(schedule, 1):
            sample, answer = invoke(kind, call, True)
            if kind == 'act' and (answer.outcome, answer.code) != _ACT_OUTCOMES[name]:
                raise ValueError(f'act stratum {name}: unexpected {answer.outcome} {answer.code}')
            if kind == 'standing':
                actual = 'held' if answer.hold is not None else ('live' if answer.slips else 'empty')
                if actual != name:
                    raise ValueError(f'standing stratum {name}: observed {actual}')
            if kind == 'scope' and (answer is None) != (name == 'other'):
                raise ValueError(f'scope stratum {name}: unexpected coverage')
            realized[name] += 1
            samples.append(sample)
            if n % 100 == 0:
                _floor(store, 3)
                calibration.append(_calibrate())
        if any(abs(realized[name] - queries * share / 100) > 1
               for name, share in _SHAPES[kind].items()):
            raise ValueError(f'{kind} stratum counts missed their shares')
        record = _clocks(samples)
        clocks[kind], counts[kind] = record, dict(realized)
        kinds[kind] = verdict(record['wall_p99_ns'] / 1_000_000, record['cpu_p99_ns'] / 1_000_000)
    overall = next(label for label in ('over-bound', 'inconclusive', 'within-bound') if label in kinds.values())
    return BenchResult(clocks, counts, {int(k): v for k, v in stats['file_size_counts'].items()},
                       dict(sizes), {k: tuple(v) for k, v in stats['standing_fanout'].items()},
                       tuple(calibration), load_start, os.getloadavg(), os.cpu_count() or 1,
                       kinds, overall, _LIMITS)


def _warmup_count():
    return 1000


def _atomic_json(out, record):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=out.parent, delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(record, stream, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(tmp, out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--relays', type=int, default=50_000)
    parser.add_argument('--seats', type=int, default=32)
    parser.add_argument('--queries', type=int, default=10_000)
    parser.add_argument('--out', type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        store = args.store.resolve()
        out = args.out.resolve()
        if args.queries < 1 or not out.parent.is_dir():
            raise ValueError('need positive --queries and an existing output directory')
        before = _available_bytes(store)
        start = time.perf_counter_ns()
        stats = seed(store, args.relays, args.seats)
        seed_ns = time.perf_counter_ns() - start
        start = time.perf_counter_ns()
        with closing(sqlite3.connect(store / 'store.sqlite3')) as conn:
            conn.execute('PRAGMA foreign_keys = ON')
            result = measure(conn, queries=args.queries, warmup=_warmup_count())
        run_ns = time.perf_counter_ns() - start
        _atomic_json(out, {**asdict(result), 'seed': asdict(stats), 'seed_ns': seed_ns,
                           'measure_ns': run_ns, 'free_bytes_before': before,
                           'free_bytes_after': _available_bytes(store)})
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return {'within-bound': 0, 'over-bound': 1, 'inconclusive': 3}[result.verdict]


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
