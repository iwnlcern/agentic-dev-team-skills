"""v3 relay header: the field registry and the one parser (DD-v3-engine § 3, § 5).

Every reader of a v3 draft calls ``parse``; no rule reads the body.
"""

from dataclasses import dataclass
import re
import unicodedata

from relay_engine import errors


HEADER_CAP = 8192
BODY_CAP = 65536
SUBJECT_CAP = 400


@dataclass(frozen=True)
class FieldSpec:
    name: str
    count: str  # "1", "0-1" or "0-n"
    kinds: frozenset | None  # kinds the field may appear on; None = any kind
    reader: str  # the function that consumes the value


@dataclass(frozen=True)
class FindingSpec:
    severity: str
    check_index: int | None
    summary: str


@dataclass(frozen=True)
class Draft:
    kind: str
    to: tuple
    cc: tuple
    parent: str | None
    bundle: str | None
    verdict: str | None
    needs: str
    subject: str
    refs: tuple
    findings: tuple
    fixes: tuple
    resolves: tuple
    body: bytes
    header_bytes: int


KINDS = frozenset(
    "AUDIT DESIGN DESIGN-REVIEW PLAN PLAN-REVIEW IMPL REVIEW REVIEW-FOLD "
    "MERGE-GATE LIVE-VERIFY SITREP RECONCILE QUESTION".split())
NEEDS = frozenset({"act", "answer", "review", "none"})
_REVIEW_VERDICTS = frozenset({"approve", "must-revise", "reject-narrow",
                              "human-decision-required"})
VERDICTS = {
    "DESIGN-REVIEW": _REVIEW_VERDICTS,
    "PLAN-REVIEW": _REVIEW_VERDICTS,
    "REVIEW": _REVIEW_VERDICTS,
    "MERGE-GATE": frozenset({"merge-ready", "merge-blocked",
                             "human-decision-required"}),
    "LIVE-VERIFY": frozenset({"live-verified", "failed-live-verification",
                              "merged-not-deployed",
                              "deployed-not-live-verified"}),
}
FINDING_KINDS = frozenset({"DESIGN-REVIEW", "PLAN-REVIEW", "REVIEW",
                           "MERGE-GATE"})
SEVERITIES = frozenset({"blocking", "advisory"})
RESOLUTIONS = frozenset({"fix-accepted", "fix-refused", "withdrawn"})

FIELDS = (
    FieldSpec("KIND", "1", None, "submit.submit -> relays.kind"),
    FieldSpec("TO", "1", None, "submit.submit -> recipients (role to)"),
    FieldSpec("CC", "0-1", None, "submit.submit -> recipients (role cc)"),
    FieldSpec("PARENT", "0-1", None,
              "submit.submit -> relays.parent_id and the parent edge"),
    FieldSpec("BUNDLE", "0-1", None, "submit.submit -> bundles"),
    FieldSpec("VERDICT", "0-1", frozenset(VERDICTS),
              "submit.submit -> relays.verdict"),
    FieldSpec("NEEDS", "1", None, "submit.submit -> relays.needs"),
    FieldSpec("SUBJECT", "1", None, "submit.submit -> relays.subject"),
    FieldSpec("REF", "0-n", None, "refs.parse_ref via submit.submit -> edges"),
    FieldSpec("FINDING", "0-n", FINDING_KINDS,
              "submit.submit -> findings"),
    FieldSpec("FIXES", "0-n", frozenset({"REVIEW-FOLD"}),
              "submit.submit -> findings.fixed_commit"),
    FieldSpec("RESOLVE", "0-n", None,
              "submit.submit -> findings status transition"),
)
_SPECS = {spec.name: spec for spec in FIELDS}

ROLE_WORDS = frozenset({
    "planner", "implementer", "reviewer", "orchestrator-planner",
    "orchestrator-reviewer", "master-planner", "master-reviewer",
    "domain-planner", "domain-reviewer"})
_ALIASES = {"pair-planner": "planner", "pair-implementer": "implementer"}

