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
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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
    "Downstream Implementer",
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
HUMAN_GATE_BARE_YES = re.compile(r"^yes[ \t—–:-]*$")
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
MASTER_TIER_ROLES = {"master-planner", "master-reviewer", "domain-planner", "domain-reviewer"}
H27_CONFLICT_FIELDS = (
    "AUTHORITY", "PHASE", "FROM", "TO", "DESIGN_DOC_ID", "DESIGN_LOCK_ID",
    "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT", "DELEGATED_DISPATCH_AUTHORITY",
    "DISPATCH_ID", "PARENT_DISPATCH_ID", "COMMISSION_AUTHORIZATION", "COMMISSION_ID",
    "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
)
H27_TEMPLATE_MASTER_TIER_FROM_RE = re.compile(
    r"^<[A-Za-z][A-Za-z0-9_-]*>\.(master-planner|master-reviewer|domain-planner|domain-reviewer)$"
)

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
        special_expected = {
            "orchestrator": "orchestrator-planner",
            "operator": "operator",
        }[actual]
        if canonical_role(expected) != special_expected:
            return f"ROLE/FROM mismatch: ROLE={role!r} but FROM={from_addrs[0]!r}; do not proxy-author another seat's relay"
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
        for did in set(h27_occurrences(item[4], "DISPATCH_ID")):
            out.setdefault(did, []).append(item)
    return out


def same_owner_addr(owner: str | None, role: str) -> str:
    return f"{owner}.{role}" if owner else ""


def own_line_dispatch_present(text: str) -> bool:
    return re.search(r"^DISPATCH IMPL\s*$", operational_token_text(text), flags=re.MULTILINE) is not None


def own_line_merge_present(text: str) -> bool:
    return re.search(r"^DISPATCH MERGE\s*$", operational_token_text(text), flags=re.MULTILINE) is not None


