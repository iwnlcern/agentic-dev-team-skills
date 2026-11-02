"""Structured relay-engine refusal registry."""

from dataclasses import dataclass
import hashlib
import json
import re

from relay_engine import strings


E_DAEMON_DOWN_ESCALATION = "Hand-relay this escalation to the eligible starter: master-planner if a master tier exists (never the orchestrator session), else orchestrator-planner, else the operator. This escalation is the sole exception to daemon admission."


@dataclass(frozen=True)
class ErrorSpec:
    cause_key: str
    remedy_key: str
    explain_key: str
    cls: str


@dataclass(frozen=True)
class _VersionMismatchRemedy:
    stale: str
    client_install: object
    daemon_install: object

    def __str__(self):
        if self.stale == "client":
            return "update the client install at %s, then retry" % (
                self.client_install,)
        if self.stale == "daemon":
            return "update the daemon install at %s, then retry" % (
                self.daemon_install,)
        return "refresh both installs: client at %s; daemon at %s; then retry" % (
            self.client_install, self.daemon_install)


_TEXT = {
    'error-draft-shape-late-control-in-block-cause': "line {line}: {key} appears in the header block's text (lines {first} to {last}) but is not that line's own header, so the engine never reads it as {key}",
    'error-draft-shape-late-verdict-in-block-cause': "line {line}: {key} appears in this grant's header block text (lines {first} to {last}) but is not that line's own header, so authority never reads it as a verdict",
    'error-draft-shape-late-control-in-block-remedy': 'to mention the key in the header block, put it and its colon in inline code',
    'error-draft-shape-late-verdict-in-block-remedy': 'to mention the key in the header block, put it and its colon in inline code',

    'error-draft-shape-cause': 'the draft has a line the engine refuses',
    'error-draft-shape-remedy': 'run relay lint on the draft; it names the line and the fix',
    'error-draft-shape-explain': 'a draft is refused before admission when one of its lines would be read two ways: a line break other than a newline or carriage return, or a cycle, supersession or grant verdict line outside the header block',
    'error-draft-shape-separator-cause': 'line {line}, column {column}: {codepoint} {name} is a line break to some readers and ordinary text to others',
    'error-draft-shape-separator-remedy': 'use a newline there, or write the character as text, for example {codepoint}',
    'error-draft-shape-late-control-cause': 'line {line}: {key} is outside the header block (lines {first} to {last}), where the engine never reads it',
    'error-draft-shape-late-control-remedy': 'move the line into the header block, or quote it in code; to mention the key, put it and its colon in inline code',
    'error-draft-shape-late-verdict-cause': "line {line}: {key} is outside this grant's header block (lines {first} to {last}), so authority reads the grant as carrying no verdict",
    'error-draft-shape-late-verdict-remedy': 'move the line into the header block, where any value but an exact positive word denies the grant; or remove it; or quote it in code; to mention the key, put it and its colon in inline code',
    'error-draft-shape-boot-field-cause': 'the boot relay for this registration would carry a refused line shape in its {field} line',
    'error-draft-shape-boot-field-remedy': "the line is built from the registration's inputs and the root's stored commissioning record; re-register with a dispatch reference on one line that shows no cycle or supersession key with a colon, or ask the operator to re-commission the root with a commissioning path on one line that shows none",

    "error-no-root-cause": "no relay root at or above {directory}",
    "error-no-root-remedy": "pass --root with the relay root's path",
    "error-no-root-explain": "a command run without --root looks for the nearest directory at or above the working directory holding .engine/ with its relay record",
    "error-no-root-unstarted-cause": "{directory} holds .engine/ but no relay record",
    "error-no-root-unstarted-remedy": "if {directory} is not the relay root, pass --root with the relay root's path. If it is, its daemon has never been started. " + E_DAEMON_DOWN_ESCALATION,
    "error-relay-not-found-cause": "no filed relay is {target} under the root {root}",
    "error-relay-not-found-remedy": "pass the relay's root-relative path as INDEX.md's file cell gives it, or its seq number",
    "error-no-top-seat-cause": "no top seat is recorded on this root",
    "error-no-top-seat-remedy": "have the eligible starter run daemon start --seat <address> on this root",
    "error-no-top-seat-explain": "declarations require the current key of an explicitly designated top seat",
    "error-key-mismatch-cause": "registration tag does not name the current seat occupancy",
    "error-key-mismatch-remedy": "re-register or run from the occupying session",
    "error-id-collision-cause": "dispatch id is not reusable in this run scope",
    "error-id-collision-remedy": "use a new id, or rerun with --admits-against <root-relative path of the relay currently holding this id>",
    "error-superseded-cause": "the admission target has an applied supersession",
    "error-superseded-remedy": "re-read the superseding ruling and admit against current authority",
    "error-path-escape-cause": "draft path is outside the canonical root",
    "error-path-escape-remedy": "use the canonical drafts location",
    "error-not-found-cause": "the {field} path {rel} does not exist beneath the root",
    "error-not-found-remedy": "author the {field} beneath the root and pass its root-relative path; an absolute path must lie inside the root",
    "error-header-cause": "required relay header missing or invalid: {field}",
    "error-header-remedy": "supply every required submission header",
    "error-envelope-cause": "submitted envelope does not match server-derived bytes",
    "error-envelope-remedy": "rebuild the envelope from the unchanged draft",
    "error-envelope-edge-cause": "admits_against did not resolve to exactly one existing relay under the root",
    "error-envelope-edge-remedy": "field: admits_against; expected: a single root-relative path resolving to exactly one existing relay; existing targets: {targets}",
    "error-replay-cause": "submission id was replayed with changed content",
    "error-replay-remedy": "retry the original bytes or allocate a new submission id",
    "error-storage-cause": "the run record could not complete the operation",
    "error-storage-remedy": "ask the operator to inspect diagnostics and retry after repair",
    "error-daemon-down-cause": "no live designated writer owns this root",
    "error-daemon-down-remedy": E_DAEMON_DOWN_ESCALATION,
    "error-seat-occupied-cause": "the seat already has a current occupancy",
    "error-seat-occupied-remedy": "use explicit replacement or another seat",
    "error-commission-conflict-cause": "a different commissioning record already exists",
    "error-commission-conflict-remedy": "use the existing record or a distinct child run",
    "error-commission-late-cause": "the child run already has application state",
    "error-commission-late-remedy": "commission before the child run accepts work",
    "error-run-id-mismatch-cause": "the supplied run id differs from the root run id",
    "error-run-id-mismatch-remedy": "use the root's immutable run id",
    "error-run-id-uninitialized-cause": "the root has no established run id",
    "error-run-id-uninitialized-remedy": "start the designated writer with a valid run id",
    "error-run-id-invalid-cause": "the run id does not match the filename-safe grammar",
    "error-run-id-invalid-remedy": "use one to sixty-four filename-safe characters",
    "error-framing-cause": "unparseable frame: {reason}",
    "error-framing-remedy": "send one complete frame: 4-byte big-endian length prefix + UTF-8 JSON",
    "error-wire-version-cause": "unsupported protocol version {rejected_version}",
    "error-wire-version-remedy": "send v:2",
    "error-wire-op-cause": "unknown op {rejected_op}",
    "error-wire-op-remedy": "use an op from the daemon op table",
    "error-wire-args-cause": "args schema violation for {op}: {detail}",
    "error-wire-args-remedy": "match the op's exact args schema",
    "error-daemon-stopping-cause": "daemon is draining its stop barrier",
    "error-daemon-stopping-remedy": "retry after restart; replay semantics make the retry safe",
    "error-context-budget-cause": "context page exceeded the byte budget",
    "error-context-budget-remedy": "a single record entry or accounting disagreement exceeded the per-page byte budget",
    "error-version-mismatch-cause": "client identity (kit {client_kit}, fingerprint {client_fp}, install {client_install}) does not match daemon identity (kit {daemon_kit}, fingerprint {daemon_fp}, install {daemon_install})",
    "error-version-mismatch-remedy": "{remedy}",
    "error-version-mismatch-explain-remedy": "update the lower-kit install, or refresh both installs when attribution is indeterminate",
}

