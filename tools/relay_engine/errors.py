"""Structured relay-engine refusal registry."""

from dataclasses import dataclass
import hashlib
import json
import re

from relay_engine import strings
from relay_engine import api_definitions


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


# v3 header refusals (DD-v3-engine § 3, § 5, § 10): one code per rule; the
# cause names the line and the refused text, the remedy needs no incident value.
_HEADER_TEXT = {
    "error-header-grammar-cause": "line {line}: {value} is not a header line of the form KEY: value (capital ASCII letters and underscores, a colon, one space, a value with no space at either end)",
    "error-header-grammar-remedy": "rewrite the line as KEY: value with one space after the colon and no space at either end of the value, or end the header block with an empty line before it",
    "error-header-grammar-explain": "every line from the first line to the first empty line is a header line",
    "error-header-unknown-key-cause": "line {line}: {key} is not a v3 header field",
    "error-header-unknown-key-remedy": "use only the fields the v3 header defines; write anything else in the body",
    "error-header-unknown-key-explain": "a header field exists only when a program reads its value",
    "error-header-duplicate-cause": "line {line}: {field} appears again; it takes one value",
    "error-header-duplicate-remedy": "keep one line for each single-valued field",
    "error-header-duplicate-explain": "KIND, TO, CC, PARENT, BUNDLE, VERDICT, NEEDS and SUBJECT take one line each",
    "error-header-empty-cause": "line {line}: {key} has no value",
    "error-header-empty-remedy": "give the field a value, or remove the line",
    "error-header-empty-explain": "a header line always carries a value",
    "error-header-control-cause": "line {line}, column {column}: {codepoint} is a control, line-separator or bidirectional-control character",
    "error-header-control-remedy": "remove the character; the header allows tab and no other control character",
    "error-header-control-explain": "a header value is one line of printable text",
    "error-header-title-line-cause": "line {line} is a title line; a v3 draft starts with header lines",
    "error-header-title-line-remedy": "delete the title line and put its text in SUBJECT",
    "error-header-title-line-late-cause": "line {line} starts with #, so no empty line separates the header from the body",
    "error-header-title-line-late-remedy": "put one empty line between the last header line and the body",
    "error-header-title-line-explain": "the subject line replaces the v2 title line",
    "error-header-crlf-cause": "line {line} ends with a carriage return",
    "error-header-crlf-remedy": "save the draft with LF line endings",
    "error-header-crlf-explain": "header lines end with a line feed alone",
    "error-header-bom-cause": "line {line} starts with a UTF-8 byte-order mark",
    "error-header-bom-remedy": "save the draft as UTF-8 without a byte-order mark",
    "error-header-bom-explain": "the first header line starts at the first byte",
    "error-header-size-cause": "the header block is {size} bytes; the limit is {limit}",
    "error-header-size-unterminated-cause": "no empty line ends the header block within its first {limit} bytes",
    "error-header-size-unterminated-remedy": "end the header with one empty line; if the header itself is that long, move detail into the body",
    "error-header-size-remedy": "move detail from the header into the body",
    "error-header-size-explain": "sizes are checked before any parse; the header block is at most 8 KiB",
    "error-body-size-cause": "the body is {size} bytes; the limit is {limit}",
    "error-body-size-remedy": "put long material in an evidence file and point to it with REF",
    "error-body-size-explain": "sizes are checked before any parse; the body is at most 64 KiB",
    "error-field-enum-cause": "line {line}: {field} value {value} is not allowed; allowed: {allowed}",
    "error-field-enum-remedy": "use an allowed value for the field",
    "error-field-enum-explain": "fields with a closed set or a fixed form refuse any other value",
    "error-field-shape-cause": "line {line}: {field} value {value} does not have the form {form}",
    "error-field-shape-remedy": "write the value in the field's form",
    "error-field-shape-explain": "fields with a fixed form refuse any other spelling",
    "error-draft-utf8-cause": "the draft is not UTF-8 text: an invalid byte sequence starts at byte offset {offset} (the first byte is offset 0), in the body",
    "error-draft-utf8-header-cause": "the draft is not UTF-8 text: an invalid byte sequence starts at byte offset {offset} (the first byte is offset 0), on header line {line}",
    "error-draft-utf8-remedy": "save the draft as UTF-8",
    "error-draft-utf8-explain": "a draft, header and body alike, is UTF-8 text",
    "error-question-address-cause": "line {line}: a QUESTION goes to operator alone, and TO is {value}",
    "error-question-address-remedy": "address the QUESTION to operator alone; to ask a seat, use another kind with NEEDS: answer",
    "error-question-address-explain": "QUESTION is reserved for decision items in the operator's queue",
    "error-field-required-cause": "the draft has no {field} line: {rule}",
    "error-field-required-remedy": "add the missing line",
    "error-field-required-explain": "required fields depend on KIND and NEEDS",
    "error-field-forbidden-cause": "line {line}: {field} is not allowed here: {rule}",
    "error-field-forbidden-remedy": "remove the line, or change KIND or NEEDS so the field applies",
    "error-field-forbidden-bundle-remedy": "remove BUNDLE: a relay with a PARENT inherits its parent's bundle",
    "error-field-forbidden-explain": "some fields belong only to certain kinds, or only to replies",
    "error-field-length-cause": "line {line}: {field} is {length} characters; the limit is {limit}",
    "error-field-length-remedy": "shorten the value and put detail in the body",
    "error-field-length-explain": "length limits count characters, not bytes",
    "error-address-cause": "line {line}: {field} address {value} is refused: {reason}",
    "error-address-remedy": "write each address as <team>.<role>, <team>.<role>@<project> or operator, in lowercase, once across TO and CC",
    "error-address-explain": "addresses follow one grammar and are never case-folded",
    "error-address-alias-remedy": "write the role word {canonical}",
    "error-finding-no-check-cause": "line {line}: the finding cites REF line {cited}, and the header has {count} REF lines",
    "error-finding-no-check-remedy": "add a REF line naming the executed check's output and cite its number (1 for the first REF line), or mark the finding advisory",
    "error-finding-no-check-advisory-cause": "line {line}: the finding starts with the number {cited}, read as a REF line, and the header has {count} REF lines",
    "error-finding-no-check-relay-cause": "line {line}: the finding's check, REF line {cited}, names relay {value}; a blocking finding's check is a local path or a Git-pinned file",
    "error-finding-no-check-advisory-remedy": "cite an existing REF line by its number (1 for the first), or start the summary with a word",
    "error-finding-no-check-explain": "a blocking finding cites the REF line that records its executed check",
}


