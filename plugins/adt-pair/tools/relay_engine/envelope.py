"""Pure relay envelope parsing and submission hashing."""

from dataclasses import dataclass
import hashlib
import re

from relay_engine import errors
from relay_engine.jcs import jcs_encode


REQUIRED_FIELDS = (
    "ROLE", "PHASE", "AUTHORITY", "DISPATCH_ID", "RUN_ID",
    "CEREMONY_TIER", "EVIDENCE_TARGET", "HUMAN_GATE_REQUIRED", "FROM",
    "TO", "SUBJECT",
)

_HEADER = re.compile(r"^([A-Z][A-Z0-9_]*):[ \t]*(.*)$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Envelope:
    phase: str
    role: str
    authority: str
    dispatch_id: str
    parent_dispatch_id: str | None
    run_id: str
    from_seat: str
    to_seats: tuple[str, ...]
    cc_seats: tuple[str, ...]
    status: str | None
    subject: str
    headers: dict[str, str]


def valid_run_id(value):
    return isinstance(value, str) and _RUN_ID.fullmatch(value) is not None


def _header_error(field):
    return errors.error_for("E-HEADER", field=field)


def _seat_list(value):
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_draft(text):
    if not isinstance(text, str):
        raise TypeError("draft must be text")
    headers = {}
    started = False
    for line in text.splitlines():
        match = _HEADER.fullmatch(line)
        if not started:
            if match is None:
                continue
            started = True
        elif not line:
            break
        elif match is None:
            break
        if match is None:
            continue
        key, value = match.groups()
        if key in headers:
            raise _header_error(key)
        headers[key] = value
    for field in REQUIRED_FIELDS:
        if not headers.get(field):
            raise _header_error(field)
    if not valid_run_id(headers["RUN_ID"]):
        raise _header_error("RUN_ID")
    to_seats = _seat_list(headers["TO"])
    if not to_seats:
        raise _header_error("TO")
    return Envelope(
        phase=headers["PHASE"],
        role=headers["ROLE"],
        authority=headers["AUTHORITY"],
        dispatch_id=headers["DISPATCH_ID"],
        parent_dispatch_id=headers.get("PARENT_DISPATCH_ID"),
        run_id=headers["RUN_ID"],
        from_seat=headers["FROM"],
        to_seats=to_seats,
        cc_seats=_seat_list(headers.get("CC", "")),
        status=headers.get("STATUS"),
        subject=headers["SUBJECT"],
        headers=dict(headers),
    )


def body_sha256(body):
    if not isinstance(body, bytes):
        raise TypeError("body must be bytes")
    return hashlib.sha256(body).hexdigest()


def content_hash(envelope, body, admits_against):
    if not isinstance(envelope, Envelope):
        raise TypeError("envelope required")
    canonical = jcs_encode({
        "headers": {key: envelope.headers[key]
                    for key in sorted(envelope.headers)},
        "body_sha256": body_sha256(body),
        "admits_against": admits_against,
    })
    return hashlib.sha256(canonical).hexdigest()


LINE_BREAKS = frozenset('\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029')
SEPARATORS = {
    '\v': ('U+000B', 'LINE TABULATION'),
    '\f': ('U+000C', 'FORM FEED'),
    '\x1c': ('U+001C', 'INFORMATION SEPARATOR FOUR'),
    '\x1d': ('U+001D', 'INFORMATION SEPARATOR THREE'),
    '\x1e': ('U+001E', 'INFORMATION SEPARATOR TWO'),
    '\x85': ('U+0085', 'NEXT LINE'),
    '\u2028': ('U+2028', 'LINE SEPARATOR'),
    '\u2029': ('U+2029', 'PARAGRAPH SEPARATOR'),
}
LATE_CONTROL_KEYS = ('SUPERSEDES', 'CYCLE_TERMINUS', 'CYCLE_REOPEN')
_SHAPE_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
_SHAPE_PREFIX = re.compile(r'^[ \t]*(?:(?:[-*+]|\d{1,9}[.)]|>)[ \t]+|>)*[ \t]*')


@dataclass(frozen=True)
class DraftShape:
    kind: str
    line: int
    column: int
    char: str | None
    key: str | None
    block: tuple[int, int] | None


def first_separator(text: str) -> DraftShape | None:
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        for column, char in enumerate(line, 1):
            if char in SEPARATORS:
                return DraftShape('separator', line_number, column, char, None, None)
    return None


def _shape_verdict_key(key):
    return any(key == suffix or key.endswith('_' + suffix)
               for suffix in ('VERDICT', 'MERGE_AUTHORIZATION', 'MERGE_APPROVED'))


def _shape_header_positive(key, value):
    # The header form requires both the linter's key-specific positive value
    # and authority's exact positive word. Keep independent of both readers.
    allowed = {
        'MERGE_AUTHORIZATION': {'approved', 'granted', 'yes'},
        'HUMAN_MERGE_AUTHORIZATION': {'approved', 'granted', 'yes'},
        'MERGE_APPROVED': {'yes', 'true', 'approved'},
        'VERDICT': {'merge-ready', 'approved', 'complete'},
    }
    return (value.isascii() and value == value.lstrip(' \t') and
            value.rstrip(' \t').lower() in allowed.get(key.upper(), set()))


def _shape_block(lines):
    start = None
    for index, line in enumerate(lines):
        if _HEADER.fullmatch(line):
            if start is None:
                start = index
        elif start is not None:
            return start, index
    return None if start is None else (start, len(lines))


def _shape_outside(lines, block):
    """Return indexed non-header lines outside closed fences (R217)."""
    ordinary_openers = set()
    while True:
        result = []
        fence = None
        opener = None
        for index, line in enumerate(lines):
            if block[0] <= index < block[1]:
                continue
            match = None if index in ordinary_openers else _SHAPE_FENCE.match(line)
            if fence is None:
                if match:
                    fence, opener = match.group(1), index
                else:
                    result.append((index, line))
            elif match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
        if fence is None:
            return result
        ordinary_openers.add(opener)


def draft_shape(text: str) -> DraftShape | None:
    separator = first_separator(text)
    lines = text.splitlines()
    block = _shape_block(lines)
    if block is None:
        return separator
    headers = [_HEADER.fullmatch(line).groups() for line in lines[block[0]:block[1]]]
    phase = next((value for key, value in headers if key == 'PHASE'), '').rstrip(' \t')
    token = {'MERGE-GATE': 'DISPATCH MERGE', 'IMPL': 'DISPATCH IMPL'}.get(phase)
    outside = _shape_outside(lines, block)
    grant = token is not None and (
        any(line.strip(' \t') == token for _, line in outside) or
        (phase == 'MERGE-GATE' and any(_shape_header_positive(k, v) for k, v in headers)))
    candidates = [] if separator is None else [separator]
    for index, line in outside:
        column = _SHAPE_PREFIX.match(line).end()
        match = _HEADER.fullmatch(line[column:])
        if match is None:
            continue
        key = match.group(1)
        if key in LATE_CONTROL_KEYS:
            kind = 'late-control'
        elif grant and _shape_verdict_key(key):
            kind = 'late-verdict'
        else:
            continue
        candidates.append(DraftShape(kind, index + 1, column + 1, None, key,
                                     (block[0] + 1, block[1])))
    return min(candidates, key=lambda item: (item.line, item.column)) if candidates else None


def shape_error(shape: DraftShape, *, boot_field: str | None = None) -> errors.EngineError:
    if boot_field is not None:
        return errors.error_for('E-DRAFT-SHAPE', variant='boot-field', field=boot_field)
    if shape.kind == 'separator':
        codepoint, name = SEPARATORS[shape.char]
        return errors.error_for('E-DRAFT-SHAPE', variant='separator', line=shape.line,
                                column=shape.column, codepoint=codepoint, name=name)
    first, last = shape.block
    return errors.error_for('E-DRAFT-SHAPE', variant=shape.kind, line=shape.line,
                            key=shape.key, first=first, last=last)


def shape_message(shape: DraftShape) -> str:
    error = shape_error(shape)
    return error.cause + '; ' + error.remedy