_TEXT.update({
    "error-key-mismatch-explain": "the registration tag must identify the current seat occupancy",
    "error-id-collision-explain": "dispatch ids are reusable only inside their current open id scope",
    "error-superseded-explain": "admission cannot target authority replaced by an applied ruling",
    "error-path-escape-explain": "draft reads stay beneath the canonical root without following links",
    "error-not-found-explain": "submission reads its draft and key beneath the canonical root; a missing file refuses before admission",
    "error-header-explain": "submission requires the complete relay header contract",
    "error-envelope-explain": "the server-derived envelope and submitted envelope must agree",
    "error-replay-explain": "one submission id must always carry one content identity",
    "error-storage-explain": "the run record could not durably complete the requested operation",
    "error-daemon-down-explain": "mutating work requires the root's live designated writer",
    "error-seat-occupied-explain": "one seat has one current occupancy until explicit replacement",
    "error-commission-conflict-explain": "one child run has one canonical commissioning record",
    "error-commission-late-explain": "commissioning must precede child application state",
    "error-run-id-mismatch-explain": "the root's established run id is immutable",
    "error-run-id-uninitialized-explain": "a fresh root must establish its run id before adoption",
    "error-run-id-invalid-explain": "run ids use the filename-safe bounded grammar",
    "error-framing-explain": "wire input must be one complete length-prefixed UTF-8 JSON frame",
    "error-wire-version-explain": "wire requests use protocol version two",
    "error-wire-op-explain": "wire requests name one operation from the daemon table",
    "error-wire-args-explain": "wire operation arguments must match the operation schema",
    "error-daemon-stopping-explain": "the stop barrier refuses work arriving after its cutoff",
    "error-context-budget-explain": "context pagination refuses pages that exceed the byte budget",
    "error-version-mismatch-explain": "record operations require matching valid client and daemon identities",
})