def _is_line(value):
    return strings._is_count(value) and value >= 1


def _is_header_key(value):
    return (isinstance(value, str) and len(value) <= 64 and
            re.fullmatch(r"[A-Z][A-Z_]*(?:\.\.\.)?", value) is not None)


def _is_header_text(value):
    return strings._is_show_text(value) and len(value) <= 512


_HEADER_VALIDATORS = {}
for _key, _template in _HEADER_TEXT.items():
    for _field in strings._placeholders(_template):
        _HEADER_VALIDATORS[(_key, _field)] = {
            "line": _is_line, "column": _is_line,
            "size": strings._is_count, "limit": strings._is_count,
            "length": strings._is_count, "count": strings._is_count,
            "offset": strings._is_count,
            "key": _is_header_key, "field": _is_header_key,
            "codepoint": lambda v: (isinstance(v, str) and
                                    re.fullmatch(r"U\+[0-9A-F]{4,6}", v) is not None),
        }.get(_field, _is_header_text)
strings.register_inventory(_HEADER_TEXT, _HEADER_VALIDATORS)

strings.register_inventory({
    "error-sql-version-cause": "the embedded SQL library is version {found}; the engine needs {minimum} or newer",
    "error-sql-version-remedy": "run the engine with a Python whose embedded SQL library is at least the minimum named in the cause",
    "error-schema-mismatch-cause": "the store file holds {found}",
    "error-schema-mismatch-remedy": "point the engine at an empty file, or at a store that this engine version created",
    "error-schema-mismatch-explain": "the engine creates its tables only in an empty file and opens only a store of its own schema version and table definitions",
    "error-sql-version-explain": "the engine's tables are strict tables, which need SQL library 3.37.0 or newer",
}, {
    ("error-sql-version-cause", "found"):
        lambda v: isinstance(v, str) and re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){2}", v) is not None,
    ("error-schema-mismatch-cause", "found"):
        lambda v: strings._is_show_text(v) and len(v) <= 200,
    ("error-sql-version-cause", "minimum"):
        lambda v: isinstance(v, str) and re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,4}){2}", v) is not None,
})