def h27_master_seat_errors(text: str, fields: Dict[str, str], *, template_mode: bool) -> List[str]:
    """Ruled file/template arms for the four master-tier seats (H27 rules 1-2)."""
    errors: List[str] = []
    master_roles: List[str] = []
    for raw in h27_occurrences(text, "FROM"):
        segments = raw.split("|") if template_mode else split_addresses(raw)
        for segment in segments:
            candidate = segment.strip()
            role = from_role(candidate)
            if role in MASTER_TIER_ROLES:
                master_roles.append(role)
                continue
            if template_mode:
                match = H27_TEMPLATE_MASTER_TIER_FROM_RE.fullmatch(candidate)
                if match:
                    master_roles.append(match.group(1))
    if not master_roles:
        return errors
    role = master_roles[0]
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

    for key in H27_CONFLICT_FIELDS:
        if key in authority_keys:
            continue
        values = h27_occurrences(text, key)
        distinct_values = set(values)
        if len(distinct_values) > 1:
            errors.append(
                f"{key} carries {len(distinct_values)} distinct values across {len(values)} occurrences; "
                "an authority-critical field is fail-closed unless exactly one distinct value is present "
                "(DD-v29-master-authority-20260809 rule 5)"
            )

    if own_line_dispatch_present(text):
        errors.append(
            f"master-tier seat {role!r} may not carry DISPATCH IMPL: execution authority never enters the master tier "
            "(DD-v29-master-authority-20260809: token prohibition)"
        )
    if own_line_merge_present(text):
        errors.append(
            f"master-tier seat {role!r} may not carry DISPATCH MERGE: execution authority never enters the master tier "
            "(DD-v29-master-authority-20260809: token prohibition)"
        )
    if resolved.get("DESIGN_RECORD_KIND") == "direct-override":
        errors.append(
            f"master-tier seat {role!r} may not use DESIGN_RECORD_KIND: direct-override: that record kind stays on the "
            "operator/orchestrator chain (DD-v29-master-authority-20260809 cross-seat rule 2)"
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
    verdicts = merge_authorization_verdict_lines(text)
    if len(verdicts) > 1 or any(not merge_authorization_positive(key, value)
                                for _line, key, value in verdicts):
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
    no_action_ref = re.match(
        r"^ACTIONS_GIT_REF:[ \t]*none[ \t]+—[ \t]+\S", action_ref_block, flags=re.IGNORECASE
    ) is not None
    field_claim = bool(action_ref_block.strip()) and not no_action_ref and any(
        re.search(pattern, scoped, flags=re.IGNORECASE) for pattern in field_patterns
    )
    canonical_surface = remove_named_block(operational, "ESCALATION_SCAN")
    canonical_surface = remove_named_block(canonical_surface, "SUBJECT")
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
    to_addrs = split_addresses(fields.get("TO"))
    if len(to_addrs) != 1 or not is_implementer_address(to_addrs[0]):
        return False
    verdicts = merge_authorization_verdict_lines(text)
    if len(verdicts) > 1:
        # A duplicate/conflicting grant relay is structurally dirty and must not
        # satisfy merge lineage, even if one pasted line says approved.
        return False
    field_form = bool(verdicts) and merge_authorization_positive(verdicts[0][1], verdicts[0][2])
    return field_form or merge_token_authorized(text, fields)


def relay_order_key(path: Path, root: Optional[Path] = None) -> Tuple[int, object, str]:
    stem = path.stem
    # Preferred: timestamp anywhere in protocol-ish filenames.
    m = re.search(r"(\d{8})[-T]?(\d{6})(?:Z)?", stem)
    if m:
        return (0, f"{m.group(1)}{m.group(2)}", path.name)
    # Legacy fixtures often use numeric prefixes.
    m = re.match(r"^(\d+)", stem)
    if m:
        return (1, int(m.group(1)), path.name)
    # Deterministic fallback: stable path ordering, never filesystem stat.
    if root is not None:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
    else:
        rel = path.as_posix()
    return (2, rel, path.name)


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


_DRAFT_SHAPE_HEADER = re.compile(r"^([A-Z][A-Z0-9_]*):[ \t]*(.*)$")
SEPARATORS = {
    '\v': ('U+000B', 'LINE TABULATION'),
    '\f': ('U+000C', 'FORM FEED'),
    '\x1c': ('U+001C', 'INFORMATION SEPARATOR FOUR'),
    '\x1d': ('U+001D', 'INFORMATION SEPARATOR THREE'),
    '\x1e': ('U+001E', 'INFORMATION SEPARATOR TWO'),
    '\x85': ('U+0085', 'NEXT LINE'),
    '\u2028': ('U+2028', 'LINE SEPARATOR'),
    '\u2029': ('U+2029', 'PARAGRAPH SEPARATOR'),
}
LATE_CONTROL_KEYS = ('SUPERSEDES', 'CYCLE_TERMINUS', 'CYCLE_REOPEN')
# Unicode 16.0.0: Zs and Default_Ignorable_Code_Point, pinned by R237.
_SHAPE_ZS_RANGES = ((0x20, 0x20), (0xA0, 0xA0), (0x1680, 0x1680),
                    (0x2000, 0x200A), (0x202F, 0x202F), (0x205F, 0x205F),
                    (0x3000, 0x3000))
_SHAPE_IGNORABLE_RANGES = (
    (0xAD, 0xAD), (0x34F, 0x34F), (0x61C, 0x61C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_SHAPE_IGNORABLE = frozenset(chr(c) for a, b in _SHAPE_IGNORABLE_RANGES
                             for c in range(a, b + 1))
SHAPE_SET_ASIDE = _SHAPE_IGNORABLE | frozenset(
    chr(c) for a, b in ((9, 9),) + _SHAPE_ZS_RANGES for c in range(a, b + 1))
# Frozen Unicode 16 Cc (except tab), Cf, DICP, and braille blank.
_SHAPE_REMOVAL_RANGES = ((173, 173), (1536, 1541), (1564, 1564), (1757, 1757), (1807, 1807), (2192, 2193), (2274, 2274), (6158, 6158), (8203, 8207), (8234, 8238), (8288, 8292), (8294, 8303), (65279, 65279), (65529, 65531), (69821, 69821), (69837, 69837), (78896, 78911), (113824, 113827), (119155, 119162), (917505, 917505), (917536, 917631), (0, 8), (10, 31), (127, 159), (10240, 10240))
SHAPE_REMOVAL = _SHAPE_IGNORABLE | frozenset(
    chr(c) for a, b in _SHAPE_REMOVAL_RANGES for c in range(a, b+1))
# Derived from Unicode 16 compatibility decomposition and confusable skeletons.
SHAPE_KEY_MAP = {'0': 'O',
 '1': 'l',
 'I': 'l',
 'm': 'rn',
 '|': 'l',
 'ª': 'a',
 '²': '2',
 '³': '3',
 '¹': 'l',
 'º': 'o',
 'Æ': 'AE',
 '×': 'x',
 'æ': 'ae',
 'ı': 'i',
 'Ĳ': 'lJ',
 'ĳ': 'ij',
 'Œ': 'OE',
 'œ': 'oe',
 'ſ': 's',
 'Ƅ': 'b',
 'ƍ': 'g',
 'Ɩ': 'l',
 'Ʀ': 'R',
 'Ƨ': '2',
 'Ʒ': '3',
 'Ƽ': '5',
 'ƽ': 's',
 'ǀ': 'l',
 'ǁ': 'll',
 'Ǉ': 'LJ',
 'ǈ': 'Lj',
 'ǉ': 'lj',
 'Ǌ': 'NJ',
 'ǋ': 'Nj',
 'ǌ': 'nj',
 'Ǳ': 'DZ',
 'ǲ': 'Dz',
 'ǳ': 'dz',
 'Ȝ': '3',
 'Ȣ': '8',
 'ȣ': '8',
 'ɑ': 'a',
 'ɡ': 'g',
 'ɣ': 'y',
 'ɩ': 'i',
 'ɪ': 'i',
 'ɯ': 'w',
 'ʋ': 'u',
 'ʏ': 'y',
 'ʣ': 'dz',
 'ʦ': 'ts',
 'ʪ': 'ls',
 'ʫ': 'lz',
 'ʰ': 'h',
 'ʲ': 'j',
 'ʳ': 'r',
 'ʷ': 'w',
 'ʸ': 'y',
 'ː': ':',
 'ˠ': 'y',
 'ˡ': 'l',
 'ˢ': 's',
 'ˣ': 'x',
 '˸': ':',
 'Ϳ': 'J',
 'Α': 'A',
 'Β': 'B',
 'Ε': 'E',
 'Ζ': 'Z',
 'Η': 'H',
 'Ι': 'l',
 'Κ': 'K',
 'Μ': 'M',
 'Ν': 'N',
 'Ο': 'O',
 'Ρ': 'P',
 'Τ': 'T',
 'Υ': 'Y',
 'Χ': 'X',
 'α': 'a',
 'γ': 'y',
 'ι': 'i',
 'ν': 'v',
 'ο': 'o',
 'ρ': 'p',
 'σ': 'o',
 'υ': 'u',
 'ϒ': 'Y',
 'Ϝ': 'F',
 'Ϩ': '2',
 'ϱ': 'p',
 'ϳ': 'j',
 'Ϻ': 'M',
 'Ѕ': 'S',
 'І': 'l',
 'Ј': 'J',
 'А': 'A',
 'В': 'B',
 'Е': 'E',
 'З': '3',
 'К': 'K',
 'М': 'M',
 'Н': 'H',
 'О': 'O',
 'Р': 'P',
 'С': 'C',
 'Т': 'T',
 'У': 'Y',
 'Х': 'X',
 'Ы': 'bl',
 'Ь': 'b',
 'Ю': 'lO',
 'а': 'a',
 'б': '6',
 'г': 'r',
 'е': 'e',
 'о': 'o',
 'р': 'p',
 'с': 'c',
 'у': 'y',
 'х': 'x',
 'ѕ': 's',
 'і': 'i',
 'ј': 'j',
 'ѡ': 'w',
 'Ѵ': 'V',
 'ѵ': 'v',
 'Ү': 'Y',
 'ү': 'y',
 'һ': 'h',
 'ҽ': 'e',
 'Ӏ': 'l',
 'ӏ': 'i',
 'Ӕ': 'AE',
 'ӕ': 'ae',
 'Ӡ': '3',
 'ԁ': 'd',
 'Ԍ': 'G',
 'ԛ': 'q',
 'Ԝ': 'W',
 'ԝ': 'w',
 'Ս': 'U',
 'Տ': 'S',
 'Օ': 'O',
 'ա': 'w',
 'գ': 'q',
 'զ': 'q',
 'հ': 'h',
 'ո': 'n',
 'ռ': 'n',
 'ս': 'u',
 'ց': 'g',
 'ք': 'f',
 'օ': 'o',
 '։': ':',
 '׀': 'l',
 '׃': ':',
 'ו': 'l',
 'ט': 'v',
 'ן': 'l',
 'ס': 'o',
 'װ': 'll',
 'ا': 'l',
 'ه': 'o',
 '١': 'l',
 '٥': 'o',
 '٧': 'V',
 'ھ': 'o',
 'ہ': 'o',
 'ە': 'o',
 '۱': 'l',
 '۵': 'o',
 '۷': 'V',
 '܃': ':',
 '܄': ':',
 '߀': 'O',
 'ߊ': 'l',
 'ߺ': '_',
 'ः': ':',
 '०': 'o',
 '০': 'O',
 '৪': '8',
 '৭': '9',
 '੦': 'o',
 '੧': '9',
 '੪': '8',
 'ઃ': ':',
 '૦': 'o',
 'ଃ': '8',
 'ଠ': 'O',
 '୦': 'O',
 '୨': '9',
 '௦': 'o',
 'ం': 'o',
 '౦': 'o',
 'ಂ': 'o',
 '೦': 'o',
 'ം': 'o',
 'ഠ': 'o',
 '൦': 'o',
 '൭': '9',
 'ං': 'o',
 '๐': 'o',
 '໐': 'o',
 'ဝ': 'o',
 '၀': 'o',
 'ყ': 'y',
 'ჿ': 'o',
 'ሀ': 'U',
 'ዐ': 'O',
 'Ꭰ': 'D',
 'Ꭱ': 'R',
 'Ꭲ': 'T',
 'Ꭵ': 'i',
 'Ꭹ': 'Y',
 'Ꭺ': 'A',
 'Ꭻ': 'J',
 'Ꭼ': 'E',
 'Ꮃ': 'W',
 'Ꮇ': 'M',
 'Ꮋ': 'H',
 'Ꮍ': 'Y',
 'Ꮐ': 'G',
 'Ꮒ': 'h',
 'Ꮓ': 'Z',
 'Ꮞ': '4',
 'Ꮟ': 'b',
 'Ꮢ': 'R',
 'Ꮤ': 'W',
 'Ꮥ': 'S',
 'Ꮩ': 'V',
 'Ꮪ': 'S',
 'Ꮮ': 'L',
 'Ꮯ': 'C',
 'Ꮲ': 'P',
 'Ꮶ': 'K',
 'Ꮷ': 'd',
 'Ꮾ': '6',
 'Ᏻ': 'G',
 'Ᏼ': 'B',
 'ᐯ': 'V',
 'ᑌ': 'U',
 'ᑭ': 'P',
 'ᑯ': 'd',
 'ᑲ': 'b',
 'ᒍ': 'J',
 'ᒪ': 'L',
 'ᒿ': '2',
 'ᕁ': 'x',
 'ᕼ': 'H',
 'ᕽ': 'x',
 'ᖇ': 'R',
 'ᖯ': 'b',
 'ᖴ': 'F',
 'ᗅ': 'A',
 'ᗞ': 'D',
 'ᗪ': 'D',
 'ᗰ': 'M',
 'ᗷ': 'B',
 '᙭': 'X',
 '᙮': 'x',
 'ᚷ': 'X',
 'ᛁ': 'l',
 'ᛕ': 'K',
 'ᛖ': 'M',
 '᛬': ':',
 '᠃': ':',
 '᠉': ':',
 'ᴄ': 'c',
 'ᴏ': 'o',
 'ᴑ': 'o',
 'ᴜ': 'u',
 'ᴠ': 'v',
 'ᴡ': 'w',
 'ᴢ': 'z',
 'ᴦ': 'r',
 'ᴬ': 'A',
 'ᴭ': 'AE',
 'ᴮ': 'B',
 'ᴰ': 'D',
 'ᴱ': 'E',
 'ᴳ': 'G',
 'ᴴ': 'H',
 'ᴵ': 'l',
 'ᴶ': 'J',
 'ᴷ': 'K',
 'ᴸ': 'L',
 'ᴹ': 'M',
 'ᴺ': 'N',
 'ᴼ': 'O',
 'ᴽ': '8',
 'ᴾ': 'P',
 'ᴿ': 'R',
 'ᵀ': 'T',
 'ᵁ': 'U',
 'ᵂ': 'W',
 'ᵃ': 'a',
 'ᵅ': 'a',
 'ᵇ': 'b',
 'ᵈ': 'd',
 'ᵉ': 'e',
 'ᵍ': 'g',
 'ᵏ': 'k',
 'ᵐ': 'rn',
 'ᵒ': 'o',
 'ᵖ': 'p',
 'ᵗ': 't',
 'ᵘ': 'u',
 'ᵚ': 'w',
 'ᵛ': 'v',
 'ᵞ': 'y',
 'ᵢ': 'i',
 'ᵣ': 'r',
 'ᵤ': 'u',
 'ᵥ': 'v',
 'ᵧ': 'y',
 'ᵨ': 'p',
 'ᵫ': 'ue',
 'ᶃ': 'g',
 'ᶌ': 'y',
 'ᶜ': 'c',
 'ᶠ': 'f',
 'ᶢ': 'g',
 'ᶥ': 'i',
 'ᶦ': 'i',
 'ᶸ': 'u',
 'ᶹ': 'u',
 'ᶻ': 'z',
 'ẝ': 'f',
 'ỿ': 'y',
 'ι': 'i',
 '‖': 'll',
 '⁚': ':',
 '⁰': 'O',
 'ⁱ': 'i',
 '⁴': '4',
 '⁵': '5',
 '⁶': '6',
 '⁷': '7',
 '⁸': '8',
 '⁹': '9',
 'ⁿ': 'n',
 '₀': 'O',
 '₁': 'l',
 '₂': '2',
 '₃': '3',
 '₄': '4',
 '₅': '5',
 '₆': '6',
 '₇': '7',
 '₈': '8',
 '₉': '9',
 'ₐ': 'a',
 'ₑ': 'e',
 'ₒ': 'o',
 'ₓ': 'x',
 'ₕ': 'h',
 'ₖ': 'k',
 'ₗ': 'l',
 'ₘ': 'rn',
 'ₙ': 'n',
 'ₚ': 'p',
 'ₛ': 's',
 'ₜ': 't',
 '₨': 'Rs',
 '₶': 'lt',
 'ℂ': 'C',
 'ℊ': 'g',
 'ℋ': 'H',
 'ℌ': 'H',
 'ℍ': 'H',
 'ℎ': 'h',
 'ℐ': 'l',
 'ℑ': 'l',
 'ℒ': 'L',
 'ℓ': 'l',
 'ℕ': 'N',
 '№': 'No',
 'ℙ': 'P',
 'ℚ': 'Q',
 'ℛ': 'R',
 'ℜ': 'R',
 'ℝ': 'R',
 '℠': 'SM',
 '℡': 'TEL',
 '™': 'TM',
 'ℤ': 'Z',
 'ℨ': 'Z',
 'K': 'K',
 'ℬ': 'B',
 'ℭ': 'C',
 '℮': 'e',
 'ℯ': 'e',
 'ℰ': 'E',
 'ℱ': 'F',
 'ℳ': 'M',
 'ℴ': 'o',
 'ℹ': 'i',
 '℻': 'FAX',
 'ℽ': 'y',
 'ⅅ': 'D',
 'ⅆ': 'd',
 'ⅇ': 'e',
 'ⅈ': 'i',
 'ⅉ': 'j',
 'Ⅰ': 'l',
 'Ⅱ': 'll',
 'Ⅲ': 'lll',
 'Ⅳ': 'lV',
 'Ⅴ': 'V',
 'Ⅵ': 'Vl',
 'Ⅶ': 'Vll',
 'Ⅷ': 'Vlll',
 'Ⅸ': 'lX',
 'Ⅹ': 'X',
 'Ⅺ': 'Xl',
 'Ⅻ': 'Xll',
 'Ⅼ': 'L',
 'Ⅽ': 'C',
 'Ⅾ': 'D',
 'Ⅿ': 'M',
 'ⅰ': 'i',
 'ⅱ': 'ii',
 'ⅲ': 'iii',
 'ⅳ': 'iv',
 'ⅴ': 'v',
 'ⅵ': 'vi',
 'ⅶ': 'vii',
 'ⅷ': 'viii',
 'ⅸ': 'ix',
 'ⅹ': 'x',
 'ⅺ': 'xi',
 'ⅻ': 'xii',
 'ⅼ': 'l',
 'ⅽ': 'c',
 'ⅾ': 'd',
 'ⅿ': 'rn',
 '∞': 'oo',
 '∣': 'l',
 '∥': 'll',
 '∨': 'v',
 '∪': 'U',
 '∶': ':',
 '⊤': 'T',
 '⋁': 'v',
 '⋃': 'U',
 '⋿': 'E',
 '⍳': 'i',
 '⍴': 'p',
 '⍺': 'a',
 '⏽': 'l',
 '①': 'l',
 '②': '2',
 '③': '3',
 '④': '4',
 '⑤': '5',
 '⑥': '6',
 '⑦': '7',
 '⑧': '8',
 '⑨': '9',
 '⑩': 'lO',
 '⑪': 'll',
 '⑫': 'l2',
 '⑬': 'l3',
 '⑭': 'l4',
 '⑮': 'l5',
 '⑯': 'l6',
 '⑰': 'l7',
 '⑱': 'l8',
 '⑲': 'l9',
 '⑳': '2O',
 'Ⓐ': 'A',
 'Ⓑ': 'B',
 'Ⓒ': 'C',
 'Ⓓ': 'D',
 'Ⓔ': 'E',
 'Ⓕ': 'F',
 'Ⓖ': 'G',
 'Ⓗ': 'H',
 'Ⓘ': 'l',
 'Ⓙ': 'J',
 'Ⓚ': 'K',
 'Ⓛ': 'L',
 'Ⓜ': 'M',
 'Ⓝ': 'N',
 'Ⓞ': 'O',
 'Ⓟ': 'P',
 'Ⓠ': 'Q',
 'Ⓡ': 'R',
 'Ⓢ': 'S',
 'Ⓣ': 'T',
 'Ⓤ': 'U',
 'Ⓥ': 'V',
 'Ⓦ': 'W',
 'Ⓧ': 'X',
 'Ⓨ': 'Y',
 'Ⓩ': 'Z',
 'ⓐ': 'a',
 'ⓑ': 'b',
 'ⓒ': 'c',
 'ⓓ': 'd',
 'ⓔ': 'e',
 'ⓕ': 'f',
 'ⓖ': 'g',
 'ⓗ': 'h',
 'ⓘ': 'i',
 'ⓙ': 'j',
 'ⓚ': 'k',
 'ⓛ': 'l',
 'ⓜ': 'rn',
 'ⓝ': 'n',
 'ⓞ': 'o',
 'ⓟ': 'p',
 'ⓠ': 'q',
 'ⓡ': 'r',
 'ⓢ': 's',
 'ⓣ': 't',
 'ⓤ': 'u',
 'ⓥ': 'v',
 'ⓦ': 'w',
 'ⓧ': 'x',
 'ⓨ': 'y',
 'ⓩ': 'z',
 '⓪': 'O',
 '╳': 'X',
 '⟙': 'T',
 '⤫': 'x',
 '⤬': 'x',
 '⨯': 'x',
 'ⱼ': 'j',
 'ⱽ': 'V',
 'ⲅ': 'r',
 'Ⲏ': 'H',
 'Ⲓ': 'l',
 'Ⲕ': 'K',
 'Ⲙ': 'M',
 'Ⲛ': 'N',
 'Ⲟ': 'O',
 'ⲟ': 'o',
 'Ⲣ': 'P',
 'ⲣ': 'p',
 'Ⲥ': 'C',
 'ⲥ': 'c',
 'Ⲧ': 'T',
 'Ⲩ': 'Y',
 'Ⲭ': 'X',
 'Ⳋ': '9',
 'Ⳍ': '3',
 'Ⳑ': 'L',
 'Ⳓ': '6',
 'ⴸ': 'V',
 'ⴹ': 'E',
 'ⵏ': 'l',
 'ⵔ': 'O',
 'ⵕ': 'Q',
 'ⵝ': 'X',
 '〇': 'O',
 '㉐': 'PTE',
 '㉑': '2l',
 '㉒': '22',
 '㉓': '23',
 '㉔': '24',
 '㉕': '25',
 '㉖': '26',
 '㉗': '27',
 '㉘': '28',
 '㉙': '29',
 '㉚': '3O',
 '㉛': '3l',
 '㉜': '32',
 '㉝': '33',
 '㉞': '34',
 '㉟': '35',
 '㊱': '36',
 '㊲': '37',
 '㊳': '38',
 '㊴': '39',
 '㊵': '4O',
 '㊶': '4l',
 '㊷': '42',
 '㊸': '43',
 '㊹': '44',
 '㊺': '45',
 '㊻': '46',
 '㊼': '47',
 '㊽': '48',
 '㊾': '49',
 '㊿': '5O',
 '㋌': 'Hg',
 '㋍': 'erg',
 '㋎': 'eV',
 '㋏': 'LTD',
 '㍱': 'hPa',
 '㍲': 'da',
 '㍳': 'AU',
 '㍴': 'bar',
 '㍵': 'oV',
 '㍶': 'pc',
 '㍷': 'drn',
 '㍸': 'drn2',
 '㍹': 'drn3',
 '㍺': 'lU',
 '㎀': 'pA',
 '㎁': 'nA',
 '㎃': 'rnA',
 '㎄': 'kA',
 '㎅': 'KB',
 '㎆': 'MB',
 '㎇': 'GB',
 '㎈': 'cal',
 '㎉': 'kcal',
 '㎊': 'pF',
 '㎋': 'nF',
 '㎎': 'rng',
 '㎏': 'kg',
 '㎐': 'Hz',
 '㎑': 'kHz',
 '㎒': 'MHz',
 '㎓': 'GHz',
 '㎔': 'THz',
 '㎖': 'rnl',
 '㎗': 'dl',
 '㎘': 'kl',
 '㎙': 'frn',
 '㎚': 'nrn',
 '㎜': 'rnrn',
 '㎝': 'crn',
 '㎞': 'krn',
 '㎟': 'rnrn2',
 '㎠': 'crn2',
 '㎡': 'rn2',
 '㎢': 'krn2',
 '㎣': 'rnrn3',
 '㎤': 'crn3',
 '㎥': 'rn3',
 '㎦': 'krn3',
 '㎩': 'Pa',
 '㎪': 'kPa',
 '㎫': 'MPa',
 '㎬': 'GPa',
 '㎭': 'rad',
 '㎰': 'ps',
 '㎱': 'ns',
 '㎳': 'rns',
 '㎴': 'pV',
 '㎵': 'nV',
 '㎷': 'rnV',
 '㎸': 'kV',
 '㎹': 'MV',
 '㎺': 'pW',
 '㎻': 'nW',
 '㎽': 'rnW',
 '㎾': 'kW',
 '㎿': 'MW',
 '㏃': 'Bq',
 '㏄': 'cc',
 '㏅': 'cd',
 '㏈': 'dB',
 '㏉': 'Gy',
 '㏊': 'ha',
 '㏋': 'HP',
 '㏌': 'in',
 '㏍': 'KK',
 '㏎': 'KM',
 '㏏': 'kt',
 '㏐': 'lrn',
 '㏑': 'ln',
 '㏒': 'log',
 '㏓': 'lx',
 '㏔': 'rnb',
 '㏕': 'rnil',
 '㏖': 'rnol',
 '㏗': 'PH',
 '㏙': 'PPM',
 '㏚': 'PR',
 '㏛': 'sr',
 '㏜': 'Sv',
 '㏝': 'Wb',
 '㏿': 'gal',
 'ꓐ': 'B',
 'ꓑ': 'P',
 'ꓒ': 'd',
 'ꓓ': 'D',
 'ꓔ': 'T',
 'ꓖ': 'G',
 'ꓗ': 'K',
 'ꓙ': 'J',
 'ꓚ': 'C',
 'ꓜ': 'Z',
 'ꓝ': 'F',
 'ꓟ': 'M',
 'ꓠ': 'N',
 'ꓡ': 'L',
 'ꓢ': 'S',
 'ꓣ': 'R',
 'ꓦ': 'V',
 'ꓧ': 'H',
 'ꓪ': 'W',
 'ꓫ': 'X',
 'ꓬ': 'Y',
 'ꓮ': 'A',
 'ꓰ': 'E',
 'ꓲ': 'l',
 'ꓳ': 'O',
 'ꓴ': 'U',
 'ꓽ': ':',
 'Ꙅ': '2',
 'ꙇ': 'i',
 'Ꚙ': 'OO',
 'ꚙ': 'oo',
 'ꛟ': 'V',
 'ꛯ': '2',
 'Ꜩ': 'T3',
 'ꜱ': 's',
 'Ꜳ': 'AA',
 'ꜳ': 'aa',
 'Ꜵ': 'AO',
 'ꜵ': 'ao',
 'Ꜷ': 'AU',
 'ꜷ': 'au',
 'Ꜹ': 'AV',
 'ꜹ': 'av',
 'Ꜻ': 'AV',
 'ꜻ': 'av',
 'Ꜽ': 'AY',
 'ꜽ': 'ay',
 'Ꝏ': 'OO',
 'ꝏ': 'oo',
 'Ꝛ': '2',
 'Ꝫ': '3',
 'Ꝯ': '9',
 'ꝷ': 'tf',
 '꞉': ':',
 'Ꞙ': 'F',
 'ꞙ': 'f',
 'ꞟ': 'u',
 'Ɜ': '3',
 'Ʝ': 'J',
 'Ꭓ': 'X',
 'Ꞵ': 'B',
 'ꟲ': 'C',
 'ꟳ': 'F',
 'ꟴ': 'Q',
 'ꟹ': 'oe',
 'ꬲ': 'e',
 'ꬵ': 'f',
 'ꬽ': 'o',
 'ꭇ': 'r',
 'ꭈ': 'r',
 'ꭎ': 'u',
 'ꭒ': 'u',
 'ꭚ': 'y',
 'ꭟ': 'u',
 'ꭣ': 'uo',
 'ꭵ': 'i',
 'ꮁ': 'r',
 'ꮃ': 'w',
 'ꮓ': 'z',
 'ꮩ': 'v',
 'ꮪ': 's',
 'ꮯ': 'c',
 'ﬀ': 'ff',
 'ﬁ': 'fi',
 'ﬂ': 'fl',
 'ﬃ': 'ffi',
 'ﬄ': 'ffl',
 'ﬅ': 'st',
 'ﬆ': 'st',
 'ﮦ': 'o',
 'ﮧ': 'o',
 'ﮨ': 'o',
 'ﮩ': 'o',
 'ﮪ': 'o',
 'ﮫ': 'o',
 'ﮬ': 'o',
 'ﮭ': 'o',
 '︓': ':',
 '︳': '_',
 '︴': '_',
 '﹍': '_',
 '﹎': '_',
 '﹏': '_',
 '﹕': ':',
 'ﺍ': 'l',
 'ﺎ': 'l',
 'ﻩ': 'o',
 'ﻪ': 'o',
 'ﻫ': 'o',
 'ﻬ': 'o',
 '０': 'O',
 '１': 'l',
 '２': '2',
 '３': '3',
 '４': '4',
 '５': '5',
 '６': '6',
 '７': '7',
 '８': '8',
 '９': '9',
 '：': ':',
 'Ａ': 'A',
 'Ｂ': 'B',
 'Ｃ': 'C',
 'Ｄ': 'D',
 'Ｅ': 'E',
 'Ｆ': 'F',
 'Ｇ': 'G',
 'Ｈ': 'H',
 'Ｉ': 'l',
 'Ｊ': 'J',
 'Ｋ': 'K',
 'Ｌ': 'L',
 'Ｍ': 'M',
 'Ｎ': 'N',
 'Ｏ': 'O',
 'Ｐ': 'P',
 'Ｑ': 'Q',
 'Ｒ': 'R',
 'Ｓ': 'S',
 'Ｔ': 'T',
 'Ｕ': 'U',
 'Ｖ': 'V',
 'Ｗ': 'W',
 'Ｘ': 'X',
 'Ｙ': 'Y',
 'Ｚ': 'Z',
 '＿': '_',
 'ａ': 'a',
 'ｂ': 'b',
 'ｃ': 'c',
 'ｄ': 'd',
 'ｅ': 'e',
 'ｆ': 'f',
 'ｇ': 'g',
 'ｈ': 'h',
 'ｉ': 'i',
 'ｊ': 'j',
 'ｋ': 'k',
 'ｌ': 'l',
 'ｍ': 'rn',
 'ｎ': 'n',
 'ｏ': 'o',
 'ｐ': 'p',
 'ｑ': 'q',
 'ｒ': 'r',
 'ｓ': 's',
 'ｔ': 't',
 'ｕ': 'u',
 'ｖ': 'v',
 'ｗ': 'w',
 'ｘ': 'x',
 'ｙ': 'y',
 'ｚ': 'z',
 '｜': 'l',
 '𐊂': 'B',
 '𐊆': 'E',
 '𐊇': 'F',
 '𐊊': 'l',
 '𐊐': 'X',
 '𐊒': 'O',
 '𐊕': 'P',
 '𐊖': 'S',
 '𐊗': 'T',
 '𐊠': 'A',
 '𐊡': 'B',
 '𐊢': 'C',
 '𐊥': 'F',
 '𐊫': 'O',
 '𐊰': 'M',
 '𐊱': 'T',
 '𐊲': 'Y',
 '𐊴': 'X',
 '𐋏': 'H',
 '𐋵': 'Z',
 '𐌁': 'B',
 '𐌂': 'C',
 '𐌉': 'l',
 '𐌑': 'M',
 '𐌕': 'T',
 '𐌗': 'X',
 '𐌚': '8',
 '𐌠': 'l',
 '𐌢': 'X',
 '𐐄': 'O',
 '𐐕': 'C',
 '𐐛': 'L',
 '𐐠': 'S',
 '𐐬': 'o',
 '𐐽': 'c',
 '𐑈': 's',
 '𐒴': 'R',
 '𐓂': 'O',
 '𐓎': 'U',
 '𐓒': '7',
 '𐓪': 'o',
 '𐓶': 'u',
 '𐔓': 'N',
 '𐔖': 'O',
 '𐔘': 'K',
 '𐔜': 'C',
 '𐔝': 'V',
 '𐔥': 'F',
 '𐔦': 'L',
 '𐔧': 'X',
 '𐞁': ':',
 '𐞃': 'ae',
 '𐞇': 'dz',
 '𐞙': 'ls',
 '𐞚': 'lz',
 '𐞥': 'q',
 '𐞬': 'ts',
 '𐞲': 'y',
 '𐞶': 'l',
 '𐞷': 'll',
 '𑓐': 'O',
 '𑜀': 'rn',
 '𑜆': 'v',
 '𑜊': 'w',
 '𑜎': 'w',
 '𑜏': 'w',
 '𑢠': 'V',
 '𑢢': 'F',
 '𑢣': 'L',
 '𑢤': 'Y',
 '𑢦': 'E',
 '𑢩': 'Z',
 '𑢬': '9',
 '𑢮': 'E',
 '𑢯': '4',
 '𑢲': 'L',
 '𑢵': 'O',
 '𑢸': 'U',
 '𑢻': '5',
 '𑢼': 'T',
 '𑣀': 'v',
 '𑣁': 's',
 '𑣂': 'F',
 '𑣃': 'i',
 '𑣄': 'z',
 '𑣆': '7',
 '𑣈': 'o',
 '𑣊': '3',
 '𑣌': '9',
 '𑣕': '6',
 '𑣖': '9',
 '𑣗': 'o',
 '𑣘': 'u',
 '𑣜': 'y',
 '𑣠': 'O',
 '𑣣': 'rn',
 '𑣥': 'Z',
 '𑣦': 'W',
 '𑣩': 'C',
 '𑣬': 'X',
 '𑣯': 'W',
 '𑣲': 'C',
 '𖼈': 'V',
 '𖼊': 'T',
 '𖼖': 'L',
 '𖼨': 'l',
 '𖼵': 'R',
 '𖼺': 'S',
 '𖼻': '3',
 '𖽀': 'A',
 '𖽂': 'U',
 '𖽃': 'Y',
 '\U0001ccd6': 'A',
 '\U0001ccd7': 'B',
 '\U0001ccd8': 'C',
 '\U0001ccd9': 'D',
 '\U0001ccda': 'E',
 '\U0001ccdb': 'F',
 '\U0001ccdc': 'G',
 '\U0001ccdd': 'H',
 '\U0001ccde': 'l',
 '\U0001ccdf': 'J',
 '\U0001cce0': 'K',
 '\U0001cce1': 'L',
 '\U0001cce2': 'M',
 '\U0001cce3': 'N',
 '\U0001cce4': 'O',
 '\U0001cce5': 'P',
 '\U0001cce6': 'Q',
 '\U0001cce7': 'R',
 '\U0001cce8': 'S',
 '\U0001cce9': 'T',
 '\U0001ccea': 'U',
 '\U0001cceb': 'V',
 '\U0001ccec': 'W',
 '\U0001cced': 'X',
 '\U0001ccee': 'Y',
 '\U0001ccef': 'Z',
 '\U0001ccf0': 'O',
 '\U0001ccf1': 'l',
 '\U0001ccf2': '2',
 '\U0001ccf3': '3',
 '\U0001ccf4': '4',
 '\U0001ccf5': '5',
 '\U0001ccf6': '6',
 '\U0001ccf7': '7',
 '\U0001ccf8': '8',
 '\U0001ccf9': '9',
 '𝈆': '3',
 '𝈍': 'V',
 '𝈒': '7',
 '𝈓': 'F',
 '𝈖': 'R',
 '𝈪': 'L',
 '𝐀': 'A',
 '𝐁': 'B',
 '𝐂': 'C',
 '𝐃': 'D',
 '𝐄': 'E',
 '𝐅': 'F',
 '𝐆': 'G',
 '𝐇': 'H',
 '𝐈': 'l',
 '𝐉': 'J',
 '𝐊': 'K',
 '𝐋': 'L',
 '𝐌': 'M',
 '𝐍': 'N',
 '𝐎': 'O',
 '𝐏': 'P',
 '𝐐': 'Q',
 '𝐑': 'R',
 '𝐒': 'S',
 '𝐓': 'T',
 '𝐔': 'U',
 '𝐕': 'V',
 '𝐖': 'W',
 '𝐗': 'X',
 '𝐘': 'Y',
 '𝐙': 'Z',
 '𝐚': 'a',
 '𝐛': 'b',
 '𝐜': 'c',
 '𝐝': 'd',
 '𝐞': 'e',
 '𝐟': 'f',
 '𝐠': 'g',
 '𝐡': 'h',
 '𝐢': 'i',
 '𝐣': 'j',
 '𝐤': 'k',
 '𝐥': 'l',
 '𝐦': 'rn',
 '𝐧': 'n',
 '𝐨': 'o',
 '𝐩': 'p',
 '𝐪': 'q',
 '𝐫': 'r',
 '𝐬': 's',
 '𝐭': 't',
 '𝐮': 'u',
 '𝐯': 'v',
 '𝐰': 'w',
 '𝐱': 'x',
 '𝐲': 'y',
 '𝐳': 'z',
 '𝐴': 'A',
 '𝐵': 'B',
 '𝐶': 'C',
 '𝐷': 'D',
 '𝐸': 'E',
 '𝐹': 'F',
 '𝐺': 'G',
 '𝐻': 'H',
 '𝐼': 'l',
 '𝐽': 'J',
 '𝐾': 'K',
 '𝐿': 'L',
 '𝑀': 'M',
 '𝑁': 'N',
 '𝑂': 'O',
 '𝑃': 'P',
 '𝑄': 'Q',
 '𝑅': 'R',
 '𝑆': 'S',
 '𝑇': 'T',
 '𝑈': 'U',
 '𝑉': 'V',
 '𝑊': 'W',
 '𝑋': 'X',
 '𝑌': 'Y',
 '𝑍': 'Z',
 '𝑎': 'a',
 '𝑏': 'b',
 '𝑐': 'c',
 '𝑑': 'd',
 '𝑒': 'e',
 '𝑓': 'f',
 '𝑔': 'g',
 '𝑖': 'i',
 '𝑗': 'j',
 '𝑘': 'k',
 '𝑙': 'l',
 '𝑚': 'rn',
 '𝑛': 'n',
 '𝑜': 'o',
 '𝑝': 'p',
 '𝑞': 'q',
 '𝑟': 'r',
 '𝑠': 's',
 '𝑡': 't',
 '𝑢': 'u',
 '𝑣': 'v',
 '𝑤': 'w',
 '𝑥': 'x',
 '𝑦': 'y',
 '𝑧': 'z',
 '𝑨': 'A',
 '𝑩': 'B',
 '𝑪': 'C',
 '𝑫': 'D',
 '𝑬': 'E',
 '𝑭': 'F',
 '𝑮': 'G',
 '𝑯': 'H',
 '𝑰': 'l',
 '𝑱': 'J',
 '𝑲': 'K',
 '𝑳': 'L',
 '𝑴': 'M',
 '𝑵': 'N',
 '𝑶': 'O',
 '𝑷': 'P',
 '𝑸': 'Q',
 '𝑹': 'R',
 '𝑺': 'S',
 '𝑻': 'T',
 '𝑼': 'U',
 '𝑽': 'V',
 '𝑾': 'W',
 '𝑿': 'X',
 '𝒀': 'Y',
 '𝒁': 'Z',
 '𝒂': 'a',
 '𝒃': 'b',
 '𝒄': 'c',
 '𝒅': 'd',
 '𝒆': 'e',
 '𝒇': 'f',
 '𝒈': 'g',
 '𝒉': 'h',
 '𝒊': 'i',
 '𝒋': 'j',
 '𝒌': 'k',
 '𝒍': 'l',
 '𝒎': 'rn',
 '𝒏': 'n',
 '𝒐': 'o',
 '𝒑': 'p',
 '𝒒': 'q',
 '𝒓': 'r',
 '𝒔': 's',
 '𝒕': 't',
 '𝒖': 'u',
 '𝒗': 'v',
 '𝒘': 'w',
 '𝒙': 'x',
 '𝒚': 'y',
 '𝒛': 'z',
 '𝒜': 'A',
 '𝒞': 'C',
 '𝒟': 'D',
 '𝒢': 'G',
 '𝒥': 'J',
 '𝒦': 'K',
 '𝒩': 'N',
 '𝒪': 'O',
 '𝒫': 'P',
 '𝒬': 'Q',
 '𝒮': 'S',
 '𝒯': 'T',
 '𝒰': 'U',
 '𝒱': 'V',
 '𝒲': 'W',
 '𝒳': 'X',
 '𝒴': 'Y',
 '𝒵': 'Z',
 '𝒶': 'a',
 '𝒷': 'b',
 '𝒸': 'c',
 '𝒹': 'd',
 '𝒻': 'f',
 '𝒽': 'h',
 '𝒾': 'i',
 '𝒿': 'j',
 '𝓀': 'k',
 '𝓁': 'l',
 '𝓂': 'rn',
 '𝓃': 'n',
 '𝓅': 'p',
 '𝓆': 'q',
 '𝓇': 'r',
 '𝓈': 's',
 '𝓉': 't',
 '𝓊': 'u',
 '𝓋': 'v',
 '𝓌': 'w',
 '𝓍': 'x',
 '𝓎': 'y',
 '𝓏': 'z',
 '𝓐': 'A',
 '𝓑': 'B',
 '𝓒': 'C',
 '𝓓': 'D',
 '𝓔': 'E',
 '𝓕': 'F',
 '𝓖': 'G',
 '𝓗': 'H',
 '𝓘': 'l',
 '𝓙': 'J',
 '𝓚': 'K',
 '𝓛': 'L',
 '𝓜': 'M',
 '𝓝': 'N',
 '𝓞': 'O',
 '𝓟': 'P',
 '𝓠': 'Q',
 '𝓡': 'R',
 '𝓢': 'S',
 '𝓣': 'T',
 '𝓤': 'U',
 '𝓥': 'V',
 '𝓦': 'W',
 '𝓧': 'X',
 '𝓨': 'Y',
 '𝓩': 'Z',
 '𝓪': 'a',
 '𝓫': 'b',
 '𝓬': 'c',
 '𝓭': 'd',
 '𝓮': 'e',
 '𝓯': 'f',
 '𝓰': 'g',
 '𝓱': 'h',
 '𝓲': 'i',
 '𝓳': 'j',
 '𝓴': 'k',
 '𝓵': 'l',
 '𝓶': 'rn',
 '𝓷': 'n',
 '𝓸': 'o',
 '𝓹': 'p',
 '𝓺': 'q',
 '𝓻': 'r',
 '𝓼': 's',
 '𝓽': 't',
 '𝓾': 'u',
 '𝓿': 'v',
 '𝔀': 'w',
 '𝔁': 'x',
 '𝔂': 'y',
 '𝔃': 'z',
 '𝔄': 'A',
 '𝔅': 'B',
 '𝔇': 'D',
 '𝔈': 'E',
 '𝔉': 'F',
 '𝔊': 'G',
 '𝔍': 'J',
 '𝔎': 'K',
 '𝔏': 'L',
 '𝔐': 'M',
 '𝔑': 'N',
 '𝔒': 'O',
 '𝔓': 'P',
 '𝔔': 'Q',
 '𝔖': 'S',
 '𝔗': 'T',
 '𝔘': 'U',
 '𝔙': 'V',
 '𝔚': 'W',
 '𝔛': 'X',
 '𝔜': 'Y',
 '𝔞': 'a',
 '𝔟': 'b',
 '𝔠': 'c',
 '𝔡': 'd',
 '𝔢': 'e',
 '𝔣': 'f',
 '𝔤': 'g',
 '𝔥': 'h',
 '𝔦': 'i',
 '𝔧': 'j',
 '𝔨': 'k',
 '𝔩': 'l',
 '𝔪': 'rn',
 '𝔫': 'n',
 '𝔬': 'o',
 '𝔭': 'p',
 '𝔮': 'q',
 '𝔯': 'r',
 '𝔰': 's',
 '𝔱': 't',
 '𝔲': 'u',
 '𝔳': 'v',
 '𝔴': 'w',
 '𝔵': 'x',
 '𝔶': 'y',
 '𝔷': 'z',
 '𝔸': 'A',
 '𝔹': 'B',
 '𝔻': 'D',
 '𝔼': 'E',
 '𝔽': 'F',
 '𝔾': 'G',
 '𝕀': 'l',
 '𝕁': 'J',
 '𝕂': 'K',
 '𝕃': 'L',
 '𝕄': 'M',
 '𝕆': 'O',
 '𝕊': 'S',
 '𝕋': 'T',
 '𝕌': 'U',
 '𝕍': 'V',
 '𝕎': 'W',
 '𝕏': 'X',
 '𝕐': 'Y',
 '𝕒': 'a',
 '𝕓': 'b',
 '𝕔': 'c',
 '𝕕': 'd',
 '𝕖': 'e',
 '𝕗': 'f',
 '𝕘': 'g',
 '𝕙': 'h',
 '𝕚': 'i',
 '𝕛': 'j',
 '𝕜': 'k',
 '𝕝': 'l',
 '𝕞': 'rn',
 '𝕟': 'n',
 '𝕠': 'o',
 '𝕡': 'p',
 '𝕢': 'q',
 '𝕣': 'r',
 '𝕤': 's',
 '𝕥': 't',
 '𝕦': 'u',
 '𝕧': 'v',
 '𝕨': 'w',
 '𝕩': 'x',
 '𝕪': 'y',
 '𝕫': 'z',
 '𝕬': 'A',
 '𝕭': 'B',
 '𝕮': 'C',
 '𝕯': 'D',
 '𝕰': 'E',
 '𝕱': 'F',
 '𝕲': 'G',
 '𝕳': 'H',
 '𝕴': 'l',
 '𝕵': 'J',
 '𝕶': 'K',
 '𝕷': 'L',
 '𝕸': 'M',
 '𝕹': 'N',
 '𝕺': 'O',
 '𝕻': 'P',
 '𝕼': 'Q',
 '𝕽': 'R',
 '𝕾': 'S',
 '𝕿': 'T',
 '𝖀': 'U',
 '𝖁': 'V',
 '𝖂': 'W',
 '𝖃': 'X',
 '𝖄': 'Y',
 '𝖅': 'Z',
 '𝖆': 'a',
 '𝖇': 'b',
 '𝖈': 'c',
 '𝖉': 'd',
 '𝖊': 'e',
 '𝖋': 'f',
 '𝖌': 'g',
 '𝖍': 'h',
 '𝖎': 'i',
 '𝖏': 'j',
 '𝖐': 'k',
 '𝖑': 'l',
 '𝖒': 'rn',
 '𝖓': 'n',
 '𝖔': 'o',
 '𝖕': 'p',
 '𝖖': 'q',
 '𝖗': 'r',
 '𝖘': 's',
 '𝖙': 't',
 '𝖚': 'u',
 '𝖛': 'v',
 '𝖜': 'w',
 '𝖝': 'x',
 '𝖞': 'y',
 '𝖟': 'z',
 '𝖠': 'A',
 '𝖡': 'B',
 '𝖢': 'C',
 '𝖣': 'D',
 '𝖤': 'E',
 '𝖥': 'F',
 '𝖦': 'G',
 '𝖧': 'H',
 '𝖨': 'l',
 '𝖩': 'J',
 '𝖪': 'K',
 '𝖫': 'L',
 '𝖬': 'M',
 '𝖭': 'N',
 '𝖮': 'O',
 '𝖯': 'P',
 '𝖰': 'Q',
 '𝖱': 'R',
 '𝖲': 'S',
 '𝖳': 'T',
 '𝖴': 'U',
 '𝖵': 'V',
 '𝖶': 'W',
 '𝖷': 'X',
 '𝖸': 'Y',
 '𝖹': 'Z',
 '𝖺': 'a',
 '𝖻': 'b',
 '𝖼': 'c',
 '𝖽': 'd',
 '𝖾': 'e',
 '𝖿': 'f',
 '𝗀': 'g',
 '𝗁': 'h',
 '𝗂': 'i',
 '𝗃': 'j',
 '𝗄': 'k',
 '𝗅': 'l',
 '𝗆': 'rn',
 '𝗇': 'n',
 '𝗈': 'o',
 '𝗉': 'p',
 '𝗊': 'q',
 '𝗋': 'r',
 '𝗌': 's',
 '𝗍': 't',
 '𝗎': 'u',
 '𝗏': 'v',
 '𝗐': 'w',
 '𝗑': 'x',
 '𝗒': 'y',
 '𝗓': 'z',
 '𝗔': 'A',
 '𝗕': 'B',
 '𝗖': 'C',
 '𝗗': 'D',
 '𝗘': 'E',
 '𝗙': 'F',
 '𝗚': 'G',
 '𝗛': 'H',
 '𝗜': 'l',
 '𝗝': 'J',
 '𝗞': 'K',
 '𝗟': 'L',
 '𝗠': 'M',
 '𝗡': 'N',
 '𝗢': 'O',
 '𝗣': 'P',
 '𝗤': 'Q',
 '𝗥': 'R',
 '𝗦': 'S',
 '𝗧': 'T',
 '𝗨': 'U',
 '𝗩': 'V',
 '𝗪': 'W',
 '𝗫': 'X',
 '𝗬': 'Y',
 '𝗭': 'Z',
 '𝗮': 'a',
 '𝗯': 'b',
 '𝗰': 'c',
 '𝗱': 'd',
 '𝗲': 'e',
 '𝗳': 'f',
 '𝗴': 'g',
 '𝗵': 'h',
 '𝗶': 'i',
 '𝗷': 'j',
 '𝗸': 'k',
 '𝗹': 'l',
 '𝗺': 'rn',
 '𝗻': 'n',
 '𝗼': 'o',
 '𝗽': 'p',
 '𝗾': 'q',
 '𝗿': 'r',
 '𝘀': 's',
 '𝘁': 't',
 '𝘂': 'u',
 '𝘃': 'v',
 '𝘄': 'w',
 '𝘅': 'x',
 '𝘆': 'y',
 '𝘇': 'z',
 '𝘈': 'A',
 '𝘉': 'B',
 '𝘊': 'C',
 '𝘋': 'D',
 '𝘌': 'E',
 '𝘍': 'F',
 '𝘎': 'G',
 '𝘏': 'H',
 '𝘐': 'l',
 '𝘑': 'J',
 '𝘒': 'K',
 '𝘓': 'L',
 '𝘔': 'M',
 '𝘕': 'N',
 '𝘖': 'O',
 '𝘗': 'P',
 '𝘘': 'Q',
 '𝘙': 'R',
 '𝘚': 'S',
 '𝘛': 'T',
 '𝘜': 'U',
 '𝘝': 'V',
 '𝘞': 'W',
 '𝘟': 'X',
 '𝘠': 'Y',
 '𝘡': 'Z',
 '𝘢': 'a',
 '𝘣': 'b',
 '𝘤': 'c',
 '𝘥': 'd',
 '𝘦': 'e',
 '𝘧': 'f',
 '𝘨': 'g',
 '𝘩': 'h',
 '𝘪': 'i',
 '𝘫': 'j',
 '𝘬': 'k',
 '𝘭': 'l',
 '𝘮': 'rn',
 '𝘯': 'n',
 '𝘰': 'o',
 '𝘱': 'p',
 '𝘲': 'q',
 '𝘳': 'r',
 '𝘴': 's',
 '𝘵': 't',
 '𝘶': 'u',
 '𝘷': 'v',
 '𝘸': 'w',
 '𝘹': 'x',
 '𝘺': 'y',
 '𝘻': 'z',
 '𝘼': 'A',
 '𝘽': 'B',
 '𝘾': 'C',
 '𝘿': 'D',
 '𝙀': 'E',
 '𝙁': 'F',
 '𝙂': 'G',
 '𝙃': 'H',
 '𝙄': 'l',
 '𝙅': 'J',
 '𝙆': 'K',
 '𝙇': 'L',
 '𝙈': 'M',
 '𝙉': 'N',
 '𝙊': 'O',
 '𝙋': 'P',
 '𝙌': 'Q',
 '𝙍': 'R',
 '𝙎': 'S',
 '𝙏': 'T',
 '𝙐': 'U',
 '𝙑': 'V',
 '𝙒': 'W',
 '𝙓': 'X',
 '𝙔': 'Y',
 '𝙕': 'Z',
 '𝙖': 'a',
 '𝙗': 'b',
 '𝙘': 'c',
 '𝙙': 'd',
 '𝙚': 'e',
 '𝙛': 'f',
 '𝙜': 'g',
 '𝙝': 'h',
 '𝙞': 'i',
 '𝙟': 'j',
 '𝙠': 'k',
 '𝙡': 'l',
 '𝙢': 'rn',
 '𝙣': 'n',
 '𝙤': 'o',
 '𝙥': 'p',
 '𝙦': 'q',
 '𝙧': 'r',
 '𝙨': 's',
 '𝙩': 't',
 '𝙪': 'u',
 '𝙫': 'v',
 '𝙬': 'w',
 '𝙭': 'x',
 '𝙮': 'y',
 '𝙯': 'z',
 '𝙰': 'A',
 '𝙱': 'B',
 '𝙲': 'C',
 '𝙳': 'D',
 '𝙴': 'E',
 '𝙵': 'F',
 '𝙶': 'G',
 '𝙷': 'H',
 '𝙸': 'l',
 '𝙹': 'J',
 '𝙺': 'K',
 '𝙻': 'L',
 '𝙼': 'M',
 '𝙽': 'N',
 '𝙾': 'O',
 '𝙿': 'P',
 '𝚀': 'Q',
 '𝚁': 'R',
 '𝚂': 'S',
 '𝚃': 'T',
 '𝚄': 'U',
 '𝚅': 'V',
 '𝚆': 'W',
 '𝚇': 'X',
 '𝚈': 'Y',
 '𝚉': 'Z',
 '𝚊': 'a',
 '𝚋': 'b',
 '𝚌': 'c',
 '𝚍': 'd',
 '𝚎': 'e',
 '𝚏': 'f',
 '𝚐': 'g',
 '𝚑': 'h',
 '𝚒': 'i',
 '𝚓': 'j',
 '𝚔': 'k',
 '𝚕': 'l',
 '𝚖': 'rn',
 '𝚗': 'n',
 '𝚘': 'o',
 '𝚙': 'p',
 '𝚚': 'q',
 '𝚛': 'r',
 '𝚜': 's',
 '𝚝': 't',
 '𝚞': 'u',
 '𝚟': 'v',
 '𝚠': 'w',
 '𝚡': 'x',
 '𝚢': 'y',
 '𝚣': 'z',
 '𝚤': 'i',
 '𝚨': 'A',
 '𝚩': 'B',
 '𝚬': 'E',
 '𝚭': 'Z',
 '𝚮': 'H',
 '𝚰': 'l',
 '𝚱': 'K',
 '𝚳': 'M',
 '𝚴': 'N',
 '𝚶': 'O',
 '𝚸': 'P',
 '𝚻': 'T',
 '𝚼': 'Y',
 '𝚾': 'X',
 '𝛂': 'a',
 '𝛄': 'y',
 '𝛊': 'i',
 '𝛎': 'v',
 '𝛐': 'o',
 '𝛒': 'p',
 '𝛔': 'o',
 '𝛖': 'u',
 '𝛠': 'p',
 '𝛢': 'A',
 '𝛣': 'B',
 '𝛦': 'E',
 '𝛧': 'Z',
 '𝛨': 'H',
 '𝛪': 'l',
 '𝛫': 'K',
 '𝛭': 'M',
 '𝛮': 'N',
 '𝛰': 'O',
 '𝛲': 'P',
 '𝛵': 'T',
 '𝛶': 'Y',
 '𝛸': 'X',
 '𝛼': 'a',
 '𝛾': 'y',
 '𝜄': 'i',
 '𝜈': 'v',
 '𝜊': 'o',
 '𝜌': 'p',
 '𝜎': 'o',
 '𝜐': 'u',
 '𝜚': 'p',
 '𝜜': 'A',
 '𝜝': 'B',
 '𝜠': 'E',
 '𝜡': 'Z',
 '𝜢': 'H',
 '𝜤': 'l',
 '𝜥': 'K',
 '𝜧': 'M',
 '𝜨': 'N',
 '𝜪': 'O',
 '𝜬': 'P',
 '𝜯': 'T',
 '𝜰': 'Y',
 '𝜲': 'X',
 '𝜶': 'a',
 '𝜸': 'y',
 '𝜾': 'i',
 '𝝂': 'v',
 '𝝄': 'o',
 '𝝆': 'p',
 '𝝈': 'o',
 '𝝊': 'u',
 '𝝔': 'p',
 '𝝖': 'A',
 '𝝗': 'B',
 '𝝚': 'E',
 '𝝛': 'Z',
 '𝝜': 'H',
 '𝝞': 'l',
 '𝝟': 'K',
 '𝝡': 'M',
 '𝝢': 'N',
 '𝝤': 'O',
 '𝝦': 'P',
 '𝝩': 'T',
 '𝝪': 'Y',
 '𝝬': 'X',
 '𝝰': 'a',
 '𝝲': 'y',
 '𝝸': 'i',
 '𝝼': 'v',
 '𝝾': 'o',
 '𝞀': 'p',
 '𝞂': 'o',
 '𝞄': 'u',
 '𝞎': 'p',
 '𝞐': 'A',
 '𝞑': 'B',
 '𝞔': 'E',
 '𝞕': 'Z',
 '𝞖': 'H',
 '𝞘': 'l',
 '𝞙': 'K',
 '𝞛': 'M',
 '𝞜': 'N',
 '𝞞': 'O',
 '𝞠': 'P',
 '𝞣': 'T',
 '𝞤': 'Y',
 '𝞦': 'X',
 '𝞪': 'a',
 '𝞬': 'y',
 '𝞲': 'i',
 '𝞶': 'v',
 '𝞸': 'o',
 '𝞺': 'p',
 '𝞼': 'o',
 '𝞾': 'u',
 '𝟈': 'p',
 '𝟊': 'F',
 '𝟎': 'O',
 '𝟏': 'l',
 '𝟐': '2',
 '𝟑': '3',
 '𝟒': '4',
 '𝟓': '5',
 '𝟔': '6',
 '𝟕': '7',
 '𝟖': '8',
 '𝟗': '9',
 '𝟘': 'O',
 '𝟙': 'l',
 '𝟚': '2',
 '𝟛': '3',
 '𝟜': '4',
 '𝟝': '5',
 '𝟞': '6',
 '𝟟': '7',
 '𝟠': '8',
 '𝟡': '9',
 '𝟢': 'O',
 '𝟣': 'l',
 '𝟤': '2',
 '𝟥': '3',
 '𝟦': '4',
 '𝟧': '5',
 '𝟨': '6',
 '𝟩': '7',
 '𝟪': '8',
 '𝟫': '9',
 '𝟬': 'O',
 '𝟭': 'l',
 '𝟮': '2',
 '𝟯': '3',
 '𝟰': '4',
 '𝟱': '5',
 '𝟲': '6',
 '𝟳': '7',
 '𝟴': '8',
 '𝟵': '9',
 '𝟶': 'O',
 '𝟷': 'l',
 '𝟸': '2',
 '𝟹': '3',
 '𝟺': '4',
 '𝟻': '5',
 '𝟼': '6',
 '𝟽': '7',
 '𝟾': '8',
 '𝟿': '9',
 '𞀰': 'a',
 '𞀱': '6',
 '𞀳': 'r',
 '𞀵': 'e',
 '𞀼': 'o',
 '𞀾': 'p',
 '𞀿': 'c',
 '𞁁': 'y',
 '𞁃': 'x',
 '𞁌': 'i',
 '𞁍': 'j',
 '𞁏': 'y',
 '𞁐': 'i',
 '𞁑': 'a',
 '𞁒': '6',
 '𞁔': 'r',
 '𞁖': 'e',
 '𞁜': 'o',
 '𞁞': 'c',
 '𞁟': 'y',
 '𞁡': 'x',
 '𞁨': 'i',
 '𞁩': 's',
 '𞣇': 'l',
 '𞣋': '8',
 '𞸀': 'l',
 '𞸤': 'o',
 '𞹤': 'o',
 '𞺀': 'l',
 '𞺄': 'o',
 '🄫': 'C',
 '🄬': 'R',
 '🄭': 'CD',
 '🄮': 'WZ',
 '🄰': 'A',
 '🄱': 'B',
 '🄲': 'C',
 '🄳': 'D',
 '🄴': 'E',
 '🄵': 'F',
 '🄶': 'G',
 '🄷': 'H',
 '🄸': 'l',
 '🄹': 'J',
 '🄺': 'K',
 '🄻': 'L',
 '🄼': 'M',
 '🄽': 'N',
 '🄾': 'O',
 '🄿': 'P',
 '🅀': 'Q',
 '🅁': 'R',
 '🅂': 'S',
 '🅃': 'T',
 '🅄': 'U',
 '🅅': 'V',
 '🅆': 'W',
 '🅇': 'X',
 '🅈': 'Y',
 '🅉': 'Z',
 '🅊': 'HV',
 '🅋': 'MV',
 '🅌': 'SD',
 '🅍': 'SS',
 '🅎': 'PPV',
 '🅏': 'WC',
 '🅪': 'MC',
 '🅫': 'MD',
 '🅬': 'MR',
 '🆐': 'DJ',
 '🜀': 'QE',
 '🜇': 'AR',
 '🝌': 'C',
 '🝜': 'sss',
 '🝨': 'T',
 '🝫': 'MB',
 '🝬': 'VB',
 '🯰': 'O',
 '🯱': 'l',
 '🯲': '2',
 '🯳': '3',
 '🯴': '4',
 '🯵': '5',
 '🯶': '6',
 '🯷': '7',
 '🯸': '8',
 '🯹': '9'}
# commonmark.js 0.31.2 references whose decoded values can contribute to a key.
SHAPE_ENTITY_MAP = {'AElig': 'Æ',
 'Acy': 'А',
 'Afr': '𝔄',
 'Alpha': 'Α',
 'Aopf': '𝔸',
 'ApplyFunction': '\u2061',
 'Ascr': '𝒜',
 'Bernoullis': 'ℬ',
 'Beta': 'Β',
 'Bfr': '𝔅',
 'Bopf': '𝔹',
 'Bscr': 'ℬ',
 'CapitalDifferentialD': 'ⅅ',
 'Cayleys': 'ℭ',
 'Cfr': 'ℭ',
 'Chi': 'Χ',
 'Copf': 'ℂ',
 'Cross': '⨯',
 'Cscr': '𝒞',
 'DD': 'ⅅ',
 'DScy': 'Ѕ',
 'Dfr': '𝔇',
 'DifferentialD': 'ⅆ',
 'Dopf': '𝔻',
 'DoubleVerticalBar': '∥',
 'DownTee': '⊤',
 'Dscr': '𝒟',
 'Efr': '𝔈',
 'Eopf': '𝔼',
 'Epsilon': 'Ε',
 'Escr': 'ℰ',
 'Eta': 'Η',
 'ExponentialE': 'ⅇ',
 'Ffr': '𝔉',
 'Fopf': '𝔽',
 'Fouriertrf': 'ℱ',
 'Fscr': 'ℱ',
 'Gammad': 'Ϝ',
 'Gfr': '𝔊',
 'Gopf': '𝔾',
 'Gscr': '𝒢',
 'Hfr': 'ℌ',
 'HilbertSpace': 'ℋ',
 'Hopf': 'ℍ',
 'Hscr': 'ℋ',
 'IEcy': 'Е',
 'IJlig': 'Ĳ',
 'Ifr': 'ℑ',
 'Im': 'ℑ',
 'ImaginaryI': 'ⅈ',
 'InvisibleComma': '\u2063',
 'InvisibleTimes': '\u2062',
 'Iopf': '𝕀',
 'Iota': 'Ι',
 'Iscr': 'ℐ',
 'Iukcy': 'І',
 'Jfr': '𝔍',
 'Jopf': '𝕁',
 'Jscr': '𝒥',
 'Jsercy': 'Ј',
 'KHcy': 'Х',
 'Kappa': 'Κ',
 'Kcy': 'К',
 'Kfr': '𝔎',
 'Kopf': '𝕂',
 'Kscr': '𝒦',
 'Laplacetrf': 'ℒ',
 'Lfr': '𝔏',
 'Lopf': '𝕃',
 'Lscr': 'ℒ',
 'Mcy': 'М',
 'MediumSpace': '\u205f',
 'Mellintrf': 'ℳ',
 'Mfr': '𝔐',
 'Mopf': '𝕄',
 'Mscr': 'ℳ',
 'Mu': 'Μ',
 'Ncy': 'Н',
 'NegativeMediumSpace': '\u200b',
 'NegativeThickSpace': '\u200b',
 'NegativeThinSpace': '\u200b',
 'NegativeVeryThinSpace': '\u200b',
 'NewLine': '\n',
 'Nfr': '𝔑',
 'NoBreak': '\u2060',
 'NonBreakingSpace': '\xa0',
 'Nopf': 'ℕ',
 'Nscr': '𝒩',
 'Nu': 'Ν',
 'OElig': 'Œ',
 'Ocy': 'О',
 'Ofr': '𝔒',
 'Omicron': 'Ο',
 'Oopf': '𝕆',
 'Oscr': '𝒪',
 'Pfr': '𝔓',
 'Poincareplane': 'ℌ',
 'Popf': 'ℙ',
 'Pscr': '𝒫',
 'Qfr': '𝔔',
 'Qopf': 'ℚ',
 'Qscr': '𝒬',
 'Rcy': 'Р',
 'Re': 'ℜ',
 'Rfr': 'ℜ',
 'Rho': 'Ρ',
 'Ropf': 'ℝ',
 'Rscr': 'ℛ',
 'SOFTcy': 'Ь',
 'Scy': 'С',
 'Sfr': '𝔖',
 'Sopf': '𝕊',
 'Sscr': '𝒮',
 'TRADE': '™',
 'Tab': '\t',
 'Tau': 'Τ',
 'Tcy': 'Т',
 'Tfr': '𝔗',
 'ThickSpace': '\u205f\u200a',
 'ThinSpace': '\u2009',
 'Topf': '𝕋',
 'Tscr': '𝒯',
 'Ucy': 'У',
 'Ufr': '𝔘',
 'UnderBar': '_',
 'Union': '⋃',
 'Uopf': '𝕌',
 'Upsi': 'ϒ',
 'Upsilon': 'Υ',
 'Uscr': '𝒰',
 'Vcy': 'В',
 'Vee': '⋁',
 'Verbar': '‖',
 'Vert': '‖',
 'VerticalBar': '∣',
 'VerticalLine': '|',
 'VeryThinSpace': '\u200a',
 'Vfr': '𝔙',
 'Vopf': '𝕍',
 'Vscr': '𝒱',
 'Wfr': '𝔚',
 'Wopf': '𝕎',
 'Wscr': '𝒲',
 'Xfr': '𝔛',
 'Xopf': '𝕏',
 'Xscr': '𝒳',
 'YUcy': 'Ю',
 'Ycy': 'Ы',
 'Yfr': '𝔜',
 'Yopf': '𝕐',
 'Yscr': '𝒴',
 'Zcy': 'З',
 'ZeroWidthSpace': '\u200b',
 'Zeta': 'Ζ',
 'Zfr': 'ℨ',
 'Zopf': 'ℤ',
 'Zscr': '𝒵',
 'acy': 'а',
 'aelig': 'æ',
 'af': '\u2061',
 'afr': '𝔞',
 'alpha': 'α',
 'aopf': '𝕒',
 'ascr': '𝒶',
 'ast': '*',
 'bcy': 'б',
 'bernou': 'ℬ',
 'bfr': '𝔟',
 'bigcup': '⋃',
 'bigvee': '⋁',
 'bopf': '𝕓',
 'bscr': '𝒷',
 'bsol': '\\',
 'cfr': '𝔠',
 'circledS': 'Ⓢ',
 'colon': ':',
 'complexes': 'ℂ',
 'copf': '𝕔',
 'cscr': '𝒸',
 'cup': '∪',
 'cups': '∪︀',
 'dd': 'ⅆ',
 'dfr': '𝔡',
 'dopf': '𝕕',
 'dscr': '𝒹',
 'dscy': 'ѕ',
 'ee': 'ⅇ',
 'efr': '𝔢',
 'ell': 'ℓ',
 'emsp': '\u2003',
 'emsp13': '\u2004',
 'emsp14': '\u2005',
 'ensp': '\u2002',
 'eopf': '𝕖',
 'escr': 'ℯ',
 'expectation': 'ℰ',
 'exponentiale': 'ⅇ',
 'ffilig': 'ﬃ',
 'fflig': 'ﬀ',
 'ffllig': 'ﬄ',
 'ffr': '𝔣',
 'filig': 'ﬁ',
 'fjlig': 'fj',
 'fllig': 'ﬂ',
 'fopf': '𝕗',
 'fscr': '𝒻',
 'gamma': 'γ',
 'gcy': 'г',
 'gfr': '𝔤',
 'gopf': '𝕘',
 'gscr': 'ℊ',
 'hairsp': '\u200a',
 'hamilt': 'ℋ',
 'hfr': '𝔥',
 'hopf': '𝕙',
 'hscr': '𝒽',
 'ic': '\u2063',
 'iecy': 'е',
 'ifr': '𝔦',
 'ii': 'ⅈ',
 'ijlig': 'ĳ',
 'image': 'ℑ',
 'imagline': 'ℐ',
 'imagpart': 'ℑ',
 'imath': 'ı',
 'infin': '∞',
 'inodot': 'ı',
 'integers': 'ℤ',
 'iopf': '𝕚',
 'iota': 'ι',
 'iscr': '𝒾',
 'it': '\u2062',
 'iukcy': 'і',
 'jfr': '𝔧',
 'jopf': '𝕛',
 'jscr': '𝒿',
 'jsercy': 'ј',
 'kfr': '𝔨',
 'khcy': 'х',
 'kopf': '𝕜',
 'kscr': '𝓀',
 'lagran': 'ℒ',
 'lfr': '𝔩',
 'lopf': '𝕝',
 'lowbar': '_',
 'lrm': '\u200e',
 'lscr': '𝓁',
 'mfr': '𝔪',
 'mid': '∣',
 'midast': '*',
 'mopf': '𝕞',
 'mscr': '𝓂',
 'naturals': 'ℕ',
 'nbsp': '\xa0',
 'nfr': '𝔫',
 'nopf': '𝕟',
 'nscr': '𝓃',
 'nu': 'ν',
 'numero': '№',
 'numsp': '\u2007',
 'oS': 'Ⓢ',
 'ocy': 'о',
 'oelig': 'œ',
 'ofr': '𝔬',
 'omicron': 'ο',
 'oopf': '𝕠',
 'or': '∨',
 'order': 'ℴ',
 'orderof': 'ℴ',
 'ordf': 'ª',
 'ordm': 'º',
 'oscr': 'ℴ',
 'par': '∥',
 'parallel': '∥',
 'pfr': '𝔭',
 'phmmat': 'ℳ',
 'planckh': 'ℎ',
 'popf': '𝕡',
 'primes': 'ℙ',
 'pscr': '𝓅',
 'puncsp': '\u2008',
 'qfr': '𝔮',
 'qopf': '𝕢',
 'qscr': '𝓆',
 'quaternions': 'ℍ',
 'ratio': '∶',
 'rationals': 'ℚ',
 'rcy': 'р',
 'real': 'ℜ',
 'realine': 'ℛ',
 'realpart': 'ℜ',
 'reals': 'ℝ',
 'rfr': '𝔯',
 'rho': 'ρ',
 'rhov': 'ϱ',
 'rlm': '\u200f',
 'ropf': '𝕣',
 'rscr': '𝓇',
 'scy': 'с',
 'sfr': '𝔰',
 'shortmid': '∣',
 'shortparallel': '∥',
 'shy': '\xad',
 'sigma': 'σ',
 'smid': '∣',
 'sopf': '𝕤',
 'spar': '∥',
 'sscr': '𝓈',
 'sup1': '¹',
 'sup2': '²',
 'sup3': '³',
 'tfr': '𝔱',
 'thinsp': '\u2009',
 'times': '×',
 'top': '⊤',
 'topf': '𝕥',
 'trade': '™',
 'tscr': '𝓉',
 'ucy': 'у',
 'ufr': '𝔲',
 'uopf': '𝕦',
 'upsi': 'υ',
 'upsih': 'ϒ',
 'upsilon': 'υ',
 'uscr': '𝓊',
 'varrho': 'ϱ',
 'vee': '∨',
 'verbar': '|',
 'vert': '|',
 'vfr': '𝔳',
 'vopf': '𝕧',
 'vscr': '𝓋',
 'wfr': '𝔴',
 'wopf': '𝕨',
 'wscr': '𝓌',
 'xcup': '⋃',
 'xfr': '𝔵',
 'xopf': '𝕩',
 'xscr': '𝓍',
 'xvee': '⋁',
 'yfr': '𝔶',
 'yopf': '𝕪',
 'yscr': '𝓎',
 'zeetrf': 'ℨ',
 'zfr': '𝔷',
 'zopf': '𝕫',
 'zscr': '𝓏',
 'zwj': '\u200d',
 'zwnj': '\u200c'}


def _shape_range_class(chars):
    points = sorted(map(ord, chars))
    ranges = []
    first = last = points[0]
    for cp in points[1:]:
        if cp == last+1:
            last = cp
        else:
            ranges.append((first, last))
            first = last = cp
    ranges.append((first, last))
    return '['+''.join(re.escape(chr(a)) + ('-'+re.escape(chr(b)) if b != a else '')
                       for a, b in ranges)+']'


_SHAPE_W = _shape_range_class(SHAPE_SET_ASIDE)
_SHAPE_PREFIX = re.compile(r'^' + _SHAPE_W + r'*(?:(?:[-*+]|[0-9]{1,9}[.)]|>)' +
                           _SHAPE_W + r'+|>)*' + _SHAPE_W + r'*')
_SHAPE_OPEN = re.compile(r'^ {0,3}(`{3,})[^`]*$|^ {0,3}(~{3,}).*$')
_SHAPE_CLOSE = re.compile(r'^ {0,3}(`{3,}|~{3,})[ \t]*$')
_SHAPE_STOP = re.compile(r'^ {0,3}(?:(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]|$)|>|<[A-Za-z/!?])')
_SHAPE_TAG = re.compile(r'<[A-Za-z/!?]')
_SHAPE_ENTITY = re.compile(r'&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});')
_SHAPE_DROP = SHAPE_REMOVAL | frozenset('\t_*~\\`[]!<>') | frozenset(
    chr(c) for a, b in _SHAPE_ZS_RANGES for c in range(a, b+1))


class DraftShape(NamedTuple):
    kind: str
    line: int
    column: int
    char: str | None
    key: str | None
    block: tuple[int, int] | None


def first_separator(text: str) -> DraftShape | None:
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        for column, char in enumerate(line, 1):
            if char in SEPARATORS:
                return DraftShape('separator', line_number, column, char, None, None)
    return None


def _shape_verdict_key(key):
    return any(key == suffix or key.endswith('_' + suffix)
               for suffix in ('VERDICT', 'MERGE_AUTHORIZATION', 'MERGE_APPROVED'))


def _shape_header_positive(key, value):
    # The header form requires both the linter's key-specific positive value
    # and authority's exact positive word. Keep independent of both readers.
    allowed = {
        'MERGE_AUTHORIZATION': {'approved', 'granted', 'yes'},
        'HUMAN_MERGE_AUTHORIZATION': {'approved', 'granted', 'yes'},
        'MERGE_APPROVED': {'yes', 'true', 'approved'},
        'VERDICT': {'merge-ready', 'approved', 'complete'},
    }
    return (value.isascii() and value == value.lstrip(' \t') and
            value.rstrip(' \t').lower() in allowed.get(key.upper(), set()))


def _shape_block(lines):
    start = None
    for index, line in enumerate(lines):
        if _DRAFT_SHAPE_HEADER.fullmatch(line):
            if start is None:
                start = index
        elif start is not None:
            return start, index
    return None if start is None else (start, len(lines))


def _shape_outside(lines, block):
    """Quote closed fences on each side of the header block in linear time.

    A reverse pass records whether each opener has any later matching closer.
    The forward pass then opens only those fences and uses their first closer.
    An unclosed opener is ordinary, exactly as in R217's repeated demotion,
    without rescanning the suffix for each such opener.
    """
    result = []
    carried = False
    for start, end in ((0, block[0]), (block[1], len(lines))):
        opens = {}
        closes = {}
        longest = {'`': 0, '~': 0}
        for index in range(end - 1, start - 1, -1):
            line = lines[index]
            opening = _SHAPE_OPEN.fullmatch(line)
            if opening:
                run = opening.group(1) or opening.group(2)
                if len(run) <= longest[run[0]]:
                    opens[index] = (run[0], len(run))
            closing = _SHAPE_CLOSE.fullmatch(line)
            if closing:
                run = closing.group(1)
                closes[index] = (run[0], len(run))
                longest[run[0]] = max(longest[run[0]], len(run))
        active = None
        stopped = carried
        for index in range(start, end):
            if active is not None:
                closing = closes.get(index)
                if closing and closing[0] == active[0] and closing[1] >= active[1]:
                    active = None
            else:
                line = lines[index]
                stop = bool(_SHAPE_STOP.match(line) or (index == 0 and line.startswith('\ufeff')))
                stopped |= stop
                if not stopped and index in opens:
                    active = opens[index]
                else:
                    result.append((index, line))
                    if end == block[0]:
                        carried |= bool(stop or _SHAPE_OPEN.fullmatch(line) or
                                        _SHAPE_TAG.match(line, _SHAPE_PREFIX.match(line).end()))
    return result


def _shape_scan_work(amount=1):
    """Counting hook for state visits, gap restores and backtick comparisons."""


def _shape_code_spans(line):
    runs = [(m.start(), m.end()) for m in re.finditer(r'`+', line)]
    following, by_length = {}, {}
    for i in range(len(runs)-1, -1, -1):
        a, b = runs[i]
        _shape_scan_work()
        following[i] = by_length.get(b-a)
        by_length[b-a] = i
    spans, good, i = [], '\\`' not in line, 0
    while i < len(runs):
        end = following[i]
        if end is None:
            good = False
            i += 1
        else:
            spans.append((runs[i][0], runs[end][1]))
            i = end+1
    cursor = 0
    for a, b in spans + [(len(line), len(line))]:
        outside = line[cursor:a]
        if '<' in outside or '](' in outside or '][' in outside:
            good = False
        cursor = b
    return spans, good


def _shape_scan(lines, block, outside, grant):
    """Shift-and simulation, with a constant number of aggregated gap states."""
    keys = LATE_CONTROL_KEYS + (('VERDICT', 'MERGE_AUTHORIZATION', 'MERGE_APPROVED') if grant else ())
    masks, starts, finishes, offset = {}, 0, [], 0
    for key in keys:
        pattern = ''.join(SHAPE_KEY_MAP.get(c, c) for c in key if c != '_')+':'
        starts |= 1 << offset
        for c in pattern:
            masks[c] = masks.get(c, 0) | (1 << offset)
            offset += 1
        finishes.append(1 << (offset-1))
        offset += 1  # Separate targets, so a shift can never join them.
    finished = sum(finishes)
    indices = [i for i, _ in outside if i < block[0]] + list(range(*block))
    indices.extend(i for i, _ in outside if i >= block[1])
    html = next((i for i in indices if _SHAPE_TAG.match(lines[i], _SHAPE_PREFIX.match(lines[i]).end())), None)
    regions, region, previous = [], [], None
    for i in indices:
        merged = html is not None and i >= html
        if not merged and (not lines[i].strip(' \t') or (previous is not None and i != previous+1)):
            if region:
                regions.append(region)
            region = []
        if merged or lines[i].strip(' \t'):
            region.append(i)
        previous = i
    if region:
        regions.append(region)
    for region in regions:
        per_line = {i: _shape_code_spans(lines[i]) for i in region}
        qualified = (html is None or html > region[-1]) and all(good for _, good in per_line.values())
        good = bad = 0
        gap_good, gap_bad = [0, 0, 0], [0, 0, 0]
        last_unit = None
        for i in region:
            # Newlines leave every header/code unit, including suspended states.
            _shape_scan_work(good.bit_count()+sum(bits.bit_count() for bits in gap_good))
            bad |= good
            good = 0
            for k in range(3):
                gap_bad[k] |= gap_good[k]
                gap_good[k] = 0
            last_unit = None
            line = lines[i]
            spans = per_line[i][0] if qualified else []
            span_index = 0
            colon = line.index(':') if block[0] <= i < block[1] else -1
            j = 0
            while j < len(line):
                while span_index < len(spans) and j >= spans[span_index][1]:
                    span_index += 1
                unit = ('header', i) if j <= colon else (
                    (i, span_index) if span_index < len(spans) and spans[span_index][0] <= j else None)
                if unit != last_unit:
                    _shape_scan_work(good.bit_count()+sum(bits.bit_count() for bits in gap_good))
                    bad |= good
                    good = 0
                    for k in range(3):
                        gap_bad[k] |= gap_good[k]
                        gap_good[k] = 0
                    last_unit = unit
                raw = line[j]
                chars, end = raw, j+1
                if raw == '&':
                    match = _SHAPE_ENTITY.match(line, j)
                    if match:
                        name = match.group()[1:-1]
                        if name.startswith('#'):
                            value = int(name[2:], 16) if name[1] in 'xX' else int(name[1:])
                            chars = '\ufffd' if value == 0 or value > 0x10ffff or 0xd800 <= value <= 0xdfff else chr(value)
                            end = match.end()
                        elif name in SHAPE_ENTITY_MAP:
                            chars, end = SHAPE_ENTITY_MAP[name], match.end()
                opening = (0 if raw == '<' and _SHAPE_TAG.match(line, j) else
                           1 if raw == '(' and j and line[j-1] == ']' else
                           2 if raw == '[' and j and line[j-1] in ']!' else None)
                closing = {'>': 0, ')': 1, ']': 2}.get(raw)
                if end > j+1:  # A decoded reference never opens or closes markup.
                    opening = closing = None
                if opening is not None:
                    _shape_scan_work(good.bit_count()+bad.bit_count())
                    gap_good[opening] |= good
                    gap_bad[opening] |= bad
                for char in chars:
                    if char in _SHAPE_DROP:
                        continue
                    for c in SHAPE_KEY_MAP.get(char, char):
                        if c in _SHAPE_DROP:
                            continue
                        _shape_scan_work(good.bit_count()+bad.bit_count()+len(keys))
                        mask = masks.get(c, 0)
                        good = ((good << 1) | (starts if unit is not None else 0)) & mask
                        bad = ((bad << 1) | (starts if unit is None else 0)) & mask
                        if bad & finished:
                            target = next(k for k, bit in enumerate(finishes) if bad & bit)
                            return DraftShape('late-control' if target < 3 else 'late-verdict',
                                              i+1, j+1, None, keys[target], (block[0]+1, block[1]))
                        good &= ~finished
                if closing is not None:
                    _shape_scan_work(gap_good[closing].bit_count()+gap_bad[closing].bit_count())
                    good |= gap_good[closing]
                    bad |= gap_bad[closing]
                j = end
    return None


def draft_shape(text: str) -> DraftShape | None:
    separator = first_separator(text)
    lines = text.splitlines()
    block = _shape_block(lines)
    if block is None:
        return separator
    headers = [_DRAFT_SHAPE_HEADER.fullmatch(line).groups() for line in lines[block[0]:block[1]]]
    phase = next((value for key, value in headers if key == 'PHASE'), '').rstrip(' \t')
    token = {'MERGE-GATE': 'DISPATCH MERGE', 'IMPL': 'DISPATCH IMPL'}.get(phase)
    outside = _shape_outside(lines, block)
    grant = token is not None and (
        any(line.strip(' \t') == token for _, line in outside) or
        (phase == 'MERGE-GATE' and any(_shape_header_positive(k, v) for k, v in headers)))
    candidates = [] if separator is None else [separator]
    for index, line in outside:
        before, colon, value = line.partition(':')
        if not colon:
            continue
        retained = [(c, j) for j, c in enumerate(before) if c not in SHAPE_REMOVAL]
        clean = ''.join(c for c, _ in retained)
        prefix = _SHAPE_PREFIX.match(clean).end()
        column = retained[prefix][1] if prefix < len(retained) else len(before)
        match = _DRAFT_SHAPE_HEADER.fullmatch(clean[prefix:] + colon + value)
        if match is None:
            continue
        key = match.group(1)
        if key in LATE_CONTROL_KEYS:
            kind = 'late-control'
        elif grant and _shape_verdict_key(key):
            kind = 'late-verdict'
        else:
            continue
        candidates.append(DraftShape(kind, index + 1, column + 1, None, key,
                                     (block[0] + 1, block[1])))
    scanned = _shape_scan(lines, block, outside, grant)
    if scanned is not None:
        candidates.append(scanned)
    return min(candidates, key=lambda item: (item.line, item.column)) if candidates else None


def draft_shape_errors(text: str, *, separators_only: bool = False) -> list[str]:
    # Standalone copy: no engine import or existing linter helper is involved.
    shape = first_separator(text) if separators_only else draft_shape(text)
    if shape is None:
        return []
    if shape.kind == 'separator':
        codepoint, name = SEPARATORS[shape.char]
        cause = (f'line {shape.line}, column {shape.column}: {codepoint} {name} is a line break '
                 'to some readers and ordinary text to others')
        remedy = f'use a newline there, or write the character as text, for example {codepoint}'
    else:
        first, last = shape.block
        if first <= shape.line <= last:
            if shape.kind == 'late-control':
                cause = (f"line {shape.line}: {shape.key} appears in the header block's text "
                         f"(lines {first} to {last}) but is not that line's own header, "
                         f"so the engine never reads it as {shape.key}")
            else:
                cause = (f"line {shape.line}: {shape.key} appears in this grant's header block text "
                         f"(lines {first} to {last}) but is not that line's own header, "
                         'so authority never reads it as a verdict')
            remedy = 'to mention the key in the header block, put it and its colon in inline code'
        elif shape.kind == 'late-control':
            cause = (f'line {shape.line}: {shape.key} is outside the header block '
                     f'(lines {first} to {last}), where the engine never reads it')
            remedy = ('move the line into the header block, or quote it in code; '
                      'to mention the key, put it and its colon in inline code')
        else:
            cause = (f"line {shape.line}: {shape.key} is outside this grant's header block "
                     f'(lines {first} to {last}), so authority reads the grant as carrying no verdict')
            remedy = ('move the line into the header block, where any value but an exact positive word '
                      'denies the grant; or remove it; or quote it in code; '
                      'to mention the key, put it and its colon in inline code')
    return [cause + '; ' + remedy]


def lint_file(
    path: Path,
    *,
    template_mode: bool = False,
    freshness: bool = False,
    max_drift_minutes: int = DEFAULT_MAX_DRIFT_MINUTES,
    artifact_root: Path | None = None,
    _raw: bytes | None = None,
) -> LintResult:
    """Lint text while measuring the original body bytes.

    A supplied text snapshot without raw bytes falls back to re-encoding;
    that fallback cannot preserve original newlines or invalid UTF-8 bytes.
    """
    body = path.read_bytes()
    text = body.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    if _raw is None:
        _raw = body
    clean = sanitized_text(text)
    result = LintResult()
    fields = header_fields(text)
    for message in draft_shape_errors(text):
        result.error(message)

    if not template_mode:
        for _a5e in lock_digest_shape_errors(text):
            result.error(_a5e)

    if not template_mode:
        for _warning in plan_shape_warnings(path, text, fields, artifact_root, raw=_raw):
            result.warn(_warning)
        _ordinal_warning = plan_revision_ordinal_warning(fields)
        if _ordinal_warning is not None:
            result.warn(_ordinal_warning)

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

    hg_value = fields.get("HUMAN_GATE_REQUIRED")
    if hg_value is not None and HUMAN_GATE_BARE_YES.fullmatch(hg_value.strip()):
        result.warn(
            "HUMAN_GATE_REQUIRED: yes carries no annotation naming its ask; "
            "annotate as 'yes — <the decision>'"
        )

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

    master_errors = h27_master_seat_errors(text, fields, template_mode=template_mode)
    for err in master_errors:
        result.error(err)
    dispatch_id_conflict = h27_conflict_message(text, "DISPATCH_ID")
    if dispatch_id_conflict and dispatch_id_conflict not in master_errors:
        result.error(dispatch_id_conflict)

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


class PlanShape(NamedTuple):
    lines: int
    fenced: int
    largest_block: int
    byte_count: int
    longest_line: int


def plan_shape_measure(data: bytes) -> PlanShape:
    text = data.decode('utf-8', errors='replace')
    lines = a5_split_lines(text)
    if lines and lines[-1] == '' and text.endswith(('\n', '\r')):
        lines.pop()
    if not data:
        lines = []
    fenced = largest = block = 0
    open_char = ''
    open_len = 0
    for line in lines:
        match = A5_FENCE_RE.match(line)
        if open_char:
            if (match and match.group(2)[0] == open_char
                    and len(match.group(2)) >= open_len
                    and match.group(3).strip(' \t') == ''):
                largest = max(largest, block)
                block = 0
                open_char = ''
                open_len = 0
            else:
                fenced += 1
                block += 1
            continue
        if match:
            family = match.group(2)[0]
            if family == '`' and '`' in match.group(3):
                continue
            open_char = family
            open_len = len(match.group(2))
    return PlanShape(len(lines), fenced, max(largest, block), len(data),
                     max((len(line) for line in lines), default=0))


def plan_shape_exceeded(shape: PlanShape) -> list[tuple[str, str, str]]:
    exceeded = []
    for name, value, limit in (('block', shape.largest_block, 80),
                               ('fenced', shape.fenced, 400)):
        if value > limit:
            exceeded.append((name, str(value), str(limit)))
    if shape.lines > 400 and 2 * shape.fenced > shape.lines:
        exceeded.append(('ratio', f'{shape.fenced / shape.lines:.2f}', '0.50'))
    for name, value, limit in (('lines', shape.lines, 1500),
                               ('bytes', shape.byte_count, 65536),
                               ('longest line', shape.longest_line, 8000)):
        if value > limit:
            exceeded.append((name, str(value), str(limit)))
    return exceeded


def plan_shape_warnings(path: Path, text: str, fields: dict,
                        artifact_root: Path | None, raw: bytes | None = None) -> list[str]:
    """Measure raw body bytes when supplied, otherwise re-encode the text.

    The text-only fallback cannot recover original newlines or invalid bytes;
    lint_file supplies bytes from its read or the caller's record snapshot.
    """
    if (artifact_root is None or fields.get('PHASE') != 'PLAN'
            or not h27_pair_planner_address(fields.get('FROM'))):
        return []
    stem = fields.get('PLAN_ARTIFACT')
    warnings = []
    data = None
    target = 'relay body'
    if stem is None:
        warnings.append('plan shape: no PLAN_ARTIFACT declared; the relay body was measured')
    else:
        if a5_stem_is_bare(stem):
            rel = f'plans/{stem}.md'
            probes = ((artifact_root / rel, rel),
                      (Path(rel), './' + rel),
                      (artifact_root.parent.parent / rel, '../../' + rel))
            for candidate, display in probes:
                try:
                    if not candidate.is_file():
                        continue
                    data = candidate.read_bytes()
                except OSError:
                    continue
                target = display
                break
        if data is None:
            warnings.append(f"plan shape: PLAN_ARTIFACT {stem!r} resolves at no probe root; the relay body was measured")
    if data is None:
        data = raw if raw is not None else text.encode('utf-8', errors='replace')
    exceeded = plan_shape_exceeded(plan_shape_measure(data))
    if exceeded:
        items = ', '.join(f'{name} {value} > {limit}' for name, value, limit in exceeded)
        warnings.append(f'plan shape: {target} over threshold: {items}')
    return warnings


def plan_revision_ordinal(dispatch_id: str | None) -> int | None:
    match = re.fullmatch(r'.+-plan(?:-([1-9][0-9]{0,2}))?', dispatch_id or '')
    return (int(match.group(1)) if match.group(1) else 1) if match else None


def plan_revision_ordinal_warning(fields: dict) -> str | None:
    if fields.get('PHASE') != 'PLAN' or not h27_pair_planner_address(fields.get('FROM')):
        return None
    ordinal = plan_revision_ordinal(fields.get('DISPATCH_ID'))
    if ordinal is not None and ordinal >= 8:
        return f'plan revision ordinal {ordinal} declared by DISPATCH_ID; the corpus median is 5'
    return None


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
        xroot_locator = (
            dkey == "DESIGN_SHA256"
            and xroot_pair_plan_applicable(text)
            and bool(h27_occurrences(text, "DESIGN_SOURCE_PATH"))
        )
        if not occ[akey] and not xroot_locator:
            errors.append(f"{dkey} declared with no {akey} to locate the artifact")
        elif occ[akey] and akey not in conflicted:
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
    for message in draft_shape_errors(text, separators_only=True):
        result.error(message)
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
            result.error(f"no index rows found in {path.name}")
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


H27_LOCK_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 1)"
H27_COMMISSION_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 3)"
H27_COMMISSION_AUTH_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 3a)"
H27_COMMISSION_CHARTER_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 3b)"
H27_COMMISSION_GRANT_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 3c)"
H27_COMMISSION_RECEIPT_RULE = " (DD-v29-master-authority-20260809 cross-seat rule 3d)"
H27_COMMISSION_SURFACE = ("COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID")
H27_COMMISSION_CARRIERS = ("COMMISSION_AUTHORIZATION",) + H27_COMMISSION_SURFACE
H27_COMMISSION_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def h27_resolved_discriminator(text: str, key: str) -> Tuple[str | None, bool]:
    """Return the sole distinct value and whether conflicting values exist."""
    values = h27_occurrences(text, key)
    distinct = set(values)
    if len(distinct) > 1:
        return None, True
    return (values[0] if values else None), False