def _is_not_found_rel(value):
    return (isinstance(value, str) and value and not value.startswith("/") and
            "\x00" not in value and "\n" not in value and "\r" not in value)


def _is_identity_value(value, validator):
    return strings._is_rejected(value) or validator(value)


def _is_version_mismatch_remedy(value):
    return (isinstance(value, _VersionMismatchRemedy) and
            value.stale in {"client", "daemon", "both"} and
            _is_identity_value(value.client_install, strings.valid_install) and
            _is_identity_value(value.daemon_install, strings.valid_install))


def _shape_value(value, kind):
    # Lazy import: envelope imports this registry while its module initializes.
    from relay_engine import envelope
    if kind == 'position':
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if kind == 'codepoint':
        return value in {pair[0] for pair in envelope.SEPARATORS.values()}
    if kind == 'name':
        return value in {pair[1] for pair in envelope.SEPARATORS.values()}
    if kind == 'control':
        return value in envelope.LATE_CONTROL_KEYS
    if kind == 'verdict':
        return (isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*', value) is not None
                and envelope._shape_verdict_key(value))
    if kind == 'boot':
        return (isinstance(value, str) and
                (value == 'body' or re.fullmatch(r'[A-Z][A-Z0-9_]*', value) is not None))
    return False


strings.register_inventory(
    _TEXT,
    {
        ('error-no-root-cause', 'directory'): lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
        ('error-no-root-unstarted-cause', 'directory'): lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
        ('error-no-root-unstarted-remedy', 'directory'): lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
        ('error-relay-not-found-cause', 'root'): lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
        ('error-relay-not-found-cause', 'target'): lambda v: strings._is_rejected(v) or strings._is_show_text(v),
        ('error-draft-shape-separator-cause', 'line'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-separator-cause', 'column'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-separator-cause', 'codepoint'): lambda v: _shape_value(v, 'codepoint'),
        ('error-draft-shape-separator-cause', 'name'): lambda v: _shape_value(v, 'name'),
        ('error-draft-shape-late-control-cause', 'line'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-control-in-block-cause', 'line'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-control-cause', 'key'): lambda v: _shape_value(v, 'control'),
        ('error-draft-shape-late-control-in-block-cause', 'key'): lambda v: _shape_value(v, 'control'),
        ('error-draft-shape-late-control-cause', 'first'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-control-in-block-cause', 'first'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-control-cause', 'last'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-control-in-block-cause', 'last'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-cause', 'line'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-in-block-cause', 'line'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-cause', 'key'): lambda v: _shape_value(v, 'verdict'),
        ('error-draft-shape-late-verdict-in-block-cause', 'key'): lambda v: _shape_value(v, 'verdict'),
        ('error-draft-shape-late-verdict-cause', 'first'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-in-block-cause', 'first'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-cause', 'last'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-late-verdict-in-block-cause', 'last'): lambda v: _shape_value(v, 'position'),
        ('error-draft-shape-boot-field-cause', 'field'): lambda v: _shape_value(v, 'boot'),
        ('error-draft-shape-separator-remedy', 'codepoint'): lambda v: _shape_value(v, 'codepoint'),
        ("error-framing-cause", "reason"): lambda v: v in strings.FRAMING_REASONS,
        ("error-header-cause", "field"): lambda v: (isinstance(v, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", v) is not None),
        ("error-envelope-edge-remedy", "targets"):
            strings.IS_EXISTING_TARGETS,
        ("error-not-found-cause", "field"): lambda v: v in ("draft", "key"),
        ("error-not-found-cause", "rel"): lambda v: (
            isinstance(v, strings.RejectedValue) or _is_not_found_rel(v)),
        ("error-not-found-remedy", "field"): lambda v: v in ("draft", "key"),
        ("error-wire-version-cause", "rejected_version"): lambda v: isinstance(v, strings.RejectedValue),
        ("error-wire-op-cause", "rejected_op"): lambda v: isinstance(v, strings.RejectedValue),
        ("error-wire-args-cause", "op"): lambda v: v in strings.OPS,
        ("error-wire-args-cause", "detail"): lambda v: (isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}:[A-Za-z0-9_-]{1,32}", v) is not None),
        ("error-version-mismatch-cause", "client_install"):
            lambda v: _is_identity_value(v, strings.valid_install),
        ("error-version-mismatch-cause", "client_kit"):
            lambda v: _is_identity_value(v, strings.valid_kit),
        ("error-version-mismatch-cause", "client_fp"):
            lambda v: _is_identity_value(v, strings.valid_fp),
        ("error-version-mismatch-cause", "daemon_install"):
            lambda v: _is_identity_value(v, strings.valid_install),
        ("error-version-mismatch-cause", "daemon_kit"):
            lambda v: _is_identity_value(v, strings.valid_kit),
        ("error-version-mismatch-cause", "daemon_fp"):
            lambda v: _is_identity_value(v, strings.valid_fp),
        ("error-version-mismatch-remedy", "remedy"):
            _is_version_mismatch_remedy,
    },
)


def _spec(stem, cls):
    return ErrorSpec("error-%s-cause" % stem,
                     "error-%s-remedy" % stem,
                     "error-%s-explain" % stem, cls)


ERRORS = {
    "E-DRAFT-SHAPE": _spec("draft-shape", "integrity"),
    "no-top-seat": _spec("no-top-seat", "command"),
    "E-KEY-MISMATCH": _spec("key-mismatch", "policy"),
    "E-ID-COLLISION": _spec("id-collision", "policy"),
    "E-SUPERSEDED": _spec("superseded", "policy"),
    "E-PATH-ESCAPE": _spec("path-escape", "policy"),
    "E-HEADER": _spec("header", "integrity"),
    "E-ENVELOPE": _spec("envelope", "integrity"),
    "E-REPLAY-MISMATCH": _spec("replay", "integrity"),
    "E-STORAGE": _spec("storage", "integrity"),
    "E-DAEMON-DOWN": _spec("daemon-down", "client"),
    "E-NO-ROOT": _spec("no-root", "client"),
    "seat-occupied": _spec("seat-occupied", "registration"),
    "commission-conflict": _spec("commission-conflict", "command"),
    "commission-late": _spec("commission-late", "command"),
    "run-id-mismatch": _spec("run-id-mismatch", "command"),
    "run-id-uninitialized": _spec("run-id-uninitialized", "command"),
    "run-id-invalid": _spec("run-id-invalid", "command"),
    "E-FRAMING": _spec("framing", "wire"),
    "E-WIRE-VERSION": _spec("wire-version", "wire"),
    "E-WIRE-OP": _spec("wire-op", "wire"),
    "E-WIRE-ARGS": _spec("wire-args", "wire"),
    "E-DAEMON-STOPPING": _spec("daemon-stopping", "wire"),
    "E-CONTEXT-BUDGET": _spec("context-budget", "wire"),
    "E-VERSION-MISMATCH": _spec("version-mismatch", "policy"),
}

ERROR_VARIANTS = {
    ("E-NO-ROOT", "unstarted"): _spec("no-root-unstarted", "client"),
    ("E-PATH-ESCAPE", "relay-not-found"): _spec("relay-not-found", "policy"),
    ("E-DRAFT-SHAPE", "separator"): _spec("draft-shape-separator", "integrity"),
    ("E-DRAFT-SHAPE", "late-control"): _spec("draft-shape-late-control", "integrity"),
    ("E-DRAFT-SHAPE", "late-control-in-block"): _spec("draft-shape-late-control-in-block", "integrity"),
    ("E-DRAFT-SHAPE", "late-verdict"): _spec("draft-shape-late-verdict", "integrity"),
    ("E-DRAFT-SHAPE", "late-verdict-in-block"): _spec("draft-shape-late-verdict-in-block", "integrity"),
    ("E-DRAFT-SHAPE", "boot-field"): _spec("draft-shape-boot-field", "integrity"),

    ("E-ENVELOPE", "edge-resolution"):
        _spec("envelope-edge", "integrity"),
    ("E-PATH-ESCAPE", "not-found"):
        _spec("not-found", "policy"),
}

POLICY_CODES = {code for code, spec in ERRORS.items() if spec.cls == "policy"}


def _render_for(key, params):
    fields = strings._placeholders(strings.INVENTORY[key])
    return strings.render(key, **{field: params[field] for field in fields})


def _rejected(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        raw = type(value).__name__.encode("ascii", "replace")
    return strings.rejected_value(hashlib.sha256(raw).hexdigest()[:12],
                                  len(raw))


def _rejected_identity(value):
    if isinstance(value, str):
        raw = value.encode("utf-8", "surrogatepass")
        return strings.rejected_value(hashlib.sha256(raw).hexdigest()[:12],
                                      len(raw))
    if isinstance(value, bytes):
        return strings.rejected_value(hashlib.sha256(value).hexdigest()[:12],
                                      len(value))
    return _rejected(value)


def _identity_value(value, validator):
    if validator(value):
        return value
    return _rejected_identity(value)


def _kit_tuple(value):
    if not strings.valid_kit(value):
        return None
    return tuple(int(component) for component in value.split("."))


def _version_mismatch_remedy(params):
    client_kit = _kit_tuple(params["client_kit"])
    daemon_kit = _kit_tuple(params["daemon_kit"])
    if client_kit is None or daemon_kit is None:
        stale = "both"
    elif client_kit < daemon_kit:
        stale = "client"
    elif daemon_kit < client_kit:
        stale = "daemon"
    else:
        stale = "both"
    return _VersionMismatchRemedy(stale, params["client_install"],
                                   params["daemon_install"])


class EngineError(Exception):
    def __init__(self, code, cause_key, remedy_key, cls, **params):
        if code not in ERRORS:
            raise KeyError(code)
        registered = [ERRORS[code]] + [
            spec for (variant_code, _), spec in ERROR_VARIANTS.items()
            if variant_code == code
        ]
        if (cause_key, remedy_key, cls) not in {
                (spec.cause_key, spec.remedy_key, spec.cls)
                for spec in registered}:
            raise KeyError("error specification is not registered")
        self.code = code
        self.cause_key = cause_key
        self.remedy_key = remedy_key
        self.cls = cls
        self.params = dict(params)
        self.cause = _render_for(cause_key, self.params)
        self.remedy = _render_for(remedy_key, self.params)
        super().__init__(code)

    def as_dict(self):
        return {"code": self.code, "cause": self.cause,
                "remedy": self.remedy, "cls": self.cls}


def error_for(code, *, variant=None, **params):
    if code not in ERRORS:
        raise KeyError(code)
    if variant is None:
        spec = ERRORS[code]
    else:
        try:
            spec = ERROR_VARIANTS[(code, variant)]
        except KeyError as exc:
            raise KeyError("error variant is not registered") from exc
    if (code, variant) == ("E-PATH-ESCAPE", "not-found"):
        rel = params.get("rel")
        if not _is_not_found_rel(rel):
            params["rel"] = _rejected(rel)
    if code == "E-NO-ROOT":
        if not strings._is_show_absolute(params.get("directory")):
            params["directory"] = _rejected(params.get("directory"))
    if (code, variant) == ("E-PATH-ESCAPE", "relay-not-found"):
        for field, validator in (("root", strings._is_show_absolute),
                                 ("target", strings._is_show_text)):
            if not validator(params.get(field)):
                params[field] = _rejected(params.get(field))
    if code == "E-VERSION-MISMATCH":
        for field, validator in (
                ("client_install", strings.valid_install),
                ("client_kit", strings.valid_kit),
                ("client_fp", strings.valid_fp),
                ("daemon_install", strings.valid_install),
                ("daemon_kit", strings.valid_kit),
                ("daemon_fp", strings.valid_fp)):
            params[field] = _identity_value(params.get(field), validator)
        params["remedy"] = _version_mismatch_remedy(params)
    return EngineError(code, spec.cause_key, spec.remedy_key, spec.cls,
                       **params)


def explain_for(code):
    if code not in ERRORS:
        raise KeyError(code)
    spec = ERRORS[code]
    if code == "E-VERSION-MISMATCH":
        return (strings.render(spec.explain_key),
                strings.render("error-version-mismatch-explain-remedy"))
    return (strings.render(spec.explain_key),
            strings.render(spec.remedy_key))
