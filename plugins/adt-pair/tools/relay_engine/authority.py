"""Read-only current-use authority; admission findings are not permissions."""
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass
from datetime import datetime
import hashlib
import html
import unicodedata
import json
import os
from pathlib import Path
import re
import time

from relay_engine import cycles, envelope, errors, rules
from relay_engine.paths import Root, _parts, _read_regular_at, _open_parent


CRITICAL = ('PHASE', 'FROM', 'TO', 'DISPATCH_ID', 'PARENT_DISPATCH_ID',
            'DESIGN_LOCK_ID', 'DESIGN_DOC_ID', 'PLAN_LOCK_ID',
            'DESIGN_ARTIFACT', 'DESIGN_SHA256', 'PLAN_ARTIFACT', 'PLAN_SHA256',
            'PLAN_SOURCE_REPO', 'PLAN_SOURCE_COMMIT') + rules.XROOT_TRIGGERS


_PARSE_CACHE = ContextVar('authority_parse_cache', default=None)


def _per_check(function):
    @wraps(function)
    def cached(*args):
        cache = _PARSE_CACHE.get()
        if cache is None:
            return function(*args)
        key = (function, args)
        if key not in cache:
            cache[key] = function(*args)
        return cache[key]
    return cached


@_per_check
def _lines(text):
    return rules.a5_split_lines(text)


@_per_check
def _clean(text):
    return rules.a5_sanitized_text(text)


@_per_check
def _field_map(text, legacy=False):
    # Keep the two line and whitespace contracts distinct.
    lines = ((rules.sanitized_text(text) if '```' in text else text).splitlines()
             if legacy else _lines(text))
    pattern = re.compile(r'^([A-Z][A-Z0-9_-]*):\s*(.*)$' if legacy
                         else r'^([A-Z][A-Z0-9_-]*):[ \t]*(.*)$')
    fields = {}
    for line in lines:
        match = pattern.match(line.rstrip() if legacy else line)
        if match:
            fields.setdefault(match[1], []).append(match[2].strip())
    return fields


def _h27(text, key):
    if not re.fullmatch(r'[A-Z][A-Z0-9_-]*', key):
        return rules.h27_occurrences(text, key)
    return _field_map(text, True).get(key, [])


@_per_check
def _artifact_fields(text):
    fields = _field_map(_clean(text))
    return {key: fields.get(key, []) for key in
            ('DESIGN_SHA256', 'PLAN_SHA256', 'DESIGN_ARTIFACT', 'PLAN_ARTIFACT')}


@_per_check
def _xroot_engaged(text):
    fields = _field_map(text, True)
    return (fields.get('PHASE', [''])[0] == 'PLAN'
            and any(fields.get(key) for key in rules.XROOT_TRIGGERS))


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
        clean = _clean(text)
        fields = {key: values[0] for key, values in _field_map(clean).items()}
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
        for raw_line, clean_line in zip(_lines(text), _lines(clean)):
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
    if not re.fullmatch(r'[A-Z][A-Z0-9_-]*', key):
        pattern = re.compile(r'^' + re.escape(key) + r':[ \t]*(.*)$')
        return [match[1].strip() for line in _lines(_clean(text))
                if (match := pattern.match(line))]
    return _field_map(_clean(text)).get(key, [])


def _conflict(item):
    if item.header_conflict:
        return True
    clean = _clean(item.text)
    fields = _field_map(clean)
    legacy = _field_map(clean, True)
    return any(len(set(fields.get(key, []))) > 1
               or fields.get(key, []) != legacy.get(key, [])
               for key in CRITICAL)


def _sender(item):
    addresses = rules.split_addresses(item.fields.get('FROM'))
    return addresses[0] if len(addresses) == 1 else None


