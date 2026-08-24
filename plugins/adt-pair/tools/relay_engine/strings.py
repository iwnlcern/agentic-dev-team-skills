"""Keyed, validated user-visible text emission for relay-engine."""

from dataclasses import dataclass
import json
import re
import string as _string
import sys
import unicodedata


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


@dataclass(frozen=True)
class ExistingTargets:
    value: str

    def __str__(self):
        return self.value


INVENTORY = {
    "explain-heading": "{code}",
    "explain-cause": "cause: {cause}",
    "explain-remedy": "remedy: {remedy}",
    "usage-error": "usage error",
    "unexpected-error": "unexpected error",
    "daemon-start-failed": "daemon start failed",
    "command-result": "{result}",
    "version-result": "{result}",
    "rejected-value": "unrecognized-input (sha256:{digest}, length {length})",
    "engine-root-sweep-summary": "engine-root sweep: {count} record-known relays not re-judged; context source: {mode}",
    "engine-root-record-integrity": "record-integrity: {cause}: {path}",
    "engine-root-outside-the-record": "outside-the-record: {cause}: {path}",
}

_VALIDATORS = {}
_FACTORY_CALLED = False
LIST_BUDGET = 4096
CONTEXT_FRAME_MARGIN = 1024 * 1024
CONTEXT_PAGE_BUDGET = (16 * 1024 * 1024) - CONTEXT_FRAME_MARGIN
CONTEXT_ENTRY_CAP = 4096
_CODE_VALUES = {
    "E-KEY-MISMATCH", "E-ID-COLLISION", "E-SUPERSEDED",
    "E-PATH-ESCAPE", "E-HEADER", "E-ENVELOPE",
    "E-REPLAY-MISMATCH", "E-STORAGE", "E-DAEMON-DOWN",
    "E-VERSION-MISMATCH",
    "seat-occupied", "commission-conflict", "commission-late",
    "run-id-mismatch", "run-id-uninitialized", "run-id-invalid",
    "E-FRAMING", "E-WIRE-VERSION", "E-WIRE-OP", "E-WIRE-ARGS",
    "E-DAEMON-STOPPING",
    "E-CONTEXT-BUDGET",
}
_OPS = {
    "submit", "seat.register", "seat.replace", "seat.stand_down",
    "seat.show", "show", "status", "roster", "commission", "adopt_commission",
    "export_ruling", "adopt_ruling", "render", "verify", "reconcile",
    "migrate.check", "lint.context", "daemon.stop",
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


def _is_root_relative_path(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or
            any(part in ("", ".", "..") for part in value.split("/"))):
        return False
    try:
        received = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (len(received) <= 512 and
            all(unicodedata.category(scalar) not in
                {"Cc", "Cf", "Cs", "Zl", "Zp"} for scalar in value))


def _is_rejected(value):
    return (isinstance(value, RejectedValue) and
            _is_digest12(value.digest) and _is_count(value.length))


def _is_machine_result(value):
    return isinstance(value, MachineResult)


def _is_existing_targets(value):
    return isinstance(value, ExistingTargets)


def _is_reason(value):
    return isinstance(value, str) and value in _REASONS


def _is_op(value):
    return isinstance(value, str) and value in _OPS


def _is_detail(value):
    return (isinstance(value, str) and
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}:[A-Za-z0-9_-]{1,32}",
                         value) is not None)


def valid_install(value):
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    try:
        received = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (len(received) <= 512 and
            all(unicodedata.category(scalar) not in
                {"Cc", "Cf", "Cs", "Zl", "Zp"} for scalar in value))


def valid_kit(value):
    if not isinstance(value, str):
        return False
    try:
        received = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (len(received) <= 32 and re.fullmatch(
        r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\."
        r"(0|[1-9][0-9]{0,5})", value) is not None)


def valid_fp(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


_VALIDATORS.update({
    ("explain-heading", "code"): _is_code,
    ("explain-cause", "cause"): _is_text,
    ("explain-remedy", "remedy"): _is_text,
    ("rejected-value", "digest"): _is_digest12,
    ("rejected-value", "length"): _is_count,
    ("command-result", "result"): _is_machine_result,
    ("version-result", "result"): _is_machine_result,
    ("engine-root-sweep-summary", "count"): _is_count,
    ("engine-root-sweep-summary", "mode"):
        lambda value: (isinstance(value, str) and
                       value in {"daemon", "read-only record"}),
    ("engine-root-record-integrity", "cause"):
        lambda value: (isinstance(value, str) and
                       value in {"missing", "non-regular", "symlinked",
                                 "unreadable", "digest-mismatch"}),
    ("engine-root-record-integrity", "path"): _is_root_relative_path,
    ("engine-root-outside-the-record", "cause"):
        lambda value: (isinstance(value, str) and
                       value in {"foreign entry", "unexpected directory",
                                 "symlinked component",
                                 "non-directory ancestor",
                                 "root escape"}),
    ("engine-root-outside-the-record", "path"): _is_root_relative_path,
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


def existing_targets(paths):
    ordered = []
    for path in paths:
        if (not isinstance(path, str) or not path or path.startswith("/") or
                any(part in ("", ".", "..") for part in path.split("/")) or
                "," in path or ";" in path or "\n" in path):
            raise ValueError("canonical root-relative relay path required")
        path.encode("utf-8")
        ordered.append(path)
    ordered.sort(key=lambda value: value.encode("utf-8"))
    if not ordered:
        return ExistingTargets(
            "none — no existing relays under the root to admit against")

    prefix = []
    used = 0
    for path in ordered:
        addition = len(path.encode("utf-8")) + (2 if prefix else 0)
        if used + addition > LIST_BUDGET:
            break
        prefix.append(path)
        used += addition
    rendered = ", ".join(prefix)
    omitted = len(ordered) - len(prefix)
    if omitted:
        rendered += " … plus %d more of %d total" % (omitted, len(ordered))
    return ExistingTargets(rendered)


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
IS_EXISTING_TARGETS = _is_existing_targets