def h27_single_from(text: str) -> str | None:
    """Resolve exactly one FROM address without first-value parsing."""
    raw, conflict = h27_resolved_discriminator(text, "FROM")
    if conflict or raw is None:
        return None
    addresses = split_addresses(raw)
    return addresses[0] if len(addresses) == 1 else None


def h27_tier_classification(address: str | None) -> str:
    """Classify an origin address as foreign, direct, or pair tier."""
    role = from_role(address)
    if role in MASTER_TIER_ROLES:
        return "foreign"
    if normalized_addr(address) in SPECIAL_ADDRESS_VALUES or role in {"orchestrator-planner", "orchestrator-reviewer"}:
        return "direct"
    return "pair"


def h27_has_foreign_from_occurrence(text: str) -> bool:
    for raw in h27_occurrences(text, "FROM"):
        if any(h27_tier_classification(address) == "foreign" for address in split_addresses(raw)):
            return True
    return False


def h27_conflict_message(text: str, key: str) -> str | None:
    """C14 fails closed in every lint mode; the cited master-authority ruling
    supplies cross-seat framing, not a seat- or tree-mode limit."""
    values = h27_occurrences(text, key)
    distinct = set(values)
    if len(distinct) <= 1:
        return None
    return (
        f"{key} carries {len(distinct)} distinct values across {len(values)} occurrences; "
        "an authority-critical field is fail-closed unless exactly one distinct value is present "
        "(DD-v29-master-authority-20260809 rule 5)"
    )