HTML_OPEN = re.compile(r'<!--|<\?|<!\[|<![a-z]|<(?:script|pre|style|textarea)(?=[ \t\r\n\f\v>]|$)', re.I)
VERDICT_KEY = re.compile(r'VERDICT|MERGE[ _-]AUTHORIZATION|MERGE[ _-]APPROVED', re.I)
SEPARATOR = re.compile(r'[*_~`\]\) \t]*[:|]')
PREFIX = re.compile(r'[ \t*_~]*')
# Step 6 strips emphasis BEFORE punctuation at the word's trailing end.
# Thus punctuation may precede trailing emphasis, but not vice versa.
WORD_END = r'[.,;:!?]*[*_~]*(?=[ \t]|\Z)'
POSITIVE = re.compile(r'(?:approved|approve|granted|yes|true|complete|merge-ready)' + WORD_END, re.I | re.ASCII)
APPROVAL = re.compile(r'(?:approved|approve)' + WORD_END, re.I | re.ASCII)
MERGE_WORD = re.compile(r'merge' + WORD_END, re.I | re.ASCII)
READY_WORD = re.compile(r'ready' + WORD_END, re.I | re.ASCII)


def _header(item):
    try:
        return envelope.parse_draft(item.text).headers
    except (errors.EngineError, TypeError):
        return {}


HTML_START = re.compile(r'^ {0,3}<[A-Za-z/!?]')
FENCE_RUN = re.compile(r'`{3,}|~{3,}')


@_per_check
def _html_refusal(text):
    if '<' not in text:
        return False
    return any(HTML_START.match(line) or HTML_OPEN.search(line)
               for line in _lines(text))


def _token(text, token):
    original = _lines(text)
    if FENCE_RUN.search(text) or _html_refusal(text):
        return False
    return any(line.rstrip(' \t') == token
               and (i == 0 or not original[i-1].strip(' \t'))
               and (i+1 == len(original) or not original[i+1].strip(' \t'))
               for i, line in enumerate(original))


def _mask_same_line_spans(line):
    runs = list(re.finditer(r'`+', line))
    if not runs:
        return line
    next_run, following = {}, {}
    for i in range(len(runs)-1, -1, -1):
        size = len(runs[i][0])
        if size in next_run:
            following[i] = next_run[size]
        next_run[size] = i
    parts, offset, i = [], 0, 0
    while i < len(runs):
        opening = runs[i]
        closing = following.get(i)
        if (closing is None or (opening.start() and line[opening.start()-1] == '\\')):
            i += 1
            continue
        end = runs[closing].end()
        content = line[opening.end():runs[closing].start()]
        if any(char.isalpha() for char in content):
            parts.extend((line[offset:opening.start()], ' ' * (end-opening.start())))
            offset = end
        i = closing+1
    parts.append(line[offset:])
    return ''.join(parts)


def _normalise(line):
    line = html.unescape(line)
    mapping = {ord('<'): ' ', ord('>'): ' ', ord('\\'): None}
    if not line.isascii():
        mapping.update({ord(c): None for c in set(line) if unicodedata.category(c) == 'Cf'})
    return line.translate(mapping)


def _positive_at(value, start=0):
    start = PREFIX.match(value, start).end()
    if POSITIVE.match(value, start):
        return True
    first = MERGE_WORD.match(value, start)
    return bool(first and READY_WORD.match(value, PREFIX.match(value, first.end()).end()))


@_per_check
def _verdict_polarities(text):
    """Union of both views, without copying each occurrence's value suffix."""
    positive = negative = False
    for line in dict.fromkeys(_lines(text)):
        if not any(char in line for char in (':', '|', '&')):
            continue
        masked = _mask_same_line_spans(line)
        retained = _normalise(masked)
        removed = re.sub(r'<[^<>\r\n]*>', '', masked)
        removed = re.sub(r'\]\([^()\r\n]*\)', ']', removed)
        views = (retained,) if removed == masked else (retained, _normalise(removed))
        for view in views:
            for key in VERDICT_KEY.finditer(view):
                if ((key.start() and view[key.start()-1].isalpha())
                        or (key.end() < len(view) and view[key.end()].isalpha())):
                    continue
                separator = SEPARATOR.match(view, key.end())
                if separator is None:
                    continue
                if _positive_at(view, separator.end()):
                    positive = True
                else:
                    negative = True
        if positive and negative:
            return positive, negative
    return positive, negative


def _word(value):
    return value.split()[0].rstrip('.,;:!?') if value.split() else ''


