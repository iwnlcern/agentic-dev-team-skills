"""RFC 8785-style canonical JSON and commissioning record framing."""

from decimal import Decimal
import hashlib
import hmac
import json
import math
import re


def _encode_string(value):
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("lone surrogate is not valid Unicode")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8")


def _encode_float(value):
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    if value == 0:
        return b"0"
    magnitude = abs(value)
    shortest = repr(value).lower()
    if 1e-6 <= magnitude < 1e21:
        if "e" in shortest:
            shortest = format(Decimal(shortest), "f")
        if "." in shortest:
            shortest = shortest.rstrip("0").rstrip(".")
        return shortest.encode("ascii")
    mantissa, exponent = shortest.split("e")
    sign = ""
    if exponent.startswith(("+", "-")):
        sign, exponent = exponent[0], exponent[1:]
    exponent = exponent.lstrip("0") or "0"
    if sign != "-":
        sign = "+"
    return (mantissa + "e" + sign + exponent).encode("ascii")


def _encode(value):
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        if not -(2 ** 53 - 1) <= value <= 2 ** 53 - 1:
            raise ValueError("integer exceeds the interoperable JSON range")
        return str(value).encode("ascii")
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, str):
        return _encode_string(value)
    if isinstance(value, list):
        return b"[" + b",".join(_encode(item) for item in value) + b"]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16be"))
        return (b"{" + b",".join(_encode_string(key) + b":" +
                                    _encode(value[key]) for key in keys) +
                b"}")
    raise TypeError("value is not representable as JSON")


def jcs_encode(obj):
    return _encode(obj)


def frame_record(fields):
    line = jcs_encode(fields)
    digest = hashlib.sha256(line).hexdigest().encode("ascii")
    return line + b"\n" + digest + b"\n"


def parse_record(data):
    if not isinstance(data, bytes):
        raise TypeError("record carrier must be bytes")
    parts = data.split(b"\n")
    if len(parts) != 3 or parts[2] != b"" or not parts[0]:
        raise ValueError("record framing mismatch")
    line, carried_digest = parts[:2]
    if re.fullmatch(rb"[0-9a-f]{64}", carried_digest) is None:
        raise ValueError("record digest shape mismatch")
    actual = hashlib.sha256(line).hexdigest().encode("ascii")
    if not hmac.compare_digest(actual, carried_digest):
        raise ValueError("record digest mismatch")
    try:
        fields = json.loads(line.decode("utf-8"),
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("record JSON mismatch") from exc
    if not isinstance(fields, dict):
        raise ValueError("record object required")
    return fields, carried_digest.decode("ascii")


def commissioned_by_value(parent_root_uuid, commissioning_path,
                           dispatch_content_digest, record_digest):
    return jcs_encode({
        "parent_root_uuid": parent_root_uuid,
        "commissioning_path": commissioning_path,
        "dispatch_content_digest": dispatch_content_digest,
        "record_digest": record_digest,
    })
