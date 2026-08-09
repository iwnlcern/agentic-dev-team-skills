"""Keyed, validated user-visible text emission for relay-engine."""

from dataclasses import dataclass
import json
import re
import string as _string
import sys


@dataclass(frozen=True)
class RejectedValue:
    digest: str
    length: int

    def __str__(self):
        return "unrecognized-input (sha256:%s, length %d)" % (
            self.digest, self.length)


@dataclass(frozen=True)
class MachineResult:
    value: object

    def __str__(self):
        return json.dumps(self.value, sort_keys=True, separators=(",", ":"))


INVENTORY = {
    "explain-heading": "{code}",
    "explain-cause": "cause: {cause}",
    "explain-remedy": "remedy: {remedy}",
    "usage-error": "usage error",
    "unexpected-error": "unexpected error",
    "daemon-start-failed": "daemon start failed",
    "command-result": "{result}",
    "rejected-value": "unrecognized-input (sha256:{digest}, length {length})",
}

_VALIDATORS = {}
_FACTORY_CALLED = False
_CODE_VALUES = {
    "E-KEY-MISMATCH", "E-ID-COLLISION", "E-SUPERSEDED",
    "E-PATH-ESCAPE", "E-HEADER", "E-ENVELOPE",
    "E-REPLAY-MISMATCH", "E-STORAGE", "E-DAEMON-DOWN",
    "seat-occupied", "commission-conflict", "commission-late",
    "run-id-mismatch", "run-id-uninitialized", "run-id-invalid",
    "E-FRAMING", "E-WIRE-VERSION", "E-WIRE-OP", "E-WIRE-ARGS",
    "E-DAEMON-STOPPING",
}
_OPS = {
    "submit", "seat.register", "seat.replace", "seat.stand_down",
    "seat.show", "status", "roster", "commission", "adopt_commission",
    "export_ruling", "adopt_ruling", "render", "verify", "reconcile",
    "migrate.check", "daemon.stop",
}
_REASONS = {"length-prefix-invalid", "not-utf8", "not-json",
            "oversize", "truncated"}


def _is_code(value):
    return isinstance(value, str) and value in _CODE_VALUES


def _is_text(value):
    return isinstance(value, str) and bool(value) and "\n" not in value


def _is_digest12(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{12}", value)


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_rejected(value):
    return isinstance(value, RejectedValue)


def _is_machine_result(value):
    return isinstance(value, MachineResult)


def _is_reason(value):
    return isinstance(value, str) and value in _REASONS


def _is_op(value):
    return isinstance(value, str) and value in _OPS


def _is_detail(value):
    return (isinstance(value, str) and
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}:[A-Za-z0-9_-]{1,32}",
                         value) is not None)


_VALIDATORS.update({
    ("explain-heading", "code"): _is_code,
    ("explain-cause", "cause"): _is_text,
    ("explain-remedy", "remedy"): _is_text,
    ("rejected-value", "digest"): _is_digest12,
    ("rejected-value", "length"): _is_count,
    ("command-result", "result"): _is_machine_result,
})


def register_inventory(entries, validators):
    for key in entries:
        if key in INVENTORY:
            raise KeyError(key)
    INVENTORY.update(entries)
    for pair, validator in validators.items():
        if pair in _VALIDATORS:
            raise KeyError(pair)
        _VALIDATORS[pair] = validator


def _placeholders(template):
    return tuple(field for _, field, _, _ in _string.Formatter().parse(template)
                 if field is not None)


def render(key, **params):
    if key not in INVENTORY:
        raise KeyError(key)
    fields = _placeholders(INVENTORY[key])
    if set(params) != set(fields):
        raise ValueError("parameter set does not match inventory template")
    for field in fields:
        validator = _VALIDATORS.get((key, field))
        if validator is None or not validator(params[field]):
            raise ValueError("invalid inventory parameter")
    return INVENTORY[key].format(**params)


def rejected_value(digest, length):
    rendered = render("rejected-value", digest=digest, length=length)
    value = RejectedValue(digest, length)
    if str(value) != rendered:
        raise AssertionError("rejected value renderer mismatch")
    return value


def machine_result(value):
    result = MachineResult(value)
    str(result)
    return result


def _make_emitter():
    global _FACTORY_CALLED
    if _FACTORY_CALLED:
        raise RuntimeError("emitter factory already used")
    _FACTORY_CALLED = True
    diagnostic_sink = None

    def emit(stream_name, key, **params):
        if stream_name == "stdout":
            stream = sys.stdout
        elif stream_name == "stderr":
            stream = sys.stderr
        else:
            raise ValueError("unknown stream")
        stream.write(render(key, **params) + "\n")
        stream.flush()

    def emit_verbatim(stream, body_bytes):
        if not isinstance(body_bytes, bytes):
            raise TypeError("stored body must be bytes")
        stream.write(body_bytes)
        if hasattr(stream, "flush"):
            stream.flush()

    def report_diagnostic(exc):
        if not isinstance(exc, BaseException):
            raise TypeError("diagnostic must be an exception")
        if diagnostic_sink is not None:
            diagnostic_sink(exc)

    def set_diagnostic_sink(sink):
        nonlocal diagnostic_sink
        if sink is not None and not callable(sink):
            raise TypeError("diagnostic sink must be callable")
        diagnostic_sink = sink

    return emit, emit_verbatim, report_diagnostic, set_diagnostic_sink


emit, emit_verbatim, report_diagnostic, set_diagnostic_sink = _make_emitter()


VALIDATOR_IDENTITIES = _VALIDATORS
OPS = frozenset(_OPS)
FRAMING_REASONS = frozenset(_REASONS)