_LINE = re.compile(r"([A-Z][A-Z_]*): (\S(?:.*\S)?)", re.DOTALL)
_EMPTY = re.compile(r"[A-Z][A-Z_]*:[ \t]*")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_TEAM = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_BUNDLE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_HANDLE = re.compile(r"#[1-9][0-9]{0,17}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FINDING_HANDLE = re.compile(r"F[1-9][0-9]{0,17}")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_INDEX = re.compile(r"[1-9][0-9]{0,5}")
_DIGITS = re.compile(r"[0-9]+")


def _display(text, limit=80):
    """Show untrusted header text bounded, with every control, format,
    separator and non-space blank character escaped."""
    out = []
    for char in text[:limit]:
        code = ord(char)
        if (code < 32 or 127 <= code <= 159 or char != " " and
                unicodedata.category(char) in _HIDDEN):
            out.append("\\u%04x" % code)
        else:
            out.append(char)
    if len(text) > limit:
        out.append("...")
    return "".join(out) or "(empty)"


_HIDDEN = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp", "Zs"})


def _short_key(key):
    """A key bounded for the message: at most 64 characters."""
    return key if len(key) <= 64 else key[:61] + "..."


def _refuse(code, variant=None, **params):
    return errors.error_for(code, variant=variant, **params)


def syntactic_address(value):
    """Interim address check (platform joint lock J2); Task 6 replaces it."""
    reason, canonical = _address_problem(value)
    if reason is None:
        return
    if canonical is not None:
        raise AddressRefused(reason, canonical)
    raise AddressRefused(reason)


class AddressRefused(Exception):
    """Raised by an address validator; parse turns it into E-ADDRESS."""

    def __init__(self, reason, canonical=None):
        super().__init__(reason)
        self.reason = reason
        self.canonical = canonical


def _address_problem(value):
    if value == "operator":
        return None, None
    if any(char.isspace() for char in value):
        return "whitespace inside an address", None
    if value != value.lower():
        return "uppercase letters", None
    seat, at, project = value.partition("@")
    if at:
        if not project or "@" in project:
            return "empty or repeated @", None
        if seat == "operator":
            return "operator takes no @project", None
        if _NAME.fullmatch(project) is None:
            return "project name outside [a-z0-9][a-z0-9-]{0,31}", None
    team, dot, role = seat.partition(".")
    if not dot:
        return "not an address: no .<role> part and not operator", None
    if _TEAM.fullmatch(team) is None:
        return "team name outside [a-z0-9][a-z0-9-]{0,63}", None
    if role in _ALIASES:
        return "unknown role word", _ALIASES[role]
    if role not in ROLE_WORDS:
        return "unknown role word", None
    return None, None


def parse(data, validate_address=syntactic_address):
    """Parse one v3 draft; refuse with one typed code per rule, never skip."""
    if not isinstance(data, bytes):
        raise TypeError("draft bytes required")
    header, body, header_bytes = _split(data)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as bad:
        if bad.start < len(header):
            raise _refuse("E-DRAFT-UTF8", variant="header", offset=bad.start,
                          line=data.count(b"\n", 0, bad.start) + 1) from None
        raise _refuse("E-DRAFT-UTF8", offset=bad.start) from None
    lines = header.split(b"\n") if header else []
    seen = {}
    values = []  # (line number, key, value) in header order
    for number, raw in enumerate(lines, start=1):
        key, value = _read_line(number, raw)
        spec = _SPECS.get(key)
        if spec is None:
            raise _refuse("E-HEADER-UNKNOWN-KEY", line=number, key=_short_key(key))
        if spec.count != "0-n" and key in seen:
            raise _refuse("E-HEADER-DUPLICATE", line=number, field=key)
        seen.setdefault(key, number)
        values.append((number, key, value))
    return _validate(values, seen, body, header_bytes, validate_address)


def _split(data):
    separator = data.find(b"\n\n", 0, HEADER_CAP + 2)
    if separator < 0:
        if len(data) > HEADER_CAP:
            _unterminated(data[:HEADER_CAP])
        header = data[:-1] if data.endswith(b"\n") else data
        return header, b"", len(data)
    header_bytes = separator + 2
    if header_bytes > HEADER_CAP:
        raise _refuse("E-HEADER-SIZE", size=header_bytes, limit=HEADER_CAP)
    body = data[header_bytes:]
    if len(body) > BODY_CAP:
        raise _refuse("E-BODY-SIZE", size=len(body), limit=BODY_CAP)
    return data[:separator], body, header_bytes


def _unterminated(prefix):
    """No empty line within the cap: name the first line rule a complete line
    in the scanned bytes breaks (a carriage return first, as for a CRLF-saved
    draft), else the size.  The last piece may be cut at the cap, so it is
    never judged."""
    offset = 0
    for number, raw in enumerate(prefix.split(b"\n")[:-1], start=1):
        if b"\r" in raw:
            raise _refuse("E-HEADER-CRLF", line=number)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as bad:
            raise _refuse("E-DRAFT-UTF8", variant="header",
                          offset=offset + bad.start, line=number) from None
        key, _ = _read_line(number, raw)
        if key not in _SPECS:
            raise _refuse("E-HEADER-UNKNOWN-KEY", line=number, key=_short_key(key))
        offset += len(raw) + 1
    raise _refuse("E-HEADER-SIZE", variant="unterminated", limit=HEADER_CAP)


# bidirectional controls reorder how a value reads (A12); other format
# characters stay admitted and are escaped wherever refusal text shows them
_BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                           "\u2066\u2067\u2068\u2069")