# Item 11 diagnostics (DD-v3-engine § 10): typed codes instead of
# misattributed or untyped failures.
def _is_reason(value):
    """A short reason: a fixed phrase or an errno text (fold C9)."""
    return strings._is_show_text(value) and len(value) <= 80

strings.register_inventory({
    "error-file-missing-cause": "the file {path} cannot be read: {reason}",
    "error-file-missing-remedy": "pass the path of an existing regular file you can read; a --from-file list is UTF-8 text",
    "error-file-missing-explain": "a file argument names an existing regular file, read without following a FIFO, and a carrier is never read through a symlink",
    "error-root-not-found-cause": "no relay root at or above {directory}, the --root given: {reason}",
    "error-root-not-found-remedy": "pass --root with a relay root's path: the directory that holds .engine/",
    "error-root-not-found-explain": "--root names a relay root or a directory inside one",
    "error-carrier-invalid-cause": "the carrier is not a framed record; it may be truncated or edited",
    "error-carrier-invalid-remedy": "export the ruling again with relay export-ruling and adopt the new file unchanged",
    "error-carrier-invalid-explain": "a ruling carrier is a framed record whose digest matches its bytes",
    "error-carrier-invalid-field-cause": "the carrier's {field} cannot be used: {reason}",
    "error-carrier-invalid-target-cause": "the carried ruling supersedes {path}, which this root does not hold",
    "error-carrier-invalid-target-remedy": "adopt the carrier in the root that holds the superseded relay, or ask the exporter which root that is",
    "error-carrier-field-cause": "the carrier is a framed record, but not a version 1 ruling carrier",
    "error-carrier-field-remedy": "export the ruling again with relay export-ruling and adopt the new file unchanged",
    "error-carrier-field-explain": "a ruling carrier holds exactly the fields relay export-ruling writes",
}, {
    ("error-file-missing-cause", "path"):
        lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
    ("error-file-missing-cause", "reason"): _is_reason,
    ("error-root-not-found-cause", "reason"): _is_reason,
    ("error-carrier-invalid-field-cause", "field"):
        lambda v: isinstance(v, str) and v in {"ruling_body_b64", "ruling_content_digest"},
    ("error-carrier-invalid-field-cause", "reason"):
        lambda v: isinstance(v, str) and v in {
            "not base64", "not UTF-8 text", "does not match the carried body",
            "the ruling names no SUPERSEDES target"},
    ("error-carrier-invalid-target-cause", "path"):
        lambda v: strings._is_rejected(v) or (strings._is_show_text(v) and len(v) <= 300),
    ("error-root-not-found-cause", "directory"):
        lambda v: strings._is_rejected(v) or strings._is_show_absolute(v),
})


# v3 submit rules (DD-v3-engine § 5, § 8, E-OP2): one code per rule.
_SUBMIT_TEXT = {
    "error-submission-key-reused-cause": "this sender party already used the submission key for different draft bytes",
    "error-submission-key-reused-remedy": "retry the original bytes with this key, or use a new key for a new submission",
    "error-submission-key-reused-explain": "a sender party and submission key identify one exact full input and its original admission receipt",
    "error-parent-unknown-cause": "line {line}: PARENT {value} names no relay in this project",
    "error-parent-unknown-remedy": "name a handle or UUID of a relay in this project; the parent-candidates query lists the relays you may name",
    "error-parent-unknown-explain": "PARENT names a relay of the sender's own project",
    "error-parent-not-member-cause": "line {line}: PARENT {value} was neither addressed to you (TO or CC) nor written by you",
    "error-parent-not-member-remedy": "name a relay addressed to you or written by you; if the grant you need is not among them, ask for it again and name the answer",
    "error-parent-not-member-explain": "a reply joins only a lineage its seat took part in",
    "error-finding-unknown-cause": "line {line}: {field} names finding {value}, which this project does not have",
    "error-finding-unknown-remedy": "use the finding handle F<n> shown on the review relay that raised it",
    "error-finding-unknown-explain": "FIXES and RESOLVE name findings raised in the same project",
    "error-resolve-not-raiser-cause": "line {line}: finding {value} was raised by another seat, and you do not hold the orchestrator-planner role",
    "error-resolve-not-raiser-remedy": "ask the seat that raised the finding, or the orchestrator planner, to resolve it",
    "error-resolve-not-raiser-explain": "only the raiser or an orchestrator planner resolves a finding",
    "error-resolve-state-cause": "line {line}: {field} {transition} does not apply to finding {value}, whose status is {status}",
    "error-resolve-state-remedy": "FIXES follows open; fix-accepted and fix-refused follow fixed; withdrawn follows open or fixed",
    "error-resolve-state-explain": "a finding moves from open to fixed, then to fix-accepted, back to open on fix-refused, or to withdrawn",
}
_FINDING_STATUSES = {"open", "fixed", "fix-accepted", "withdrawn"}
_TRANSITIONS = {"fixes", "fix-accepted", "fix-refused", "withdrawn"}
strings.register_inventory(_SUBMIT_TEXT, {
    (key, field): {"line": _is_line, "field": _is_header_key,
                   "value": _is_header_text,
                   "status": lambda v: v in _FINDING_STATUSES,
                   "transition": lambda v: v in _TRANSITIONS}[field]
    for key, template in _SUBMIT_TEXT.items()
    for field in strings._placeholders(template)
})


