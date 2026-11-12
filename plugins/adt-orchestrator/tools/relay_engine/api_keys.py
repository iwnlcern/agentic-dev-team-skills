"""Seat key primitives and caller-transaction test provisioning for Slice A."""

import base64
import hashlib
import hmac
import secrets

from relay_engine.submit import new_uuid7


KEY_FORMAT = "adt-seat-key-v1"


class IssueRefused(ValueError):
    """A fixed refusal reason, with no key material in its message."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def new_key_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def key_verifier(key_id: str, key_secret: str) -> str:
    material = (KEY_FORMAT.encode("ascii") + b"\x00" + key_id.encode("ascii")
                + b"\x00" + key_secret.encode("ascii"))
    return hashlib.sha256(material).hexdigest()


def verifier_matches(stored: str, key_id: str, key_secret: str) -> bool:
    return hmac.compare_digest(stored, key_verifier(key_id, key_secret))


def issue_occupant_key(conn, seat_id: str, issued_at_ms: int) -> tuple[str, str]:
    """Issue test credentials without committing or storing the returned secret.

    The caller owns the open transaction and must roll it back on an error.
    This helper creates no key file and is not a production provisioning route.
    """
    if not conn.in_transaction:
        raise ValueError("transaction_required")
    cursor = conn.execute("SELECT state FROM seats WHERE id=?", (seat_id,))
    try:
        seat = cursor.fetchone()
    finally:
        cursor.close()
    if seat is None:
        raise IssueRefused("seat_unknown")
    if seat[0] == "stood-down":
        raise IssueRefused("seat_stood_down")
    cursor = conn.execute(
        "SELECT 1 FROM seat_occupants WHERE seat_id=? AND ended_at IS NULL", (seat_id,))
    try:
        occupied = cursor.fetchone() is not None
    finally:
        cursor.close()
    if seat[0] == "occupied" or occupied:
        raise IssueRefused("seat_occupied")

    occupant_id = new_uuid7(issued_at_ms)
    key_id = new_uuid7(issued_at_ms, last=occupant_id)
    secret = new_key_secret()
    conn.execute(
        "INSERT INTO seat_occupants (id,seat_id,key_id,key_verifier,issued_at,ended_at)"
        " VALUES (?,?,?,?,?,NULL)",
        (occupant_id, seat_id, key_id, key_verifier(key_id, secret), issued_at_ms),
    ).close()
    conn.execute("UPDATE seats SET state='occupied' WHERE id=?", (seat_id,)).close()
    return key_id, secret