def _read_line(number, raw):
    if number == 1 and raw.startswith(b"\xef\xbb\xbf"):
        raise _refuse("E-HEADER-BOM", line=number)
    if raw.endswith(b"\r"):
        raise _refuse("E-HEADER-CRLF", line=number)
    text = raw.decode("utf-8")  # the whole draft decoded in parse()
    for column, char in enumerate(text, start=1):
        code = ord(char)
        if ((code < 32 and char != "\t") or 127 <= code <= 159 or
                char in "\u2028\u2029" or char in _BIDI_CONTROLS):
            raise _refuse("E-HEADER-CONTROL", line=number, column=column,
                          codepoint="U+%04X" % code)
    if text.startswith("#"):
        if number == 1:
            raise _refuse("E-HEADER-TITLE-LINE", line=number)
        raise _refuse("E-HEADER-TITLE-LINE", variant="late", line=number)
    match = _LINE.fullmatch(text)
    if match is None:
        if _EMPTY.fullmatch(text):
            raise _refuse("E-HEADER-EMPTY", line=number,
                          key=_short_key(text.split(":", 1)[0]))
        raise _refuse("E-HEADER-GRAMMAR", line=number, value=_display(text))
    return match.group(1), match.group(2)


_LIST_FORM = "<address>, <address> with one ASCII space or none after each comma"


def _addresses(number, field, value, validate_address, seen_addresses):
    """Items split at commas; the only whitespace beside a comma is one ASCII
    space after it (A11), and nothing is stripped.  Whitespace inside an item
    is the address grammar's to refuse."""
    items = value.split(",")
    for index, item in enumerate(items):
        if index and item.startswith(" "):
            item = item[1:]
        if item[:1].isspace() or item[-1:].isspace():
            raise _refuse("E-FIELD-SHAPE", line=number, field=field,
                          value=_display(value), form=_LIST_FORM)
        items[index] = item
    result = []
    for item in items:
        if not item:
            raise _refuse("E-ADDRESS", line=number, field=field,
                          value=_display(value), reason="empty list item")
        try:
            validate_address(item)
        except AddressRefused as refused:
            if refused.canonical is not None:
                raise _refuse("E-ADDRESS", variant="alias", line=number,
                              field=field, value=_display(item),
                              reason=refused.reason,
                              canonical=refused.canonical) from None
            raise _refuse("E-ADDRESS", line=number, field=field,
                          value=_display(item), reason=refused.reason) from None
        if item in seen_addresses:
            raise _refuse("E-ADDRESS", line=number, field=field,
                          value=_display(item),
                          reason="named more than once across TO and CC")
        seen_addresses.add(item)
        result.append(item)
    return tuple(result)



