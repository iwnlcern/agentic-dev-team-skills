#!/usr/bin/env python3
"""relay-lint: truth-agnostic structure checker for agentic-dev-team relay files.

This linter checks relay shape, enums, addressing, phase/authority consistency,
downgrade field ordering, read-only status proof substance, delegated dispatch
structure, addressed merge-token authorization, review-fold scope artifacts,
relay report-field grammar, role/FROM consistency, mandatory pair-Planner parent-lineage shape, and orchestrator-reviewer visibility for authority-bearing orchestrator relays.
It does not verify whether claims are true.
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Relay filename timestamps must name the real authoring time. Wall-clock drift
# is a fabrication risk, not a cosmetic one: a stamp the author invented makes the
# append-only trail unorderable and lets a relay appear to precede its own parent.
# The stamp is checked for (a) being a real date/time and (b) being close to the
# clock at authoring time. Freshness is only enforced where it is meaningful --
# on explicitly-linted files (the authoring path) -- never on a historical sweep.
FILENAME_TS_RE = re.compile(r"(\d{8})[-T]?(\d{6})(Z?)")
DEFAULT_MAX_DRIFT_MINUTES = 2
INDEX_TIME_FORMATS = (
    "%Y%m%d-%H%M%S", "%Y%m%d-%H%M%SZ", "%Y%m%dT%H%M%SZ",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
)
# Position-based strictness boundary for an append-only index. Rows *after* the
# marker line must be non-decreasing and at/after the marker's own stamp; rows
# before it are grandfathered history (see --index-audit). Position, not value,
# defines the boundary -- a value-based cutoff would be defeated by pre-existing
# rows stamped in the future, which would force every later row to keep matching
# the fabrication instead of the real clock.
INDEX_MONOTONIC_MARKER_RE = re.compile(
    r"<!--\s*relay-lint:\s*monotonic-from\s+(\S[^>]*?)\s*-->", re.IGNORECASE
)

ROLE_VALUES = {
    "Planner", "Implementer", "Orchestrator Planner", "Orchestrator Reviewer", "Reviewer",
    "Downstream Agent Pair", "Downstream Implementer",
    "Master Planner", "Master Reviewer", "Domain Planner", "Domain Reviewer",
    "Pair Planner", "Pair Implementer",
    "Operator",
}
PHASE_VALUES = {
    "AUDIT", "DESIGN", "DESIGN-REVIEW", "PLAN", "PLAN-REVIEW", "IMPL", "REVIEW-FOLD",
    "MERGE-GATE", "LIVE-VERIFY", "SITREP", "RECONCILE",
}
AUTHORITY_VALUES = {
    "read-only", "plan-only", "design-only", "review-only", "implementation",
    "fold-in-only", "merge-gated", "live-verify", "report-only",
    "design-only for Planner; read-only challenge/answers for Implementer",
}
CEREMONY_VALUES = {"tiny", "small", "medium", "large", "production-risk"}
EVIDENCE_VALUES = {"E1", "E2", "E3", "E4"}
ADDRESS_ROLE_VALUES = {
    "planner", "implementer", "orchestrator-planner", "orchestrator-reviewer", "reviewer",
    "master-planner", "master-reviewer", "domain-planner", "domain-reviewer",
    "pair-planner", "pair-implementer",
}
SPECIAL_ADDRESS_VALUES = {"orchestrator", "operator"}
VERDICT_VALUES = {
    "merge-blocked", "merged-not-deployed", "deployed-not-live-verified",
    "failed-live-verification", "complete", "human-decision-required",
}
SCAN_RESULT_VALUES = {"all-no", "trigger-present", "unknown-present"}
DESIGN_RECORD_KIND_VALUES = {"design-doc", "audit-record", "direct-override"}
DESIGN_REVIEW_VERDICT_VALUES = {"approve", "must-revise", "reject-narrow", "human-decision-required"}
PLACEHOLDER_RE = re.compile(r"^<[^<>]+>$")
ABSENCE_WORD_RE = re.compile(r"^(unavailable|none|e0/unverified|unverified|n/?a|n\.a\.)\b", re.IGNORECASE)
STRUCTURED_ABSENT_RE = re.compile(r"^(unavailable|none|e0/unverified|unverified|n/?a|n\.a\.)\s*[—–-]+\s*\S", re.IGNORECASE)
SCOPE_DIFF_ROW_RE = re.compile(r"^\s*-\s+(\S.*?)\s*(?:->|:)\s*(in|out)\s*$", re.IGNORECASE)
READ_ONLY_PHASES = {"AUDIT", "DESIGN", "DESIGN-REVIEW", "PLAN", "PLAN-REVIEW", "RECONCILE", "SITREP"}
ROW_BEARING_HEADERS = ("SCOPE_DIFF", "SCOPE_DIFF_RESULT", "FOLD_SCOPE", "FOLD_SCOPE_RESULT")
ROLE_TO_ADDRESS_ROLE = {
    "Planner": "planner",
    "Implementer": "implementer",
    "Orchestrator Planner": "orchestrator-planner",
    "Orchestrator Reviewer": "orchestrator-reviewer",
    "Reviewer": "reviewer",
    "Downstream Implementer": "implementer",
    "Master Planner": "master-planner",
    "Master Reviewer": "master-reviewer",
    "Domain Planner": "domain-planner",
    "Domain Reviewer": "domain-reviewer",
    "Pair Planner": "pair-planner",
    "Pair Implementer": "pair-implementer",
    # H32 (192606 ruling): ROLE is author-truth; where a special address
    # authors, the enum grows rather than borrowing a seat fiction. Operator
    # classes to the operator special address.
    "Operator": "operator",
}
# D3: the pair tier gains a prefix; bare legacy forms stay valid permanently.
# Gates compare canonical roles so pair-planner/planner are one seat-role.
PAIR_ROLE_CANONICAL = {"pair-planner": "planner", "pair-implementer": "implementer"}


def canonical_role(role: str | None) -> str | None:
    return PAIR_ROLE_CANONICAL.get(role, role) if role is not None else None


LINEAGE_DIRECT_FROM_ROLES = {"operator", "orchestrator", "orchestrator-planner"}
UNRULED_AUTHORITY_ROLES = {"master-planner", "master-reviewer", "domain-planner", "domain-reviewer"}

CANONICAL_SCAN_ROWS = [
    "authz/tenant/RLS/permissions/secrets",
    "migration/backfill/destructive-write/canonical-data-repair",
    "money/inventory/orders/planning/accounting/trust-critical-state",
    "AI-or-automation-acts-downstream",
    "worker/scheduler/queue/retry/async-side-effect",
    "cross-repo/service-contract/generated-schema/shared-API-event",
    "user-visible-control-with-materializer/downstream-consumer",
    "test-runtime-role-mismatch",
    "broad-scope-expansion/ambiguous-product-semantics/residual-risk/live-verify-skip",
]
CANONICAL_SCAN_ROWS_NORMALIZED = {row.lower(): row for row in CANONICAL_SCAN_ROWS}

# Allowed authority hints by phase. Some templates combine semicolon-separated authorities.
PHASE_AUTHORITY_ALLOWED = {
    "AUDIT": {"read-only", "review-only", "report-only"},
    "DESIGN": {"design-only", "read-only", "review-only", "plan-only"},
    "DESIGN-REVIEW": {"review-only", "read-only"},
    "PLAN": {"plan-only", "read-only", "review-only"},
    "PLAN-REVIEW": {"review-only", "read-only", "plan-only"},
    "IMPL": {"implementation"},
    "REVIEW-FOLD": {"fold-in-only", "implementation"},
    "MERGE-GATE": {"merge-gated", "review-only", "report-only"},
    "LIVE-VERIFY": {"live-verify", "report-only"},
    "SITREP": {"report-only", "read-only"},
    "RECONCILE": {"review-only", "report-only", "read-only"},
}

MIN_HEADER_FIELDS = [
    "ROLE", "PHASE", "AUTHORITY", "DISPATCH_ID", "CEREMONY_TIER",
    "EVIDENCE_TARGET", "HUMAN_GATE_REQUIRED",
]

class LintResult:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(errors="replace")


def strip_fenced_blocks(text: str) -> str:
    out: List[str] = []
    in_fence = False
    for line in text.splitlines():
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return "\n".join(out)


def strip_inline_code_spans(text: str) -> str:
    """Remove single-line Markdown inline-code spans.

    Operational dispatch tokens inside backticks are quoted mentions, not
    authority. This intentionally keeps fenced-code stripping separate so
    normal relay parsing can still operate on non-token fields.
    """
    return "\n".join(re.sub(r"`[^`\n]*`", "", line) for line in text.splitlines())


def operational_token_text(text: str) -> str:
    return strip_inline_code_spans(strip_fenced_blocks(text))


def fenced_dispatch_mentions(text: str) -> List[int]:
    warnings: List[int] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence and "DISPATCH IMPL" in line:
            warnings.append(i)
    return warnings


GENERIC_FIELD_HEADER_RE = re.compile(r"^[A-Z][A-Z0-9_-]*:\s*")


def duplicate_row_bearing_header_errors(text: str) -> List[str]:
    """Detect multiple operational SCOPE_DIFF/FOLD_SCOPE blocks.

    The relay report of record carries exactly one row-bearing block and one
    matching RESULT block. Honest quoting belongs in fenced or backticked text,
    which is stripped from operational text before this check.
    """
    op = operational_token_text(text)
    errors: List[str] = []
    for header in ROW_BEARING_HEADERS:
        matches = list(re.finditer(rf"^{re.escape(header)}\s*:", op, flags=re.IGNORECASE | re.MULTILINE))
        if len(matches) > 1:
            errors.append(f"duplicate {header} block; the report of record carries exactly one")
    return errors


def ambiguous_continuation_errors(text: str, header: str) -> List[str]:
    """Find indented continuation-looking lines after a blank-closed field block.

    Field blocks are contiguous: they end at the first blank line or the
    next ALL-CAPS header. An indented non-header line after that blank is
    ambiguous because weak agents can hide a continuation-line action claim
    below the boundary. This is a structural error, not a truth check.
    """
    lines = sanitized_text(text).splitlines()
    header_re = re.compile(rf"^{re.escape(header)}:\s*", re.IGNORECASE)
    errors: List[str] = []
    in_block = False
    for idx, line in enumerate(lines):
        if header_re.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        if GENERIC_FIELD_HEADER_RE.match(line):
            in_block = False
            continue
        if not line.strip():
            in_block = False
            j = idx + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = lines[j]
                if nxt[:1].isspace() and not GENERIC_FIELD_HEADER_RE.match(nxt):
                    errors.append(
                        f"{header} has ambiguous indented continuation after a blank line at line {j + 1}; field blocks must be contiguous"
                    )
            continue
    return errors




def detached_row_errors(text: str) -> List[str]:
    """Find row-shaped lines outside their row-bearing field block.

    A line such as '- file -> out' visually reads as part of SCOPE_DIFF or
    FOLD_SCOPE even when a blank line or RESULT header has closed the block.
    Truth-agnostic lint treats that as structural ambiguity for row-bearing
    blocks. Flush-left prose remains outside this check.
    """
    errors: List[str] = []
    lines = operational_token_text(text).splitlines()
    header_re = GENERIC_FIELD_HEADER_RE
    for header in ("SCOPE_DIFF", "FOLD_SCOPE"):
        result_re = re.compile(rf"^{header}_RESULT\s*:", re.IGNORECASE)
        header_line_re = re.compile(rf"^{re.escape(header)}\s*:", re.IGNORECASE)
        for start, line in enumerate(lines):
            if not header_line_re.match(line):
                continue
            i = start + 1
            # Consume the contiguous block body until a blank line or any header.
            while i < len(lines) and lines[i].strip() and not header_re.match(lines[i]):
                i += 1
            # Continue scanning the post-block window until the next real header,
            # but pass through this block's own RESULT header so PB/FD11-style
            # trailing rows directly under RESULT are visible.
            while i < len(lines) and (not header_re.match(lines[i]) or result_re.match(lines[i])):
                if SCOPE_DIFF_ROW_RE.match(lines[i]):
                    errors.append(f"row-shaped line outside the {header} block: {lines[i].strip()!r}; rows must sit contiguously under their header")
                i += 1
    return errors


def block_start_index(text: str, header: str) -> int:
    """Line index of the first 'HEADER:' line in operational text, or -1."""
    for i, line in enumerate(operational_token_text(text).splitlines()):
        if re.match(rf"^{re.escape(header)}\s*:", line):
            return i
    return -1


def substantive_actions_ref(text: str, fields: Dict[str, str]) -> bool:
    """ACTIONS_GIT_REF is present with real content.

    A one-line structured absence such as 'none — no edits made' is an absence
    report, not a fold action. If refs appear on continuation lines after an
    absence prefix, the record is substantive and must be scoped.
    """
    if "ACTIONS_GIT_REF" not in fields:
        return False
    content = field_block_content(text, "ACTIONS_GIT_REF")
    if not content:
        return False
    return not (STRUCTURED_ABSENT_RE.match(content) and len(content.splitlines()) == 1)



def implementation_work_claimed(text: str, fields: Dict[str, str]) -> bool:
    """ACTIONS_GIT_REF claims implementation work, not only merge visibility.

    This powers the non-addressee IMPL-report trap. Merge claims have their
    own MERGE-GATE lineage and may contain words like branch/commit/merged; do
    not make that channel require an implementation-dispatch parent.
    """
    if not substantive_actions_ref(text, fields):
        return False
    content = field_block_content(text, "ACTIONS_GIT_REF")
    patterns = [
        r"\bedited\b",
        r"\bcreated\b",
        r"\bdeleted\b",
        r"\bchanged\b",
        r"\bmodified\b",
        r"\bupdated\b",
        r"\bimplemented\b",
        r"\bapplied\s+(?:a\s+)?patch\b",
        r"\bmigration\s+file\b",
        r"\bbackfill\b",
        r"\bopened\s+a\s+PR\b",
    ]
    return any(re.search(pattern, content, flags=re.IGNORECASE) for pattern in patterns)

def remove_named_block(text: str, header: str) -> str:
    lines = text.splitlines()
    out: List[str] = []
    in_block = False
    header_re = re.compile(rf"^{re.escape(header)}:\s*", re.IGNORECASE)
    for line in lines:
        if header_re.match(line):
            in_block = True
            out.append("")
            continue
        if in_block and (not line.strip() or GENERIC_FIELD_HEADER_RE.match(line)):
            in_block = False
        out.append("" if in_block else line)
    return "\n".join(out)


def sanitized_text(text: str) -> str:
    return strip_fenced_blocks(text)


def h27_occurrences(text: str, key: str) -> List[str]:
    """Every own-line occurrence of key over fence-sanitized lines."""
    out: List[str] = []
    pat = re.compile(r"^" + re.escape(key) + r":\s*(.*)$")
    for line in sanitized_text(text).splitlines():
        m = pat.match(line.rstrip())
        if m:
            out.append(m.group(1).strip())
    return out


def extract_named_block(text: str, header: str) -> str:
    """Return a contiguous FIELD: block.

    Blocks include the field line plus continuation lines until the first blank
    line or the next ALL-CAPS header field.

    Header fields in relays may be written either inline::

        ACTIONS_GIT_REF: merge=abc123 target_contains=yes

    or as a continuation block::

        ACTIONS_GIT_REF:
          commit abc123 — merged to master

    The merge-claim check needs the whole standardized field block, not only
    the same-line value harvested by ``header_fields``.
    """
    lines = sanitized_text(text).splitlines()
    block: List[str] = []
    in_block = False
    header_re = re.compile(rf"^{re.escape(header)}:\s*", re.IGNORECASE)
    for line in lines:
        if header_re.match(line):
            in_block = True
            block.append(line)
            continue
        if in_block and (not line.strip() or GENERIC_FIELD_HEADER_RE.match(line)):
            break
        if in_block:
            block.append(line)
    return "\n".join(block)


def field_block_content(text: str, header: str) -> str:
    """Return same-line value plus continuation lines of a FIELD: block, stripped."""
    block = extract_named_block(text, header)
    if not block:
        return ""
    lines = block.splitlines()
    first = re.sub(rf"^{re.escape(header)}:\s*", "", lines[0], flags=re.IGNORECASE)
    return "\n".join([first] + lines[1:]).strip()


def report_field_error(text: str, header: str) -> str | None:
    """Strict-mode substance check for a report-of-record field.

    Empty fields, placeholders, and reason-less reserved absence words are lint
    errors. Structured absence with a reason is allowed for FINAL_GIT_STATUS_SHORT
    and ACTIONS_GIT_REF; SCOPE_DIFF has its own row grammar and no structured-none
    escape for delegated dispatch.
    """
    content = field_block_content(text, header)
    if not content:
        return f"{header} is empty; provide content or 'unavailable — <reason>' / 'none — <reason>'"
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if lines and all(PLACEHOLDER_RE.match(line) for line in lines):
        return f"{header} carries a placeholder, not content; placeholders are valid only in --templates mode"
    if ABSENCE_WORD_RE.match(content) and not STRUCTURED_ABSENT_RE.match(content):
        return f"{header} uses a reserved absence word without a reason; use 'unavailable — <reason>' / 'none — <reason>'"
    return None


def parse_marked_rows(text: str, header: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Parse '<path> -> in|out' rows under a field block.

    Unparseable non-blank lines are errors, never dropped. The error text
    interpolates the header so existing SCOPE_DIFF assertions stay byte-stable.
    """
    block = extract_named_block(text, header)
    rows: List[Tuple[str, str]] = []
    errors: List[str] = []
    for line in block.splitlines()[1:]:
        if not line.strip():
            continue
        m = SCOPE_DIFF_ROW_RE.match(line)
        if m:
            rows.append((m.group(1), m.group(2).lower()))
        else:
            errors.append(f"unparseable {header} row: {line.strip()!r}")
    return rows, errors