def h27_select_origins(phases, lock_id: str, before_order):
    """Select the consumer-relative origin universe for one lock identity."""
    selected = []
    for item in phases:
        _path, order, _phase, _fields, text = item
        if order >= before_order:
            continue
        design_doc_ids = h27_occurrences(text, "DESIGN_DOC_ID")
        phases_seen = h27_occurrences(text, "PHASE")
        if lock_id not in design_doc_ids or not any(phase in {"DESIGN", "AUDIT"} for phase in phases_seen):
            continue
        selected.append(item)
    return selected


def h27_group_and_route(origins, consumer_text: str) -> Tuple[str, str | None, List[object], str | None]:
    """Owner-group selected origins and implement the lock routing table."""
    foreign_groups: Dict[str | None, List[object]] = {}
    pair_group = False
    for item in origins:
        origin_text = item[4]
        address, from_conflict = h27_resolved_discriminator(origin_text, "FROM")
        if from_conflict:
            return "refuse", None, [], "origin-conflict"
        address = h27_single_from(origin_text)
        tier = h27_tier_classification(address)
        if tier == "foreign":
            foreign_groups.setdefault(from_owner(address), []).append(item)
        elif tier == "pair":
            pair_group = True
    if foreign_groups and pair_group:
        return "refuse", None, [], "collision"
    if len(foreign_groups) > 1:
        return "refuse", None, [], "multiple-foreign"
    if foreign_groups:
        owner, items = next(iter(foreign_groups.items()))
        return "foreign", owner, items, None
    consumer_address = h27_single_from(consumer_text)
    if h27_tier_classification(consumer_address) == "foreign":
        return "refuse", from_owner(consumer_address), [], "master-no-origin"
    return "pair", None, [], None