def _mirror_value(what, value, address):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("%s must be non-empty text without edge whitespace" % what)
    if any(unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp")
           for c in value):
        raise ValueError("%s holds a control or line-break character" % what)
    if address and ("," in value or any(c.isspace() for c in value)):
        raise ValueError("%s must be one address without commas or spaces" % what)
    return value


def render_mirror_header(sender, to, cc, parent_mirror_path):
    """FROM, TO, CC (when any) and IN_REPLY_TO (when a parent exists) lines,
    in that order and in 2.9.7's spelling, for readers of the mirror tree
    such as pdc's inbox sweeper; a value that could forge a line is refused."""
    if isinstance(to, str) or isinstance(cc, str) or not to:
        raise ValueError("TO must be a non-empty sequence of addresses; CC a sequence")
    lines = ["FROM: " + _mirror_value("FROM", sender, True),
             "TO: " + ", ".join(_mirror_value("TO", seat, True) for seat in to)]
    if cc:
        lines.append("CC: " + ", ".join(_mirror_value("CC", seat, True) for seat in cc))
    if parent_mirror_path is not None:
        lines.append("IN_REPLY_TO: " + _mirror_value("IN_REPLY_TO", parent_mirror_path, False))
    return lines


def _enum(number, field, value, allowed):
    if value not in allowed:
        raise _refuse("E-FIELD-ENUM", line=number, field=field,
                      value=_display(value), allowed=" ".join(sorted(allowed)))
    return value


def _shape(number, field, value, form):
    raise _refuse("E-FIELD-SHAPE", line=number, field=field,
                  value=_display(value), form=form)


def _validate(values, seen, body, header_bytes, validate_address):
    first = {}
    lists = {"REF": [], "FINDING": [], "FIXES": [], "RESOLVE": []}
    for number, key, value in values:
        if key in lists:
            lists[key].append((number, value))
        else:
            first[key] = (number, value)
    for field in ("KIND", "TO", "NEEDS", "SUBJECT"):
        if field not in first:
            raise _refuse("E-FIELD-REQUIRED", field=field,
                          rule="every draft carries %s" % field)
    kind = _enum(first["KIND"][0], "KIND", first["KIND"][1], KINDS)
    needs = _enum(first["NEEDS"][0], "NEEDS", first["NEEDS"][1], NEEDS)

    seen_addresses = set()
    to = _addresses(first["TO"][0], "TO", first["TO"][1], validate_address,
                    seen_addresses)
    if kind == "QUESTION" and to != ("operator",):
        raise _refuse("E-QUESTION-ADDRESS", line=first["TO"][0],
                      value=_display(first["TO"][1]))
    cc = ()
    if "CC" in first:
        cc = _addresses(first["CC"][0], "CC", first["CC"][1],
                        validate_address, seen_addresses)

    number, subject = first["SUBJECT"]
    if len(subject) > SUBJECT_CAP:
        raise _refuse("E-FIELD-LENGTH", line=number, field="SUBJECT",
                      length=len(subject), limit=SUBJECT_CAP)

    parent = bundle = None
    if "PARENT" in first:
        number, parent = first["PARENT"]
        if _HANDLE.fullmatch(parent) is None and _UUID.fullmatch(parent) is None:
            _shape(number, "PARENT", parent, "#<n> or a lowercase UUID")
    if "BUNDLE" in first:
        number, bundle = first["BUNDLE"]
        if parent is not None:
            raise _refuse("E-FIELD-FORBIDDEN", variant="bundle", line=number,
                          field="BUNDLE",
                          rule="BUNDLE opens a lineage, so it never sits beside PARENT")
        if _BUNDLE.fullmatch(bundle) is None:
            _shape(number, "BUNDLE", bundle, "[a-z0-9][a-z0-9-]{0,47}")
    if parent is None and bundle is None:
        raise _refuse("E-FIELD-REQUIRED", field="PARENT",
                      rule="a draft names its PARENT, or opens a lineage with BUNDLE")

    verdict = _verdict(kind, needs, first.get("VERDICT"))
    refs = tuple(value for _, value in lists["REF"])
    findings = _findings(kind, needs, lists["FINDING"], len(refs))
    fixes = _fixes(kind, lists["FIXES"])
    resolves = _resolves(lists["RESOLVE"])
    return Draft(kind=kind, to=to, cc=cc, parent=parent, bundle=bundle,
                 verdict=verdict, needs=needs, subject=subject, refs=refs,
                 findings=findings, fixes=fixes, resolves=resolves,
                 body=body, header_bytes=header_bytes)