def _approved(item):
    keys = {item.fields.get('PHASE', '').replace('-', '_')+'_VERDICT', 'VERDICT'}
    header = _header(item)
    positive = any(key in keys and APPROVAL.match(value, PREFIX.match(value).end())
                   for key, value in header.items())
    return bool(positive and not _html_refusal(item.text)
                and not _verdict_polarities(item.text)[1])


def _same_seat(left, right):
    return (left == right or (left is not None and right is not None
            and rules.address_owner(left) == rules.address_owner(right)
            and rules.canonical_role(rules.address_role(left)) == rules.canonical_role(rules.address_role(right))))


def _opener_parent(ledger, rows, item):
    opener = cycles._opener(ledger, item.fields.get('PARENT_DISPATCH_ID'))
    if opener is None:
        return None
    # Earlier-only edges also terminate self-parent and repeated-id cycles.
    return next((row for row in rows if row.seq == opener[0]
                 and row.seq < item.seq), None)


def _parent(ledger, rows, item, phase, sender):
    candidates = [row for row in rows if row.seq < item.seq
                  and row.fields.get('DISPATCH_ID') == item.fields.get('PARENT_DISPATCH_ID')
                  and row.fields.get('PHASE') == phase
                  and _same_seat(_sender(row), sender)]
    if phase in ('PLAN', 'DESIGN'):
        pins = _declarations(item, set())
        declarations = {row.seq: _declarations(row, set()) for row in candidates}
        # A review selects the planner revision it names. Where no planner
        # supplied a digest, the review may supply it; a digestless review
        # inherits the latest planner declaration. Final lineage validation
        # still reports malformed or unresolved declarations.
        constrained = {path: digest for path, digest in pins.items()
                       if digest is not None and any(decl.get(path) is not None
                                                     for decl in declarations.values())}
        candidates = [row for row in candidates
                      if all(declarations[row.seq].get(path) == digest
                             for path, digest in constrained.items())]
    # Select the effective review before checking its verdict.
    return max(candidates, key=lambda row: row.seq, default=None)


def _lineage(rows, grant, seat, action, reasons, ledger):
    lineage = [grant]
    sender = _sender(grant)
    direct = rules.from_is_direct_authority(grant.fields)
    owner = rules.address_owner(seat)
    planner = owner+'.planner' if owner else ''
    if grant.engine_authored or not (direct or (action == 'implement' and _same_seat(sender,planner))):
        reasons.add('authority.issuer-not-permitted')
        return lineage
    if direct:
        # Declared ancestors supply artifact evidence, not a permission gate.
        item = grant
        while (parent_id := item.fields.get('PARENT_DISPATCH_ID')):
            if rules.STRUCTURED_ABSENT_RE.match(parent_id):
                break
            parent = _opener_parent(ledger, rows, item)
            if parent is None:
                break
            lineage.append(parent)
            item = parent
        return lineage
    if any(_word(value.lower()) != 'all-in'
           for value in _occurrences(grant.text, 'SCOPE_DIFF_RESULT')):
        reasons.add('authority.issuer-not-permitted')
    review = _parent(ledger, rows, grant, 'PLAN-REVIEW', seat)
    if review is None:
        reasons.add('authority.lineage-incomplete');return lineage
    lineage.append(review)
    if not _approved(review):reasons.add('authority.lineage-incomplete')
    plan = _parent(ledger, rows, review, 'PLAN', planner)
    if plan is None:
        reasons.add('authority.lineage-incomplete');return lineage
    lineage.append(plan)
    if seat not in rules.split_addresses(plan.fields.get('TO')):
        reasons.add('authority.lineage-incomplete')
    lock = plan.fields.get('DESIGN_LOCK_ID')
    if lock and not _xroot_engaged(plan.text):
        design_review = _parent(ledger, rows,plan,'DESIGN-REVIEW',seat)
        if design_review is None:
            reasons.add('authority.lineage-incomplete');return lineage
        lineage.append(design_review)
        design = _parent(ledger, rows,design_review,'DESIGN',planner)
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
    fields = _artifact_fields(item.text)
    for digest, locator, directory in rules.A5_LOCK_PAIRS:
        digests, locators = fields[digest], fields[locator]
        if digest == 'DESIGN_SHA256' and _xroot_engaged(item.text):
            continue  # The pinned source path is checked by _external_evidence.
        if not digests and not locators:continue
        if (len(set(locators)) != 1 or not rules.a5_stem_is_bare(locators[0])
                or (digests and (len(set(digests)) != 1
                    or rules.A5_SHA256_RE.fullmatch(digests[0]) is None))):
            reasons.add('authority.could-not-evaluate')
            continue
        declarations[f'{directory}/{locators[0]}.md'] = digests[0] if digests else None
    for key in ('PLAN_LOCK_ID', 'DESIGN_LOCK_ID'):
        # Foreign lock locators belong to the pinned Git source, not this root.
        if any(_h27(item.text, field)
               for field in ('PLAN_SOURCE_REPO', 'PLAN_SOURCE_COMMIT')):
            continue
        if key == 'DESIGN_LOCK_ID' and _xroot_engaged(item.text):
            continue
        value = item.fields.get(key, '')
        locator, separator, digest = value.partition(' @ ')
        if '/' not in locator and not locator.endswith('.md'):
            continue  # A symbolic lock id is not an artifact declaration.
        if digest.startswith('sha256 '):
            digest = digest[len('sha256 '):]
        if (not rules.xroot_valid_relpath(locator)
                or (separator and rules.A5_SHA256_RE.fullmatch(digest) is None)):
            reasons.add('authority.could-not-evaluate')
            continue
        digest = digest if separator else None
        if (declarations.get(locator) is not None and digest is not None
                and declarations[locator] != digest):
            reasons.add('authority.header-conflict')
        if digest is not None or locator not in declarations:
            declarations[locator] = digest
    return declarations