# Admission-hook values are type identities only, never exception messages.
def _is_hook_name(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and
            re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|<locals>|<lambda>))*",
                value) is not None)


def _is_hook_kind(value):
    return (isinstance(value, str) and 0 < len(value) <= 128 and
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None)


_HOOK_TEXT = {
    "error-admission-hook-cause": "the admission hook {hook} failed with {kind}; nothing from this draft was stored",
    "error-admission-hook-ended-transaction-cause": "the admission hook {hook} ended the transaction; nothing from this draft was stored",
    "error-admission-hook-remedy": "report it to the orchestrator; the draft can be submitted again once the hook is fixed",
    "error-admission-hook-explain": "admission and the hook's records commit together, so a failed hook refuses the whole draft rather than storing a relay without them",
}
strings.register_inventory(_HOOK_TEXT, {
    (key, field): {"hook": _is_hook_name, "kind": _is_hook_kind}[field]
    for key, template in _HOOK_TEXT.items()
    for field in strings._placeholders(template)
})


# A served sender's binding (slice 2a D5): raised by Platform's binding check, never by Engine.
_SENDER_REASONS = {"key_replaced", "seat_stood_down", "session_not_current"}
_SENDER_TEXT = {
    "error-sender-not-current-cause": "the sender's seat binding is not current ({reason}); nothing from this draft was stored",
    "error-sender-not-current-remedy": "sign in again with the seat's current key and session, then submit the same draft again",
    "error-sender-not-current-explain": "a served submit is admitted only while the sender's key, seat and session are current; key_replaced, seat_stood_down and session_not_current name what changed",
}
strings.register_inventory(_SENDER_TEXT, {
    (key, field): {"reason": lambda v: isinstance(v, str) and v in _SENDER_REASONS}[field]
    for key, template in _SENDER_TEXT.items()
    for field in strings._placeholders(template)
})


# v3 review snapshots (DD-v3-engine § 17): one code per rule.
_SNAPSHOT_TEXT = {
    "error-snapshot-stale-cause": "line {line}: {value} changed after review request #{handle} snapshotted it",
    "error-snapshot-stale-remedy": "request review of the current copy, then approve that request",
    "error-snapshot-stale-explain": "an approve locks the copy the review request saw, so the artifact must be unchanged when the approve is submitted",
    "error-snapshot-not-to-cause": "line {line}: only a TO addressee of review request #{handle} may approve it",
    "error-snapshot-not-to-remedy": "ask a TO addressee of the request to approve it, or reply with another verdict",
    "error-snapshot-not-to-explain": "an approve that locks a snapshot comes from a seat the request asked for review",
    "error-snapshot-no-ref-cause": "line {line}: this approve of review request #{handle} references none of the artifacts the request snapshotted",
    "error-snapshot-no-ref-remedy": "add a REF line for each artifact you approve, naming the file or Git path the request named",
    "error-snapshot-no-ref-explain": "an approve names the artifacts it locks",
    "error-snapshot-no-bytes-cause": "snapshot {handle} holds no bytes: {reason}",
    "error-snapshot-no-bytes-remedy": "read the artifact at the recorded digest from where it lives, such as Git at the cited commit",
    "error-snapshot-no-bytes-explain": "a file over 1 MiB is recorded by digest only, and a Git snapshot is read from Git",
    "error-snapshot-not-text-cause": "snapshot {handle} is not UTF-8 text",
    "error-snapshot-not-text-remedy": "write clean copies of both snapshots and compare them with a tool that reads bytes",
    "error-snapshot-not-text-explain": "a line difference is computed only between UTF-8 texts",
    "error-output-exists-cause": "{path} already exists",
    "error-output-exists-remedy": "name a path that does not exist, or ask for the file to be overwritten",
    "error-output-exists-explain": "a clean copy never replaces a file unless asked",
}
_NO_BYTES_REASONS = {"the file was over 1 MiB when the request was submitted",
                     "a Git snapshot is read from its repository, which was not given",
                     "a Git snapshot's repository is not known to this project",
                     "a Git snapshot's repository has no registered checkout in this project",
                     "no evidence store was configured when the request was submitted",
                     "the blob cannot be read from its checkout",
                     "no evidence store was given to read its bytes",
                     "the evidence store does not hold its bytes",
                     "the evidence store reports its bytes missing",
                     "the evidence store reports its bytes corrupt",
                     "the evidence store cannot decode its bytes"}