def parse_scope_diff_rows(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    return parse_marked_rows(text, "SCOPE_DIFF")


def header_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for line in sanitized_text(text).splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$", line.rstrip())
        if m:
            fields.setdefault(m.group(1), m.group(2).strip())
    return fields


def find_line(text: str, token: str) -> int | None:
    for i, line in enumerate(sanitized_text(text).splitlines(), start=1):
        if re.match(rf"^{re.escape(token)}:\s*", line):
            return i
    return None


def parse_scan_rows(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Return normalized scan rows and structural row errors.

    ESCALATION_SCAN is a contiguous field block. Row values must begin with
    yes/no/unknown. Unparseable rows are returned as errors instead of being
    silently dropped.
    """
    rows: Dict[str, str] = {}
    errors: List[str] = []
    block = extract_named_block(text, "ESCALATION_SCAN")
    for line in block.splitlines()[1:]:
        if not line.strip():
            continue
        m = re.match(r"^\s*-\s*([^:]+):\s*(.*)$", line)
        if not m:
            errors.append(f"unparseable ESCALATION_SCAN row: {line.strip()!r}")
            continue
        raw_key = m.group(1).strip()
        raw_value = m.group(2).strip()
        norm_key = raw_key.lower()
        if norm_key not in CANONICAL_SCAN_ROWS_NORMALIZED:
            errors.append(f"non-canonical ESCALATION_SCAN row: {raw_key!r}")
            continue
        vm = re.match(r"^(yes|no|unknown)\b", raw_value, flags=re.IGNORECASE)
        if not vm:
            errors.append(f"ESCALATION_SCAN row {raw_key!r} has non-canonical value: {raw_value!r}")
            continue
        if norm_key in rows:
            errors.append(f"duplicate ESCALATION_SCAN row: {raw_key!r}")
        rows[norm_key] = vm.group(1).lower()
    return rows, errors


def authority_consistent(phase: str, authority: str) -> bool:
    allowed = PHASE_AUTHORITY_ALLOWED.get(phase)
    if not allowed:
        return True
    lower = authority.lower()
    return any(a in lower for a in allowed)


def enum_field(result: LintResult, fields: Dict[str, str], name: str, values: set[str], *, template_mode: bool) -> None:
    val = fields.get(name)
    if val is None:
        return
    if template_mode and ("<" in val or "|" in val):
        return
    if val not in values:
        result.error(f"{name} has non-canonical value: {val!r}")

def split_addresses(raw: str | None) -> List[str]:
    if raw is None or raw.strip() == "":
        return []
    return [part.strip() for part in raw.split(",")]


def address_role(address: str) -> str | None:
    val = address.strip()
    low = val.lower()
    if low in SPECIAL_ADDRESS_VALUES:
        return low
    m = re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.([A-Za-z][A-Za-z0-9-]*)$", val)
    if not m:
        return None
    role = m.group(1).lower()
    return role if role in ADDRESS_ROLE_VALUES else None


def address_owner(address: str) -> str | None:
    val = address.strip()
    if val.lower() in SPECIAL_ADDRESS_VALUES:
        return val.lower()
    if "." not in val:
        return None
    role = address_role(val)
    if role is None:
        return None
    return val.rsplit(".", 1)[0].lower()


def addr_is(address: str | None, owner: str | None, role: str) -> bool:
    if address is None or owner is None:
        return False
    return address_owner(address) == owner and canonical_role(address_role(address)) == role


def dispatch_is_delegated(fields: Dict[str, str], clean: str) -> bool:
    """Delegation is structural (H28): only the DELEGATED_DISPATCH_AUTHORITY
    field marks a dispatch as delegated. The former bare-word prose operand was
    negation-blind and misfired on placeholder prose mentioning delegation.
    Direct-authority FROM seats carry their own authorization regardless."""
    if fields.get("DELEGATED_DISPATCH_AUTHORITY", "").lower() in {"yes", "true"}:
        return True
    return False


def is_implementer_address(address: str) -> bool:
    return canonical_role(address_role(address)) == "implementer"


def impl_dispatch_parent_qualifies(actor_from: List[str], item) -> bool:
    """rev19 shared structural predicate: item qualifies as the IMPL report's
    DISPATCH IMPL parent for actor_from iff it carries a live own-line token
    and is addressed, one-to-one, to the same owner and canonical role with
    both roles parseable. Consumed by BOTH multi-holder qualification and the
    singleton order-eligibility branch; design rev19 forbids any second copy."""
    if len(actor_from) != 1:
        return False
    if not own_line_dispatch_present(item[4]):
        return False
    t = split_addresses(item[3].get("TO"))
    return (
        len(t) == 1
        and address_role(t[0]) is not None
        and address_role(actor_from[0]) is not None
        and address_owner(t[0]) == address_owner(actor_from[0])
        and canonical_role(address_role(t[0])) == canonical_role(address_role(actor_from[0]))
    )


def validate_address_fields(result: LintResult, fields: Dict[str, str], *, template_mode: bool) -> Dict[str, List[str]]:
    parsed: Dict[str, List[str]] = {}
    for name in ("FROM", "TO", "CC"):
        raw = fields.get(name)
        if raw is None:
            continue
        if template_mode and ("<" in raw or "|" in raw):
            parsed[name] = []
            continue
        addrs = split_addresses(raw)
        parsed[name] = addrs
        if not addrs:
            result.error(f"{name} address list is empty")
            continue
        if name == "FROM" and len(addrs) != 1:
            result.error("FROM must contain exactly one address")
        for addr in addrs:
            if address_role(addr) is None:
                result.error(f"{name} has invalid address {addr!r}; expected operator, orchestrator, or <owner>.<role>")
            elif addr != addr.lower():
                result.warn(f"{name} address {addr!r} is non-canonical case; addresses compare case-insensitively and canonicalize to lowercase")
    return parsed


def owners_in_addresses(addrs: List[str]) -> set[str]:
    owners = set()
    for addr in addrs:
        owner = address_owner(addr)
        if owner and owner not in SPECIAL_ADDRESS_VALUES:
            owners.add(owner)
    return owners


def normalized_addr(address: str | None) -> str:
    return (address or "").strip().lower()


def from_role(address: str | None) -> str | None:
    if not address:
        return None
    return address_role(address.strip())


def from_owner(address: str | None) -> str | None:
    if not address:
        return None
    return address_owner(address.strip())


def from_is_direct_authority(fields: Dict[str, str]) -> bool:
    addrs = split_addresses(fields.get("FROM"))
    if len(addrs) != 1:
        return False
    role = from_role(addrs[0])
    return role in LINEAGE_DIRECT_FROM_ROLES


def role_from_consistency_error(fields: Dict[str, str]) -> str | None:
    """Tripwire for proxy-authorship confusion.

    A relay's ROLE is the seat writing the relay. A non-special FROM address
    must carry the same role suffix. This does not prove identity; it catches
    the observed confusion-class failure where an agent was baited into writing
    a relay whose FROM was someone else's seat.
    """
    role = fields.get("ROLE")
    expected = ROLE_TO_ADDRESS_ROLE.get(role or "")
    if not expected:
        return None
    from_addrs = split_addresses(fields.get("FROM"))
    if len(from_addrs) != 1:
        return None
    actual = from_role(from_addrs[0])
    if actual in SPECIAL_ADDRESS_VALUES:
        return None
    if actual and canonical_role(actual) != canonical_role(expected):
        return f"ROLE/FROM mismatch: ROLE={role!r} but FROM={from_addrs[0]!r}; do not proxy-author another seat's relay"
    return None


def parse_row_evidence(text: str, header: str) -> set[str]:
    """Parse '<path> -> <evidence>' rows from a row-evidence block."""
    block = extract_named_block(text, header)
    paths: set[str] = set()
    for line in block.splitlines()[1:]:
        if not line.strip():
            continue
        m = re.match(r"^\s*-\s+(\S.*?)\s*(?:->|:)\s*(\S.*)$", line)
        if m and m.group(2).strip() and not PLACEHOLDER_RE.match(m.group(2).strip()):
            paths.add(m.group(1).strip())
    return paths


def row_evidence_errors(text: str, row_header: str, evidence_header: str) -> List[str]:
    rows, row_errors = parse_marked_rows(text, row_header)
    if row_errors:
        return []
    in_paths = [path for path, mark in rows if mark == "in"]
    if not in_paths:
        return []
    evidence_paths = parse_row_evidence(text, evidence_header)
    errors: List[str] = []
    if not evidence_paths:
        errors.append(f"{row_header} has IN rows but missing {evidence_header}; verify row truth against file/scope evidence, not row shape")
        return errors
    missing = [path for path in in_paths if path not in evidence_paths]
    for path in missing:
        errors.append(f"{evidence_header} missing evidence for IN row {path!r}")
    return errors


def dispatch_id_map(phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]]) -> Dict[str, List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]]]:
    out: Dict[str, List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]]] = {}
    for item in phases:
        did = item[3].get("DISPATCH_ID")
        if did:
            out.setdefault(did, []).append(item)
    return out


def same_owner_addr(owner: str | None, role: str) -> str:
    return f"{owner}.{role}" if owner else ""


def own_line_dispatch_present(text: str) -> bool:
    return re.search(r"^DISPATCH IMPL\s*$", operational_token_text(text), flags=re.MULTILINE) is not None


def own_line_merge_present(text: str) -> bool:
    return re.search(r"^DISPATCH MERGE\s*$", operational_token_text(text), flags=re.MULTILINE) is not None


def unruled_authority_errors(text: str, fields: Dict[str, str]) -> List[str]:
    """Fail-closed surfaces for role words whose authority semantics have no shipped ruling."""
    errors: List[str] = []
    from_addrs = split_addresses(fields.get("FROM"))
    role = from_role(from_addrs[0]) if len(from_addrs) == 1 else None
    if role not in UNRULED_AUTHORITY_ROLES:
        return errors
    authority_keys = (
        "DELEGATED_DISPATCH_AUTHORITY",
        "DESIGN_LOCK_ID",
        "DESIGN_RECORD_KIND",
    )
    occurrences: Dict[str, List[str]] = {key: [] for key in authority_keys}
    for line in sanitized_text(text).splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$", line.rstrip())
        if match and match.group(1) in occurrences:
            occurrences[match.group(1)].append(match.group(2).strip())

    resolved: Dict[str, str] = {}
    for key in authority_keys:
        values = occurrences[key]
        distinct_values = set(values)
        if len(distinct_values) > 1:
            errors.append(
                f"{key} carries {len(distinct_values)} distinct values across {len(values)} occurrences; "
                "an authority-critical field is fail-closed unless exactly one distinct value is present"
            )
        elif values:
            resolved[key] = values[0]

    if own_line_dispatch_present(text):
        errors.append(
            f"authority semantics for FROM role {role!r} are unruled; "
            "a dispatch token from this seat is fail-closed; no shipped ruling defines this seat's authority"
        )
    if resolved.get("DESIGN_RECORD_KIND") == "direct-override":
        errors.append(
            f"authority semantics for FROM role {role!r} are unruled; "
            "DESIGN_RECORD_KIND: direct-override from this seat is fail-closed; no shipped ruling defines this seat's authority"
        )
    elif resolved.get("DESIGN_LOCK_ID") or resolved.get("DESIGN_RECORD_KIND"):
        errors.append(
            f"authority semantics for FROM role {role!r} are unruled; "
            "a design-lock claim from this seat is fail-closed; no shipped ruling defines this seat's authority"
        )
    if resolved.get("DELEGATED_DISPATCH_AUTHORITY", "").lower() == "yes":
        errors.append(
            f"authority semantics for FROM role {role!r} are unruled; "
            "a delegated-dispatch-authority claim from this seat is fail-closed; no shipped ruling defines this seat's authority"
        )
    return errors


def inline_dispatch_mentions(text: str) -> List[int]:
    lines = operational_token_text(text).splitlines()
    bad: List[int] = []
    for i, line in enumerate(lines, start=1):
        if "DISPATCH IMPL" in line and line.strip() != "DISPATCH IMPL":
            bad.append(i)
    return bad


def inline_merge_mentions(text: str) -> List[int]:
    lines = operational_token_text(text).splitlines()
    bad: List[int] = []
    for i, line in enumerate(lines, start=1):
        if "DISPATCH MERGE" in line and line.strip() != "DISPATCH MERGE":
            bad.append(i)
    return bad


def fenced_token_mentions(text: str, token: str) -> List[int]:
    warnings: List[int] = []
    in_fence = False
    for i, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence and token in line:
            warnings.append(i)
    return warnings




def merge_authorization_verdict_lines(text: str) -> List[Tuple[int, str, str]]:
    """Operational MERGE-GATE authorization verdict lines.

    Field-form merge authorization is a single-verdict record. A second
    MERGE_AUTHORIZATION/HUMAN_MERGE_AUTHORIZATION/MERGE_APPROVED line, or a
    merge-form VERDICT line, is structurally ambiguous: humans read the latest
    context/denial, while regex search can accidentally grant from a pasted
    draft. Fenced/backticked quotations are stripped before scanning.
    """
    verdicts: List[Tuple[int, str, str]] = []
    op = operational_token_text(text)
    for line_no, line in enumerate(op.splitlines(), start=1):
        m = re.match(r"^\s*(MERGE_AUTHORIZATION|HUMAN_MERGE_AUTHORIZATION|MERGE_APPROVED|VERDICT)\s*:\s*(.*)$", line, flags=re.IGNORECASE)
        if not m:
            continue
        key = m.group(1).upper()
        value = m.group(2).strip()
        if key == "VERDICT" and not re.match(r"^(merge[- ]ready|approved|complete|denied|merge[- ]blocked|blocked|rejected)\b", value, flags=re.IGNORECASE):
            continue
        verdicts.append((line_no, key, value))
    return verdicts


def merge_authorization_positive(key: str, value: str) -> bool:
    key = key.upper()
    if key in {"MERGE_AUTHORIZATION", "HUMAN_MERGE_AUTHORIZATION"}:
        return re.match(r"^(approved|granted|yes)\b", value, flags=re.IGNORECASE) is not None
    if key == "MERGE_APPROVED":
        return re.match(r"^(yes|true|approved)\b", value, flags=re.IGNORECASE) is not None
    if key == "VERDICT":
        return re.match(r"^(merge[- ]ready|approved|complete)\b", value, flags=re.IGNORECASE) is not None
    return False


def merge_token_authorized(text: str, fields: Dict[str, str]) -> bool:
    if fields.get("PHASE") != "MERGE-GATE":
        return False
    if not own_line_merge_present(text):
        return False
    from_addrs = split_addresses(fields.get("FROM"))
    if len(from_addrs) != 1:
        return False
    from_addr = from_addrs[0].strip()
    from_low = from_addr.lower()
    if from_low not in {"operator", "orchestrator"} and address_role(from_addr) != "orchestrator-planner":
        return False
    to_addrs = split_addresses(fields.get("TO"))
    return len(to_addrs) == 1 and is_implementer_address(to_addrs[0])


def plan_review_approved(text: str) -> bool:
    # Same-line only. Do not allow DOTALL/prose-later approve matches.
    patterns = [
        r"^\s*(?:PLAN_REVIEW_VERDICT|PLAN-REVIEW VERDICT|VERDICT|Verdict)\s*:\s*approve(?:d)?\s*$",
        r"^\s*(?:PLAN_REVIEW_VERDICT|PLAN-REVIEW VERDICT|VERDICT|Verdict)\s*:\s*approve(?:d)?\s+[—-].*$",
    ]
    clean = sanitized_text(text)
    return any(re.search(p, clean, flags=re.IGNORECASE | re.MULTILINE) for p in patterns)

def strip_negated_merge_phrases(value: str) -> str:
    """Remove common no-merge status phrases before positive merge detection.

    Merge visibility is scoped to ACTIONS_GIT_REF. Honest reports such as
    "NOT merged to master; awaiting MERGE-GATE authorization" must not count
    as merge claims, while canonical forms such as ``merge=<sha>`` must.
    """
    patterns = [
        r"\bnot\s+merged\b[^;\n.]*[;\n.]?",
        r"\bno\s+merge\b[^;\n.]*[;\n.]?",
        r"\bawaiting\s+(?:a\s+)?merge(?:-gate)?(?:\s+authorization)?\b[^;\n.]*[;\n.]?",
        r"\bmerge\s+(?:pending|not\s+authorized|awaiting\s+authorization)\b[^;\n.]*[;\n.]?",
    ]
    out = value
    for pattern in patterns:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    return out


def merge_claimed(text: str, fields: Dict[str, str]) -> bool:
    # Prose merge visibility is scoped to the standardized
    # ACTIONS_GIT_REF *block*, not only the same-line field value. A later revision
    # layers a canonical machine-form scan on top: merge=<sha> outside fenced
    # blocks, inline code spans, and ESCALATION_SCAN blocks counts as a merge
    # claim in relay-root mode even when it is flush-left prose after the block.
    # Free-prose flush-left claims remain outside truth-agnostic lint's scope.
    operational = operational_token_text(text)
    action_ref_block = extract_named_block(operational, "ACTIONS_GIT_REF")
    scoped = strip_negated_merge_phrases(action_ref_block)
    field_patterns = [
        r"\bmerge\s*=\s*[A-Za-z0-9_.:-]+",
        r"\bmerge\s+commit\b",
        r"\bmerged\b",
        r"\bgit\s+merge\b",
    ]
    field_claim = bool(action_ref_block.strip()) and any(
        re.search(pattern, scoped, flags=re.IGNORECASE) for pattern in field_patterns
    )
    canonical_surface = remove_named_block(operational, "ESCALATION_SCAN")
    canonical_claim = re.search(r"\bmerge\s*=\s*\S", canonical_surface, flags=re.IGNORECASE) is not None
    return field_claim or canonical_claim


def merge_authorized(text: str, fields: Dict[str, str]) -> bool:
    if fields.get("PHASE") != "MERGE-GATE":
        return False
    from_addrs = split_addresses(fields.get("FROM"))
    if len(from_addrs) != 1:
        return False
    from_addr = from_addrs[0].strip()
    from_low = from_addr.lower()
    if from_low not in {"operator", "orchestrator"} and address_role(from_addr) != "orchestrator-planner":
        # Prevent self-authorization: an Implementer-authored MERGE-GATE relay
        # does not satisfy the gate that polices that Implementer's merge claim.
        return False
    verdicts = merge_authorization_verdict_lines(text)
    if len(verdicts) > 1:
        # A duplicate/conflicting grant relay is structurally dirty and must not
        # satisfy merge lineage, even if one pasted line says approved.
        return False
    field_form = bool(verdicts) and merge_authorization_positive(verdicts[0][1], verdicts[0][2])
    return field_form or merge_token_authorized(text, fields)


def relay_order_key(path: Path) -> Tuple[int, object, str]:
    stem = path.stem
    # Preferred: timestamp anywhere in protocol-ish filenames.
    m = re.search(r"(\d{8})[-T]?(\d{6})(?:Z)?", stem)
    if m:
        return (0, f"{m.group(1)}{m.group(2)}", path.name)
    # Legacy fixtures often use numeric prefixes.
    m = re.match(r"^(\d+)", stem)
    if m:
        return (1, int(m.group(1)), path.name)
    try:
        return (2, path.stat().st_mtime_ns, path.name)
    except OSError:
        return (3, path.name, path.name)


def clock_now(is_utc: bool) -> datetime.datetime:
    if is_utc:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return datetime.datetime.now()


def parse_stamp(raw: str) -> Optional[datetime.datetime]:
    """Parse an index/marker timestamp in any accepted form; None if not a real time."""
    raw = raw.strip()
    for fmt in INDEX_TIME_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def filename_timestamp(name: str) -> Tuple[str, Optional[datetime.datetime], str, bool]:
    """Extract the relay filename stamp.

    Returns (status, dt, raw, is_utc) where status is "absent" (no stamp-shaped
    text), "invalid" (stamp-shaped but not a real date/time, e.g. minute 62), or
    "ok". A stamp-shaped-but-invalid name can never be ordered and no real clock
    ever produced it; template mode skips stamp checks entirely.
    """
    stem = Path(name).stem
    m = FILENAME_TS_RE.search(stem)
    if not m:
        return ("absent", None, "", False)
    raw = f"{m.group(1)}-{m.group(2)}"
    is_utc = bool(m.group(3))
    try:
        dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return ("invalid", None, raw, is_utc)
    return ("ok", dt, raw, is_utc)


def drift_error(raw: str, dt: datetime.datetime, is_utc: bool, max_drift_minutes: int) -> Optional[str]:
    now = clock_now(is_utc)
    drift_min = (dt - now).total_seconds() / 60.0
    if abs(drift_min) <= max_drift_minutes:
        return None
    if abs(drift_min) >= 1440:
        magnitude = f"{abs(drift_min) / 1440.0:.1f} days"
    else:
        magnitude = f"{abs(drift_min):.0f} min"
    direction = "in the future" if drift_min > 0 else "in the past"
    zone = "UTC" if is_utc else "local"
    return (
        f"timestamp {raw} is {magnitude} {direction} of the {zone} clock "
        f"(now {now.strftime('%Y%m%d-%H%M%S')}, tolerance +/-{max_drift_minutes} min); "
        "stamp the relay with the real current time"
    )


def check_filename_timestamp(
    result: LintResult,
    path: Path,
    *,
    freshness: bool,
    max_drift_minutes: int,
    template_mode: bool,
) -> None:
    if template_mode:
        return
    status, dt, raw, is_utc = filename_timestamp(path.name)
    if status == "invalid":
        result.error(f"filename timestamp {raw} is not a real date/time")
        return
    if status == "absent":
        if freshness:
            result.error(
                "filename carries no YYYYMMDD-HHMMSS timestamp; authoring drift cannot be verified"
            )
        return
    if not freshness or dt is None:
        return
    msg = drift_error(raw, dt, is_utc, max_drift_minutes)
    if msg:
        result.error(f"filename {msg}")


def lint_file(
    path: Path,
    *,
    template_mode: bool = False,
    freshness: bool = False,
    max_drift_minutes: int = DEFAULT_MAX_DRIFT_MINUTES,
) -> LintResult:
    text = read(path)
    clean = sanitized_text(text)
    result = LintResult()
    fields = header_fields(text)

    if not template_mode:
        for _a5e in lock_digest_shape_errors(text):
            result.error(_a5e)

    check_filename_timestamp(
        result, path, freshness=freshness,
        max_drift_minutes=max_drift_minutes, template_mode=template_mode,
    )

    if not template_mode:
        for f in MIN_HEADER_FIELDS:
            if f not in fields:
                result.error(f"missing required header field {f}")

    enum_field(result, fields, "ROLE", ROLE_VALUES, template_mode=template_mode)
    enum_field(result, fields, "PHASE", PHASE_VALUES, template_mode=template_mode)
    enum_field(result, fields, "AUTHORITY", AUTHORITY_VALUES, template_mode=template_mode)
    enum_field(result, fields, "CEREMONY_TIER", CEREMONY_VALUES, template_mode=template_mode)
    enum_field(result, fields, "EVIDENCE_TARGET", EVIDENCE_VALUES, template_mode=template_mode)
    enum_field(result, fields, "DESIGN_RECORD_KIND", DESIGN_RECORD_KIND_VALUES, template_mode=template_mode)
    enum_field(result, fields, "DESIGN_REVIEW_VERDICT", DESIGN_REVIEW_VERDICT_VALUES, template_mode=template_mode)

    phase = fields.get("PHASE")
    authority = fields.get("AUTHORITY", "")
    if phase in PHASE_VALUES and authority and not template_mode:
        if not authority_consistent(phase, authority):
            result.error(f"phase/authority inconsistent: PHASE={phase}, AUTHORITY={authority!r}")

    addresses = validate_address_fields(result, fields, template_mode=template_mode)
    if not template_mode:
        mismatch = role_from_consistency_error(fields)
        if mismatch:
            result.error(mismatch)
    for err in duplicate_row_bearing_header_errors(text):
        result.error(err)
    for err in detached_row_errors(text):
        result.error(err)
    if not template_mode:
        for block_header in ("ACTIONS_GIT_REF", "FINAL_GIT_STATUS_SHORT", "ESCALATION_SCAN", "SCOPE_DIFF", "FOLD_SCOPE"):
            if block_header in fields:
                for err in ambiguous_continuation_errors(text, block_header):
                    result.error(err)
    has_dispatch = own_line_dispatch_present(text)
    if has_dispatch and not template_mode:
        to_addrs = addresses.get("TO", [])
        if "TO" not in fields:
            result.error("DISPATCH IMPL requires a TO address")
        elif len(to_addrs) != 1:
            result.error("DISPATCH IMPL requires exactly one TO addressee")
        elif not is_implementer_address(to_addrs[0]):
            result.error("DISPATCH IMPL requires TO to be exactly one implementer-role address")

    if not template_mode:
        for err in unruled_authority_errors(text, fields):
            result.error(err)

    has_merge_dispatch = own_line_merge_present(text)
    if has_merge_dispatch and not template_mode:
        to_addrs = addresses.get("TO", [])
        from_addrs = addresses.get("FROM", [])
        if phase != "MERGE-GATE":
            result.error("DISPATCH MERGE is valid only in PHASE: MERGE-GATE relay files")
        if "TO" not in fields:
            result.error("DISPATCH MERGE requires a TO address")
        elif len(to_addrs) != 1:
            result.error("DISPATCH MERGE requires exactly one TO addressee")
        elif not is_implementer_address(to_addrs[0]):
            result.error("DISPATCH MERGE requires TO to be exactly one implementer-role address")
        if len(from_addrs) != 1:
            result.error("DISPATCH MERGE requires exactly one FROM grantor")
        else:
            from_addr = from_addrs[0].strip()
            from_low = from_addr.lower()
            if from_low not in {"operator", "orchestrator"} and address_role(from_addr) != "orchestrator-planner":
                result.error("DISPATCH MERGE FROM must be operator, orchestrator, or an orchestrator-planner-role address")

    if phase == "MERGE-GATE" and not template_mode:
        verdict_lines = merge_authorization_verdict_lines(text)
        if len(verdict_lines) > 1:
            result.error("duplicate/conflicting merge authorization; the grant of record carries exactly one verdict line")

    # Merge/live verdict enum when structurally visible.
    verdict = fields.get("MERGE_LIVE_VERDICT") or fields.get("VERDICT")
    if verdict and verdict not in VERDICT_VALUES and not template_mode:
        # Only enforce if it looks like a merge/live verdict, not arbitrary audit verdict text.
        if phase in {"MERGE-GATE", "LIVE-VERIFY"}:
            result.error(f"non-canonical merge/live verdict: {verdict!r}")

    # Suspicious dispatch tokens: valid token must be bare, unfenced, un-backticked,
    # and alone on its own line. Inline quoted/code mentions are inert.
    for line_no in inline_dispatch_mentions(text):
        result.error(f"DISPATCH IMPL appears inline on line {line_no}; dispatch token is valid only bare/unfenced/un-backticked and alone on its own line")
    for line_no in inline_merge_mentions(text):
        result.error(f"DISPATCH MERGE appears inline on line {line_no}; merge token is valid only bare/unfenced/un-backticked and alone on its own line")
    for line_no in fenced_token_mentions(text, "DISPATCH IMPL"):
        result.warn(f"DISPATCH IMPL appears inside fenced code on line {line_no}; quoted/fenced tokens are inert")
    for line_no in fenced_token_mentions(text, "DISPATCH MERGE"):
        result.warn(f"DISPATCH MERGE appears inside fenced code on line {line_no}; quoted/fenced tokens are inert")

    # Downgrade ordering and scan shape.
    # H29: CEREMONY_DOWNGRADE gates on an actual downgrade value; `none` is a
    # declaration of no downgrade, not a trigger. The other keys stay
    # presence-triggered: a scan or waiver that exists must validate.
    _cd_val = fields.get("CEREMONY_DOWNGRADE", "").strip().lower()
    has_downgrade = ("CEREMONY_DOWNGRADE" in fields and _cd_val not in {"", "none"}) or any(
        k in fields for k in ("WHY_DOWNGRADE_IS_SAFE", "OPERATOR_WAIVER", "ESCALATION_SCAN", "ESCALATION_SCAN_RESULT")
    )
    if has_downgrade:
        scan_line = find_line(text, "ESCALATION_SCAN")
        result_line = find_line(text, "ESCALATION_SCAN_RESULT")
        why_line = find_line(text, "WHY_DOWNGRADE_IS_SAFE")
        waiver_line = find_line(text, "OPERATOR_WAIVER")
        if scan_line is None:
            result.error("downgrade/waiver present but ESCALATION_SCAN is missing")
        if result_line is None:
            result.error("downgrade/waiver present but ESCALATION_SCAN_RESULT is missing")
        if why_line is not None:
            if scan_line is None or result_line is None or why_line < max(scan_line or 0, result_line or 0):
                result.error("WHY_DOWNGRADE_IS_SAFE appears before completed ESCALATION_SCAN + ESCALATION_SCAN_RESULT")
        rows, row_errors = parse_scan_rows(text)
        for e in row_errors:
            result.error(e)
        missing = [row for row in CANONICAL_SCAN_ROWS if row.lower() not in rows]
        for row in missing:
            result.error(f"missing canonical ESCALATION_SCAN row: {row}")
        values = set(rows.values())
        if fields.get("ESCALATION_SCAN_RESULT") and fields.get("ESCALATION_SCAN_RESULT") not in SCAN_RESULT_VALUES and not template_mode:
            result.error(f"ESCALATION_SCAN_RESULT has non-canonical value: {fields.get('ESCALATION_SCAN_RESULT')!r}")
        if not missing and not row_errors:
            expected_result = "trigger-present" if "yes" in values else "unknown-present" if "unknown" in values else "all-no"
            if fields.get("ESCALATION_SCAN_RESULT") and fields.get("ESCALATION_SCAN_RESULT") != expected_result and not template_mode:
                result.error(f"ESCALATION_SCAN_RESULT={fields.get('ESCALATION_SCAN_RESULT')!r} inconsistent with rows; expected {expected_result!r}")
        has_yes_unknown = any(value in {"yes", "unknown"} for value in rows.values())
        if has_yes_unknown:
            if why_line is not None:
                result.error("WHY_DOWNGRADE_IS_SAFE forbidden when any ESCALATION_SCAN row is yes/unknown")
            if waiver_line is None:
                result.error("OPERATOR_WAIVER required when any ESCALATION_SCAN row is yes/unknown")

    # Read-only/report-only final status. The relay file is the report of record.
    if phase in READ_ONLY_PHASES:
        if "FINAL_GIT_STATUS_SHORT" not in fields:
            result.error(f"{phase} report missing FINAL_GIT_STATUS_SHORT or explicit unavailable reason")
        elif not template_mode:
            err = report_field_error(text, "FINAL_GIT_STATUS_SHORT")
            if err:
                result.error(err)

    # Structurally detectable action claim references. Exclude scan/template/code blocks.
    action_scan_text = remove_named_block(clean, "ESCALATION_SCAN")
    action_claim = re.search(r"\b(edited|created|deleted|committed|pushed|opened a PR|merged|migration file|backfill|changed git state)\b", action_scan_text, flags=re.IGNORECASE)
    if action_claim and "ACTIONS_GIT_REF" not in fields:
        result.error("structurally detectable edit/commit/PR/migration claim lacks ACTIONS_GIT_REF")
    elif action_claim and not template_mode:
        err = report_field_error(text, "ACTIONS_GIT_REF")
        if err:
            result.error(err)

    # REVIEW-FOLD actions require a pre-action FOLD_SCOPE artifact.
    fold_gate = bool(action_claim) or substantive_actions_ref(text, fields)
    if phase == "REVIEW-FOLD" and fold_gate and not template_mode:
        if "FOLD_SCOPE" not in fields:
            result.error("REVIEW-FOLD report claims actions without FOLD_SCOPE; list every touched file against the findings scope before editing")
        else:
            rows, row_errors = parse_marked_rows(text, "FOLD_SCOPE")
            for e in row_errors:
                result.error(e)
            if not rows:
                result.error("FOLD_SCOPE requires >=1 parsed '<path> -> in|out' row; an empty scope list does not license a fold edit")
            elif any(value == "out" for _, value in rows):
                result.error("FOLD_SCOPE contains an OUT row alongside claimed actions; an OUT file requires a deviation relay before any edit")
        if "FOLD_SCOPE_RESULT" not in fields:
            result.error("REVIEW-FOLD report claims actions without FOLD_SCOPE_RESULT")
        elif fields.get("FOLD_SCOPE_RESULT") != "all-in":
            result.error("REVIEW-FOLD edits require FOLD_SCOPE_RESULT: all-in; any OUT file goes to a deviation relay before edit")
        fs_idx = block_start_index(text, "FOLD_SCOPE")
        agr_idx = block_start_index(text, "ACTIONS_GIT_REF")
        if fs_idx != -1 and agr_idx != -1 and fs_idx > agr_idx:
            result.error("FOLD_SCOPE appears after ACTIONS_GIT_REF; the scope artifact precedes the action claim (field-ordering)")

    # Delegated dispatch requires substantive SCOPE_DIFF.
    has_dispatch = own_line_dispatch_present(text)
    delegated = dispatch_is_delegated(fields, clean)
    if has_dispatch and delegated:
        if "SCOPE_DIFF" not in fields:
            result.error("delegated DISPATCH IMPL missing SCOPE_DIFF")
        elif not template_mode:
            rows, row_errors = parse_scope_diff_rows(text)
            for e in row_errors:
                result.error(e)
            if not rows:
                result.error("delegated DISPATCH IMPL requires SCOPE_DIFF with >=1 parsed '<path> -> in|OUT' row; a vacuous or structured-none diff does not authorize dispatch")
            elif any(value == "out" for _, value in rows) and fields.get("SCOPE_DIFF_RESULT") == "all-in":
                result.error("SCOPE_DIFF_RESULT: all-in inconsistent with an OUT row")
        if "SCOPE_DIFF_RESULT" not in fields:
            result.error("delegated DISPATCH IMPL missing SCOPE_DIFF_RESULT")
        elif fields.get("SCOPE_DIFF_RESULT") != "all-in":
            result.error("delegated DISPATCH IMPL requires SCOPE_DIFF_RESULT: all-in")



    # Reconciliation-layer row-truth check. Truth-agnostic lint cannot prove the
    # evidence is correct, but when a relay declares ROW_TRUTH_CHECK required it
    # can require every IN row to carry row-evidence for later reconciliation.
    # This preserves the base shape checks while making the honest-false
    # battery fixtures mechanically enforceable.
    if not template_mode and fields.get("ROW_TRUTH_CHECK", "").lower() in {"yes", "true", "required"}:
        if "SCOPE_DIFF" in fields:
            for e in row_evidence_errors(text, "SCOPE_DIFF", "SCOPE_ROW_EVIDENCE"):
                result.error(e)
        if "FOLD_SCOPE" in fields:
            for e in row_evidence_errors(text, "FOLD_SCOPE", "FOLD_SCOPE_EVIDENCE"):
                result.error(e)

    return result



def design_doc_items_for_owner(phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]], owner: str | None):
    if not owner:
        return []
    out = []
    for item in phases:
        _p, _order, phase, fields, _text = item
        if phase != "DESIGN" or not fields.get("DESIGN_DOC_ID"):
            continue
        addrs = split_addresses(fields.get("FROM"))
        if len(addrs) == 1 and from_owner(addrs[0]) == owner:
            out.append(item)
    return out


def design_review_items_for_owner(phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]], owner: str | None):
    if not owner:
        return []
    out = []
    for item in phases:
        _p, _order, phase, fields, _text = item
        if phase != "DESIGN-REVIEW":
            continue
        addrs = split_addresses(fields.get("FROM"))
        if len(addrs) == 1 and from_owner(addrs[0]) == owner:
            out.append(item)
    return out


def design_review_gate_applies(fields: Dict[str, str], owner: str | None, phases, *, require_record_kind: bool = True) -> bool:
    """Whether observable relay-root context makes a design-review gate relevant.

    This is intentionally truth-agnostic: it checks only relay fields on disk. A
    pair Planner can say audit-record or omit DESIGN_RECORD_KIND, but if a
    same-owner DESIGN relay with DESIGN_DOC_ID is visible, the design-doc gate is
    in play for that owner.
    """
    if fields.get("DESIGN_RECORD_KIND") == "design-doc":
        return True
    return bool(design_doc_items_for_owner(phases, owner))


def is_direct_design_override(fields: Dict[str, str]) -> bool:
    return fields.get("DESIGN_RECORD_KIND") == "direct-override" and from_is_direct_authority(fields)


def design_record_kind_error_context(fields: Dict[str, str], owner: str | None, phases) -> bool:
    """Discriminator for missing DESIGN_RECORD_KIND.

    Missing DESIGN_RECORD_KIND is hard only when design-review context is
    visible in the relay root. Isolated legacy DESIGN_LOCK_ID relays remain
    tolerated under the report-of-record precedent.
    """
    if not fields.get("DESIGN_LOCK_ID"):
        return False
    if fields.get("DESIGN_RECORD_KIND"):
        return False
    if design_doc_items_for_owner(phases, owner):
        return True
    if design_review_items_for_owner(phases, owner):
        return True
    parent_id = fields.get("PARENT_DISPATCH_ID")
    if parent_id:
        for _p, _order, phase, review_fields, _text in phases:
            if review_fields.get("DISPATCH_ID") == parent_id and phase == "DESIGN-REVIEW":
                return True
    return False



def orchestrator_review_in_broad_set(phase: str, fields: Dict[str, str], text: str) -> bool:
    """Broad SET for orchestrator-review visibility.

    Caller has already confirmed the relay is FROM orchestrator or an
    orchestrator-planner-role address. This is a visibility gate, not an
    approval/verdict lineage gate.
    """
    if phase in {"AUDIT", "DESIGN", "REVIEW-FOLD", "MERGE-GATE"}:
        return True
    if phase == "PLAN" and fields.get("DELEGATED_DISPATCH_AUTHORITY", "").strip().lower() == "yes":
        return True
    if phase == "IMPL" and own_line_dispatch_present(text):
        return True
    return False


def run_has_operator_orch_review_waiver(phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]]) -> bool:
    """Return true only for an operator-authored no-reviewer waiver.

    An orchestrator-planner-authored ORCH_REVIEW_WAIVER is not a waiver; it is
    the self-waiver dodge this gate exists to close.
    """
    for _p, _order, _phase, fields, _text in phases:
        from_addrs = split_addresses(fields.get("FROM"))
        if len(from_addrs) == 1 and normalized_addr(from_addrs[0]) == "operator" and fields.get("ORCH_REVIEW_WAIVER", "").strip():
            return True
    return False


def orchestrator_review_gate_errors(path: Path, phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]]) -> List[str]:
    """Orchestrator-pair review visibility gate.

    Every orchestrator authority relay in the broad SET must address an
    orchestrator-reviewer in TO or CC unless the run root contains an
    operator-authored no-reviewer waiver. Pair-seat and operator-authored relays
    are exempt by construction because the FROM-set check comes first.
    """
    errors: List[str] = []
    waiver = run_has_operator_orch_review_waiver(phases)
    for f, _order, phase, fields, text in phases:
        from_addrs = split_addresses(fields.get("FROM"))
        if len(from_addrs) != 1:
            continue
        from_addr = from_addrs[0]
        is_orchestrator_from = (from_role(from_addr) == "orchestrator-planner") or (normalized_addr(from_addr) == "orchestrator")
        if not is_orchestrator_from:
            continue
        if not orchestrator_review_in_broad_set(phase, fields, text):
            continue
        addrs = split_addresses(fields.get("TO")) + split_addresses(fields.get("CC"))
        if any(address_role(addr) == "orchestrator-reviewer" for addr in addrs):
            continue
        if waiver:
            continue
        errors.append(f"{f.relative_to(path)}: orchestrator authority relay must CC <run>.orchestrator-reviewer (or run under an operator no-reviewer waiver)")
    return errors


def split_index_cells(stripped: str) -> list[str]:
    cells: list[str] = []
    cur: list[str] = []
    backslashes = 0
    for ch in stripped:
        if ch == "\\":
            backslashes += 1
            cur.append(ch)
            continue
        if ch == "|":
            if backslashes % 2 == 1:
                cur[-1:] = ["|"]
            else:
                cells.append("".join(cur).strip())
                cur = []
            backslashes = 0
            continue
        backslashes = 0
        cur.append(ch)
    cells.append("".join(cur).strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


A5_LOCK_PAIRS = (
    ("DESIGN_SHA256", "DESIGN_ARTIFACT", "designs"),
    ("PLAN_SHA256", "PLAN_ARTIFACT", "plans"),
)

A5_SHA256_RE = re.compile(r"[0-9a-f]{64}")
A5_INDEX_ROOT_MARKER_RE = re.compile(r"root:\s+(\S.*)")
A5_NO_RELAY_CELL_RE = re.compile(r"none \u2014 \S.*")


A5_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
A5_LINE_SPLIT_RE = re.compile(r"\r\n|\r|\n")


def a5_split_lines(text: str) -> List[str]:
    """A5-owned line splitter (a5-plan r5 R3): CommonMark line endings ONLY
    (CRLF, CR, LF). Python's splitlines() also splits on Unicode separators
    such as U+2028/U+0085, which would turn a forbidden closer suffix into a
    bare closer and let embedded decoys become own-line declarations."""
    return A5_LINE_SPLIT_RE.split(text)


def a5_sanitized_text(text: str) -> str:
    """A5-owned fence sanitizer (design rev32, CommonMark 0.31.2 SS4.5 at line
    level): 0-3 space indentation; opener info strings, the backtick family's
    containing no backtick; closers of the SAME family with run length >= the
    opener's and whitespace-only suffix; opposite families never close;
    shorter runs, suffix-bearing pseudo-closers, and 4+-space fence-like
    lines are content; unclosed regions strip through EOF; every line inside
    a region strips. Container-block contexts deliberately unmodeled."""
    out: List[str] = []
    open_char = ""
    open_len = 0
    for line in a5_split_lines(text):
        m = A5_FENCE_RE.match(line)
        if open_char:
            out.append("")
            if (m and m.group(2)[0] == open_char and len(m.group(2)) >= open_len
                    and m.group(3).strip(" \t") == ""):
                open_char = ""
                open_len = 0
            continue
        if m:
            fam = m.group(2)[0]
            if fam == "`" and "`" in m.group(3):
                out.append(line)
                continue
            open_char = fam
            open_len = len(m.group(2))
            out.append("")
            continue
        out.append(line)
    return "\n".join(out)


def a5_field_occurrences(text: str) -> Dict[str, List[str]]:
    """Every occurrence of the four A5 fields, in file order per key (A4's
    distinct-VALUE semantics consume these; header_fields stays untouched)."""
    keys = ("DESIGN_SHA256", "PLAN_SHA256", "DESIGN_ARTIFACT", "PLAN_ARTIFACT")
    occ: Dict[str, List[str]] = {k: [] for k in keys}
    for line in a5_split_lines(a5_sanitized_text(text)):
        m = re.match(r"^([A-Z][A-Z0-9_-]*):\s*(.*)$", line.rstrip())
        if m and m.group(1) in occ:
            occ[m.group(1)].append(m.group(2).strip())
    return occ


def a5_stem_is_bare(stem: str) -> bool:
    return bool(stem) and "/" not in stem and "\\" not in stem and " @ " not in stem and not stem.endswith(".md")


def a5_filename_stamp(f: Path):
    """Eligibility stamp: the LINTER'S OWN filename parser must accept it
    (design rev28; a5-plan r2 R1 — `filename_timestamp` owns the accepted
    grammar including the T-separator, no-hyphen, and Z forms, so eligibility
    never narrows it; a digit-shaped impossible calendar stamp is ineligible)."""
    _status, _dt, _raw, _utc = filename_timestamp(f.name)
    return _dt if _status == "ok" else None


def lock_digest_shape_errors(text: str) -> List[str]:
    """A5 check 1, shape half (both modes, authored relays only — the caller
    gates on template_mode; design rev28 rules 2/4/5).

    Shape preconditions are gated on digest OCCURRENCE; conflicting-duplicate
    refusals are unconditional from birth; a conflicted field's own shape
    validation is REPLACED by its refusal (A4 precedent)."""
    errors: List[str] = []
    occ = a5_field_occurrences(text)
    conflicted = set()
    for key, values in occ.items():
        distinct = set(values)
        if len(distinct) > 1:
            conflicted.add(key)
            errors.append(
                f"{key} carries {len(distinct)} distinct values across {len(values)} occurrences; "
                "an authority-critical field is fail-closed unless exactly one distinct value is present"
            )
    for dkey, akey, _sub in A5_LOCK_PAIRS:
        if not occ[dkey]:
            continue
        if dkey not in conflicted:
            dval = occ[dkey][0]
            if A5_SHA256_RE.fullmatch(dval) is None:
                errors.append(f"{dkey} value {dval!r} is not a 64-hex lowercase sha256 digest")
        if not occ[akey]:
            errors.append(f"{dkey} declared with no {akey} to locate the artifact")
        elif akey not in conflicted:
            aval = occ[akey][0]
            if not a5_stem_is_bare(aval):
                errors.append(f"{akey} value {aval!r} is not a bare filename stem")
    return errors


def a5_eligible_declaration(f: Path, text: str, dkey: str, akey: str):
    """A relay's eligible declaration for one pair, or None (design rev28:
    parse-valid filename stamp, shape-valid, single-distinct both fields)."""
    stamp = a5_filename_stamp(f)
    if stamp is None:
        return None
    occ = a5_field_occurrences(text)
    dvals, avals = set(occ[dkey]), set(occ[akey])
    if len(dvals) != 1 or len(avals) != 1:
        return None
    declared, stem = next(iter(dvals)), next(iter(avals))
    if A5_SHA256_RE.fullmatch(declared) is None or not a5_stem_is_bare(stem):
        return None
    return (stamp, stem, declared)


def a5_resolve_and_compare(path: Path, dkey: str, stem: str, sub: str, declared: str) -> List[str]:
    """Candidate partition for the GOVERNING declaration (design rev28 rule 3):
    no candidate -> generic missing; invalid/unreadable candidates -> one
    per-path finding each, generic EXCLUDED; readable regular candidates ->
    comparison, additive."""
    import hashlib as _a5_hashlib
    import os as _a5_os

    def disp(p: Path, cwd_probe: bool = False) -> str:
        if cwd_probe:
            return "./" + str(p)
        try:
            return str(p.relative_to(path))
        except ValueError:
            return _a5_os.path.relpath(p)

    errors: List[str] = []
    rel = f"{sub}/{stem}.md"
    probes = [(path / rel, False), (Path(rel), True), (path.parent.parent / rel, False)]
    candidates = [(p, c) for p, c in probes if p.exists()]
    if not candidates:
        return [f"{dkey}: no artifact resolves for stem {stem!r} under {sub}/ at any probe root"]
    for p, c in candidates:
        if not p.is_file():
            errors.append(f"{dkey}: {disp(p, c)} is not a readable regular file")
            continue
        try:
            data = p.read_bytes()
        except OSError:
            errors.append(f"{dkey}: {disp(p, c)} is not a readable regular file")
            continue
        actual = _a5_hashlib.sha256(data).hexdigest()
        if actual != declared:
            errors.append(f"{dkey}: {disp(p, c)} digest {actual} does not match the declared {declared}")
    return errors


def lint_relay_index(path: Path, *, audit: bool = False) -> LintResult:
    """Check an append-only relay INDEX for timestamp truth and monotonicity.

    This is an ORDERING check, not a drift check. Enforced on rows after the
    `monotonic-from` marker (all rows when no marker is present): every stamp is a
    real date/time, stamps never decrease, no stamp predates the marker, each row's
    stamp matches its file's own filename stamp, and the newest row is not in the
    future. An index that has not been appended to recently is not a defect. Rows
    before the marker are grandfathered history, reported only under audit.
    """
    result = LintResult()
    text = read(path)
    lines = text.splitlines()

    marker_line = 0
    marker_dt: Optional[datetime.datetime] = None
    _markers = list(INDEX_MONOTONIC_MARKER_RE.finditer(text))
    m = _markers[-1] if _markers else None
    if m:
        for i, line in enumerate(lines, 1):
            if INDEX_MONOTONIC_MARKER_RE.search(line):
                marker_line = i
        marker_dt = parse_stamp(m.group(1))
        if marker_dt is None:
            result.error(f"monotonic-from marker value {m.group(1)!r} is not a valid timestamp")

    rows: List[Tuple[int, str, Optional[datetime.datetime], str]] = []
    header_arity: int | None = None
    grandfathered_arity_issues: List[Tuple[int, int]] = []
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_index_cells(stripped)
        if len(cells) < 2:
            continue
        if cells[0].lower() == "time":
            if header_arity is None:
                header_arity = len(cells)
            continue
        if set(cells[0]) <= set("-: "):
            continue
        if header_arity is not None and len(cells) != header_arity:
            message = (
                f"line {lineno}: row has {len(cells)} cells, header declares {header_arity}; "
                "a literal | in cell prose must be escaped as \\|"
            )
            if lineno > marker_line:
                result.error(message)
            else:
                grandfathered_arity_issues.append((lineno, len(cells)))
        rows.append((lineno, cells[0], parse_stamp(cells[0]), cells[-1]))

    if header_arity is None:
        result.warn("no header row; arity not checked")

    # A5 check 2: marker-gated file-cell resolution (forms lock M07).
    # Placed BEFORE the no-rows early return (a5-plan r2 R3): a header-only
    # or marker-only INDEX still gets duplicate-marker and root validation;
    # the row loop is naturally empty when there are no rows.
    # Own-line anchored recognition over fence-sanitized lines; no marker, no
    # check; exactly one or none; the root must be a directory; a file cell
    # either resolves as a file under the root or carries the explicit
    # no-relay form; {root} renders the resolved base the join used.
    a5_markers = []
    for _a5line in a5_split_lines(a5_sanitized_text(text)):
        _a5m = A5_INDEX_ROOT_MARKER_RE.fullmatch(_a5line.strip())
        if _a5m:
            a5_markers.append(_a5m.group(1))
    if len(a5_markers) > 1:
        result.error(f"INDEX declares {len(a5_markers)} root markers; at most one is permitted")
    elif len(a5_markers) == 1:
        _a5val = a5_markers[0]
        _a5root = Path(_a5val) if Path(_a5val).is_absolute() else path.parent / _a5val
        if not _a5root.is_dir():
            result.error(f"root marker value {_a5val!r} does not resolve to a directory")
        else:
            import os as _a5_os
            _a5disp = str(_a5root) if Path(_a5val).is_absolute() else _a5_os.path.relpath(_a5root, path.parent)
            for _a5ln, _a5raw, _a5dt, _a5cell in rows:
                if A5_NO_RELAY_CELL_RE.fullmatch(_a5cell):
                    continue
                if not (_a5root / _a5cell).is_file():
                    result.error(
                        f"line {_a5ln}: file cell {_a5cell!r} does not resolve under the declared root {_a5disp}"
                    )

    if not rows:
        # H26: a freshly seeded INDEX -- the boot shape with BOTH the header
        # row AND a root marker present, zero data rows -- is a valid
        # pre-first-row state, not a defect. Either component alone is still
        # an error: the grant covers exactly the marker+header boot state.
        if header_arity is None or not a5_markers:
            result.error(f"no index rows found in {path}")
        return result

    scoped = [r for r in rows if r[0] > marker_line]
    grandfathered = [r for r in rows if r[0] <= marker_line]

    for lineno, raw, dt, _file_cell in scoped:
        if dt is None:
            result.error(f"line {lineno}: index time {raw!r} is not a valid timestamp")

    parsed = [(ln, raw, dt, fc) for ln, raw, dt, fc in scoped if dt is not None]

    # Stamps must never decrease, and none may predate the strictness boundary.
    for (lineno, raw, dt, _fc), (_pln, praw, pdt, _pfc) in zip(parsed[1:], parsed[:-1]):
        if dt < pdt:
            result.error(
                f"line {lineno}: index time {raw} precedes the previous row {praw}; "
                "an append-only index must be non-decreasing"
            )
    if marker_dt is not None:
        for lineno, raw, dt, _fc in parsed:
            if dt < marker_dt:
                result.error(
                    f"line {lineno}: index time {raw} predates the monotonic-from boundary "
                    f"{marker_dt.strftime('%Y%m%d-%H%M%S')}"
                )

    # The row must agree with the relay file it points at.
    for lineno, raw, dt, file_cell in parsed:
        status, fdt, fraw, _is_utc = filename_timestamp(file_cell)
        if status == "invalid":
            result.error(f"line {lineno}: referenced file timestamp {fraw} is not a real date/time")
        elif status == "ok" and fdt != dt:
            result.error(
                f"line {lineno}: index time {raw} disagrees with its filename timestamp {fraw}"
            )

    # Ordering only -- no lower bound. An index nobody has appended to for a while
    # is not a defect, so the newest row is checked against a ceiling, not a window:
    # a row cannot claim a time later than the clock that appended it. The tight
    # wall-clock window belongs on the relay filename, which is authored once.
    if parsed:
        lineno, raw, dt, file_cell = parsed[-1]
        _s, _d, _r, is_utc = filename_timestamp(file_cell)
        now = clock_now(is_utc)
        if dt > now:
            ahead = (dt - now).total_seconds() / 60.0
            magnitude = f"{ahead / 1440.0:.1f} days" if ahead >= 1440 else f"{ahead:.0f} min"
            zone = "UTC" if is_utc else "local"
            result.error(
                f"line {lineno}: newest index time {raw} is {magnitude} ahead of the {zone} clock "
                f"(now {now.strftime('%Y%m%d-%H%M%S')}); an append-only index cannot carry a row "
                "stamped later than the append that wrote it"
            )

    if audit:
        hist = [(ln, raw, dt) for ln, raw, dt, _fc in grandfathered if dt is not None]
        unparsed = [(ln, raw) for ln, raw, dt, _fc in grandfathered if dt is None]
        viol = [
            (ln, praw, raw)
            for (ln, raw, dt), (_pln, praw, pdt) in zip(hist[1:], hist[:-1])
            if dt < pdt
        ]
        result.warn(
            f"grandfathered history: {len(grandfathered)} rows before the marker, "
            f"{len(viol)} non-monotonic, {len(unparsed)} unparseable"
        )
        for ln, praw, raw in viol:
            result.warn(f"line {ln}: (grandfathered) {praw} -> {raw} decreases")
        for ln, raw in unparsed:
            result.warn(f"line {ln}: (grandfathered) unparseable time {raw!r}")
        for ln, arity in grandfathered_arity_issues:
            result.warn(
                f"line {ln}: (grandfathered) row has {arity} cells, "
                f"header declares {header_arity}; a literal | in cell prose must be escaped as \\|"
            )

    return result


def lint_relay_root(path: Path, *, template_mode: bool = False) -> LintResult:
    result = LintResult()
    all_md = sorted((p for p in path.rglob("*.md") if p.is_file()), key=relay_order_key)
    index_files = [p for p in all_md if p.name == "INDEX.md"]
    files = [p for p in all_md if p.name != "INDEX.md"]
    if not all_md:
        result.error(f"no .md relay files found under {path}")
        return result
    per_file: List[Tuple[Path, LintResult]] = [(f, lint_file(f, template_mode=template_mode)) for f in files]
    for f, r in per_file:
        for e in r.errors:
            result.error(f"{f.relative_to(path)}: {e}")
        for w in r.warnings:
            result.warn(f"{f.relative_to(path)}: {w}")

    for idx in index_files:
        r = lint_relay_index(idx)
        for e in r.errors:
            result.error(f"{idx.relative_to(path)}: {e}")
        for w in r.warnings:
            result.warn(f"{idx.relative_to(path)}: {w}")

    # Cross-file implementation-dispatch lineage. Pair-Planner
    # dispatches are parent-lineage aware when PARENT_DISPATCH_ID is present: the
    # dispatch parent must be an approving PLAN-REVIEW relay, and that review's
    # parent must be the pair Planner's PLAN. Direct operator/orchestrator grants
    # are override paths and are exempt from this pair-lineage gate.
    phases: List[Tuple[Path, Tuple[int, object, str], str, Dict[str, str], str]] = []
    for f, _ in per_file:
        tx = read(f)
        fields = header_fields(tx)
        phases.append((f, relay_order_key(f), fields.get("PHASE", ""), fields, tx))
    by_id = dispatch_id_map(phases)

    def resolve_parent(by_id, did, before_order, predicate):
        items = by_id.get(did or "", [])
        if not items:
            return None, [], []
        if len(items) == 1:
            return items[0], items, []
        qualifying = sorted((it for it in items if it[1] < before_order and predicate(it)), key=lambda it: it[1])
        if not qualifying:
            return None, items, []
        return qualifying[-1], items, qualifying[:-1]

    def owner_review_by_id(did: str | None, owner: str | None, want_lock: str, before_order):
        # Latest same-owner Implementer DESIGN-REVIEW of the
        # locked doc, before the PLAN. Do not filter on verdict; the
        # downstream DESIGN_REVIEW_VERDICT check must catch a thread ending in
        # must-revise after an earlier approve.
        if not did or not owner:
            return None
        owned = []
        for item in by_id.get(did, []):
            _p, item_order, item_phase, item_fields, _text = item
            if item_phase != "DESIGN-REVIEW" or item_fields.get("DESIGN_DOC_ID") != want_lock or item_order >= before_order:
                continue
            addrs = split_addresses(item_fields.get("FROM"))
            if len(addrs) == 1 and addr_is(addrs[0], owner, "implementer"):
                owned.append(item)
        return sorted(owned, key=lambda item: item[1])[-1] if owned else None

    def owner_design_by_id(did: str | None, owner: str | None, want_lock: str, before_order):
        # Latest same-owner pair-Planner DESIGN of the locked doc,
        # before the DESIGN-REVIEW. Orchestrator PHASE: DESIGN dispatches that
        # share the thread id are never design-of-record candidates.
        if not did or not owner:
            return None
        owned = []
        for item in by_id.get(did, []):
            _p, item_order, item_phase, item_fields, _text = item
            if item_phase != "DESIGN" or item_fields.get("DESIGN_DOC_ID") != want_lock or item_order >= before_order:
                continue
            addrs = split_addresses(item_fields.get("FROM"))
            if len(addrs) == 1 and addr_is(addrs[0], owner, "planner"):
                owned.append(item)
        return sorted(owned, key=lambda item: item[1])[-1] if owned else None

    # Design-review lineage gate. A pair-Planner PLAN that locks a
    # design-doc-backed design must parent to an approving Implementer
    # DESIGN-REVIEW relay, and that review must parent to the DESIGN relay that
    # introduced the matching DESIGN_DOC_ID.
    for f, order, phase, fields, text in phases:
        if phase != "PLAN" or not fields.get("DESIGN_LOCK_ID"):
            continue
        from_addrs = split_addresses(fields.get("FROM"))
        if len(from_addrs) != 1:
            continue
        from_addr = from_addrs[0]
        plan_owner = from_owner(from_addr)
        if canonical_role(from_role(from_addr)) != "planner" or not plan_owner or plan_owner in SPECIAL_ADDRESS_VALUES:
            continue
        lock_id = fields.get("DESIGN_LOCK_ID", "")
        kind = fields.get("DESIGN_RECORD_KIND")

        if design_record_kind_error_context(fields, plan_owner, phases):
            result.error(f"{f.relative_to(path)}: DESIGN_LOCK_ID present with design-review context but DESIGN_RECORD_KIND is missing")
            continue

        if kind == "direct-override":
            if not is_direct_design_override(fields):
                result.error(f"{f.relative_to(path)}: pair Planner cannot use DESIGN_RECORD_KIND: direct-override; only operator/orchestrator/orchestrator-planner authority may override design review")
            continue

        same_owner_designs = design_doc_items_for_owner(phases, plan_owner)
        if kind == "audit-record":
            if same_owner_designs:
                result.error(f"{f.relative_to(path)}: DESIGN_RECORD_KIND: audit-record conflicts with observable same-owner DESIGN relay carrying DESIGN_DOC_ID")
            continue

        if not design_review_gate_applies(fields, plan_owner, phases):
            continue

        if kind not in {"design-doc", None}:
            # Enum errors are handled in lint_file; avoid duplicate/confusing lineage errors.
            continue
        if kind is None:
            # Missing-kind with context was handled above; isolated legacy path is exempt.
            continue

        design_candidates = [item for item in same_owner_designs if item[3].get("DESIGN_DOC_ID") == lock_id]
        earlier_designs = [item for item in design_candidates if item[1] < order]
        if not earlier_designs:
            result.error(f"{f.relative_to(path)}: DESIGN_LOCK_ID {lock_id!r} has no earlier same-owner DESIGN relay carrying matching DESIGN_DOC_ID")
            continue
        design_item = sorted(earlier_designs, key=lambda item: item[1])[-1]
        dp, do, dph, dfields, dtext = design_item

        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: design-doc PLAN requires PARENT_DISPATCH_ID to the approving DESIGN-REVIEW relay")
            continue
        review_item = owner_review_by_id(fields.get("PARENT_DISPATCH_ID"), plan_owner, lock_id, order)
        if review_item is None:
            result.error(f"{f.relative_to(path)}: design-doc PLAN parent {fields.get('PARENT_DISPATCH_ID')!r} does not resolve to a relay in this lineage")
            continue
        rp, ro, rph, rfields, rtext = review_item
        if ro >= order:
            result.error(f"{f.relative_to(path)}: approving DESIGN-REVIEW parent is not earlier than the PLAN relay")
        if rph != "DESIGN-REVIEW":
            result.error(f"{f.relative_to(path)}: design-doc PLAN parent must be a DESIGN-REVIEW relay")
        rfrom = split_addresses(rfields.get("FROM"))
        if len(rfrom) != 1 or not addr_is(rfrom[0], plan_owner, "implementer"):
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must be FROM {plan_owner}.implementer")
        if rfields.get("DESIGN_REVIEW_VERDICT") != "approve":
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must have DESIGN_REVIEW_VERDICT: approve")
        if rfields.get("DESIGN_DOC_ID") != lock_id:
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent DESIGN_DOC_ID must match PLAN DESIGN_LOCK_ID {lock_id!r}")
        review_parent = owner_design_by_id(rfields.get("PARENT_DISPATCH_ID"), plan_owner, lock_id, ro)
        if review_parent is None:
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent lacks a resolvable DESIGN parent")
            continue
        vdp, vdo, vdph, vdfields, vdtext = review_parent
        if vdo >= ro:
            result.error(f"{f.relative_to(path)}: DESIGN parent is not earlier than the DESIGN-REVIEW relay")
        if vdph != "DESIGN":
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW parent must point to a DESIGN relay")
        vfrom = split_addresses(vdfields.get("FROM"))
        if len(vfrom) != 1 or not addr_is(vfrom[0], plan_owner, "planner"):
            result.error(f"{f.relative_to(path)}: DESIGN-REVIEW must review the pair Planner's DESIGN relay")
        if vdfields.get("DESIGN_DOC_ID") != lock_id:
            result.error(f"{f.relative_to(path)}: DESIGN parent DESIGN_DOC_ID must match PLAN DESIGN_LOCK_ID {lock_id!r}")

    for f, order, phase, fields, text in phases:
        clean = sanitized_text(text)
        if not own_line_dispatch_present(text):
            continue
        from_addrs = split_addresses(fields.get("FROM"))
        from_addr = from_addrs[0] if len(from_addrs) == 1 else ""
        from_role_val = from_role(from_addr)
        to_addrs = split_addresses(fields.get("TO"))
        dispatch_owner = address_owner(to_addrs[0]) if len(to_addrs) == 1 else None

        # Direct operator/orchestrator/orchestrator-planner grants are explicit
        # override paths and do not belong to the pair-lineage walk. Pair
        # Planner grants are different: the privileged action is inert without
        # its approving PLAN-REVIEW parent. The parent edge is
        # mandatory for that path because the observed failures were edge-less.
        if from_role_val in LINEAGE_DIRECT_FROM_ROLES:
            continue
        if canonical_role(from_role_val) != "planner":
            continue
        if not dispatch_owner or dispatch_owner in SPECIAL_ADDRESS_VALUES:
            continue
        if from_owner(from_addr) != dispatch_owner:
            result.error(f"{f.relative_to(path)}: pair-Planner DISPATCH IMPL FROM owner does not match TO implementer owner")
            continue
        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: pair-Planner DISPATCH IMPL requires PARENT_DISPATCH_ID to an approving PLAN-REVIEW relay; absence is not a delegated-dispatch escape hatch")
            continue

        did = fields.get("PARENT_DISPATCH_ID")
        review_item, holders, extra_qualifying = resolve_parent(
            by_id,
            did,
            order,
            lambda it: it[2] == "PLAN-REVIEW"
            and (lambda a: len(a) == 1 and addr_is(a[0], dispatch_owner, "implementer"))(
                split_addresses(it[3].get("FROM"))
            ),
        )
        if review_item is None:
            if holders:
                result.error(
                    f"{f.relative_to(path)}: DISPATCH IMPL parent {did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier PLAN-REVIEW relay "
                    f"from {dispatch_owner}.implementer"
                )
            else:
                result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent {did!r} does not resolve to a relay in this lineage")
            continue
        if extra_qualifying:
            result.warn(
                f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {did!r} qualify as the "
                f"PLAN-REVIEW parent; selected latest {review_item[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}"
            )
        rp, ro, rph, rfields, rtext = review_item
        if ro >= order:
            result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent {fields.get('PARENT_DISPATCH_ID')!r} is not earlier than the dispatch relay")
        if rph != "PLAN-REVIEW" or not plan_review_approved(rtext):
            result.error(f"{f.relative_to(path)}: DISPATCH IMPL parent must be an earlier PLAN-REVIEW relay with verdict approve")
        rfrom = split_addresses(rfields.get("FROM"))
        if len(rfrom) != 1 or not addr_is(rfrom[0], dispatch_owner, "implementer"):
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent must be FROM {dispatch_owner}.implementer")
        plan_did = rfields.get("PARENT_DISPATCH_ID")
        plan_item, holders, extra_qualifying = resolve_parent(
            by_id,
            plan_did,
            ro,
            lambda it: it[2] == "PLAN"
            and (lambda a: len(a) == 1 and addr_is(a[0], dispatch_owner, "planner"))(
                split_addresses(it[3].get("FROM"))
            ),
        )
        if plan_item is None:
            if holders:
                result.error(
                    f"{f.relative_to(path)}: PLAN-REVIEW parent {plan_did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier PLAN relay "
                    f"from {dispatch_owner}.planner"
                )
            else:
                result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent lacks a resolvable pair-Planner PLAN parent")
            continue
        if extra_qualifying:
            result.warn(
                f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {plan_did!r} qualify as the "
                f"PLAN parent; selected latest {plan_item[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}"
            )
        pp, po, pph, pfields, ptext = plan_item
        if po >= ro:
            result.error(f"{f.relative_to(path)}: pair-Planner PLAN parent is not earlier than the PLAN-REVIEW relay")
        if pph != "PLAN":
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW parent must point to a PLAN relay")
        pfrom = split_addresses(pfields.get("FROM"))
        if len(pfrom) != 1 or not addr_is(pfrom[0], dispatch_owner, "planner"):
            result.error(f"{f.relative_to(path)}: PLAN-REVIEW must review the pair Planner's PLAN, not a CC'd orchestrator dispatch")
        if not any(addr_is(a, dispatch_owner, "implementer") for a in split_addresses(pfields.get("TO"))):
            result.error(f"{f.relative_to(path)}: pair-Planner PLAN must address the Implementer in TO for review")

    # Non-addressee action trap: if an IMPL report claims substantive
    # implementation actions, it must point back to the dispatch that authorized
    # those actions. Missing parent is an error, not an opt-out; otherwise a
    # sibling Implementer can act on a CC/cross-read dispatch and report clean.
    # Merge-claim visibility is handled by the separate MERGE-GATE lineage below.
    for f, order, phase, fields, text in phases:
        if phase != "IMPL":
            continue
        if not implementation_work_claimed(text, fields):
            continue
        if from_is_direct_authority(fields) and own_line_dispatch_present(text):
            # KR-8 Class A (D26): a relay issued by a direct-authority seat
            # (operator/orchestrator/orchestrator-planner) that itself carries
            # the live own-line token IS the dispatch, not a report owing a
            # parent edge. Seat-scoped deliberately: any other FROM -- an
            # implementer embedding a live token to dodge the trap -- still
            # falls through and is caught below.
            continue
        if not fields.get("PARENT_DISPATCH_ID"):
            result.error(f"{f.relative_to(path)}: IMPL report with substantive actions requires PARENT_DISPATCH_ID to the addressed DISPATCH IMPL relay")
            continue
        actor_from = split_addresses(fields.get("FROM"))
        did = fields.get("PARENT_DISPATCH_ID")
        parent, holders, extra_qualifying = resolve_parent(
            by_id,
            did,
            order,
            lambda it: impl_dispatch_parent_qualifies(actor_from, it),
        )
        if parent is None:
            if holders:
                actor = actor_from[0] if len(actor_from) == 1 else fields.get("FROM", "")
                result.error(
                    f"{f.relative_to(path)}: IMPL report parent {did!r} is held by {len(holders)} relays "
                    f"({', '.join(h[0].name for h in holders)}); none is an earlier DISPATCH IMPL relay "
                    f"addressed to {actor}"
                )
            else:
                result.error(f"{f.relative_to(path)}: IMPL report parent {did!r} does not resolve to a relay in this lineage")
            continue
        if len(holders) == 1 and impl_dispatch_parent_qualifies(actor_from, parent) and parent[1] >= order:
            result.error(
                f"{f.relative_to(path)}: IMPL report parent {did!r} is held by 1 relays "
                f"({parent[0].name}); none is an earlier DISPATCH IMPL relay addressed to {actor_from[0]}"
            )
            continue
        if extra_qualifying:
            result.warn(
                f"{f.relative_to(path)}: {len(extra_qualifying)+1} relays under {did!r} qualify as the "
                f"DISPATCH IMPL parent; selected latest {parent[0].name}; candidates: "
                f"{', '.join(h[0].name for h in extra_qualifying)}"
            )
        pp, po, pph, pfields, ptext = parent
        if not own_line_dispatch_present(ptext):
            result.error(f"{f.relative_to(path)}: IMPL report parent must be a DISPATCH IMPL relay")
            continue
        parent_to = split_addresses(pfields.get("TO"))
        if len(actor_from) == 1 and len(parent_to) == 1 and not (
            address_role(actor_from[0]) is not None
            and address_role(parent_to[0]) is not None
            and address_owner(actor_from[0]) == address_owner(parent_to[0])
            and canonical_role(address_role(actor_from[0])) == canonical_role(address_role(parent_to[0]))
        ):
            result.error(f"{f.relative_to(path)}: IMPL report FROM {actor_from[0]!r} is not the addressee of the parent DISPATCH IMPL relay")

    # Merge-claim visibility: report claiming a merge commit needs an earlier merge authorization.
    for f, order, phase, fields, text in phases:
        if merge_claimed(text, fields):
            claim_dispatch_id = fields.get("DISPATCH_ID")
            prior_merge_auth = any(
                o < order
                and claim_dispatch_id
                and fl.get("DISPATCH_ID") == claim_dispatch_id
                and merge_authorized(tx, fl)
                for p, o, ph, fl, tx in phases
            )
            if not prior_merge_auth:
                result.error(f"{f.relative_to(path)}: relay claims a merge/merge commit without an earlier MERGE-GATE authorization relay with the same DISPATCH_ID")

    # Reconciliation-layer honest-false-row support: when a lineage declares
    # ROW_TRUTH_CHECK, an OUT->IN flip for the same path is structurally dirty
    # unless a future release adds an explicit, evidence-backed override.
    row_state: Dict[Tuple[str, str], Tuple[str, Path]] = {}
    for f, order, phase, fields, text in phases:
        if fields.get("ROW_TRUTH_CHECK", "").lower() not in {"yes", "true", "required"}:
            continue
        did = fields.get("DISPATCH_ID", "")
        for header in ("SCOPE_DIFF", "FOLD_SCOPE"):
            rows, row_errors = parse_marked_rows(text, header)
            if row_errors:
                continue
            for row_path, val in rows:
                key = (did, row_path)
                if key in row_state:
                    prior_val, prior_file = row_state[key]
                    if prior_val == "out" and val == "in":
                        result.error(f"{f.relative_to(path)}: {header} flips {row_path!r} from OUT in {prior_file.relative_to(path)} to IN without a reconciliation evidence override")
                row_state[key] = (val, f)

    # Orchestrator-review visibility gate. Every orchestrator
    # authority-bearing relay in the broad SET must make the reviewer seat
    # visible by address, unless the run carries an operator-authored waiver.
    for err in orchestrator_review_gate_errors(path, phases):
        result.error(err)

    # Lock references that look like paths should exist.
    for f, _ in per_file:
        fields = header_fields(read(f))
        for key in ("DESIGN_LOCK_ID", "PLAN_LOCK_ID"):
            val = fields.get(key)
            bare = val.split(" @ ", 1)[0] if val else ""
            if bare and ("/" in bare or bare.endswith(".md")):
                if Path(bare).is_absolute():
                    found = Path(bare).exists()
                else:
                    found = (
                        (path / bare).exists()
                        or Path(bare).exists()
                        or (path.parent.parent / bare).exists()
                    )
                if not found:
                    result.error(f"{f.relative_to(path)}: {key} references missing file {bare}")

    # A5 check 1 (root mode): the GOVERNING declaration per (key, stem) —
    # latest parse-valid stamp; distinct values at an indistinguishable
    # latest timestamp refuse; identical values co-govern; earlier
    # declarations are acknowledged history (design rev28 rule 3).
    a5_decls: Dict[Tuple[str, str], List[Tuple[object, Path, str]]] = {}
    for f, _ in per_file:
        _a5text = read(f)
        for _a5dkey, _a5akey, _a5sub in A5_LOCK_PAIRS:
            _a5d = a5_eligible_declaration(f, _a5text, _a5dkey, _a5akey)
            if _a5d is not None:
                a5_decls.setdefault((_a5dkey, _a5d[1]), []).append((_a5d[0], f, _a5d[2]))
    for (_a5dkey, _a5stem), _a5list in sorted(a5_decls.items()):
        _a5latest = max(item[0] for item in _a5list)
        _a5stratum = sorted((item for item in _a5list if item[0] == _a5latest), key=lambda item: str(item[1]))
        _a5values = {item[2] for item in _a5stratum}
        if len(_a5values) > 1:
            for _stamp, _a5f, _v in _a5stratum:
                result.error(
                    f"{_a5f.relative_to(path)}: {_a5dkey}: stem {_a5stem!r} carries {len(_a5values)} "
                    "indistinguishable latest declarations; verification refuses"
                )
            continue
        _a5declared = next(iter(_a5values))
        _a5sub = dict((k, s) for k, _a, s in A5_LOCK_PAIRS)[_a5dkey]
        _a5errs = a5_resolve_and_compare(path, _a5dkey, _a5stem, _a5sub, _a5declared)
        for _stamp, _a5f, _v in _a5stratum:
            for _a5e in _a5errs:
                result.error(f"{_a5f.relative_to(path)}: {_a5e}")

    return result


def print_result(label: str, result: LintResult) -> None:
    for w in result.warnings:
        print(f"WARN {label}: {w}")
    for e in result.errors:
        print(f"ERROR {label}: {e}")
    if result.ok and not result.warnings:
        print(f"OK {label}")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Lint agentic-dev-team relay files.")
    parser.add_argument("paths", nargs="*", type=Path, help="relay .md files to lint")
    parser.add_argument("--relay-root", type=Path, help="lint all .md files under a dispatch relay directory and cross-check lineage")
    parser.add_argument("--templates", action="store_true", help="lint relay templates with placeholder values instead of strict real-relay values")
    parser.add_argument("--index", type=Path, help="check an append-only relay INDEX for timestamp validity, monotonicity, filename agreement, and no future newest row")
    parser.add_argument("--index-audit", action="store_true", help="with --index, also report grandfathered pre-marker history as warnings")
    parser.add_argument("--no-freshness", action="store_true", help="skip the filename authoring-drift check (use when re-verifying a historical relay)")
    parser.add_argument("--max-drift-minutes", type=int, default=DEFAULT_MAX_DRIFT_MINUTES, help=f"filename authoring-drift tolerance in minutes (default {DEFAULT_MAX_DRIFT_MINUTES})")
    args = parser.parse_args(argv)
    if not args.paths and not args.relay_root and not args.index:
        parser.print_help(sys.stderr)
        return 2
    freshness = not args.no_freshness

    overall = LintResult()
    if args.relay_root:
        # A historical sweep never checks freshness: those relays are legitimately old.
        r = lint_relay_root(args.relay_root, template_mode=args.templates)
        print_result(str(args.relay_root), r)
        overall.errors.extend(r.errors)
        overall.warnings.extend(r.warnings)
    if args.index:
        r = lint_relay_index(args.index, audit=args.index_audit)
        print_result(str(args.index), r)
        overall.errors.extend(r.errors)
        overall.warnings.extend(r.warnings)
    for path in args.paths:
        # Explicitly-linted files are the authoring path: enforce drift here.
        r = lint_file(
            path, template_mode=args.templates,
            freshness=freshness, max_drift_minutes=args.max_drift_minutes,
        )
        print_result(str(path), r)
        overall.errors.extend(r.errors)
        overall.warnings.extend(r.warnings)
    return 1 if overall.errors else 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
