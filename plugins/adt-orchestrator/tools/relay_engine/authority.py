"""Read-only current-use authority; admission findings are not permissions."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

from relay_engine import cycles, errors, rules
from relay_engine.paths import Root, _parts, _read_regular_at, _open_parent


CRITICAL = ('PHASE', 'FROM', 'TO', 'DISPATCH_ID', 'PARENT_DISPATCH_ID',
            'DESIGN_LOCK_ID', 'DESIGN_DOC_ID', 'PLAN_LOCK_ID',
            'DESIGN_ARTIFACT', 'DESIGN_SHA256', 'PLAN_ARTIFACT', 'PLAN_SHA256',
            'PLAN_SOURCE_REPO', 'PLAN_SOURCE_COMMIT') + rules.XROOT_TRIGGERS


@dataclass(frozen=True)
class Evidence:
    seq: int
    path: str
    text: str
    fields: dict
    superseded: bool
    engine_authored: bool
    header_conflict: bool = False


def authorizations(ledger, seat: str, action: str):
    """The single record evidence source, including malformed would-be grants.

    Denied and misaddressed records remain visible so check can distinguish a
    denied grant from an unknown path. No persisted finding is consulted.
    """
    if action not in {'implement', 'merge'}:
        raise errors.error_for('E-HEADER', field='ACTION')
    superseded = {r[0] for r in ledger.execute(
        'SELECT target_seq FROM supersession_edges WHERE applied=1')}
    authored = {r[0] for r in ledger.execute(
        'SELECT boot_relay_seq FROM seat_events WHERE boot_relay_seq IS NOT NULL')}
    authored.update(r[0] for r in ledger.execute('SELECT notice_seq FROM routed_notices'))
    for seq, path, raw, sender, targets, phase, dispatch, parent in ledger.execute(
            'SELECT seq,rendered_path,body,from_seat,to_seats,phase,dispatch_id,'
            'parent_dispatch_id FROM relays ORDER BY seq'):
        text = bytes(raw).decode('utf-8')
        clean = rules.a5_sanitized_text(text)
        fields = {}
        for line in rules.a5_split_lines(clean):
            match = re.match(r'^([A-Z][A-Z0-9_-]*):[ \t]*(.*)$', line)
            if match:
                fields.setdefault(match[1], match[2].strip())
        envelope = {'FROM': sender, 'TO': ', '.join(json.loads(targets or '[]')),
                    'PHASE': phase, 'DISPATCH_ID': dispatch,
                    'PARENT_DISPATCH_ID': parent or ''}
        conflict = False
        for key, value in envelope.items():
            actual = fields.get(key, '')
            if key == 'TO':
                differs = rules.split_addresses(actual) != rules.split_addresses(value)
            elif key == 'FROM':
                differs = rules.normalized_addr(actual) != rules.normalized_addr(value)
            else:
                differs = actual != value
            conflict |= differs
        # The authenticated header block itself must remain visible, even if
        # another body block repeats the same values outside a fence.
        started = False
        for raw_line, clean_line in zip(rules.a5_split_lines(text), rules.a5_split_lines(clean)):
            match = re.match(r'^([A-Z][A-Z0-9_]*):[ \t]*', raw_line)
            if not started and match is None:
                continue
            if match is None:
                break
            started = True
            if match[1] in envelope and raw_line != clean_line:
                conflict = True
        fields.update(envelope)
        yield Evidence(seq, path, text, fields, seq in superseded,
                       seq in authored, conflict)


def _occurrences(text, key):
    pattern = re.compile(r'^' + re.escape(key) + r':[ \t]*(.*)$')
    return [match[1].strip() for line in rules.a5_split_lines(rules.a5_sanitized_text(text))
            if (match := pattern.match(line))]


def _conflict(item):
    return item.header_conflict or any(
        len(set(_occurrences(item.text, key))) > 1
        or _occurrences(item.text, key) != rules.h27_occurrences(item.text, key)
        for key in CRITICAL)


def _sender(item):
    addresses = rules.split_addresses(item.fields.get('FROM'))
    return addresses[0] if len(addresses) == 1 else None


def _operational_lines(text):
    return [re.sub(r'`[^`]*`', '', line)
            for line in rules.a5_split_lines(rules.a5_sanitized_text(text))]


def _token(text, token):
    return any(line.rstrip(' \t') == token for line in _operational_lines(text))


def _verdict_lines(item, merge=False):
    phase = item.fields.get('PHASE', '')
    keys = (phase.replace('-', '_')+'_VERDICT', phase+' VERDICT', 'VERDICT')
    if merge:
        keys += ('MERGE_AUTHORIZATION', 'HUMAN_MERGE_AUTHORIZATION', 'MERGE_APPROVED')
    values = []
    for line in _operational_lines(item.text):
        match = re.match(r'^[ \t]*([^:]+):[ \t]*(.*)$', line)
        if match and match[1].strip().upper() in keys:
            values.append((match[1].strip().upper(), match[2].strip().lower()))
    return values


def _word(value):
    return value.split()[0].rstrip('.,;:!?') if value.split() else ''


def _approved(item):
    values = {_word(value) for _, value in _verdict_lines(item)}
    return len(values) == 1 and values <= {'approve', 'approved'}


def _same_seat(left, right):
    return (left == right or (left is not None and right is not None
            and rules.address_owner(left) == rules.address_owner(right)
            and rules.canonical_role(rules.address_role(left)) == rules.canonical_role(rules.address_role(right))))


def _parent(rows, item, phase, sender):
    matches = [row for row in rows if row.seq < item.seq
               and row.fields.get('DISPATCH_ID') == item.fields.get('PARENT_DISPATCH_ID')
               and row.fields.get('PHASE') == phase and _same_seat(_sender(row),sender)]
    return max(matches, key=lambda row: row.seq, default=None)


def _lineage(rows, grant, seat, action, reasons):
    lineage = [grant]
    sender = _sender(grant)
    direct = rules.from_is_direct_authority(grant.fields)
    owner = rules.address_owner(seat)
    planner = owner+'.planner' if owner else ''
    if grant.engine_authored or not (direct or (action == 'implement' and _same_seat(sender,planner))):
        reasons.add('authority.issuer-not-permitted')
        return lineage
    if direct:
        # A direct grant supplies its own authority, independent of ancestors.
        return lineage
    if 'deviation-present' in _occurrences(grant.text, 'SCOPE_DIFF_RESULT'):
        reasons.add('authority.issuer-not-permitted')
    review = _parent(rows, grant, 'PLAN-REVIEW', seat)
    if review is None:
        reasons.add('authority.lineage-incomplete');return lineage
    lineage.append(review)
    if not _approved(review):reasons.add('authority.lineage-incomplete')
    plan = _parent(rows, review, 'PLAN', planner)
    if plan is None:
        reasons.add('authority.lineage-incomplete');return lineage
    lineage.append(plan)
    if seat not in rules.split_addresses(plan.fields.get('TO')):
        reasons.add('authority.lineage-incomplete')
    lock = plan.fields.get('DESIGN_LOCK_ID')
    if lock and not rules.xroot_design_engaged(plan.text):
        design_review = _parent(rows,plan,'DESIGN-REVIEW',seat)
        if design_review is None:
            reasons.add('authority.lineage-incomplete');return lineage
        lineage.append(design_review)
        design = _parent(rows,design_review,'DESIGN',planner)
        if not _approved(design_review) or design_review.fields.get('DESIGN_DOC_ID') != lock:
            reasons.add('authority.lineage-incomplete')
        if design is None:
            reasons.add('authority.lineage-incomplete');return lineage
        lineage.append(design)
        if design.fields.get('DESIGN_DOC_ID') != lock:
            reasons.add('authority.lineage-incomplete')
    return lineage


def _declarations(item, reasons):
    declarations = {}
    for digest, locator, directory in rules.A5_LOCK_PAIRS:
        fields = rules.a5_field_occurrences(item.text)
        digests, locators = fields[digest], fields[locator]
        if digest == 'DESIGN_SHA256' and rules.xroot_design_engaged(item.text):
            continue  # The pinned source path is checked by _external_evidence.
        if not digests and not locators:continue
        if (len(set(digests)) != 1 or len(set(locators)) != 1
                or rules.A5_SHA256_RE.fullmatch(digests[0]) is None
                or not rules.a5_stem_is_bare(locators[0])):
            reasons.add('authority.could-not-evaluate')
            continue
        declarations[f'{directory}/{locators[0]}.md'] = digests[0]
    for key in ('PLAN_LOCK_ID', 'DESIGN_LOCK_ID'):
        # Foreign lock locators belong to the pinned Git source, not this root.
        if any(rules.h27_occurrences(item.text, field)
               for field in ('PLAN_SOURCE_REPO', 'PLAN_SOURCE_COMMIT')):
            continue
        if key == 'DESIGN_LOCK_ID' and rules.xroot_design_engaged(item.text):
            continue
        value = item.fields.get(key, '')
        locator, separator, digest = value.partition(' @ ')
        if '/' not in locator and not locator.endswith('.md'):
            continue  # A symbolic lock id is not an artifact declaration.
        if digest.startswith('sha256 '):
            digest = digest[len('sha256 '):]
        if (not separator or not rules.xroot_valid_relpath(locator)
                or rules.A5_SHA256_RE.fullmatch(digest) is None):
            reasons.add('authority.could-not-evaluate')
            continue
        if locator in declarations and declarations[locator] != digest:
            reasons.add('authority.header-conflict')
        declarations[locator] = digest
    return declarations


def _required_artifacts(rows,lineage,seat,reasons):
    required = {}
    grant = lineage[0]
    direct = rules.from_is_direct_authority(grant.fields)
    base_lineage = lineage if direct else lineage[1:]
    for row in sorted(base_lineage,key=lambda row:row.seq):
        required.update(_declarations(row,reasons))
    if not direct:
        for locator, digest in _declarations(grant, reasons).items():
            if required.get(locator) != digest:
                reasons.add('authority.artifact-changed')
    base = dict(required)
    for origin in lineage:
        phase = origin.fields.get('PHASE')
        if phase not in {'PLAN','DESIGN'}:continue
        key = 'PLAN_LOCK_ID' if phase=='PLAN' else 'DESIGN_DOC_ID'
        lock = origin.fields.get(key)
        if not lock:continue
        origin_declarations = _declarations(origin,reasons)
        for carrier in rows:
            if (carrier.seq <= origin.seq or carrier.fields.get('PHASE') != phase
                    or not _same_seat(_sender(carrier),_sender(origin)) or carrier.fields.get(key) != lock
                    or carrier.superseded or _conflict(carrier) or carrier.engine_authored):continue
            ignored = set()
            declarations = _declarations(carrier,ignored)
            if ignored:continue
            amendments = {path:digest for path,digest in declarations.items()
                          if any(re.fullmatch(re.escape(path0[:-3])+r'-(?:erratum|supplement|amendment)-[0-9]+\.md',path)
                                 for path0 in origin_declarations) and path not in base}
            if not amendments:continue
            matches = []
            for review in rows:
                if (review.seq > carrier.seq and review.fields.get('PHASE') == phase+'-REVIEW'
                        and _same_seat(_sender(review),seat)
                        and review.fields.get('PARENT_DISPATCH_ID') == carrier.fields.get('DISPATCH_ID')):
                    review_reasons=set()
                    review_declarations=_declarations(review,review_reasons)
                    if not review_reasons and all(review_declarations.get(p)==d for p,d in amendments.items()):
                        matches.append(review)
            effective=max(matches,key=lambda row:row.seq,default=None)
            if (effective is not None and _approved(effective) and not effective.superseded
                    and not effective.engine_authored and not _conflict(effective)):
                required.update(amendments)
    return required


def read_artifact(root, locator):
    """Resolve a root/sprint artifact, reading a regular file without FIFO waits."""
    _parts(locator)
    snapshots = []
    for base in dict.fromkeys((root, root.parent.parent)):
        # Root paths are canonical daemon roots; every artifact component is
        # opened relative to a held descriptor, with no symlink following.
        with Root(base) as directory:
            try:
                parent, name = _open_parent(directory.dirfd, locator)
                try:
                    snapshots.append(_read_regular_at(parent, name))
                finally:
                    os.close(parent)
            except FileNotFoundError:
                continue
    if not snapshots:
        raise FileNotFoundError(locator)
    if any(data != snapshots[0] for data in snapshots[1:]):
        raise OSError('artifact locations disagree')
    return snapshots[0]


def _external_evidence(root, lineage, reasons):
    # One deadline covers every Git query across every declared external edge.
    token = rules._XROOT_BUDGET.set((time.monotonic()+5, []))
    seen = set()
    try:
        for item in lineage:
            if rules.xroot_design_engaged(item.text):
                declaration = rules.xroot_declaration(item.fields,item.text)
                identity = tuple((key,declaration.get(key)) for key in rules.XROOT_REQUIRED)
                if identity in seen:
                    continue
                seen.add(identity)
                if any(not declaration.get(key) for key in rules.XROOT_REQUIRED):
                    reasons.add('authority.could-not-evaluate')
                    continue
                try:
                    rules.xroot_verify(root,declaration)
                except rules.XrootAxisError as exc:
                    if exc.axis == 'byte' and 'does not match' in exc.detail:
                        reasons.add('authority.artifact-changed')
                    elif exc.axis in ('authority','authority-population','ambiguity'):
                        reasons.add('authority.lineage-incomplete')
                    else:
                        reasons.add('authority.could-not-evaluate')
            if any(rules.h27_occurrences(item.text,key) for key in ('PLAN_SOURCE_REPO','PLAN_SOURCE_COMMIT')):
                result=rules.LintResult()
                rules.xroot_plan_gate(root,root/item.path,item.fields,item.text,result)
                if result.errors:
                    reasons.add('authority.could-not-evaluate')
        if rules._XROOT_BUDGET.get()[1]:
            reasons.add('authority.could-not-evaluate')
    except (OSError,ValueError,KeyError):
        reasons.add('authority.could-not-evaluate')
    finally:
        rules._XROOT_BUDGET.reset(token)


def check(ledger, root, relay_path: str, seat: str, action: str) -> dict:
    try:_parts(relay_path)
    except (TypeError,ValueError):
        raise errors.error_for('E-HEADER', field='RELAY_PATH')
    rows = tuple(authorizations(ledger,seat,action))
    grant = next((row for row in rows if row.path==relay_path),None)
    if grant is None:raise errors.error_for('E-HEADER', field='RELAY_PATH')
    reasons = set()
    targets = rules.split_addresses(grant.fields.get('TO'))
    if targets != [seat] or not rules.is_implementer_address(seat):
        reasons.add('authority.not-addressee')
    verdict_lines = _verdict_lines(grant, merge=action == 'merge')
    verdicts = [_word(value) for _, value in verdict_lines]
    if action=='merge':
        positives=[rules.merge_authorization_positive(key, value) for key, value in verdict_lines]
        shape=(grant.fields.get('PHASE')=='MERGE-GATE'
               and (_token(grant.text, 'DISPATCH MERGE') or any(positives)))
    else:
        positives=[word in {'approve', 'approved', 'granted', 'yes'} for word in verdicts]
        shape=grant.fields.get('PHASE')=='IMPL' and _token(grant.text, 'DISPATCH IMPL')
    if len(set(verdicts))>1:reasons.add('authority.verdict-conflict')
    if any(not positive for positive in positives):reasons.add('authority.denied')
    if not shape:reasons.add('authority.shape-absent')
    lineage=_lineage(rows,grant,seat,action,reasons)
    for row in lineage:
        if _conflict(row):reasons.add('authority.header-conflict')
        if row.superseded:reasons.add('authority.superseded')
        if cycles.state(ledger,row.fields.get('DISPATCH_ID',''))=='closed':
            reasons.add('authority.cycle-closed')
    required=_required_artifacts(rows,lineage,seat,reasons)
    root_path=Path(root.path if isinstance(root,Root) else root)
    if not root_path.is_dir() or ledger.execute("SELECT value FROM meta WHERE key='run_id'").fetchone() is None:
        reasons.add('authority.could-not-evaluate')
    else:
        _external_evidence(root_path,lineage,reasons)
        for locator,digest in sorted(required.items()):
            try:actual=hashlib.sha256(read_artifact(root_path,locator)).hexdigest()
            except (OSError,ValueError):reasons.add('authority.could-not-evaluate');continue
            if actual!=digest:reasons.add('authority.artifact-changed')
    return {'allowed':not reasons,'reasons':sorted(reasons),'relay':relay_path,
            'seat':seat,'action':action,'checked_at':datetime.now().strftime('%Y%m%d-%H%M%S')}