strings.register_inventory(_SNAPSHOT_TEXT, {
    (key, field): {"line": _is_line, "value": _is_header_text,
                   "handle": lambda v: strings._is_count(v) and v >= 1,
                   "reason": lambda v: v in _NO_BYTES_REASONS,
                   "path": lambda v: strings._is_rejected(v) or (
                       strings._is_show_text(v) and len(v) <= 300)}[field]
    for key, template in _SNAPSHOT_TEXT.items()
    for field in strings._placeholders(template)
})


# OB-SNAP-ACL-1 (DD-v3-engine-snapshot-acl D7): an overwrite that cannot keep the file's owner or group.
_OWNERSHIP_TEXT = {
    "error-output-ownership-cause": "{path} is not overwritten: the copy cannot keep its {reason}",
    "error-output-ownership-remedy": "remove the file or write the copy elsewhere",
    "error-output-ownership-explain": "a copy never takes over a file another user or group owns",
}
_OWNERSHIP_REASONS = frozenset({"owner", "group"})
strings.register_inventory(_OWNERSHIP_TEXT, {
    (key, field): {"path": lambda v: strings._is_rejected(v) or (
                       strings._is_show_text(v) and len(v) <= 300),
                   "reason": lambda v: v in _OWNERSHIP_REASONS}[field]
    for key, template in _OWNERSHIP_TEXT.items()
    for field in strings._placeholders(template)
})


# v3 references (DD-v3-engine § 6): one code per refusal rule.
_REF_TEXT = {
    "error-ref-spelling-cause": "REF {value} is not a reference spelling: {reason}",
    "error-ref-spelling-remedy": "write #<n>, #<n>@<project>, a lowercase UUID, <repo>@<rev>:<path>[#L<a>[-L<b>]] or file:<absolute or ~/ path>, exactly as it re-renders",
    "error-ref-spelling-explain": "a reference is accepted only when its parsed parts re-render to the same text",
    "error-ref-unresolved-cause": "REF {value} does not resolve: {reason}",
    "error-ref-unresolved-remedy": "commit the file, or correct the repository, revision or path",
    "error-ref-unresolved-explain": "every reference target is checked when the relay is submitted",
    "error-ref-ambiguous-cause": "REF {value} is ambiguous: an entry named {literal} also exists at commit {commit}",
    "error-ref-ambiguous-remedy": "rename the entry whose name ends in a line suffix, or cite another file",
    "error-ref-ambiguous-explain": "a path ending in #L<n> could name a file or a line range, so both existing is refused",
    "error-ref-range-cause": "REF {value} cites lines {start} to {end}; the file has {count} lines at commit {commit}",
    "error-ref-range-remedy": "cite lines inside the file at that revision",
    "error-ref-range-explain": "a line range is checked against the file at the cited revision",
    "error-ref-timeout-cause": "REF {value} was not checked: a Git query reached its time limit",
    "error-ref-timeout-remedy": "retry the submit; if it repeats, the machine is busy or the repository is large for the limit",
    "error-ref-timeout-explain": "Git queries run under a time limit; reaching it says nothing about the reference",
    "error-ref-git-environment-cause": "REF {value} was not checked: the engine's environment holds {count} GIT_ variables, among them {name}, which change what Git answers",
    "error-ref-git-environment-remedy": "start the engine with no GIT_ variables in its environment",
    "error-ref-git-environment-explain": "references resolve against the repository alone, so a variable that selects or configures Git is refused",
}