def h27_same_position_latest(items):
    """Return every item at the latest chronological position, ignoring tie-break names."""
    latest_position = max(item[1][:2] for item in items)
    return [item for item in items if item[1][:2] == latest_position]


def h27_chronological_position(order):
    """The ordering position without a filename/path tie-breaker."""
    return order[:2]


def h27_is_earlier(order, before_order) -> bool:
    """Strict chronology: a filename never makes a same-position relay earlier."""
    return h27_chronological_position(order) < h27_chronological_position(before_order)


def h27_commission_surface(text: str) -> Tuple[str | None, ...]:
    """Resolve the four byte-equality operands without first-value collapse."""
    return tuple(h27_resolved_discriminator(text, key)[0] for key in H27_COMMISSION_SURFACE)


def h27_commission_surface_complete(text: str) -> bool:
    """A stage surface is complete only when all four sole values are non-empty."""
    return all(value not in {None, ""} for value in h27_commission_surface(text))


def h27_commission_id_grammatical(value: str | None) -> bool:
    """The design grammar permits digits and hyphens throughout the tail."""
    return value is not None and H27_COMMISSION_ID_RE.fullmatch(value) is not None


def h27_direct_path_has_commission_carrier(
    from_role_value: str | None, dda_presence: bool, carrier_presence: set[str],
) -> bool:
    """Direct grants are insulated iff all five commission carriers are absent."""
    return bool(
        dda_presence
        and from_role_value in {"operator", "orchestrator", "orchestrator-planner"}
        and carrier_presence
    )


def h27_receipt_self_grants(text: str, is_receipt: bool) -> bool:
    """A commissioned pair receipt may consume a grant but never author one."""
    return is_receipt and "yes" in h27_occurrences(text, "DELEGATED_DISPATCH_AUTHORITY")


def h27_receiving_seat_errors(root: Path, phases) -> List[str]:
    """Rule 3(e): conflict-first, every-occurrence receiving-seat refusal."""
    errors: List[str] = []
    for item in phases:
        relname = item[0].relative_to(root)
        text = item[4]
        dda_occurrences = h27_occurrences(text, "DELEGATED_DISPATCH_AUTHORITY")
        if not dda_occurrences:
            continue
        to_occurrences = h27_occurrences(text, "TO")
        targets = [target for raw in to_occurrences for target in split_addresses(raw)]
        if not any(from_role(target) in MASTER_TIER_ROLES for target in targets):
            continue
        conflicts = []
        for key, values in (
            ("DELEGATED_DISPATCH_AUTHORITY", dda_occurrences),
            ("TO", to_occurrences),
        ):
            if len(set(values)) > 1:
                conflicts.append(key)
                errors.append(
                    f"{relname}: rule 3e gate: {key} carries {len(set(values))} distinct values across "
                    f"{len(values)} occurrences; a conflicting discriminator contributes no passing operand "
                    "(DD-v29-master-authority-20260809 rule 5)"
                )
        if conflicts:
            continue
        if "yes" not in dda_occurrences:
            continue
        seen_targets = set()
        for raw in to_occurrences:
            for target in split_addresses(raw):
                normalized = normalized_addr(target)
                if from_role(target) not in MASTER_TIER_ROLES or normalized in seen_targets:
                    continue
                seen_targets.add(normalized)
                errors.append(
                    f"{relname}: DELEGATED_DISPATCH_AUTHORITY: yes addressed to master/domain seat {target!r}; "
                    "the tier never receives delegated dispatch authority "
                    "(DD-v29-master-authority-20260809 cross-seat rule 3e)"
                )
    return errors


def h27_pair_planner_address(raw: str | None) -> bool:
    """Accept legacy planner and explicit pair-planner, never special/tier/reviewer/multiple."""
    addresses = split_addresses(raw)
    if len(addresses) != 1 or canonical_role(from_role(addresses[0])) != "planner":
        return False
    owner = from_owner(addresses[0])
    return owner is not None and normalized_addr(owner) not in SPECIAL_ADDRESS_VALUES


def h27_direct_commission_grantor(address: str | None) -> bool:
    return from_role(address) in {"operator", "orchestrator", "orchestrator-planner"}


def h27_any_role_occurrence(text: str, roles: set[str]) -> bool:
    return any(
        from_role(address) in roles
        for raw in h27_occurrences(text, "FROM")
        for address in split_addresses(raw)
    )


def h27_commission_auth_shape_ok(item) -> bool:
    """Task 9.5a stage-(a) carrier parsing and local shape, conflict-first."""
    _path, _order, _phase, _fields, text = item
    for key in (
        "FROM", "PHASE", "AUTHORITY", "TO", "COMMISSION_AUTHORIZATION",
        "COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
    ):
        if h27_resolved_discriminator(text, key)[1]:
            return False
    from_addr = h27_single_from(text)
    phase, _ = h27_resolved_discriminator(text, "PHASE")
    authority, _ = h27_resolved_discriminator(text, "AUTHORITY")
    carrier, _ = h27_resolved_discriminator(text, "COMMISSION_AUTHORIZATION")
    to_raw, _ = h27_resolved_discriminator(text, "TO")
    commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
    to_addrs = split_addresses(to_raw)
    return (
        h27_direct_commission_grantor(from_addr)
        and phase == "PLAN"
        and authority == "plan-only"
        and carrier == "yes"
        and len(to_addrs) == 1
        and from_role(to_addrs[0]) == "master-planner"
        and h27_commission_surface_complete(text)
        and h27_pair_planner_address(commission_to)
    )


def h27_commission_charter_shape_ok(item) -> bool:
    """Task 9.5a charter-local shape; parent resolution deliberately belongs to 9.5b."""
    _path, _order, _phase, _fields, text = item
    for key in (
        "FROM", "PHASE", "AUTHORITY", "DESIGN_DOC_ID", "DESIGN_RECORD_KIND",
        "COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
    ):
        if h27_resolved_discriminator(text, key)[1]:
            return False
    from_addr = h27_single_from(text)
    phase, _ = h27_resolved_discriminator(text, "PHASE")
    authority, _ = h27_resolved_discriminator(text, "AUTHORITY")
    design_doc_id, _ = h27_resolved_discriminator(text, "DESIGN_DOC_ID")
    kind, _ = h27_resolved_discriminator(text, "DESIGN_RECORD_KIND")
    charter_doc_id, _ = h27_resolved_discriminator(text, "CHARTER_DOC_ID")
    return (
        from_role(from_addr) == "master-planner"
        and phase == "DESIGN"
        and authority == "design-only"
        and design_doc_id not in {None, ""}
        and design_doc_id == charter_doc_id
        and kind == "design-doc"
        and h27_commission_surface_complete(text)
    )


def h27_commission_approval_shape_ok(item) -> bool:
    """Task 9.5a approval-local shape; edge and same-owner checks belong to 9.5b."""
    _path, _order, _phase, _fields, text = item
    for key in (
        "FROM", "PHASE", "DESIGN_DOC_ID", "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT",
        "COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
    ):
        if h27_resolved_discriminator(text, key)[1]:
            return False
    phase, _ = h27_resolved_discriminator(text, "PHASE")
    kind, _ = h27_resolved_discriminator(text, "DESIGN_RECORD_KIND")
    verdict, _ = h27_resolved_discriminator(text, "DESIGN_REVIEW_VERDICT")
    return (
        phase == "DESIGN-REVIEW"
        and kind == "design-doc"
        and verdict in DESIGN_REVIEW_VERDICT_VALUES
        and h27_commission_surface_complete(text)
    )


def h27_commission_grant_shape_ok(item) -> bool:
    """Task 9.5a grant-local shape, including literal marker and pair target."""
    _path, _order, _phase, _fields, text = item
    for key in (
        "FROM", "PHASE", "AUTHORITY", "TO", "DELEGATED_DISPATCH_AUTHORITY",
        "COMMISSION_ID", "COMMISSION_SCOPE", "COMMISSION_TO", "CHARTER_DOC_ID",
    ):
        if h27_resolved_discriminator(text, key)[1]:
            return False
    from_addr = h27_single_from(text)
    phase, _ = h27_resolved_discriminator(text, "PHASE")
    authority, _ = h27_resolved_discriminator(text, "AUTHORITY")
    marker, _ = h27_resolved_discriminator(text, "DELEGATED_DISPATCH_AUTHORITY")
    to_raw, _ = h27_resolved_discriminator(text, "TO")
    commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
    to_addrs = split_addresses(to_raw)
    return (
        from_role(from_addr) == "master-planner"
        and phase == "PLAN"
        and authority == "plan-only"
        and marker == "yes"
        and h27_pair_planner_address(commission_to)
        and len(to_addrs) == 1
        and normalized_addr(to_addrs[0]) == normalized_addr(commission_to)
        and h27_commission_surface_complete(text)
    )