def _verdict(kind, needs, entry):
    allowed = VERDICTS.get(kind)
    if allowed is None:
        if entry is not None:
            raise _refuse("E-FIELD-FORBIDDEN", line=entry[0], field="VERDICT",
                          rule="%s carries no verdict" % kind)
        return None
    if needs == "review":
        if entry is not None:
            raise _refuse("E-FIELD-FORBIDDEN", line=entry[0], field="VERDICT",
                          rule="NEEDS: review asks for a verdict, so the request carries none")
        return None
    if entry is None:
        raise _refuse("E-FIELD-REQUIRED", field="VERDICT",
                      rule="a %s reply (NEEDS other than review) carries VERDICT" % kind)
    return _enum(entry[0], "VERDICT", entry[1], allowed)


def _findings(kind, needs, entries, ref_count):
    form = "<blocking|advisory> [<REF line number>] <summary>, one space between words"
    result = []
    for number, value in entries:
        if kind not in FINDING_KINDS or needs == "review":
            raise _refuse("E-FIELD-FORBIDDEN", line=number, field="FINDING",
                          rule="FINDING sits only on replies of DESIGN-REVIEW, "
                               "PLAN-REVIEW, REVIEW and MERGE-GATE")
        pieces = value.split(" ")
        if "" in pieces or len(pieces) < 2:
            _shape(number, "FINDING", value, form)
        severity, rest = pieces[0], pieces[1:]
        _enum(number, "FINDING", severity, SEVERITIES)
        index = None
        if _DIGITS.fullmatch(rest[0]):
            if _INDEX.fullmatch(rest[0]) is None:
                _shape(number, "FINDING", value, form)
            index, rest = int(rest[0]), rest[1:]
        if not rest:
            _shape(number, "FINDING", value, form)
        summary = " ".join(rest)
        if len(summary) > 200:
            raise _refuse("E-FIELD-LENGTH", line=number, field="FINDING",
                          length=len(summary), limit=200)
        if index is None and severity == "blocking":
            raise _refuse("E-FINDING-NO-CHECK", line=number, cited="none",
                          count=ref_count)
        if index is not None and index > ref_count:
            raise _refuse("E-FINDING-NO-CHECK",
                          variant=None if severity == "blocking" else "advisory",
                          line=number, cited=str(index), count=ref_count)
        result.append(FindingSpec(severity, index, summary))
    return tuple(result)


def _fixes(kind, entries):
    result = []
    for number, value in entries:
        if kind != "REVIEW-FOLD":
            raise _refuse("E-FIELD-FORBIDDEN", line=number, field="FIXES",
                          rule="FIXES sits only on REVIEW-FOLD")
        parts = value.split(" ")
        if (len(parts) != 2 or _FINDING_HANDLE.fullmatch(parts[0]) is None or
                _COMMIT.fullmatch(parts[1]) is None):
            _shape(number, "FIXES", value,
                   "F<n> <full commit id, 40 or 64 lowercase hex>")
        result.append((parts[0], parts[1]))
    return tuple(result)


def _resolves(entries):
    form = "F<n> <fix-accepted|fix-refused|withdrawn> <reason>, one space between words"
    result = []
    for number, value in entries:
        pieces = value.split(" ")
        if ("" in pieces or len(pieces) < 3 or
                _FINDING_HANDLE.fullmatch(pieces[0]) is None):
            _shape(number, "RESOLVE", value, form)
        handle, status = pieces[0], pieces[1]
        _enum(number, "RESOLVE", status, RESOLUTIONS)
        reason = " ".join(pieces[2:])
        if len(reason) > 200:
            raise _refuse("E-FIELD-LENGTH", line=number, field="RESOLVE",
                          length=len(reason), limit=200)
        result.append((handle, status, reason))
    return tuple(result)
