"""Structured relay-engine refusal registry."""

from dataclasses import dataclass

from relay_engine import strings


E_DAEMON_DOWN_ESCALATION = "Hand-relay this escalation to the eligible starter: master-planner if a master tier exists (never the orchestrator session), else orchestrator-planner, else the operator. This escalation is the sole exception to daemon admission."


@dataclass(frozen=True)
class ErrorSpec:
    cause_key: str
    remedy_key: str
    cls: str


_TEXT = {
    "error-key-mismatch-cause": "registration tag does not name the current seat occupancy",
    "error-key-mismatch-remedy": "re-register or run from the occupying session",
    "error-id-collision-cause": "dispatch id is not reusable in this run scope",
    "error-id-collision-remedy": "use a new id or admit against the current id scope",
    "error-superseded-cause": "the admission target has an applied supersession",
    "error-superseded-remedy": "re-read the superseding ruling and admit against current authority",
    "error-path-escape-cause": "draft path is outside the canonical root",
    "error-path-escape-remedy": "use the canonical drafts location",
    "error-header-cause": "required relay header is missing or invalid",
    "error-header-remedy": "supply every required submission header",
    "error-envelope-cause": "submitted envelope does not match server-derived bytes",
    "error-envelope-remedy": "rebuild the envelope from the unchanged draft",
    "error-replay-cause": "submission id was replayed with changed content",
    "error-replay-remedy": "retry the original bytes or allocate a new submission id",
    "error-storage-cause": "the run record could not complete the operation",
    "error-storage-remedy": "inspect operator diagnostics and retry after repair",
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
    "error-wire-version-remedy": "send v:1",
    "error-wire-op-cause": "unknown op {rejected_op}",
    "error-wire-op-remedy": "use an op from the daemon op table",
    "error-wire-args-cause": "args schema violation for {op}: {detail}",
    "error-wire-args-remedy": "match the op's exact args schema",
    "error-daemon-stopping-cause": "daemon is draining its stop barrier",
    "error-daemon-stopping-remedy": "retry after restart; replay semantics make the retry safe",
}

strings.register_inventory(
    _TEXT,
    {
        ("error-framing-cause", "reason"): lambda v: v in strings.FRAMING_REASONS,
        ("error-wire-version-cause", "rejected_version"): lambda v: isinstance(v, strings.RejectedValue),
        ("error-wire-op-cause", "rejected_op"): lambda v: isinstance(v, strings.RejectedValue),
        ("error-wire-args-cause", "op"): lambda v: v in strings.OPS,
        ("error-wire-args-cause", "detail"): lambda v: (isinstance(v, str) and __import__("re").fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}:[A-Za-z0-9_-]{1,32}", v) is not None),
    },
)


ERRORS = {
    "E-KEY-MISMATCH": ErrorSpec("error-key-mismatch-cause", "error-key-mismatch-remedy", "policy"),
    "E-ID-COLLISION": ErrorSpec("error-id-collision-cause", "error-id-collision-remedy", "policy"),
    "E-SUPERSEDED": ErrorSpec("error-superseded-cause", "error-superseded-remedy", "policy"),
    "E-PATH-ESCAPE": ErrorSpec("error-path-escape-cause", "error-path-escape-remedy", "policy"),
    "E-HEADER": ErrorSpec("error-header-cause", "error-header-remedy", "integrity"),
    "E-ENVELOPE": ErrorSpec("error-envelope-cause", "error-envelope-remedy", "integrity"),
    "E-REPLAY-MISMATCH": ErrorSpec("error-replay-cause", "error-replay-remedy", "integrity"),
    "E-STORAGE": ErrorSpec("error-storage-cause", "error-storage-remedy", "integrity"),
    "E-DAEMON-DOWN": ErrorSpec("error-daemon-down-cause", "error-daemon-down-remedy", "client"),
    "seat-occupied": ErrorSpec("error-seat-occupied-cause", "error-seat-occupied-remedy", "registration"),
    "commission-conflict": ErrorSpec("error-commission-conflict-cause", "error-commission-conflict-remedy", "command"),
    "commission-late": ErrorSpec("error-commission-late-cause", "error-commission-late-remedy", "command"),
    "run-id-mismatch": ErrorSpec("error-run-id-mismatch-cause", "error-run-id-mismatch-remedy", "command"),
    "run-id-uninitialized": ErrorSpec("error-run-id-uninitialized-cause", "error-run-id-uninitialized-remedy", "command"),
    "run-id-invalid": ErrorSpec("error-run-id-invalid-cause", "error-run-id-invalid-remedy", "command"),
    "E-FRAMING": ErrorSpec("error-framing-cause", "error-framing-remedy", "wire"),
    "E-WIRE-VERSION": ErrorSpec("error-wire-version-cause", "error-wire-version-remedy", "wire"),
    "E-WIRE-OP": ErrorSpec("error-wire-op-cause", "error-wire-op-remedy", "wire"),
    "E-WIRE-ARGS": ErrorSpec("error-wire-args-cause", "error-wire-args-remedy", "wire"),
    "E-DAEMON-STOPPING": ErrorSpec("error-daemon-stopping-cause", "error-daemon-stopping-remedy", "wire"),
}

POLICY_CODES = {code for code, spec in ERRORS.items() if spec.cls == "policy"}


def _render_for(key, params):
    fields = strings._placeholders(strings.INVENTORY[key])
    return strings.render(key, **{field: params[field] for field in fields})


class EngineError(Exception):
    def __init__(self, code, cause_key, remedy_key, cls, **params):
        if code not in ERRORS:
            raise KeyError(code)
        spec = ERRORS[code]
        if (cause_key, remedy_key, cls) != (
                spec.cause_key, spec.remedy_key, spec.cls):
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


def error_for(code, **params):
    if code not in ERRORS:
        raise KeyError(code)
    spec = ERRORS[code]
    return EngineError(code, spec.cause_key, spec.remedy_key, spec.cls,
                       **params)