def h27_foreign_lock_errors(
    root: Path, consumer, lock_id: str, owner: str | None, origins, phases,
) -> List[str]:
    """Validate the foreign lock origin/review/consumer lifecycle branch."""
    consumer_path, consumer_order, _consumer_phase, _consumer_fields, consumer_text = consumer
    consumer_name = consumer_path.relative_to(root)
    latest_origins = h27_same_position_latest(origins)
    if len(latest_origins) != 1:
        return [
            f"{consumer_name}: DESIGN_LOCK_ID {lock_id!r} has {len(latest_origins)} same-position latest origins "
            f"for owner {owner}; lock resolution fails closed{H27_LOCK_RULE}"
        ]
    origin = latest_origins[0]
    origin_path, _origin_order, _origin_phase, _origin_fields, origin_text = origin
    origin_name = origin_path.relative_to(root)
    for key in ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "DESIGN_DOC_ID", "DESIGN_RECORD_KIND"):
        _value, conflict = h27_resolved_discriminator(origin_text, key)
        if conflict:
            return [
                f"{consumer_name}: foreign origin {origin_name} has conflicting {key} occurrences; "
                f"lock lifecycle refuses first-value resolution{H27_LOCK_RULE}"
            ]

    origin_from = h27_single_from(origin_text)
    origin_role = from_role(origin_from)
    origin_phase, _ = h27_resolved_discriminator(origin_text, "PHASE")
    origin_authority, _ = h27_resolved_discriminator(origin_text, "AUTHORITY")
    origin_id, _ = h27_resolved_discriminator(origin_text, "DISPATCH_ID")
    origin_doc, _ = h27_resolved_discriminator(origin_text, "DESIGN_DOC_ID")
    origin_kind, _ = h27_resolved_discriminator(origin_text, "DESIGN_RECORD_KIND")
    if origin_kind is None:
        return [f"{consumer_name}: foreign origin {origin_name} requires DESIGN_RECORD_KIND{H27_LOCK_RULE}"]
    if origin_role in {"master-reviewer", "domain-reviewer"}:
        return [
            f"{consumer_name}: foreign {origin_kind} may not originate from reviewer seat {origin_role!r} "
            f"in PHASE {origin_phase}{H27_LOCK_RULE}"
        ]
    if origin_kind == "design-doc" and not (
        origin_role in {"master-planner", "domain-planner"}
        and origin_phase == "DESIGN" and origin_authority == "design-only"
    ):
        return [
            f"{consumer_name}: foreign design-doc origin {origin_name} must be planner-seat PHASE DESIGN "
            f"with AUTHORITY: design-only{H27_LOCK_RULE}"
        ]
    if origin_kind == "audit-record" and not (
        origin_role in {"master-planner", "domain-planner"}
        and origin_phase == "AUDIT" and origin_authority in {"review-only", "report-only"}
    ):
        return [
            f"{consumer_name}: foreign audit-record origin {origin_name} must be planner-seat PHASE AUDIT "
            f"with AUTHORITY: review-only or report-only{H27_LOCK_RULE}"
        ]
    if origin_kind not in {"design-doc", "audit-record"} or origin_doc != lock_id or not origin_id:
        return [f"{consumer_name}: foreign origin {origin_name} does not resolve lock identity {lock_id!r}{H27_LOCK_RULE}"]

    review_candidates = []
    for item in phases:
        _review_path, review_order, _review_phase, _review_fields, review_text = item
        if review_order >= consumer_order:
            continue
        review_phases = h27_occurrences(review_text, "PHASE")
        review_docs = h27_occurrences(review_text, "DESIGN_DOC_ID")
        review_parents = h27_occurrences(review_text, "PARENT_DISPATCH_ID")
        if "DESIGN-REVIEW" not in review_phases or lock_id not in review_docs:
            continue
        if origin_id not in review_parents:
            continue
        review_candidates.append(item)
    if not review_candidates:
        return [
            f"{consumer_name}: foreign DESIGN_LOCK_ID {lock_id!r} has no earlier DESIGN-REVIEW parented "
            f"to latest origin {origin_name}{H27_LOCK_RULE}"
        ]
    latest_reviews = h27_same_position_latest(review_candidates)
    if len(latest_reviews) != 1:
        return [
            f"{consumer_name}: latest origin {origin_name} has {len(latest_reviews)} same-position latest "
            f"DESIGN-REVIEW candidates; lock resolution fails closed{H27_LOCK_RULE}"
        ]
    review = latest_reviews[0]
    review_path, _review_order, _review_phase, _review_fields, review_text = review
    review_name = review_path.relative_to(root)
    for key in (
        "FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
        "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT",
    ):
        _value, conflict = h27_resolved_discriminator(review_text, key)
        if conflict:
            return [
                f"{consumer_name}: foreign approval {review_name} has conflicting {key} occurrences; "
                f"lock lifecycle refuses first-value resolution{H27_LOCK_RULE}"
            ]

    review_from = h27_single_from(review_text)
    expected_review_role = "master-reviewer" if origin_role == "master-planner" else "domain-reviewer"
    expected_review_from = same_owner_addr(owner, expected_review_role)
    if normalized_addr(review_from) != expected_review_from:
        return [
            f"{consumer_name}: foreign approval {review_name} must be FROM {expected_review_from}, "
            f"the same-owner tier reviewer{H27_LOCK_RULE}"
        ]
    review_kind, _ = h27_resolved_discriminator(review_text, "DESIGN_RECORD_KIND")
    if review_kind is None:
        return [
            f"{consumer_name}: foreign approval {review_name} requires DESIGN_RECORD_KIND equal to "
            f"origin kind {origin_kind!r}{H27_LOCK_RULE}"
        ]
    if review_kind != origin_kind:
        return [
            f"{consumer_name}: foreign approval {review_name} DESIGN_RECORD_KIND {review_kind!r} does not "
            f"equal origin kind {origin_kind!r}{H27_LOCK_RULE}"
        ]
    review_verdict, _ = h27_resolved_discriminator(review_text, "DESIGN_REVIEW_VERDICT")
    if review_verdict != "approve":
        return [
            f"{consumer_name}: latest foreign approval {review_name} requires DESIGN_REVIEW_VERDICT: approve; "
            f"got {review_verdict!r}{H27_LOCK_RULE}"
        ]
    consumer_kind, _ = h27_resolved_discriminator(consumer_text, "DESIGN_RECORD_KIND")
    if consumer_kind is not None and consumer_kind != origin_kind:
        return [
            f"{consumer_name}: consumer DESIGN_RECORD_KIND {consumer_kind!r} does not equal foreign origin "
            f"kind {origin_kind!r}{H27_LOCK_RULE}"
        ]
    return []


def h27_lock_lifecycle_precompute(root: Path, phases):
    """Precompute every consumer route and all foreign-branch diagnostics."""
    routes: Dict[Path, str] = {}
    errors: List[str] = []
    for consumer in phases:
        consumer_path, consumer_order, _phase, _fields, consumer_text = consumer
        lock_values = h27_occurrences(consumer_text, "DESIGN_LOCK_ID")
        if not lock_values:
            continue
        consumer_name = consumer_path.relative_to(root)
        consumer_conflict = False
        for key in ("DESIGN_LOCK_ID", "DESIGN_RECORD_KIND", "FROM"):
            conflict = h27_conflict_message(consumer_text, key)
            if conflict:
                consumer_conflict = True
                if not h27_has_foreign_from_occurrence(consumer_text):
                    errors.append(f"{consumer_name}: {conflict}")
        if consumer_conflict:
            routes[consumer_path] = "refuse"
            continue
        lock_id, _ = h27_resolved_discriminator(consumer_text, "DESIGN_LOCK_ID")
        if not lock_id:
            routes[consumer_path] = "refuse"
            continue
        origins = h27_select_origins(phases, lock_id, consumer_order)
        route, owner, foreign_origins, refusal = h27_group_and_route(origins, consumer_text)
        routes[consumer_path] = route
        if refusal == "collision":
            errors.append(
                f"{consumer_name}: DESIGN_LOCK_ID {lock_id!r} resolves to both pair and master/domain origin "
                f"groups; lock routing fails closed{H27_LOCK_RULE}"
            )
        elif refusal == "multiple-foreign":
            owner_count = len({from_owner(h27_single_from(item[4])) for item in origins if h27_tier_classification(h27_single_from(item[4])) == "foreign"})
            errors.append(
                f"{consumer_name}: DESIGN_LOCK_ID {lock_id!r} resolves to {owner_count} master/domain owner "
                f"groups; lock routing fails closed{H27_LOCK_RULE}"
            )
        elif refusal == "master-no-origin":
            errors.append(
                f"{consumer_name}: master/domain-seat consumer DESIGN_LOCK_ID {lock_id!r} has no resolvable "
                f"earlier master/domain origin{H27_LOCK_RULE}"
            )
        elif refusal == "origin-conflict":
            conflicted_origin = next(
                item for item in origins if h27_resolved_discriminator(item[4], "FROM")[1]
            )
            origin_name = conflicted_origin[0].relative_to(root)
            origin_label = "foreign origin" if h27_has_foreign_from_occurrence(conflicted_origin[4]) else "origin"
            errors.append(
                f"{consumer_name}: {origin_label} {origin_name} has conflicting FROM occurrences; "
                f"lock lifecycle refuses first-value resolution{H27_LOCK_RULE}"
            )
        elif route == "foreign":
            errors.extend(h27_foreign_lock_errors(root, consumer, lock_id, owner, foreign_origins, phases))
    return routes, errors


