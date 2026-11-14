"""Strict Ed25519 verification of public data using only the standard library.

RFC 8032 decoding, canonical encodings, S < L, non-small-order public keys
and the cofactorless equation are required. Mixed-order keys are permitted.
All arithmetic processes public data; this module is not a signing primitive.
"""
import hashlib


_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_MINUS_ONE = pow(2, (_P - 1) // 4, _P)
_IDENTITY = (0, 1, 1, 0)
_IDENTITY_BYTES = b"\x01" + b"\x00" * 31


def _add(left, right):
    """Complete addition in extended Edwards coordinates (a = -1)."""
    x1, y1, z1, t1 = left
    x2, y2, z2, t2 = right
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * _D * t1 * t2 % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _multiply(scalar, point):
    result = _IDENTITY
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _encode(point):
    x, y, z, _ = point
    inverse = pow(z, _P - 2, _P)
    x, y = x * inverse % _P, y * inverse % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode(encoded):
    if not isinstance(encoded, bytes) or len(encoded) != 32:
        return None
    packed = int.from_bytes(encoded, "little")
    sign, y = packed >> 255, packed & ((1 << 255) - 1)
    if y >= _P:
        return None
    y %= _P
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    square = u * pow(v, _P - 2, _P) % _P
    x = pow(square, (_P + 3) // 8, _P)
    if (x * x * v - u) % _P:
        x = x * _SQRT_MINUS_ONE % _P
    if (x * x * v - u) % _P:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _P - x
    point = (x, y, 1, x * y % _P)
    # Deliberately backs up y < p and the sign rule in the complete decoder.
    if _encode(point) != encoded:
        return None
    return point


_BASE = _decode(bytes.fromhex("58" + "66" * 31))


def check_public_key(public_key: bytes) -> bool:
    """Accept a canonical curve point unless its order divides eight."""
    point = _decode(public_key)
    return point is not None and _encode(_multiply(8, point)) != _IDENTITY_BYTES


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a strict, cofactorless Ed25519 signature; malformed input refuses."""
    if not isinstance(message, bytes) or not isinstance(signature, bytes) or len(signature) != 64:
        return False
    scalar = int.from_bytes(signature[32:], "little")
    if scalar >= _L:
        return False
    public = _decode(public_key)
    r_point = _decode(signature[:32])
    if public is None or r_point is None:
        return False
    if _encode(_multiply(8, public)) == _IDENTITY_BYTES:
        return False
    challenge = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(),
                               "little") % _L
    x, y, z, t = public
    negative = (-x % _P, y, z, -t % _P)
    candidate = _add(_multiply(scalar, _BASE), _multiply(challenge, negative))
    return _encode(candidate) == signature[:32]