def _is_ref_text(value):
    return strings._is_show_text(value) and len(value) <= 512


def _is_commit(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is not None


strings.register_inventory(_REF_TEXT, {
    (key, field): {"commit": _is_commit, "start": strings._is_count,
                   "name": lambda v: (isinstance(v, str) and v.startswith("GIT_")
                                      and strings._is_show_text(v) and len(v) <= 64),
                   "end": strings._is_count, "count": strings._is_count,
                   }.get(field, _is_ref_text)
    for key, template in _REF_TEXT.items()
    for field in strings._placeholders(template)
})


def _spec(stem, cls):
    return ErrorSpec("error-%s-cause" % stem,
                     "error-%s-remedy" % stem,
                     "error-%s-explain" % stem, cls)


_SETTINGS_TEXT = {
    "error-setting-operator-only-cause": "only the operator may change project settings",
    "error-setting-operator-only-remedy": "ask the operator to make this setting change",
    "error-setting-operator-only-explain": "the setting step accepts only the operator principal supplied by the authenticated caller",
    "error-setting-unknown-cause": "the setting key is not registered",
    "error-setting-unknown-remedy": "use jev_record (bool), jev_act (bool), or doorbell_off_minutes (int from 1 to 1440)",
    "error-setting-unknown-explain": "project settings accept only the three registered keys",
    "error-setting-value-cause": "the setting value has the wrong type or is outside its range",
    "error-setting-value-remedy": "use jev_record (bool), jev_act (bool), or doorbell_off_minutes (int from 1 to 1440); bool is not an int for this setting",
    "error-setting-value-explain": "boolean settings require booleans and the doorbell threshold requires an integer from 1 to 1440",
    "error-jev-act-needs-record-cause": "Jev act requires Jev record",
    "error-jev-act-needs-record-remedy": "enable jev_record before jev_act, and disable jev_act before disabling jev_record",
    "error-jev-act-needs-record-explain": "the setting step refuses either change that would leave Jev act enabled without Jev record",
}
strings.register_inventory(_SETTINGS_TEXT, {})


ERRORS = {
    "E-SETTING-OPERATOR-ONLY": _spec("setting-operator-only", "policy"),
    "E-JEV-ACT-NEEDS-RECORD": _spec("jev-act-needs-record", "policy"),
    "E-SETTING-UNKNOWN": _spec("setting-unknown", "command"),
    "E-SETTING-VALUE": _spec("setting-value", "command"),
    "E-SNAPSHOT-STALE": _spec("snapshot-stale", "integrity"),
    "E-SNAPSHOT-NOT-TO": _spec("snapshot-not-to", "integrity"),
    "E-SNAPSHOT-NO-REF": _spec("snapshot-no-ref", "integrity"),
    "E-SNAPSHOT-NO-BYTES": _spec("snapshot-no-bytes", "command"),
    "E-SNAPSHOT-NOT-TEXT": _spec("snapshot-not-text", "command"),
    "E-OUTPUT-EXISTS": _spec("output-exists", "command"),
    "E-OUTPUT-OWNERSHIP": _spec("output-ownership", "command"),
    "E-ADMISSION-HOOK": _spec("admission-hook", "integrity"),
    "E-SUBMISSION-KEY-REUSED": _spec("submission-key-reused", "integrity"),
    "E-SENDER-NOT-CURRENT": _spec("sender-not-current", "policy"),
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
    "E-HEADER-GRAMMAR": _spec("header-grammar", "integrity"),
    "E-HEADER-UNKNOWN-KEY": _spec("header-unknown-key", "integrity"),
    "E-HEADER-DUPLICATE": _spec("header-duplicate", "integrity"),
    "E-HEADER-EMPTY": _spec("header-empty", "integrity"),
    "E-HEADER-CONTROL": _spec("header-control", "integrity"),
    "E-HEADER-TITLE-LINE": _spec("header-title-line", "integrity"),
    "E-HEADER-CRLF": _spec("header-crlf", "integrity"),
    "E-HEADER-BOM": _spec("header-bom", "integrity"),
    "E-HEADER-SIZE": _spec("header-size", "integrity"),
    "E-BODY-SIZE": _spec("body-size", "integrity"),
    "E-FIELD-ENUM": _spec("field-enum", "integrity"),
    "E-FIELD-REQUIRED": _spec("field-required", "integrity"),
    "E-FIELD-FORBIDDEN": _spec("field-forbidden", "integrity"),
    "E-FIELD-LENGTH": _spec("field-length", "integrity"),
    "E-ADDRESS": _spec("address", "integrity"),
    "E-FINDING-NO-CHECK": _spec("finding-no-check", "integrity"),
    "E-FIELD-SHAPE": _spec("field-shape", "integrity"),
    "E-DRAFT-UTF8": _spec("draft-utf8", "integrity"),
    "E-QUESTION-ADDRESS": _spec("question-address", "integrity"),
    "E-SQLITE-VERSION": _spec("sql-version", "client"),
    "E-SCHEMA-MISMATCH": _spec("schema-mismatch", "integrity"),
    "E-PARENT-UNKNOWN": _spec("parent-unknown", "integrity"),
    "E-PARENT-NOT-MEMBER": _spec("parent-not-member", "integrity"),
    "E-FINDING-UNKNOWN": _spec("finding-unknown", "integrity"),
    "E-RESOLVE-NOT-RAISER": _spec("resolve-not-raiser", "integrity"),
    "E-RESOLVE-STATE": _spec("resolve-state", "integrity"),
    "E-REF-SPELLING": _spec("ref-spelling", "integrity"),
    "E-REF-UNRESOLVED": _spec("ref-unresolved", "integrity"),
    "E-REF-AMBIGUOUS": _spec("ref-ambiguous", "integrity"),
    "E-REF-RANGE": _spec("ref-range", "integrity"),
    "E-REF-TIMEOUT": _spec("ref-timeout", "command"),
    "E-REF-GIT-ENVIRONMENT": _spec("ref-git-environment", "client"),
    "E-FILE-MISSING": _spec("file-missing", "client"),
    "E-ROOT-NOT-FOUND": _spec("root-not-found", "client"),
    "E-CARRIER-INVALID": _spec("carrier-invalid", "command"),
    "E-CARRIER-FIELD": _spec("carrier-field", "command"),
}

ERROR_VARIANTS = {
    ("E-ADMISSION-HOOK", "ended-transaction"): ErrorSpec(
        "error-admission-hook-ended-transaction-cause", "error-admission-hook-remedy",
        "error-admission-hook-explain", "integrity"),
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
    ("E-DRAFT-UTF8", "header"): ErrorSpec(
        "error-draft-utf8-header-cause", "error-draft-utf8-remedy",
        "error-draft-utf8-explain", "integrity"),
    ("E-HEADER-SIZE", "unterminated"): ErrorSpec(
        "error-header-size-unterminated-cause", "error-header-size-unterminated-remedy",
        "error-header-size-explain", "integrity"),
    ("E-HEADER-TITLE-LINE", "late"): ErrorSpec(
        "error-header-title-line-late-cause", "error-header-title-line-late-remedy",
        "error-header-title-line-explain", "integrity"),
    ("E-FINDING-NO-CHECK", "advisory"): ErrorSpec(
        "error-finding-no-check-advisory-cause", "error-finding-no-check-advisory-remedy",
        "error-finding-no-check-explain", "integrity"),
    ("E-FINDING-NO-CHECK", "relay"): ErrorSpec(
        "error-finding-no-check-relay-cause", "error-finding-no-check-remedy",
        "error-finding-no-check-explain", "integrity"),
    ("E-FIELD-FORBIDDEN", "bundle"): ErrorSpec(
        "error-field-forbidden-cause", "error-field-forbidden-bundle-remedy",
        "error-field-forbidden-explain", "integrity"),
    ("E-ADDRESS", "alias"): ErrorSpec(
        "error-address-cause", "error-address-alias-remedy",
        "error-address-explain", "integrity"),
    ("E-CARRIER-INVALID", "field"): ErrorSpec(
        "error-carrier-invalid-field-cause", "error-carrier-invalid-remedy",
        "error-carrier-invalid-explain", "command"),
    ("E-CARRIER-INVALID", "target"): ErrorSpec(
        "error-carrier-invalid-target-cause", "error-carrier-invalid-target-remedy",
        "error-carrier-invalid-explain", "command"),
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
    if code in ("E-NO-ROOT", "E-ROOT-NOT-FOUND"):
        if not strings._is_show_absolute(params.get("directory")):
            params["directory"] = _rejected(params.get("directory"))
    if code == "E-FILE-MISSING":
        if not strings._is_show_absolute(params.get("path")):
            params["path"] = _rejected(params.get("path"))
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


def api_error_catalog() -> api_definitions.ClosedErrorDefinitions:
    """Export the sole closed discovery error catalog, separate from legacy errors."""
    d = api_definitions
    # Reuse the neutral producer's nodes rather than author a second window/digest type.
    types = [t for t in d.discovery_definitions().types
             if t.name in {"ProtocolVersion", "ProtocolWindow", "Digest"}]
    types.append(d.TypeDef("ApiPinField", d.LiteralSet(("build_id", "schema_digest"))))
    rows = (
        ("E_API_PROTOCOL", "wire", "ApiProtocolCause", (
            ("malformed_frame", "resend_valid_request"),
            ("malformed_json", "resend_valid_request"),
            ("invalid_envelope", "resend_valid_request"),
            ("invalid_args", "resend_valid_request"),
            ("unknown_operation", "refresh_schema"),
        )),
        ("E_API_STATE", "wire", "ApiStateCause", (
            ("handshake_required", "reconnect_explicitly"),
            ("already_connected", "reconnect_explicitly"),
            ("non_increasing_request_id", "reconnect_explicitly"),
        )),
        ("E_API_INCOMPATIBLE", "version", "ApiIncompatibleCause", (
            ("protocol_window", "update_client"),
            ("pin_mismatch", "select_matching_candidate"),
        )),
        ("E_API_UNAVAILABLE", "unavailable", "ApiUnavailableCause", (
            ("runtime_not_ready", "restore_expected_runtime"),
            ("schema_unavailable", "restore_expected_runtime"),
        )),
        ("E_API_INTERNAL", "internal", "ApiInternalCause", (
            ("handler_fault", "report_nonsecret_failure"),
        )),
        ("E_API_AUTH", "auth", "ApiAuthCause", (
            ("key_rejected", "check_key_file"),
            ("key_replaced", "obtain_current_key"),
            ("seat_stood_down", "ask_top_seat"),
            ("session_not_current", "sign_in_again"),
            ("not_signed_in", "sign_in_first"),
            ("already_bound", "open_new_connection"),
        )),
        ("E_API_CONFLICT", "conflict", "ApiConflictCause", (
            ("operation_key_reused", "use_new_operation_key"),
        )),
    )
    variants = []
    for code, api_class, cause_name, branches in rows:
        arms = []
        branch_defs = []
        for kind, remedy in sorted(branches):
            suffix = "".join(part.title() for part in kind.split("_"))
            kind_name = "ApiKind" + suffix
            leaf_name = "ApiCause" + suffix
            types.append(d.TypeDef(kind_name, d.LiteralSet((kind,))))
            members = [d.FieldDef("kind", d.TypeRef(kind_name))]
            if kind == "protocol_window":
                members.extend((d.FieldDef("requested", d.TypeRef("ProtocolWindow")),
                                d.FieldDef("supported", d.TypeRef("ProtocolWindow"))))
            elif kind == "pin_mismatch":
                members.extend((d.FieldDef("field", d.TypeRef("ApiPinField")),
                                d.FieldDef("required", d.TypeRef("Digest")),
                                d.FieldDef("actual", d.TypeRef("Digest"))))
            types.append(d.TypeDef(leaf_name, d.Record(tuple(members))))
            arms.append(d.TypeRef(leaf_name))
            branch_defs.append(d.ErrorBranchDef(kind, remedy))
        types.append(d.TypeDef(cause_name, d.TaggedUnion("kind", tuple(arms))))
        variants.append(d.ErrorVariantDef(code, api_class, d.TypeRef(cause_name),
                                         tuple(branch_defs)))
    return d.ClosedErrorDefinitions(tuple(sorted(types, key=lambda item: item.name)),
                                    tuple(sorted(variants, key=lambda item: item.code)))