def _required_artifacts(rows,lineage,seat,reasons):
    required = {}
    grant = lineage[0]
    direct = rules.from_is_direct_authority(grant.fields)
    base_lineage = lineage if direct else lineage[1:]
    for row in sorted(base_lineage,key=lambda row:row.seq):
        for locator, digest in _declarations(row,reasons).items():
            # A later locator-only declaration cannot erase an earlier pin.
            if digest is not None or locator not in required:
                required[locator] = digest
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
                          if digest is not None and any(re.fullmatch(re.escape(path0[:-3])+r'-(?:erratum|supplement|amendment)-[0-9]+\.md',path)
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
    if not direct and 'authority.lineage-incomplete' not in reasons:
        for locator, digest in _declarations(grant, reasons).items():
            if digest is None:
                required.setdefault(locator, None)
            elif required.get(locator) != digest:
                reasons.add('authority.artifact-changed')
    if any(digest is None for digest in required.values()):
        reasons.add('authority.could-not-evaluate')
    return {locator:digest for locator,digest in required.items() if digest is not None}


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
            if _xroot_engaged(item.text):
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
            if any(_h27(item.text,key) for key in ('PLAN_SOURCE_REPO','PLAN_SOURCE_COMMIT')):
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
    token = _PARSE_CACHE.set({})
    try:
        return _check(ledger, root, relay_path, seat, action)
    finally:
        _PARSE_CACHE.reset(token)


def _check(ledger, root, relay_path: str, seat: str, action: str) -> dict:
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
    positive, negative = _verdict_polarities(grant.text)
    header_positive = (action == 'merge' and any(
        rules.merge_authorization_positive(key, value) for key, value in _header(grant).items()))
    if action == 'merge':
        shape = (grant.fields.get('PHASE') == 'MERGE-GATE'
                 and (_token(grant.text, 'DISPATCH MERGE') or header_positive))
    else:
        shape = grant.fields.get('PHASE') == 'IMPL' and _token(grant.text, 'DISPATCH IMPL')
    if _html_refusal(grant.text):
        shape = False
    if negative:
        reasons.add('authority.denied')
        if positive or header_positive:
            reasons.add('authority.verdict-conflict')
    if not shape:
        reasons.add('authority.shape-absent')
    lineage=_lineage(rows,grant,seat,action,reasons,ledger)
    for row in lineage:
        if _conflict(row):reasons.add('authority.header-conflict')
        if row is grant or not rules.from_is_direct_authority(grant.fields):
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
