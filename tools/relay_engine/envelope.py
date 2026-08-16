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