def h27_commission_precompute(root: Path, phases) -> List[str]:
    """Validate commission carriers, exact ancestry, and authorization lifetime."""
    errors: List[str] = []

    dispatch_holders: Dict[str, List[object]] = {}
    for phase_item in phases:
        for dispatch_id in set(h27_occurrences(phase_item[4], "DISPATCH_ID")):
            dispatch_holders.setdefault(dispatch_id, []).append(phase_item)

    def relname(item) -> str:
        return str(item[0].relative_to(root))

    def marker_universe(marker: str, commission_id: str, before_order):
        return sorted(
            (
                item for item in phases
                if h27_is_earlier(item[1], before_order)
                and h27_occurrences(item[4], marker)
                and commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            ),
            key=lambda item: item[1],
        )

    def latest_at_one_position(items):
        if not items:
            return None, 0
        latest = h27_same_position_latest(items)
        return (latest[0] if len(latest) == 1 else None), len(latest)

    def selected_conflict(item, keys) -> str | None:
        for key in keys:
            if h27_resolved_discriminator(item[4], key)[1]:
                return key
        return None

    def resolve_ancestry(parent_id: str | None, before_order, consumer, edge: str, site_rule: str):
        """Resolve one edge over every local DISPATCH_ID occurrence, then validate."""
        if parent_id in {None, ""}:
            return None
        candidates = sorted(
            (item for item in dispatch_holders.get(parent_id, []) if h27_is_earlier(item[1], before_order)),
            key=lambda item: item[1],
        )
        if not candidates:
            errors.append(
                f"{relname(consumer)}: {edge} {parent_id!r} does not resolve to an earlier relay{site_rule}"
            )
            return None
        latest = h27_same_position_latest(candidates)
        if len(latest) != 1:
            errors.append(
                f"{relname(consumer)}: {edge} {parent_id!r} has {len(latest)} same-position latest holders; "
                f"ambiguity fails closed{site_rule}"
            )
            return None
        selected = latest[0]
        if h27_resolved_discriminator(selected[4], "DISPATCH_ID")[1]:
            errors.append(
                f"{relname(consumer)}: selected {edge} relay {relname(selected)} has conflicting DISPATCH_ID "
                f"occurrences; ancestry resolution refuses first-value resolution{site_rule}"
            )
            return None
        return selected

    def latest_stage(candidates, consumer, label: str, site_rule: str):
        candidates = sorted(candidates, key=lambda item: item[1])
        if not candidates:
            return None
        latest = h27_same_position_latest(candidates)
        if len(latest) != 1:
            errors.append(
                f"{relname(consumer)}: commission {h27_resolved_discriminator(consumer[4], 'COMMISSION_ID')[0]!r} "
                f"has {len(latest)} {label} at the latest order position; ambiguity fails closed{site_rule}"
            )
            return False
        return latest[0]

    def charter_candidates(charter_doc_id: str, commission_id: str, before_order):
        return [
            item for item in phases
            if h27_is_earlier(item[1], before_order)
            and (
                charter_doc_id in h27_occurrences(item[4], "DESIGN_DOC_ID")
                or commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            )
            and h27_occurrences(item[4], "DESIGN_DOC_ID")
            and h27_any_role_occurrence(item[4], {"master-planner"})
        ]

    def review_candidates(charter, charter_doc_id: str, commission_id: str, before_order):
        charter_id, charter_id_conflict = h27_resolved_discriminator(charter[4], "DISPATCH_ID")
        if charter_id_conflict or charter_id in {None, ""}:
            return []
        return [
            item for item in phases
            if h27_is_earlier(item[1], before_order)
            and (
                charter_doc_id in h27_occurrences(item[4], "DESIGN_DOC_ID")
                or commission_id in h27_occurrences(item[4], "COMMISSION_ID")
            )
            and h27_occurrences(item[4], "DESIGN_DOC_ID")
            and h27_any_role_occurrence(item[4], {"master-reviewer"})
        ]

    def stage_conflict(item, keys, consumer, label: str, site_rule: str) -> bool:
        conflict = selected_conflict(item, keys)
        if conflict:
            errors.append(
                f"{relname(consumer)}: selected {label} {relname(item)} has conflicting {conflict} occurrences; "
                f"ancestry resolution refuses first-value resolution{site_rule}"
            )
            return True
        return False

    def same_owner_reviewer(review, charter) -> bool:
        review_from = h27_single_from(review[4])
        charter_from = h27_single_from(charter[4])
        expected = same_owner_addr(from_owner(charter_from), "master-reviewer")
        return from_role(review_from) == "master-reviewer" and normalized_addr(review_from) == normalized_addr(expected)

    def charter_parent_authorization(charter, authorization, consumer, site_rule: str) -> bool:
        parent_id, parent_conflict = h27_resolved_discriminator(charter[4], "PARENT_DISPATCH_ID")
        if parent_conflict:
            return False
        if parent_id in {None, ""}:
            errors.append(
                f"{relname(consumer)}: selected charter revision {relname(charter)} lacks a usable "
                f"PARENT_DISPATCH_ID{site_rule}"
            )
            return False
        parent = resolve_ancestry(parent_id, charter[1], consumer, "charter parent", site_rule)
        if parent is None:
            return False
        if parent is not authorization:
            errors.append(
                f"{relname(consumer)}: charter parent {parent_id!r} resolves to {relname(parent)}, which is not "
                f"the reselected stage-(a) authorization{site_rule}"
            )
            return False
        return True

    def selected_authorization_conflict(selected, consumer) -> bool:
        conflict = selected_conflict(
            selected,
            ("FROM", "PHASE", "AUTHORITY", "TO", "DISPATCH_ID", "COMMISSION_AUTHORIZATION")
            + H27_COMMISSION_SURFACE,
        )
        if conflict:
            errors.append(
                f"{relname(consumer)}: selected authorization-universe member {relname(selected)} has conflicting "
                f"{conflict} occurrences; universe selection refuses first-value resolution{H27_COMMISSION_AUTH_RULE}"
            )
            return True
        return False

    def authorization_for(consumer, commission_id: str, site_rule: str):
        candidates = marker_universe("COMMISSION_AUTHORIZATION", commission_id, consumer[1])
        selected, tied = latest_at_one_position(candidates)
        if not candidates:
            errors.append(
                f"{relname(consumer)}: commission {commission_id!r} has no earlier authorization-universe member{site_rule}"
            )
            return None
        if selected is None:
            errors.append(
                f"{relname(consumer)}: commission {commission_id!r} has {tied} authorization-universe candidates "
                f"at the latest order position; ambiguity fails closed{H27_COMMISSION_AUTH_RULE}"
            )
            return None
        if selected_authorization_conflict(selected, consumer):
            return None
        if not h27_commission_auth_shape_ok(selected):
            errors.append(
                f"{relname(consumer)}: latest authorization-universe member {relname(selected)} fails stage-(a) "
                f"shape; marker-bearing malformed authorization shadows and fails{H27_COMMISSION_AUTH_RULE}"
            )
            return None
        return selected

    def authorization_conflict_for(consumer, commission_id: str) -> bool:
        """Report only a uniquely selected authorization's conflict, without trusting its shape."""
        selected, _tied = latest_at_one_position(
            marker_universe("COMMISSION_AUTHORIZATION", commission_id, consumer[1])
        )
        if selected is None:
            return False
        return selected_authorization_conflict(selected, consumer)

    def add_nonmaster_conflicts(item) -> bool:
        """File-mode H27 already reports master-seat conflicts; cover special/pair stages here."""
        from_addr = h27_single_from(item[4])
        is_master = from_role(from_addr) in MASTER_TIER_ROLES
        conflicted = False
        for key in H27_CONFLICT_FIELDS:
            conflict = h27_conflict_message(item[4], key)
            if conflict:
                conflicted = True
                if not is_master:
                    errors.append(f"{relname(item)}: {conflict}")
        return conflicted

    for item in phases:
        _path, order, _phase, _fields, text = item
        carrier_presence = {key for key in H27_COMMISSION_CARRIERS if h27_occurrences(text, key)}
        dda_presence = bool(h27_occurrences(text, "DELEGATED_DISPATCH_AUTHORITY"))
        master_grant_marker = dda_presence and h27_any_role_occurrence(text, {"master-planner"})
        if not carrier_presence and not master_grant_marker:
            continue

        commission_id, commission_id_conflict = h27_resolved_discriminator(text, "COMMISSION_ID")
        if (
            carrier_presence == {"COMMISSION_ID"}
            and not dda_presence
            and not commission_id_conflict
            and commission_id not in {None, ""}
        ):
            # A non-empty ID alone is an inert reference. Empty ID is engaged.
            continue

        from_addr = h27_single_from(text)
        from_role_value = from_role(from_addr)
        phase, _ = h27_resolved_discriminator(text, "PHASE")
        auth_marker = bool(h27_occurrences(text, "COMMISSION_AUTHORIZATION"))

        # A direct grant is insulated only when it carries none of the five
        # commission carriers. Any one carrier engages the full machine; the
        # authorization carrier produces the both-markers case.
        if h27_direct_path_has_commission_carrier(from_role_value, dda_presence, carrier_presence):
            errors.append(
                f"{relname(item)}: a direct delegation grant carries commission gating carriers; "
                "direct path must carry none of the five"
                f"{H27_COMMISSION_RULE}"
            )
            continue

        if (
            auth_marker
            and not dda_presence
            and h27_occurrences(text, "COMMISSION_ID")
            and not commission_id_conflict
            and not h27_commission_id_grammatical(commission_id)
        ):
            errors.append(
                f"{relname(item)}: commission machine engaged with non-grammatical COMMISSION_ID "
                f"{commission_id!r}; expected [a-z0-9][a-z0-9-]*{H27_COMMISSION_RULE}"
            )
            continue

        # Logical CH identity is validated at the authorization trust root.
        # Charter-local DESIGN_DOC_ID equality is checked at its own stage;
        # later equality-surface diagnostics remain unchanged.
        charter_doc_id, _ = h27_resolved_discriminator(text, "CHARTER_DOC_ID")
        if (
            auth_marker
            and not commission_id_conflict
            and h27_commission_id_grammatical(commission_id)
            and charter_doc_id not in {None, f"CH-{commission_id}"}
        ):
            errors.append(
                f"{relname(item)}: CHARTER_DOC_ID {charter_doc_id!r} must equal "
                f"{'CH-' + commission_id!r}{H27_COMMISSION_RULE}"
            )
            continue

        # Stage (a) is marker-defined. Both markers intentionally do not exit
        # here; they fall through to delegation-stage classification.
        if auth_marker and not dda_presence:
            if add_nonmaster_conflicts(item):
                continue
            if not h27_commission_auth_shape_ok(item):
                errors.append(
                    f"{relname(item)}: relay carries COMMISSION_AUTHORIZATION but fails stage-(a) shape "
                    "(literal yes, direct-authority FROM, PHASE PLAN, AUTHORITY plan-only, TO exactly one "
                    "master-planner, complete equality surface, pair-planner COMMISSION_TO)"
                    f"{H27_COMMISSION_AUTH_RULE}"
                )
            continue

        is_receipt = phase == "PLAN" and canonical_role(from_role_value) == "planner"
        if is_receipt and not h27_commission_surface_complete(text):
            if add_nonmaster_conflicts(item):
                continue
            errors.append(f"{relname(item)}: commissioned pair receipt has incomplete equality surface{H27_COMMISSION_RECEIPT_RULE}")
            continue

        if add_nonmaster_conflicts(item):
            continue
        if commission_id in {None, ""}:
            errors.append(
                f"{relname(item)}: commission machine engaged but relay is not consumed by any Task 9.5a stage"
                f"{H27_COMMISSION_RULE}"
            )
            continue

        # Charter stage: select the marker-universe authorization, then resolve
        # the mandatory parent generically across every earlier DISPATCH_ID holder.
        design_doc_id, _ = h27_resolved_discriminator(text, "DESIGN_DOC_ID")
        if phase == "DESIGN" and from_role_value == "master-planner" and design_doc_id != charter_doc_id:
            errors.append(
                f"{relname(item)}: charter DESIGN_DOC_ID {design_doc_id!r} must equal "
                f"CHARTER_DOC_ID {charter_doc_id!r}{H27_COMMISSION_CHARTER_RULE}"
            )
            continue
        if phase == "DESIGN" and from_role_value == "master-planner" and design_doc_id == charter_doc_id:
            if not h27_commission_charter_shape_ok(item):
                errors.append(
                    f"{relname(item)}: charter fails stage-(b) shape; requires master-planner PHASE DESIGN, "
                    "AUTHORITY design-only, matching document identity, and DESIGN_RECORD_KIND design-doc"
                    f"{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            authorization = authorization_for(item, commission_id, H27_COMMISSION_CHARTER_RULE)
            if authorization is None:
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: charter PARENT_DISPATCH_ID is mandatory and must resolve the selected "
                    f"authorization{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "charter parent", H27_COMMISSION_CHARTER_RULE)
            if parent is None:
                continue
            if parent is not authorization:
                selected_id, _ = h27_resolved_discriminator(authorization[4], "DISPATCH_ID")
                if h27_commission_auth_shape_ok(parent):
                    errors.append(
                        f"{relname(item)}: charter parent {parent_id!r} does not equal selected authorization "
                        f"{selected_id!r}{H27_COMMISSION_CHARTER_RULE}"
                    )
                else:
                    errors.append(
                        f"{relname(item)}: charter parent {parent_id!r} resolves to {relname(parent)}, which is not "
                        f"the selected stage-(a) authorization{H27_COMMISSION_CHARTER_RULE}"
                    )
                continue
            auth_to, _ = h27_resolved_discriminator(authorization[4], "TO")
            auth_to_addrs = split_addresses(auth_to)
            if len(auth_to_addrs) != 1 or normalized_addr(auth_to_addrs[0]) != normalized_addr(from_addr):
                errors.append(
                    f"{relname(item)}: charter FROM must equal the master-planner address selected authorization TO"
                    f"{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            if h27_commission_surface(text) != h27_commission_surface(authorization[4]):
                errors.append(
                    f"{relname(item)}: charter equality surface is not byte-equal to the selected authorization"
                    f"{H27_COMMISSION_CHARTER_RULE}"
                )
            continue

        # Approval resolves the latest charter revision, its exact edge, and
        # independently reselects the live authorization before accepting it.
        if phase == "DESIGN-REVIEW" and from_role_value == "master-reviewer" and design_doc_id not in {None, ""}:
            if not h27_commission_approval_shape_ok(item):
                errors.append(
                    f"{relname(item)}: charter approval requires DESIGN_RECORD_KIND: design-doc and a "
                    f"grammatical DESIGN_REVIEW_VERDICT{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, order), item,
                "charter revisions", H27_COMMISSION_CHARTER_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(f"{relname(item)}: charter approval has no earlier charter revision{H27_COMMISSION_CHARTER_RULE}")
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: charter approval PARENT_DISPATCH_ID is mandatory and must resolve the latest "
                    f"charter revision{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "charter approval parent", H27_COMMISSION_CHARTER_RULE)
            if parent is None:
                continue
            if parent is not charter:
                charter_id_value, _ = h27_resolved_discriminator(charter[4], "DISPATCH_ID")
                if h27_commission_charter_shape_ok(parent):
                    errors.append(
                        f"{relname(item)}: charter approval parent {parent_id!r} does not equal latest charter "
                        f"revision {charter_id_value!r}{H27_COMMISSION_CHARTER_RULE}"
                    )
                else:
                    errors.append(
                        f"{relname(item)}: charter approval parent {parent_id!r} resolves to {relname(parent)}, "
                        f"which is not the latest charter revision{H27_COMMISSION_CHARTER_RULE}"
                    )
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "charter revision", H27_COMMISSION_CHARTER_RULE,
            )
            if charter_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    f"{relname(item)}: charter approval parent is not a valid stage-(b) charter revision"
                    f"{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            if not same_owner_reviewer(item, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    f"{relname(item)}: charter approval must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_CHARTER_RULE}"
                )
                continue
            charter_id, conflict = h27_resolved_discriminator(charter[4], "COMMISSION_ID")
            if conflict or charter_id in {None, ""}:
                errors.append(f"{relname(item)}: selected charter revision has no usable COMMISSION_ID{H27_COMMISSION_CHARTER_RULE}")
                continue
            authorization = authorization_for(item, charter_id, H27_COMMISSION_CHARTER_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(charter, authorization, item, H27_COMMISSION_CHARTER_RULE):
                continue
            surfaces = {
                h27_commission_surface(text),
                h27_commission_surface(charter[4]),
                h27_commission_surface(authorization[4]),
            }
            if len(surfaces) != 1:
                errors.append(
                    f"{relname(item)}: approval equality surface is not byte-equal to the selected authorization "
                    f"and charter revision{H27_COMMISSION_CHARTER_RULE}"
                )
            continue

        # Grant local shape and four-way equality. The literal marker is
        # checked separately so YES/true remain universe members but fail.
        if dda_presence and from_role_value == "master-planner":
            marker, _ = h27_resolved_discriminator(text, "DELEGATED_DISPATCH_AUTHORITY")
            if marker != "yes":
                errors.append(
                    f"{relname(item)}: master-planner grant requires literal DELEGATED_DISPATCH_AUTHORITY: yes; "
                    f"got {marker!r}{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
            if not h27_pair_planner_address(commission_to):
                errors.append(
                    f"{relname(item)}: COMMISSION_TO {commission_to!r} is not exactly one non-special "
                    f"pair-planner address{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            to_raw, _ = h27_resolved_discriminator(text, "TO")
            to_addrs = split_addresses(to_raw)
            if len(to_addrs) != 1 or normalized_addr(to_addrs[0]) != normalized_addr(commission_to):
                errors.append(f"{relname(item)}: grant TO must be exactly the COMMISSION_TO address{H27_COMMISSION_GRANT_RULE}")
                continue
            if not h27_commission_grant_shape_ok(item):
                errors.append(f"{relname(item)}: master-planner grant fails stage-(c) shape{H27_COMMISSION_GRANT_RULE}")
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, order), item,
                "charter revisions", H27_COMMISSION_GRANT_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(f"{relname(item)}: grant lacks an earlier charter revision{H27_COMMISSION_GRANT_RULE}")
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "latest charter revision", H27_COMMISSION_GRANT_RULE,
            )
            if charter_conflicted:
                eager_approval, _tied = latest_at_one_position(
                    review_candidates(charter, charter_doc_id, commission_id, order)
                )
                if eager_approval is not None:
                    stage_conflict(
                        eager_approval,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                        item, "charter review", H27_COMMISSION_GRANT_RULE,
                    )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    f"{relname(item)}: selected latest charter revision {relname(charter)} fails stage-(b) shape"
                    f"{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            approval = latest_stage(
                review_candidates(charter, charter_doc_id, commission_id, order), item,
                "charter reviews", H27_COMMISSION_GRANT_RULE,
            )
            if approval is False:
                continue
            if approval is None:
                errors.append(
                    f"{relname(item)}: grant has no earlier charter review for the latest charter revision"
                    f"{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            approval_conflicted = stage_conflict(
                approval,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                item, "charter review", H27_COMMISSION_GRANT_RULE,
            )
            if approval_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            verdict, _ = h27_resolved_discriminator(approval[4], "DESIGN_REVIEW_VERDICT")
            if not h27_commission_approval_shape_ok(approval) or verdict != "approve":
                if h27_commission_approval_shape_ok(approval):
                    errors.append(
                        f"{relname(item)}: latest charter review {relname(approval)} requires "
                        f"DESIGN_REVIEW_VERDICT: approve; got {verdict!r}{H27_COMMISSION_GRANT_RULE}"
                    )
                else:
                    errors.append(f"{relname(item)}: grant parent is not a valid charter review{H27_COMMISSION_GRANT_RULE}")
                continue
            if not same_owner_reviewer(approval, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    f"{relname(item)}: charter review must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            parent_id, _ = h27_resolved_discriminator(text, "PARENT_DISPATCH_ID")
            if parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: grant PARENT_DISPATCH_ID is mandatory and must resolve the latest approving "
                    f"review{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            parent = resolve_ancestry(parent_id, order, item, "grant parent", H27_COMMISSION_GRANT_RULE)
            if parent is None:
                continue
            if parent is not approval:
                approval_id, _ = h27_resolved_discriminator(approval[4], "DISPATCH_ID")
                if h27_commission_approval_shape_ok(parent):
                    errors.append(
                        f"{relname(item)}: grant parent {parent_id!r} does not equal latest charter review "
                        f"{approval_id!r}{H27_COMMISSION_GRANT_RULE}"
                    )
                else:
                    errors.append(
                        f"{relname(item)}: grant parent {parent_id!r} resolves to {relname(parent)}, which is not "
                        f"the latest charter review{H27_COMMISSION_GRANT_RULE}"
                    )
                continue
            approval_parent_id, _ = h27_resolved_discriminator(approval[4], "PARENT_DISPATCH_ID")
            if approval_parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: selected charter review {relname(approval)} lacks a usable "
                    f"PARENT_DISPATCH_ID{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            approval_parent = resolve_ancestry(
                approval_parent_id, approval[1], item, "approval parent", H27_COMMISSION_GRANT_RULE,
            )
            if approval_parent is None:
                continue
            if approval_parent is not charter:
                errors.append(
                    f"{relname(item)}: approval parent {approval_parent_id!r} resolves to {relname(approval_parent)}, "
                    f"which is not the charter revision{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            charter_id, conflict = h27_resolved_discriminator(charter[4], "COMMISSION_ID")
            if conflict or charter_id in {None, ""}:
                errors.append(f"{relname(item)}: selected charter revision has no usable COMMISSION_ID{H27_COMMISSION_GRANT_RULE}")
                continue
            authorization = authorization_for(item, charter_id, H27_COMMISSION_GRANT_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(charter, authorization, item, H27_COMMISSION_GRANT_RULE):
                continue
            auth_to, _ = h27_resolved_discriminator(authorization[4], "TO")
            auth_to_addrs = split_addresses(auth_to)
            if len(auth_to_addrs) != 1 or normalized_addr(auth_to_addrs[0]) != normalized_addr(from_addr):
                errors.append(
                    f"{relname(item)}: grant FROM must equal the master-planner address selected authorization TO"
                    f"{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            surfaces = {
                h27_commission_surface(text), h27_commission_surface(approval[4]),
                h27_commission_surface(charter[4]), h27_commission_surface(authorization[4]),
            }
            if len(surfaces) != 1:
                errors.append(
                    f"{relname(item)}: grant equality surface is not byte-equal across selected authorization, "
                    f"charter revision, approval, and grant{H27_COMMISSION_GRANT_RULE}"
                )
            continue

        # Pair receipt: target first, then latest grant-universe selection.
        if is_receipt:
            if h27_receipt_self_grants(text, is_receipt):
                errors.append(
                    f"{relname(item)}: commissioned pair receipt carries DELEGATED_DISPATCH_AUTHORITY: yes; "
                    f"self-grant is prohibited{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            commission_to, _ = h27_resolved_discriminator(text, "COMMISSION_TO")
            if normalized_addr(from_addr) != normalized_addr(commission_to):
                errors.append(
                    f"{relname(item)}: commissioned pair receipt FROM {from_addr!r} does not equal "
                    f"COMMISSION_TO {commission_to!r}{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            candidates = marker_universe("DELEGATED_DISPATCH_AUTHORITY", commission_id, order)
            selected, tied = latest_at_one_position(candidates)
            if not candidates:
                errors.append(
                    f"{relname(item)}: commission {commission_id!r} has no earlier grant-universe member"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            if selected is None:
                errors.append(
                    f"{relname(item)}: commission {commission_id!r} has {tied} grant-universe candidates at the "
                    f"latest order position; ambiguity fails closed{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            conflict = selected_conflict(
                selected,
                ("FROM", "PHASE", "AUTHORITY", "TO", "DELEGATED_DISPATCH_AUTHORITY") + H27_COMMISSION_SURFACE,
            )
            if conflict:
                errors.append(
                    f"{relname(item)}: selected grant-universe member {relname(selected)} has conflicting {conflict} "
                    f"occurrences; universe selection refuses first-value resolution{H27_COMMISSION_RECEIPT_RULE}"
                )
                eager_charter, _tied = latest_at_one_position(
                    charter_candidates(charter_doc_id, commission_id, selected[1])
                )
                if eager_charter is not None:
                    stage_conflict(
                        eager_charter,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                        item, "latest charter revision", H27_COMMISSION_RECEIPT_RULE,
                    )
                    eager_approval, _tied = latest_at_one_position(
                        review_candidates(eager_charter, charter_doc_id, commission_id, selected[1])
                    )
                    if eager_approval is not None:
                        stage_conflict(
                            eager_approval,
                            ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                             "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                            item, "charter review", H27_COMMISSION_RECEIPT_RULE,
                        )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_grant_shape_ok(selected):
                errors.append(
                    f"{relname(item)}: latest grant-universe member {relname(selected)} fails stage-(c) shape; "
                    f"marker-bearing malformed grant shadows and fails{H27_COMMISSION_GRANT_RULE}"
                )
                continue
            if h27_commission_surface(text) != h27_commission_surface(selected[4]):
                errors.append(
                    f"{relname(item)}: latest grant-universe member is not targeted to this pair planner with a "
                    f"byte-equal surface{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            grant_parent_id, grant_parent_conflict = h27_resolved_discriminator(
                selected[4], "PARENT_DISPATCH_ID",
            )
            if grant_parent_conflict:
                errors.append(
                    f"{relname(item)}: selected grant-universe member {relname(selected)} has conflicting "
                    f"PARENT_DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            if grant_parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: grant PARENT_DISPATCH_ID is mandatory and must resolve an approving review"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            charter = latest_stage(
                charter_candidates(charter_doc_id, commission_id, selected[1]), item,
                "charter revisions", H27_COMMISSION_RECEIPT_RULE,
            )
            if charter is False:
                continue
            if charter is None:
                errors.append(
                    f"{relname(item)}: selected latest charter revision <none> fails stage-(b) shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            charter_conflicted = stage_conflict(
                charter,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND") + H27_COMMISSION_SURFACE,
                item, "latest charter revision", H27_COMMISSION_RECEIPT_RULE,
            )
            if charter_conflicted:
                eager_approval, _tied = latest_at_one_position(
                    review_candidates(charter, charter_doc_id, commission_id, selected[1])
                )
                if eager_approval is not None:
                    stage_conflict(
                        eager_approval,
                        ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                         "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                        item, "charter review", H27_COMMISSION_RECEIPT_RULE,
                    )
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_charter_shape_ok(charter):
                errors.append(
                    f"{relname(item)}: selected latest charter revision {relname(charter)} fails stage-(b) shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            approval = latest_stage(
                review_candidates(charter, charter_doc_id, commission_id, selected[1]), item,
                "charter reviews", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval is False:
                continue
            if approval is None:
                errors.append(
                    f"{relname(item)}: selected grant has no earlier charter review"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            approval_conflicted = stage_conflict(
                approval,
                ("FROM", "PHASE", "AUTHORITY", "DISPATCH_ID", "PARENT_DISPATCH_ID", "DESIGN_DOC_ID",
                 "DESIGN_RECORD_KIND", "DESIGN_REVIEW_VERDICT") + H27_COMMISSION_SURFACE,
                item, "charter review", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval_conflicted:
                authorization_conflict_for(item, commission_id)
                continue
            if not h27_commission_approval_shape_ok(approval):
                errors.append(
                    f"{relname(item)}: selected charter review {relname(approval)} fails stage-(b) review shape"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            approval_verdict, _ = h27_resolved_discriminator(approval[4], "DESIGN_REVIEW_VERDICT")
            if approval_verdict != "approve":
                errors.append(
                    f"{relname(item)}: selected charter review {relname(approval)} requires "
                    f"DESIGN_REVIEW_VERDICT: approve; got {approval_verdict!r}{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            if not same_owner_reviewer(approval, charter):
                charter_owner = from_owner(h27_single_from(charter[4]))
                errors.append(
                    f"{relname(item)}: charter review must be FROM {same_owner_addr(charter_owner, 'master-reviewer')}, "
                    f"the charter owner's tier reviewer{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            grant_parent = resolve_ancestry(
                grant_parent_id, selected[1], item, "grant parent", H27_COMMISSION_RECEIPT_RULE,
            )
            if grant_parent is None:
                continue
            if grant_parent is not approval:
                errors.append(
                    f"{relname(item)}: selected grant does not parent to the latest charter review before the grant"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            approval_parent_id, approval_parent_conflict = h27_resolved_discriminator(
                approval[4], "PARENT_DISPATCH_ID",
            )
            if approval_parent_conflict:
                errors.append(
                    f"{relname(item)}: selected charter review {relname(approval)} has conflicting "
                    f"PARENT_DISPATCH_ID occurrences; ancestry resolution refuses first-value resolution"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            if approval_parent_id in {None, ""}:
                errors.append(
                    f"{relname(item)}: selected charter review {relname(approval)} lacks a usable "
                    f"PARENT_DISPATCH_ID{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            approval_parent = resolve_ancestry(
                approval_parent_id, approval[1], item, "approval parent", H27_COMMISSION_RECEIPT_RULE,
            )
            if approval_parent is None:
                continue
            if approval_parent is not charter:
                errors.append(
                    f"{relname(item)}: approval parent {approval_parent_id!r} does not resolve to the latest "
                    f"charter revision before the review"
                    f"{H27_COMMISSION_RECEIPT_RULE}"
                )
                continue
            authorization = authorization_for(item, commission_id, H27_COMMISSION_RECEIPT_RULE)
            if authorization is None:
                continue
            if not charter_parent_authorization(
                charter, authorization, item, H27_COMMISSION_RECEIPT_RULE,
            ):
                continue
            continue

        errors.append(
            f"{relname(item)}: commission machine engaged but relay is not consumed by any Task 9.5a stage"
            f"{H27_COMMISSION_RULE}"
        )
    return errors


# --- xroot: declared cross-root design edge ---

XROOT_PREFIX = "declared design edge failed verification"
XROOT_TRIGGERS = (
    "DESIGN_SOURCE_REPO",
    "DESIGN_SOURCE_COMMIT",
    "DESIGN_SOURCE_ROOT",
    "DESIGN_SOURCE_PATH",
    "DESIGN_OWNER",
)
XROOT_REQUIRED = XROOT_TRIGGERS + ("DESIGN_DOC_ID", "DESIGN_SHA256")
XROOT_GIT_TIMEOUT_SECONDS = 5
XROOT_MAX_TREE_DEPTH = 256


class XrootAxisError(Exception):
    def __init__(self, axis: str, detail: str) -> None:
        super().__init__(detail)
        self.axis = axis
        self.detail = detail


def xroot_error(axis: str, detail: str) -> str:
    return f"{XROOT_PREFIX}: {axis}: {detail}"


def xroot_design_engaged(text: str) -> bool:
    """Whether a PLAN-phase relay carries any declared-edge trigger."""
    fields = header_fields(text)
    if fields.get("PHASE") != "PLAN":
        return False
    return any(h27_occurrences(text, key) for key in XROOT_TRIGGERS)


def xroot_pair_plan_applicable(text: str) -> bool:
    """Whether text is inside the locked A5 pair-Planner PLAN boundary."""
    fields = header_fields(text)
    if fields.get("PHASE") != "PLAN":
        return False
    from_values = h27_occurrences(text, "FROM")
    if len(from_values) != 1:
        return False
    from_addrs = split_addresses(from_values[0])
    if len(from_addrs) != 1 or canonical_role(from_role(from_addrs[0])) != "planner":
        return False
    return any(h27_occurrences(text, key) for key in XROOT_TRIGGERS)


def xroot_pair_plan_carrier_shape(text: str) -> tuple[bool, bool]:
    """Return the ROLE and FROM halves of the canonical carrier shape."""
    fields = header_fields(text)
    role_ok = canonical_role(ROLE_TO_ADDRESS_ROLE.get(fields.get("ROLE", ""))) == "planner"
    from_values = h27_occurrences(text, "FROM")
    from_addrs = split_addresses(from_values[0]) if len(from_values) == 1 else []
    from_ok = len(from_addrs) == 1 and canonical_role(from_role(from_addrs[0])) == "planner"
    return role_ok, from_ok


def xroot_field_conflicts(text: str, keys: tuple[str, ...]) -> List[str]:
    conflicts: List[str] = []
    for key in keys:
        values = {value.strip() for value in h27_occurrences(text, key)}
        if len(values) > 1:
            conflicts.append(key)
    return conflicts


def xroot_declaration(fields: Dict[str, str], text: str) -> Dict[str, str] | None:
    if not xroot_design_engaged(text):
        return None
    return {
        key: fields.get(key, "").strip()
        for key in XROOT_REQUIRED + ("DESIGN_LOCK_ID", "DESIGN_RECORD_KIND")
    }


def xroot_git(repo: Path, *args: str, axis: str = "repository", binary: bool = False):
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            timeout=XROOT_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise XrootAxisError("repository", "git executable is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise XrootAxisError(axis, "Git object query timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise XrootAxisError(axis, "Git object query failed") from exc
    return completed.stdout if binary else completed.stdout.strip()


def xroot_repository_and_commit(
    path: Path,
    declaration: Dict[str, str],
    repo_key: str = "DESIGN_SOURCE_REPO",
    commit_key: str = "DESIGN_SOURCE_COMMIT",
) -> tuple[Path, str]:
    try:
        consuming_top = Path(
            xroot_git(path, "rev-parse", "--path-format=absolute", "--show-toplevel")
        )
    except XrootAxisError as exc:
        if exc.axis == "repository" and exc.detail == "git executable is unavailable":
            raise
        raise XrootAxisError("repository", "consuming relay root is not inside a Git repository") from exc
    declared_repo = Path(declaration[repo_key])
    candidate = declared_repo if declared_repo.is_absolute() else consuming_top / declared_repo
    candidate = Path(os.path.realpath(candidate))
    if not candidate.is_dir():
        raise XrootAxisError("repository", f"{repo_key} does not name an existing directory")
    try:
        reported_top = Path(
            xroot_git(candidate, "rev-parse", "--path-format=absolute", "--show-toplevel")
        )
    except XrootAxisError as exc:
        if exc.axis == "repository" and exc.detail == "git executable is unavailable":
            raise
        raise XrootAxisError("repository", f"{repo_key} is not a Git repository top level") from exc
    if Path(os.path.realpath(reported_top)) != candidate:
        raise XrootAxisError(
            "repository",
            f"{repo_key} resolves inside a Git repository but not to its top level",
        )
    commit = declaration[commit_key]
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise XrootAxisError(
            "commit", f"{commit_key} must be a full lowercase 40-hex object id"
        )
    try:
        object_type = xroot_git(candidate, "cat-file", "-t", commit, axis="commit")
    except XrootAxisError as exc:
        if exc.detail == "Git object query failed":
            raise XrootAxisError("commit", f"{commit_key} object is unavailable") from exc
        raise
    if object_type != "commit":
        raise XrootAxisError(
            "commit", f"{commit_key} object type is {object_type}, not commit"
        )
    return candidate, commit


def xroot_valid_relpath(value: str) -> bool:
    parts = value.split("/")
    return bool(
        value
        and not value.startswith("/")
        and "\x00" not in value
        and all(part not in {"", ".", ".."} and not part.startswith(":") for part in parts)
    )


def xroot_commit_tree(repo: Path, commit: str) -> str:
    content = xroot_git(repo, "cat-file", "-p", commit, axis="commit")
    first = content.splitlines()[0] if content else ""
    if re.fullmatch(r"tree [0-9a-f]{40}", first) is None:
        raise XrootAxisError("commit", "commit object lacks a canonical root tree")
    return first.split(" ", 1)[1]


def xroot_tree_entries(repo: Path, tree_oid: str) -> dict[bytes, tuple[str, str]]:
    raw = xroot_git(repo, "cat-file", "tree", tree_oid, axis="repository", binary=True)
    entries: dict[bytes, tuple[str, str]] = {}
    offset = 0
    while offset < len(raw):
        space = raw.find(b" ", offset)
        nul = raw.find(b"\x00", space + 1)
        if space < 0 or nul < 0 or nul + 21 > len(raw):
            raise XrootAxisError("repository", "pinned tree object has malformed entry bytes")
        try:
            mode = raw[offset:space].decode("ascii")
        except UnicodeDecodeError as exc:
            raise XrootAxisError(
                "repository", "pinned tree object has malformed entry bytes"
            ) from exc
        if mode not in {"40000", "040000", "100644", "100755", "120000", "160000"}:
            raise XrootAxisError("repository", "pinned tree object has malformed entry bytes")
        name = raw[space + 1:nul]
        if not name:
            raise XrootAxisError("repository", "pinned tree object has malformed entry bytes")
        oid = raw[nul + 1:nul + 21].hex()
        if mode == "120000":
            kind = "symlink"
        elif mode in {"40000", "040000"}:
            kind = "tree"
        else:
            kind = "blob"
        entries[name] = (kind, oid)
        offset = nul + 21
    return entries


def xroot_walk(
    repo: Path,
    root_tree: str,
    relpath: str,
    field_name: str,
) -> tuple[str, str] | None:
    tree_oid = root_tree
    parts = relpath.split("/")
    for index, part in enumerate(parts):
        entry = xroot_tree_entries(repo, tree_oid).get(part.encode("utf-8"))
        if entry is None:
            return None
        kind, oid = entry
        if kind == "symlink":
            raise XrootAxisError(
                "declaration", f"{field_name} traverses symlink component {part}"
            )
        if index < len(parts) - 1:
            if kind != "tree":
                return None
            tree_oid = oid
    return kind, oid


def xroot_paths(
    repo: Path,
    commit: str,
    declaration: Dict[str, str],
) -> tuple[str, str]:
    for key in ("DESIGN_SOURCE_ROOT", "DESIGN_SOURCE_PATH"):
        if not xroot_valid_relpath(declaration[key]):
            raise XrootAxisError("declaration", f"{key} must be a literal relative path")
    commit_tree = xroot_commit_tree(repo, commit)
    root_entry = xroot_walk(
        repo, commit_tree, declaration["DESIGN_SOURCE_ROOT"], "DESIGN_SOURCE_ROOT"
    )
    if root_entry is None:
        raise XrootAxisError(
            "authority-population", "DESIGN_SOURCE_ROOT does not exist at pinned commit"
        )
    root_kind, root_oid = root_entry
    if root_kind != "tree":
        raise XrootAxisError(
            "authority-population", f"DESIGN_SOURCE_ROOT resolves to {root_kind}, not tree"
        )
    path_entry = xroot_walk(
        repo, commit_tree, declaration["DESIGN_SOURCE_PATH"], "DESIGN_SOURCE_PATH"
    )
    if path_entry is None:
        raise XrootAxisError("byte", "DESIGN_SOURCE_PATH does not exist at pinned commit")
    path_kind, blob_oid = path_entry
    if path_kind != "blob":
        raise XrootAxisError("byte", f"DESIGN_SOURCE_PATH resolves to {path_kind}, not blob")
    return root_oid, blob_oid


class XrootRelay(NamedTuple):
    name: str
    fields: Dict[str, str]
    text: str
    stamp: datetime.datetime | None
    stamp_raw: str | None


def xroot_relays(repo: Path, root_tree: str) -> List[XrootRelay]:
    relays: List[XrootRelay] = []
    stack = [("tree", root_tree, "", b"", 0)]
    while stack:
        kind, oid, relname, name_bytes, depth = stack.pop()
        if kind == "tree":
            if depth > XROOT_MAX_TREE_DEPTH:
                raise XrootAxisError(
                    "repository",
                    f"DESIGN_SOURCE_ROOT exceeds maximum tree depth {XROOT_MAX_TREE_DEPTH}",
                )
            entries = sorted(xroot_tree_entries(repo, oid).items(), reverse=True)
            for child_name, (child_kind, child_oid) in entries:
                try:
                    decoded = child_name.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise XrootAxisError(
                        "authority-population",
                        "DESIGN_SOURCE_ROOT contains a non-UTF-8 filename",
                    ) from exc
                child_relname = f"{relname}/{decoded}" if relname else decoded
                stack.append((child_kind, child_oid, child_relname, child_name, depth + 1))
            continue
        name = name_bytes.decode("utf-8")
        if name == "INDEX.md":
            continue
        if kind == "symlink":
            raise XrootAxisError(
                "authority-population",
                f"DESIGN_SOURCE_ROOT contains symlink carrier {relname}",
            )
        if not name.endswith(".md"):
            continue
        blob = xroot_git(repo, "cat-file", "blob", oid, axis="authority-population", binary=True)
        try:
            relay_text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise XrootAxisError(
                "authority-population", f"authority carrier {relname} is not UTF-8"
            ) from exc
        status, stamp, raw, _utc = filename_timestamp(name)
        relays.append(
            XrootRelay(
                relname,
                header_fields(relay_text),
                relay_text,
                stamp if status == "ok" else None,
                raw if status == "ok" else None,
            )
        )
    return relays


def xroot_target_ok(relay: XrootRelay, key: str, expected: str) -> bool:
    values = [value.strip() for value in h27_occurrences(relay.text, key)]
    if expected not in values:
        return False
    if len(set(values)) > 1:
        raise XrootAxisError(
            "authority-population", f"conflicting {key} occurrences in {relay.name}"
        )
    return True


def xroot_unique_latest(
    candidates: List[XrootRelay],
    *,
    empty_axis: str,
    empty_detail: str,
    carrier_kind: str,
) -> XrootRelay:
    if not candidates:
        raise XrootAxisError(empty_axis, empty_detail)
    for relay in candidates:
        if relay.stamp is None:
            raise XrootAxisError(
                "ordering", f"authority carrier {relay.name} lacks a filename timestamp"
            )
    latest_stamp = max(relay.stamp for relay in candidates)
    latest = [relay for relay in candidates if relay.stamp == latest_stamp]
    if len(latest) != 1:
        raw = latest[0].stamp_raw or "unknown"
        raise XrootAxisError(
            "ambiguity", f"multiple latest {carrier_kind} share timestamp {raw}"
        )
    return latest[0]


def xroot_selected_conflicts(relay: XrootRelay, keys: tuple[str, ...]) -> None:
    for key in keys:
        values = h27_occurrences(relay.text, key)
        if len(set(value.strip() for value in values)) > 1:
            raise XrootAxisError(
                "authority-population",
                f"conflicting {key} occurrences in selected carrier {relay.name}",
            )


def xroot_selected_role(relay: XrootRelay, kind: str) -> tuple[str, str]:
    if not relay.fields.get("ROLE"):
        raise XrootAxisError("authority-population", f"selected {kind} {relay.name} lacks ROLE")
    role = canonical_role(ROLE_TO_ADDRESS_ROLE.get(relay.fields["ROLE"]))
    from_addrs = split_addresses(relay.fields.get("FROM"))
    if (
        role is None
        or len(from_addrs) != 1
        or role != canonical_role(from_role(from_addrs[0]))
    ):
        raise XrootAxisError(
            "authority-population", f"selected {kind} {relay.name} ROLE does not match FROM"
        )
    return role, from_owner(from_addrs[0]) or ""


def xroot_validate_selected(
    relay: XrootRelay,
    *,
    kind: str,
    owner: str,
) -> tuple[str, str]:
    conflict_keys = (
        "ROLE",
        "FROM",
        "AUTHORITY",
        "DISPATCH_ID",
        "DESIGN_RECORD_KIND",
    )
    if kind == "origin":
        conflict_keys += ("PARENT_DISPATCH_ID",)
    else:
        conflict_keys += ("DESIGN_REVIEW_VERDICT",)
    xroot_selected_conflicts(relay, conflict_keys)
    if not relay.fields.get("DESIGN_RECORD_KIND"):
        raise XrootAxisError(
            "authority-population", f"selected {kind} {relay.name} lacks DESIGN_RECORD_KIND"
        )
    if relay.fields["DESIGN_RECORD_KIND"] != "design-doc":
        raise XrootAxisError(
            "authority-population",
            f"selected {kind} {relay.name} DESIGN_RECORD_KIND is not design-doc",
        )
    role, relay_owner = xroot_selected_role(relay, kind)
    if not relay.fields.get("DISPATCH_ID"):
        raise XrootAxisError(
            "authority-population", f"selected {kind} {relay.name} lacks DISPATCH_ID"
        )
    expected_authority = "design-only" if kind == "origin" else "review-only"
    if relay.fields.get("AUTHORITY") != expected_authority:
        raise XrootAxisError(
            "authority-population",
            f"selected {kind} {relay.name} AUTHORITY is not {expected_authority}",
        )
    if relay_owner != owner:
        raise XrootAxisError(
            "authority-owner",
            f"selected {kind} owner {relay_owner} does not match DESIGN_OWNER {owner}",
        )
    return role, relay.fields["DISPATCH_ID"]


def xroot_authority(
    repo: Path,
    root_tree: str,
    declaration: Dict[str, str],
) -> None:
    relays = xroot_relays(repo, root_tree)
    doc_id = declaration["DESIGN_DOC_ID"]
    origins = [
        relay for relay in relays
        if xroot_target_ok(relay, "PHASE", "DESIGN")
        and xroot_target_ok(relay, "DESIGN_DOC_ID", doc_id)
    ]
    origin = xroot_unique_latest(
        origins,
        empty_axis="authority-population",
        empty_detail=f"no DESIGN origin found for DESIGN_DOC_ID {doc_id}",
        carrier_kind="DESIGN origins",
    )
    origin_role, origin_dispatch = xroot_validate_selected(
        origin, kind="origin", owner=declaration["DESIGN_OWNER"]
    )
    reviews = [
        relay for relay in relays
        if xroot_target_ok(relay, "PHASE", "DESIGN-REVIEW")
        and xroot_target_ok(relay, "DESIGN_DOC_ID", doc_id)
        and xroot_target_ok(relay, "PARENT_DISPATCH_ID", origin_dispatch)
    ]
    review = xroot_unique_latest(
        reviews,
        empty_axis="authority-parent",
        empty_detail=f"no DESIGN-REVIEW found for selected origin {origin_dispatch}",
        carrier_kind="DESIGN-REVIEW carriers",
    )
    review_role, _review_dispatch = xroot_validate_selected(
        review, kind="review", owner=declaration["DESIGN_OWNER"]
    )
    peer_roles = {
        "planner": {"implementer", "pair-implementer"},
        "pair-planner": {"implementer", "pair-implementer"},
        "domain-planner": {"domain-reviewer"},
        "master-planner": {"master-reviewer"},
    }
    if review_role not in peer_roles.get(origin_role, set()):
        raise XrootAxisError(
            "authority-population",
            f"selected review {review.name} is not a peer for selected origin role {origin_role}",
        )
    verdict = review.fields.get("DESIGN_REVIEW_VERDICT")
    if verdict not in DESIGN_REVIEW_VERDICT_VALUES:
        raise XrootAxisError(
            "authority-population",
            f"selected review {review.name} has invalid DESIGN_REVIEW_VERDICT",
        )
    if review.stamp is None or origin.stamp is None or review.stamp <= origin.stamp:
        raise XrootAxisError("ordering", "selected review is not later than selected origin")
    if verdict != "approve":
        raise XrootAxisError(
            "authority-verdict",
            f"selected review {review.name} verdict {verdict} is not approve",
        )


def xroot_plan_gate(
    path: Path,
    f: Path,
    fields: Dict[str, str],
    text: str,
    result: LintResult,
) -> bool:
    source_keys = ("PLAN_SOURCE_REPO", "PLAN_SOURCE_COMMIT")
    if not any(h27_occurrences(text, key) for key in source_keys):
        return False
    rel = f.relative_to(path)
    conflicts = xroot_field_conflicts(text, source_keys)
    if conflicts:
        result.error(
            f"{rel}: {xroot_error('declaration', f'conflicting {conflicts[0]} values')}"
        )
        return True
    declaration = {key: fields.get(key, "").strip() for key in source_keys}
    missing = [key for key in source_keys if not declaration[key]]
    if missing:
        result.error(f"{rel}: {xroot_error('declaration', 'missing ' + ', '.join(missing))}")
        return True
    locks = []
    for key in ("DESIGN_LOCK_ID", "PLAN_LOCK_ID"):
        lock = fields.get(key, "")
        lock_path, separator, annotation = lock.partition(" @ ")
        if not lock_path or ("/" not in lock_path and not lock_path.endswith(".md")):
            continue
        locks.append((key, lock_path, separator, annotation))
    if not locks:
        return True
    try:
        repo, commit = xroot_repository_and_commit(
            path,
            declaration,
            repo_key="PLAN_SOURCE_REPO",
            commit_key="PLAN_SOURCE_COMMIT",
        )
        commit_tree = xroot_commit_tree(repo, commit)
        for key, lock_path, separator, annotation in locks:
            if not xroot_valid_relpath(lock_path):
                raise XrootAxisError(
                    "declaration", f"{key} path must be a literal relative path"
                )
            entry = xroot_walk(repo, commit_tree, lock_path, key)
            if entry is None:
                raise XrootAxisError("byte", f"{key} path does not exist at pinned commit")
            kind, blob_oid = entry
            if kind != "blob":
                raise XrootAxisError("byte", f"{key} path resolves to {kind}, not blob")
            blob = xroot_git(repo, "cat-file", "blob", blob_oid, axis="byte", binary=True)
            if separator:
                if re.fullmatch(r"[0-9a-f]{64}", annotation) is None:
                    raise XrootAxisError(
                        "byte", f"{key} annotation is not a full lowercase 64-hex digest"
                    )
                if hashlib.sha256(blob).hexdigest() != annotation:
                    raise XrootAxisError(
                        "byte", f"{key} annotation digest does not match pinned blob bytes"
                    )
    except XrootAxisError as exc:
        result.error(f"{rel}: {xroot_error(exc.axis, exc.detail)}")
    except Exception:
        result.error(
            f"{rel}: {xroot_error('repository', 'unexpected Git or object parse failure')}"
        )
    return True


def xroot_verify(path: Path, declaration: Dict[str, str]) -> None:
    repo, commit = xroot_repository_and_commit(path, declaration)
    relay_tree, blob_oid = xroot_paths(repo, commit, declaration)
    declared_digest = declaration["DESIGN_SHA256"]
    if re.fullmatch(r"[0-9a-f]{64}", declared_digest) is None:
        raise XrootAxisError("byte", "DESIGN_SHA256 must be a full lowercase 64-hex digest")
    blob = xroot_git(repo, "cat-file", "blob", blob_oid, axis="byte", binary=True)
    if hashlib.sha256(blob).hexdigest() != declared_digest:
        raise XrootAxisError("byte", "DESIGN_SHA256 does not match pinned blob bytes")
    xroot_authority(repo, relay_tree, declaration)


def xroot_design_gate(
    path: Path,
    f: Path,
    fields: Dict[str, str],
    text: str,
    phases,
    result: LintResult,
) -> bool:
    declaration = xroot_declaration(fields, text)
    if declaration is None:
        return False
    rel = f.relative_to(path)
    role_ok, from_ok = xroot_pair_plan_carrier_shape(text)
    if not role_ok:
        result.error(
            f"{rel}: {xroot_error('declaration', 'declared edge carrier ROLE must be pair-Planner class')}"
        )
        return True
    if not from_ok:
        result.error(
            f"{rel}: {xroot_error('declaration', 'declared edge carrier FROM must be exactly one pair-Planner-class address')}"
        )
        return True
    conflicts = xroot_field_conflicts(
        text, XROOT_REQUIRED + ("DESIGN_LOCK_ID", "DESIGN_RECORD_KIND")
    )
    if conflicts:
        result.error(
            f"{rel}: {xroot_error('declaration', f'conflicting {conflicts[0]} values')}"
        )
        return True
    missing = [key for key in XROOT_REQUIRED if not declaration[key]]
    if not declaration["DESIGN_LOCK_ID"]:
        missing.append("DESIGN_LOCK_ID")
    if missing:
        result.error(
            f"{rel}: {xroot_error('declaration', 'missing ' + ', '.join(missing))}"
        )
        return True
    if declaration["DESIGN_RECORD_KIND"] != "design-doc":
        result.error(
            f"{rel}: {xroot_error('declaration', 'DESIGN_RECORD_KIND must be design-doc under a declared edge')}"
        )
        return True
    for local_f, _order, local_phase, local_fields, _local_text in phases:
        if local_f == f or local_phase != "DESIGN":
            continue
        local_from = split_addresses(local_fields.get("FROM"))
        if (
            local_fields.get("DESIGN_DOC_ID") == declaration["DESIGN_DOC_ID"]
            and len(local_from) == 1
            and from_owner(local_from[0]) == declaration["DESIGN_OWNER"]
        ):
            result.error(
                f"{rel}: {xroot_error('ambiguity', 'same-owner local DESIGN carries DESIGN_DOC_ID ' + declaration['DESIGN_DOC_ID'])}"
            )
            return True
    try:
        xroot_verify(path, declaration)
    except XrootAxisError as exc:
        result.error(f"{rel}: {xroot_error(exc.axis, exc.detail)}")
    except Exception:
        result.error(
            f"{rel}: {xroot_error('repository', 'unexpected Git or object parse failure')}"
        )
    return True


def lint_relay_root(path: Path, *, template_mode: bool = False) -> LintResult:
    path = Path(os.path.realpath(path))
    result = LintResult()
    all_md = sorted((p for p in path.rglob("*.md") if p.is_file() and ".engine" not in p.relative_to(path).parts),
                    key=lambda p: relay_order_key(p, path))
    index_files = [p for p in all_md if p.name == "INDEX.md"]
    files = [p for p in all_md if p.name != "INDEX.md"]
    if not all_md:
        result.error(f"no .md relay files found under {path}")
        return result
    per_file: List[Tuple[Path, LintResult]] = [(f, lint_file(f, template_mode=template_mode, artifact_root=path)) for f in files]
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
        phases.append((f, relay_order_key(f, path), fields.get("PHASE", ""), fields, tx))
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

    lock_routes, lock_lifecycle_errors = h27_lock_lifecycle_precompute(path, phases)
    for lock_lifecycle_error in lock_lifecycle_errors:
        result.error(lock_lifecycle_error)

    for receiving_seat_error in h27_receiving_seat_errors(path, phases):
        result.error(receiving_seat_error)

    for commission_error in h27_commission_precompute(path, phases):
        result.error(commission_error)

    # Design-review lineage gate. A pair-Planner PLAN that locks a
    # design-doc-backed design must parent to an approving Implementer
    # DESIGN-REVIEW relay, and that review must parent to the DESIGN relay that
    # introduced the matching DESIGN_DOC_ID.
    for f, order, phase, fields, text in phases:
        if phase != "PLAN":
            continue
        if xroot_design_gate(path, f, fields, text, phases, result):
            continue
        if (
            any(h27_occurrences(text, key) for key in ("PLAN_SOURCE_REPO", "PLAN_SOURCE_COMMIT"))
            and fields.get("DESIGN_LOCK_ID")
            and (
                "/" in fields["DESIGN_LOCK_ID"].split(" @ ", 1)[0]
                or fields["DESIGN_LOCK_ID"].split(" @ ", 1)[0].endswith(".md")
            )
        ):
            continue
        if lock_routes.get(f, "pair") != "pair": continue
        if not fields.get("DESIGN_LOCK_ID"):
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
        lock_text = read(f)
        fields = header_fields(lock_text)
        plan_source_engaged = xroot_plan_gate(path, f, fields, lock_text, result)
        for key in ("DESIGN_LOCK_ID", "PLAN_LOCK_ID"):
            val = fields.get(key)
            bare = val.split(" @ ", 1)[0] if val else ""
            if plan_source_engaged and bare and ("/" in bare or bare.endswith(".md")):
                continue
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
            artifact_root=args.relay_root,
        )
        print_result(str(path), r)
        overall.errors.extend(r.errors)
        overall.warnings.extend(r.warnings)
    return 1 if overall.errors else 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
