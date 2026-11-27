"""Pure planning of copied 2.9.7 history; no destination writes or live reads.

Non-master decisions map namespace names to ``running`` or ``finished``.
Relay links are zero-based positions, resolved only against earlier rows.
"""
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import uuid
from zoneinfo import ZoneInfo

from relay_engine import addresses, header, identity_import

_ZONE = ZoneInfo('America/Los_Angeles')
_HEADER = re.compile(rb'[A-Z][A-Z0-9_]*:')


@dataclass(frozen=True)
class Listed:
    reason: str
    item: str = ''
    blocking: bool = True


@dataclass(frozen=True)
class SourceRoot:
    copy_dir: Path
    root_uuid: str
    run_id: str
    relays: tuple[dict, ...]
    seat_events: tuple[dict, ...]
    commissions: tuple[dict, ...]
    runs: tuple[dict, ...]
    admission_findings: tuple[dict, ...]
    meta: dict
    manifest: dict
    manifest_sha256: str


def _read_ledger(duplicate):
    """Extract ordered source rows from a disposable ledger directory."""
    conn = sqlite3.connect(duplicate / 'ledger.db')
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def rows(table):
            if table not in tables:
                return ()
            return tuple(dict(r) for r in conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid'))

        meta = {r['key']: r['value'] for r in rows('meta')}
        if not meta.get('root_uuid') or not meta.get('run_id'):
            raise ValueError('source identity missing')
        audit = {name: rows(name) for name in ('seat_events', 'commissions', 'runs', 'admission_findings')}
        return meta, rows('relays'), audit
    finally:
        conn.close()


def _source_root(root, ledger, manifest, manifest_bytes, rendered):
    meta, raw_relays, audit = ledger
    relays = []
    for raw in raw_relays:
        payload, mtime, error = rendered(raw)
        row = {**raw, 'rendered_bytes': payload, 'file_mtime': mtime}
        if error is not None:
            row['file_error'] = error
        relays.append(row)
    return SourceRoot(root, meta['root_uuid'], meta['run_id'], tuple(relays),
                      audit['seat_events'], audit['commissions'], audit['runs'],
                      audit['admission_findings'], meta, manifest, hashlib.sha256(manifest_bytes).hexdigest())


def read_source(copy_dir) -> SourceRoot:
    """Read a staged copy, retaining exact file bytes and source audit rows."""
    root = Path(copy_dir).resolve()
    ledger_names = ('ledger.db', 'ledger.db-wal', 'ledger.db-shm')
    ledger_paths = {'.engine/' + name: name for name in ledger_names}
    manifest_name = '.source-manifest.json'

    def open_regular(path, relative):
        try:
            stream = open(path, 'rb', opener=lambda name, flags: os.open(
                name, flags | os.O_NOFOLLOW | os.O_NONBLOCK))
        except OSError as exc:
            raise ValueError('cannot read copied file: ' + relative) from exc
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('copied path is not a regular file: ' + relative)
        except BaseException:
            stream.close()
            raise
        return stream

    try:
        with open_regular(root / manifest_name, manifest_name) as stream:
            manifest_bytes = stream.read()
        document = json.loads(manifest_bytes)
        canonical = json.dumps(document, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False).encode('utf-8')
        if manifest_bytes != canonical:
            raise ValueError('noncanonical manifest')
        if (not isinstance(document, dict) or document.keys() != {'files'}
                or not isinstance(document['files'], list)):
            raise ValueError('expected a files-only manifest object with a list')
        manifest = {}
        staged_paths = {}
        previous = ''
        for entry in document['files']:
            if not isinstance(entry, dict) or entry.keys() != {'path', 'size', 'sha256'}:
                raise ValueError('invalid manifest entry keys')
            path, size, digest = entry['path'], entry['size'], entry['sha256']
            if not isinstance(path, str) or not path:
                raise ValueError('invalid manifest path')
            relative = Path(path)
            if (relative.is_absolute() or relative.as_posix() != path or path == '.'
                    or '..' in relative.parts or '\x00' in path
                    or path in ledger_names or path == manifest_name):
                raise ValueError('invalid manifest path: ' + path)
            if path <= previous:
                raise ValueError('manifest paths are not strictly increasing: ' + path)
            if (type(size) is not int or size < 0 or not isinstance(digest, str)
                    or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
                raise ValueError('invalid size or digest: ' + path)
            manifest[path] = (size, digest)
            staged_paths[ledger_paths.get(path, path)] = path
            previous = path
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('missing or invalid .source-manifest.json: ' + str(exc)) from exc

    def walk_error(exc):
        relative = Path(exc.filename).relative_to(root).as_posix()
        raise ValueError('cannot list copied folder: ' + relative) from exc

    staged_files = set()
    for directory, folders, files in os.walk(root, onerror=walk_error, followlinks=False):
        for name in folders + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            try:
                mode = os.lstat(path).st_mode
            except OSError as exc:
                raise ValueError('cannot inspect copied path: ' + relative) from exc
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError('unexpected copied entry: ' + relative)
            if relative == manifest_name:
                continue
            if relative not in staged_paths:
                logical_path = '.engine/' + relative if relative in ledger_names else relative
                raise ValueError('unlisted copied file: ' + logical_path)
            staged_files.add(relative)

    if 'ledger.db' not in staged_files:
        raise ValueError('missing copied ledger.db')
    missing = staged_paths.keys() - staged_files
    if missing:
        raise ValueError('missing regular copied file: ' + staged_paths[min(missing)])

    def checked_file(path, relative, destination=None):
        digest = hashlib.sha256()
        size = 0
        try:
            with open_regular(path, relative) as stream:
                mtime = os.fstat(stream.fileno()).st_mtime
                for block in iter(lambda: stream.read(128 * 1024), b''):
                    size += len(block)
                    digest.update(block)
                    if destination is not None:
                        destination.write(block)
        except OSError as exc:
            raise ValueError('cannot read copied bytes: ' + relative) from exc
        if manifest[relative] != (size, digest.hexdigest()):
            raise ValueError('source manifest mismatch: ' + relative)
        return mtime

    # SQLite may write sidecars even for mode=ro. Only open the private
    # duplicate, populated with the same ledger bytes checked against the list.
    with tempfile.TemporaryDirectory() as temporary:
        duplicate = Path(temporary)
        for staged, logical in staged_paths.items():
            if logical in ledger_paths:
                with (duplicate / staged).open('wb') as destination:
                    checked_file(root / staged, logical, destination)
            else:
                checked_file(root / staged, logical)
        ledger = _read_ledger(duplicate)

    def rendered(row):
        relative = row['rendered_path']
        path = Path(relative)
        if relative in manifest:
            with io.BytesIO() as payload:
                mtime = checked_file(root / path, relative, payload)
                return payload.getvalue(), mtime, None
        if path.is_absolute() or not (root / path).resolve().is_relative_to(root):
            return None, None, 'rendered path escapes copy'
        return None, None, 'rendered path missing from source manifest'

    return _source_root(root, ledger, manifest, manifest_bytes, rendered)


def created_at_ms(stamp: str, rank: int, file_mtime: float | None) -> int | Listed:
    """Resolve a wall second, refusing gaps and an unproved repeated hour."""
    if type(rank) is not int or not 0 <= rank <= 999:
        return Listed('rank outside 0..999', str(rank))
    try:
        wall = datetime.strptime(stamp, '%Y%m%d-%H%M%S')
        if wall.strftime('%Y%m%d-%H%M%S') != stamp:
            return Listed('invalid stamp', stamp)
        candidates = {int(wall.replace(tzinfo=_ZONE, fold=fold).timestamp())
                      for fold in (0, 1)}
        candidates = {t for t in candidates
                      if datetime.fromtimestamp(t, _ZONE).replace(tzinfo=None) == wall}
        if not candidates:
            return Listed('wall time does not exist', stamp)
        if len(candidates) > 1:
            if file_mtime is None or int(file_mtime) not in candidates:
                return Listed('repeated hour needs matching file mtime', stamp)
            second = int(file_mtime)
        else:
            second = next(iter(candidates))
        return second * 1000 + rank
    except (ValueError, TypeError, OverflowError, OSError):
        return Listed('invalid stamp or file mtime', str(stamp))


def canonical_role(role: str) -> tuple[str, str | None] | None:
    try:
        parsed = addresses.parse_address('team.' + role)
        return parsed.role, None
    except addresses.AddressRefused as exc:
        if exc.canonical is not None:
            return exc.canonical, role
        return None


def _party(token):
    if token == 'operator':
        return token
    if not isinstance(token, str) or token.count('.') != 1 or '@' in token:
        return None
    team, role = token.split('.')
    mapped = canonical_role(role)
    if mapped is None:
        return None
    canonical = team + '.' + mapped[0]
    try:
        addresses.parse_address(canonical)
    except addresses.AddressRefused:
        return None
    return canonical


def _foreign(token):
    return isinstance(token, str) and ('@' in token or token.count('.') > 1)


def _tokens(raw):
    if not raw:
        return ()
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            decoded = None
        if isinstance(decoded, list):
            return tuple(decoded)
        return tuple(t.strip() for t in raw.split(',') if t.strip())
    return tuple(raw)


def _split_document(document):
    """Keep prefix and legacy header verbatim, without using the v3 parser."""
    offset = 0
    started = False
    for line in document.splitlines(keepends=True):
        if _HEADER.match(line):
            started = True
        elif started:
            end = offset + len(line) if not line.strip() else offset
            return document[:end], document[end:]
        offset += len(line)
    if started:
        return document, b''
    raise ValueError('missing legacy header boundary')


@dataclass(frozen=True)
class Provenance:
    root_uuid: str
    seq: int
    stamp: str
    zone: str
    offset: int
    rendered_path: str
    origin: str
    headers: dict
    file_sha256: str
    original_phase: str | None
    foreign: tuple[str, ...]


@dataclass(frozen=True)
class PlannedRelay:
    created_at: int
    kind: str
    verdict: str | None
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    body: bytes
    header_bytes: bytes
    header_sha256: str
    parent: int | None
    parent_spelling: str | None
    answers: tuple[int, ...]
    provenance: tuple[Provenance, ...]


@dataclass(frozen=True)
class RelayPlan:
    sources: tuple[SourceRoot, ...]
    relays: tuple[PlannedRelay, ...]
    listed: tuple[Listed, ...]
    answer_items: tuple[tuple[int, str, str], ...]
    refused: int
    deduplicated: int
    spellings: dict = field(default_factory=dict)


def plan_relays(sources: Sequence[SourceRoot]) -> RelayPlan:
    sources = tuple(sources)
    ordered = []
    for n, source in enumerate(sources):
        for row in source.relays:
            converted = created_at_ms(row['stamp'], 0, row['file_mtime'])
            finding = converted if isinstance(converted, Listed) else None
            if finding is not None:
                if finding.reason == 'repeated hour needs matching file mtime':
                    wall = datetime.strptime(row['stamp'], '%Y%m%d-%H%M%S')
                    converted = int(wall.replace(tzinfo=_ZONE, fold=0).timestamp()) * 1000
                else:
                    converted = None
            # A missing conversion has a diagnostic sort position only.
            sort_second = 0 if converted is None else converted
            ordered.append((sort_second, n, row['seq'], source, row, finding, converted))
    ordered.sort(key=lambda entry: entry[:3])
    listed, answer_items = [], []
    rank_anchors, ranks = {}, Counter()
    admitted, first_eligible = {}, {}
    source_paths, source_roots = defaultdict(dict), defaultdict(set)
    refused = deduplicated = 0
    for occurrence, (_, _, seq, source, row, time_finding, converted) in enumerate(ordered):
        stamp = row['stamp']
        spelling = row['rendered_path']
        document = row['body']
        if isinstance(document, str):
            document = document.encode()
        digest = hashlib.sha256(document).hexdigest()
        key = digest, spelling
        source_paths[spelling][source.root_uuid] = key
        source_roots[key].add(source.root_uuid)
        rank = None
        moment = None
        if converted is not None:
            if key not in rank_anchors:
                rank_anchors[key] = (converted, ranks[converted])
                ranks[converted] += 1
            ranked_second, rank = rank_anchors[key]
            moment = ranked_second + rank
        failure = None
        if row['rendered_bytes'] != document or digest != row['body_sha256']:
            failure = Listed('source bytes differ', spelling)
        elif rank is not None and rank > 999:
            failure = Listed('rank outside 0..999', str(rank))
        elif time_finding is not None:
            if time_finding.reason == 'repeated hour needs matching file mtime':
                listed.append(time_finding)
            else:
                failure = time_finding
        try:
            raw_headers = json.loads(row['headers_json'])
            if not isinstance(raw_headers, dict):
                raise ValueError('headers must be an object')
            prefix, body = _split_document(document)
        except (ValueError, TypeError) as exc:
            failure = Listed('invalid source header: ' + str(exc), spelling)
        if failure is not None:
            listed.append(failure)
            refused += 1
            continue
        sender = _party(row['from_seat'])
        if sender is None:
            listed.append(Listed('sender must be seat or operator', row['from_seat']))
            refused += 1
            continue
        to, cc, foreign = [], [], []
        for field_name, destination in (('to_seats', to), ('cc_seats', cc)):
            for token in _tokens(row.get(field_name)):
                if _foreign(token):
                    if token not in foreign:
                        foreign.append(token)
                    listed.append(Listed('display-only, link pending a foreign-party representation', token, False))
                    continue
                party = _party(token)
                if party is None:
                    listed.append(Listed('display-only recipient', token, False))
                elif party in to or party in cc:
                    listed.append(Listed('duplicate recipient', token, False))
                else:
                    destination.append(party)
        kind = 'SITREP' if row['phase'] == 'BOOT' else row['phase']
        if kind not in header.KINDS:
            listed.append(Listed('unknown phase', row['phase']))
            refused += 1
            continue
        if row['phase'] == 'BOOT':
            listed.append(Listed('BOOT imported as SITREP', spelling, False))
        verdict_key = kind.replace('-', '_') + '_VERDICT'
        verdict = raw_headers.get(verdict_key)
        if verdict not in header.VERDICTS.get(kind, ()):
            if verdict is not None:
                listed.append(Listed('verdict dropped', spelling + ': ' + str(verdict), False))
            verdict = None
        offset = int(datetime.fromtimestamp(converted / 1000, _ZONE).utcoffset().total_seconds())
        wall = datetime.strptime(stamp, '%Y%m%d-%H%M%S')
        if time_finding is None and wall.replace(tzinfo=_ZONE, fold=0).utcoffset() != wall.replace(tzinfo=_ZONE, fold=1).utcoffset():
            listed.append(Listed('offset resolved by file mtime', spelling, False))
        provenance = Provenance(source.root_uuid, seq, stamp, 'America/Los_Angeles', offset,
                                spelling, row['origin'], raw_headers, digest,
                                'BOOT' if row['phase'] == 'BOOT' else None, tuple(foreign))
        if key in admitted:
            previous = admitted[key]
            admitted[key] = replace(previous, provenance=previous.provenance + (provenance,))
            deduplicated += 1
            continue
        admitted[key] = PlannedRelay(moment, kind, verdict, sender, tuple(to), tuple(cc), row.get('subject') or '',
                                     body, prefix, hashlib.sha256(prefix).hexdigest(), None, None,
                                     (), (provenance,))
        first_eligible[key] = occurrence
    # Emit in rank order while keeping eligibility tied to the actual surviving occurrence.
    admitted_identities = [key for key in rank_anchors if key in admitted]
    relays = [admitted[key] for key in admitted_identities]
    positions = {key: index for index, key in enumerate(admitted_identities)}
    prefixes = defaultdict(set)
    for source in sources:
        canonical = source.meta.get('canonical_root')
        if not isinstance(canonical, str) or not canonical.startswith('/'):
            continue
        canonical = canonical.rstrip('/')
        parts = canonical.strip('/').split('/')
        for n in range(len(parts)):
            prefixes['/'.join(parts[n:]) + '/'].add(source.root_uuid)
        prefixes[canonical + '/'].add(source.root_uuid)
    spellings = Counter()
    for index, relay in enumerate(relays):
        parent, parent_spelling, answers = None, None, []
        entries = _tokens(relay.provenance[0].headers.get('IN_REPLY_TO'))
        reply_identity = admitted_identities[index]
        holding_roots = source_roots[reply_identity]
        for entry in entries:
            candidate_roots = set()
            targets = set()
            if entry in source_paths:
                way = 'bare'
                candidates = source_paths[entry]
                for root in holding_roots:
                    if root in candidates:
                        targets.add(candidates[root])
                        candidate_roots.add(root)
                    else:
                        targets.update(candidates.values())
                        candidate_roots.update(candidates)
            else:
                matches = [prefix for prefix in prefixes if entry.startswith(prefix)]
                if matches:
                    way = 'prefixed'
                    prefix = max(matches, key=len)
                    named_roots = prefixes[prefix]
                    if len(named_roots) > 1:
                        # A prefix tie is ambiguous even if only one has a file.
                        candidate_roots.update(named_roots)
                    else:
                        root = next(iter(named_roots))
                        candidate = source_paths.get(entry[len(prefix):], {}).get(root)
                        if candidate is not None:
                            targets.add(candidate)
                            candidate_roots.add(root)
                else:
                    way = 'neither'
            ambiguous = len(targets) > 1 or (way == 'prefixed' and len(candidate_roots) > 1)
            identity = next(iter(targets), None) if not ambiguous else None
            target = positions.get(identity)
            if target is not None and (target >= index
                                       or first_eligible[identity] >= first_eligible[reply_identity]):
                target = None
            outcome = 'ambiguous' if ambiguous else 'resolved' if target is not None else 'unresolved'
            spellings[way, outcome] += 1
            if ambiguous:
                answer_items.append((index, entry, 'ambiguous'))
                listed.append(Listed('ambiguous reference', entry + ': ' +
                                     ', '.join(sorted(candidate_roots)), False))
                continue
            if target is not None and parent is None:
                parent, parent_spelling = target, entry
                continue
            if target is None:
                answer_items.append((index, entry, 'unresolved'))
            elif target == parent or target in answers:
                continue
            elif relay.sender not in relays[target].to + relays[target].cc:
                answer_items.append((index, entry, 'not-received'))
            else:
                answers.append(target)
        if entries and parent is None:
            spelling = relay.provenance[0].rendered_path
            listed.append(Listed('parent unresolved', spelling + ': ' + ', '.join(entries), False))
        relays[index] = replace(relay, parent=parent, parent_spelling=parent_spelling, answers=tuple(answers))
    return RelayPlan(sources, tuple(relays), tuple(listed), tuple(answer_items), refused, deduplicated, dict(spellings))


@dataclass(frozen=True)
class IdentityPlan:
    instances: tuple[identity_import.ImportedTeamInstance, ...]
    seats: tuple[identity_import.ImportedSeat, ...]
    top_seat_id: str | None
    aliases: dict
    display_only: tuple[str, ...]
    listed: tuple[Listed, ...]
    decisions: dict
    seat_map: dict
    crosschecks: tuple[tuple[str, str, str | None], ...]


def _stable_id(label, timestamp):
    # Version and variant are set explicitly for Python 3.11 compatibility.
    number = int.from_bytes(hashlib.sha256(label.encode()).digest()[:16], 'big')
    number = (number & ((1 << 80) - 1)) | ((timestamp & ((1 << 48) - 1)) << 80)
    number = (number & ~(15 << 76)) | (7 << 76)
    number = (number & ~(3 << 62)) | (2 << 62)
    return str(uuid.UUID(int=number))


def plan_identities(sources, decisions) -> IdentityPlan:
    """Map namespaces and aliases, delegating all top-seat selection to Platform."""
    sources = tuple(sources)
    relay_plan = plan_relays(sources)
    evidence = defaultdict(lambda: {'roles': set(), 'times': [], 'levels': set()})
    aliases, display, listed, seat_map = Counter(), [], [], {}
    for source in sources:
        for row in source.relays:
            for token in (row['from_seat'],) + _tokens(row.get('to_seats')) + _tokens(row.get('cc_seats')):
                canonical = _party(token)
                if canonical == 'operator':
                    continue
                if canonical is None:
                    if token not in display:
                        display.append(token)
                    continue
                team, role = canonical.split('.')
                info = evidence[team]
                info['roles'].add(role)
                level = role.split('-')[0] if '-' in role else 'pair'
                info['levels'].add(level)
                literal_role = token.split('.')[1]
                if literal_role != role:
                    aliases[team, literal_role] += 1
        # A namespace without parties still needs a choice and level evidence.
        for run in source.runs:
            evidence[run['run_id']]
    for relay in relay_plan.relays:
        for team in {token.split('.')[0] for token in (relay.sender,) + relay.to + relay.cc if token != 'operator'}:
            evidence[team]['times'].append(relay.created_at)
    parents = {}
    for source in sources:
        by_seq = {r['seq']: r for r in source.relays}
        for commission in source.commissions:
            row = by_seq.get(commission['dispatch_seq'])
            parent = _party(row['from_seat']) if row else None
            child = commission['child_run_id']
            if parent and parent != 'operator' and parent.split('.')[0] in evidence:
                namespace = parent.split('.')[0]
                if child in parents and parents[child] != namespace:
                    listed.append(Listed('conflicting commission parents', child))
                parents[child] = namespace
    roots = sorted({source.root_uuid for source in sources})

    def identity_id(kind, name, opened, role=None):
        parts = [kind, roots, name, opened]
        if role is not None:
            parts.append(role)
        return _stable_id(json.dumps(parts, ensure_ascii=False, separators=(',', ':')), opened)

    instance_ids = {name: identity_id('instance', name, min(info['times'], default=0))
                    for name, info in evidence.items()}
    instances, seats = [], []
    for name, info in sorted(evidence.items()):
        levels = info['levels']
        if not levels:
            listed.append(Listed('team level unresolved', name))
            continue
        level = max(levels, key=('pair', 'orchestrator', 'domain', 'master').index)
        if not info['times']:
            listed.append(Listed('team has no importable relay', name))
            continue
        opened, last = min(info['times']), max(info['times'])
        choice = 'running' if level == 'master' else decisions.get(name)
        if choice not in ('running', 'finished'):
            listed.append(Listed('team needs finished-or-running choice', name))
        closed = last if choice == 'finished' else None
        instance = identity_import.ImportedTeamInstance(instance_ids[name], name, level,
                    instance_ids.get(parents.get(name)), opened, closed)
        instances.append(instance)
        for role in sorted(info['roles']):
            seat = identity_import.ImportedSeat(identity_id('seat', name, opened, role), instance.id, role)
            seats.append(seat)
            for source in sources:
                seat_map[source.root_uuid, name, role] = seat.id
    top = None
    try:
        top = identity_import.derive_top_seat(instances, seats)
    except identity_import.IdentityImportRefused as exc:
        listed.append(Listed(exc.reason, str(exc.item)))
    canonical_seats = {instance.name + '.' + seat.role: seat.id
                       for instance in instances for seat in seats if seat.team_instance_id == instance.id}
    crosschecks = []
    for source in sources:
        recorded = source.meta.get('top_seat')
        if recorded:
            matched = canonical_seats.get(_party(recorded))
            crosschecks.append((source.root_uuid, recorded, matched))
            if matched is None or matched != top:
                listed.append(Listed('recorded top seat disagrees', recorded))
    return IdentityPlan(tuple(instances), tuple(seats), top, dict(aliases), tuple(display),
                        tuple(listed), dict(decisions), seat_map, tuple(crosschecks))


class Report:
    """Computed check-step data; rendering returns text and emits nothing."""
    def __init__(self, relays: RelayPlan, identities: IdentityPlan | None = None, *, store_id=None, project_id=None):
        self.relays = relays
        self.identities = identities
        self.store_id = store_id
        self.project_id = project_id
        self.counts = {'spellings': dict(relays.spellings), 'imported': len(relays.relays), 'deduplicated': relays.deduplicated,
                       'refused': relays.refused,
                       'display_only': sum(len({token for p in r.provenance
                           for key in ('TO', 'CC') for token in _tokens(p.headers.get(key))
                           if token not in ('operator', 'none') and _party(token) is None})
                           for r in relays.relays)}
        self.source_manifests = {s.root_uuid: s.manifest for s in relays.sources}
        self.root_to_project = {s.root_uuid: project_id for s in relays.sources}
        self.seat_map = {} if identities is None else identities.seat_map
        self.audit_rows = {s.root_uuid: {'seat_events': s.seat_events,
                           'admission_findings': s.admission_findings, 'commissions': s.commissions}
                           for s in relays.sources}

    def unresolved(self) -> list[str]:
        items = self.relays.listed + (() if self.identities is None else self.identities.listed)
        return [item.reason + ': ' + item.item for item in items if item.blocking]

    def answers_listed(self) -> list[tuple[int, str, str]]:
        return list(self.relays.answer_items)

    def shown(self) -> list[str]:
        items = self.relays.listed + (() if self.identities is None else self.identities.listed)
        return [item.reason + ': ' + item.item for item in items if not item.blocking] + [
            f'answer {position}: {spelling}: {reason}'
            for position, spelling, reason in self.answers_listed()]

    def render(self) -> str:
        lines = [f"{self.counts['imported']} imported; {self.counts['deduplicated']} deduplicated; "
                 f"{self.counts['refused']} refused; {self.counts['display_only']} display-only",
                 f'store_id={self.store_id}; project_id={self.project_id}',
                 'sources=' + repr(self.source_manifests), 'projects=' + repr(self.root_to_project),
                 'seats=' + repr(self.seat_map), 'audit=' + repr(self.audit_rows)]
        lines.extend(f'source {source.root_uuid}: manifest sha256 {source.manifest_sha256}, {len(source.manifest)} files'
                     for source in self.relays.sources)
        lines.append('spellings: ' + ', '.join(f'{way}/{outcome}={count}'
                     for (way, outcome), count in sorted(self.counts['spellings'].items()) if count))
        if self.identities is not None:
            lines.extend(['choices=' + repr(self.identities.decisions), 'top_seat=' + repr(self.identities.top_seat_id),
                          'crosschecks=' + repr(self.identities.crosschecks), 'aliases=' + repr(self.identities.aliases),
                          'display_only=' + repr(self.identities.display_only)])
        for item in self.relays.listed + (() if self.identities is None else self.identities.listed):
            lines.append(item.reason + ': ' + item.item)
        lines.extend(f'answer {position}: {spelling}: {reason}' for position, spelling, reason in self.answers_listed())
        return '\n'.join(lines) + '\n'
