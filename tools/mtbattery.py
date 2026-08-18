#!/usr/bin/env python3
"""Run the data-only relay-lint mutation battery."""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
DEFAULT_SUITE = (sys.executable, "tools/check-relay-lint-fixtures.py")
# A fixture result has the harness's fixed outcome fields. Other status lines
# may legitimately end in PASS/FAIL and must not enter the battery denominator.
RESULT_LINE = re.compile(r"^(?:--relay-root |--index )?.+: expected=[01] observed=[01] (PASS|FAIL)$")
RESULT_DETAIL = re.compile(r"^(?P<label>(?:--relay-root |--index )?.+): expected=[01] observed=[01] (?P<outcome>PASS|FAIL)$")

# This map is deliberately owned by the runner rather than inferred from the
# arm table: it makes a claimed ledger-row/anchor pairing independently
# checkable.  Several rows share a function, but no row may borrow another
# row's predicate merely by changing its label.
ROW_SELECTORS = {
    "C1": ("h27_master_seat_errors", "Call", "own_line_dispatch_present(text)"),
    "C2": ("h27_master_seat_errors", "Call", "own_line_merge_present(text)"),
    "C3": ("h27_master_seat_errors", "Compare", "resolved.get('DESIGN_RECORD_KIND') == 'direct-override'"),
    "C4": {
        ("h27_group_and_route", "BoolOp", "foreign_groups and pair_group"),
        ("h27_select_origins", "Compare", "order >= before_order"),
        ("h27_group_and_route", "Compare", "tier == 'foreign'"),
        ("h27_group_and_route", "Compare", "len(foreign_groups) > 1"),
        ("h27_group_and_route", "Compare", "h27_tier_classification(consumer_address) == 'foreign'"),
        ("h27_foreign_lock_errors", "Call", "h27_same_position_latest(origins)"),
    },
    "C5": {
        ("h27_foreign_lock_errors", "Compare", "origin_role in {'master-reviewer', 'domain-reviewer'}"),
        ("h27_foreign_lock_errors", "BoolOp", "origin_role in {'master-planner', 'domain-planner'} and origin_phase == 'DESIGN' and (origin_authority == 'design-only')"),
        ("h27_foreign_lock_errors", "BoolOp", "origin_role in {'master-planner', 'domain-planner'} and origin_phase == 'AUDIT' and (origin_authority in {'review-only', 'report-only'})"),
        ("h27_foreign_lock_errors", "Compare", "origin_kind is None"),
        ("h27_foreign_lock_errors", "BoolOp", "origin_kind not in {'design-doc', 'audit-record'} or origin_doc != lock_id or (not origin_id)"),
    },
    "C5a": {
        ("h27_lock_lifecycle_precompute", "Compare", "route == 'foreign'"),
    },
    "C5b": ("h27_foreign_lock_errors", "Compare", "review_kind != origin_kind"),
    "C5c": ("h27_foreign_lock_errors", "BoolOp", "consumer_kind is not None and consumer_kind != origin_kind"),
    "C5d": ("h27_foreign_lock_errors", "Compare", "normalized_addr(review_from) != expected_review_from"),
    "C6": {
        ("h27_foreign_lock_errors", "Call", "h27_same_position_latest(origins)"),
        ("h27_foreign_lock_errors", "Compare", "review_order >= consumer_order"),
        ("h27_foreign_lock_errors", "Compare", "origin_id not in review_parents"),
        ("h27_foreign_lock_errors", "Call", "h27_same_position_latest(review_candidates)"),
        ("h27_foreign_lock_errors", "Compare", "review_verdict != 'approve'"),
    },
    "C7": {
        ("h27_commission_auth_shape_ok", "Compare", "carrier == 'yes'"),
        ("h27_commission_auth_shape_ok", "Call", "h27_direct_commission_grantor(from_addr)"),
        ("h27_commission_auth_shape_ok", "Compare", "phase == 'PLAN'"),
        ("h27_commission_auth_shape_ok", "Compare", "authority == 'plan-only'"),
        ("h27_commission_auth_shape_ok", "Compare", "len(to_addrs) == 1"),
        ("h27_commission_auth_shape_ok", "Compare", "from_role(to_addrs[0]) == 'master-planner'"),
    },
    "C8": {
        ("h27_commission_surface_complete", "Call", "all((value not in {None, ''} for value in h27_commission_surface(text)))"),
        ("h27_commission_precompute", "BoolOp", "carrier_presence == {'COMMISSION_ID'} and (not dda_presence) and (not commission_id_conflict) and (commission_id not in {None, ''})"),
        ("h27_commission_precompute", "Compare", "h27_commission_surface(text) != h27_commission_surface(authorization[4])"),
        ("h27_commission_precompute", "Set", "{h27_commission_surface(text), h27_commission_surface(charter[4]), h27_commission_surface(authorization[4])}"),
        ("h27_commission_precompute", "Set", "{h27_commission_surface(text), h27_commission_surface(approval[4]), h27_commission_surface(charter[4]), h27_commission_surface(authorization[4])}"),
    },
    "C8a": {
        ("h27_pair_planner_address", "BoolOp", "len(addresses) != 1 or canonical_role(from_role(addresses[0])) != 'planner'"),
        ("h27_commission_precompute", "Call", "h27_occurrences(item[4], marker)"),
        ("h27_commission_precompute", "Compare", "commission_id in h27_occurrences(item[4], 'COMMISSION_ID')"),
        ("h27_commission_precompute", "Compare", "len(latest) == 1"),
    },
    "C8b": ("h27_commission_charter_shape_ok", "Compare", "authority == 'design-only'"),
    "C8c": {
        ("h27_commission_id_grammatical", "BoolOp", "value is not None and H27_COMMISSION_ID_RE.fullmatch(value) is not None"),
        ("h27_commission_id_grammatical", "Call", "H27_COMMISSION_ID_RE.fullmatch(value)"),
        ("h27_commission_precompute", "Compare", "charter_doc_id not in {None, f'CH-{commission_id}'}"),
        ("h27_commission_precompute", "Compare", "design_doc_id != charter_doc_id"),
        ("h27_commission_precompute", "SetComp", "{key for key in H27_COMMISSION_CARRIERS if h27_occurrences(text, key)}"),
    },
    "C9": ("h27_commission_precompute", "Call", "authorization_for(item, charter_id, H27_COMMISSION_CHARTER_RULE)"),
    "C9a": ("h27_commission_precompute", "Call", "resolve_ancestry(parent_id, order, item, 'charter parent', H27_COMMISSION_CHARTER_RULE)"),
    "C10": {
        ("h27_commission_precompute", "Call", "authorization_for(item, charter_id, H27_COMMISSION_GRANT_RULE)"),
        ("h27_commission_precompute", "Call", "authorization_for(item, commission_id, H27_COMMISSION_RECEIPT_RULE)"),
    },
    "C11": {
        ("h27_receipt_self_grants", "BoolOp", "is_receipt and 'yes' in h27_occurrences(text, 'DELEGATED_DISPATCH_AUTHORITY')"),
        ("h27_direct_path_has_commission_carrier", "Name", "carrier_presence"),
    },
    "C12": ("h27_receiving_seat_errors", "Call", "any((from_role(target) in MASTER_TIER_ROLES for target in targets))"),
    "C12a": {
        ("selected_authorization_conflict", "Call", "selected_conflict(selected, ('FROM', 'PHASE', 'AUTHORITY', 'TO', 'DISPATCH_ID', 'COMMISSION_AUTHORIZATION') + H27_COMMISSION_SURFACE)"),
        ("stage_conflict", "Call", "selected_conflict(item, keys)"),
        ("h27_commission_precompute", "Call", "selected_conflict(selected, ('FROM', 'PHASE', 'AUTHORITY', 'TO', 'DELEGATED_DISPATCH_AUTHORITY') + H27_COMMISSION_SURFACE)"),
    },
    "C13": ("dispatch_id_map", "Call", "set(h27_occurrences(item[4], 'DISPATCH_ID'))"),
    "C14": ("h27_conflict_message", "Compare", "len(distinct) <= 1"),
    "C15": ("role_from_consistency_error", "Compare", "canonical_role(expected) != special_expected"),
    "C16": {
        ("h27_master_seat_errors", "Compare", "match.group(1) in occurrences"),
        ("lint_relay_root", "Compare", "rfields.get('DESIGN_REVIEW_VERDICT') != 'approve'"),
    },
    "XR1": {("xroot_pair_plan_applicable", "Call", "any((h27_occurrences(text, key) for key in XROOT_TRIGGERS))")},
    "XR2": {("xroot_design_gate", "UnaryOp", "not declaration[key]")},
    "XR3": {("xroot_field_conflicts", "Compare", "len(values) > 1")},
    "XR4": {("xroot_paths", "Call", "xroot_valid_relpath(declaration[key])")},
    "XR5": {("xroot_repository_and_commit", "Compare", "object_type != 'commit'")},
    "XR6": {("xroot_verify", "Compare", "hashlib.sha256(blob).hexdigest() != declared_digest")},
    "XR7": {("xroot_validate_selected", "Compare", "relay_owner != owner")},
    "XR8": {("xroot_authority", "Compare", "verdict != 'approve'")},
    "XR9": {("xroot_target_ok", "Compare", "expected not in values")},
    "XR10": {("xroot_design_gate", "BoolOp", "local_fields.get('DESIGN_DOC_ID') == declaration['DESIGN_DOC_ID'] and len(local_from) == 1 and (from_owner(local_from[0]) == declaration['DESIGN_OWNER'])")},
    "XR11": {("xroot_declaration", "Call", "xroot_pair_plan_applicable(text)")},
    "XR12": {("xroot_authority", "ListComp", "[relay for relay in relays if xroot_target_ok(relay, 'PHASE', 'DESIGN') and xroot_target_ok(relay, 'DESIGN_DOC_ID', doc_id)]")},
    "XR13": {("xroot_authority", "ListComp", "[relay for relay in relays if xroot_target_ok(relay, 'PHASE', 'DESIGN-REVIEW') and xroot_target_ok(relay, 'DESIGN_DOC_ID', doc_id) and xroot_target_ok(relay, 'PARENT_DISPATCH_ID', origin_dispatch)]")},
    "XR14": {("xroot_target_ok", "Compare", "expected not in values")},
    "XR15": {("xroot_authority", "Compare", "review_role not in peer_roles.get(origin_role, set())")},
    "XR16": {("xroot_authority", "Call", "xroot_relays(repo, root_tree)")},
    "XR17": {("xroot_plan_gate", "Call", "any((h27_occurrences(text, key) for key in source_keys))")},
    "XR18": {("xroot_plan_gate", "Call", "xroot_valid_relpath(lock_path)")},
    "XR19": {("lock_digest_shape_errors", "Call", "xroot_pair_plan_applicable(text)")},
}

# Keep rows with a single reviewed selector equally explicit.  The inventory
# shape above is needed where one row owns several independently reviewed
# predicates; every selector in every inventory must be carried by an arm.
for _row, _selector in tuple(ROW_SELECTORS.items()):
    if isinstance(_selector, tuple):
        ROW_SELECTORS[_row] = {_selector}


def parse_results(output: str) -> tuple[int, int]:
    """Count fixture result lines emitted by a suite."""
    outcomes = [RESULT_LINE.search(line) for line in output.splitlines()]
    return (
        sum(match is not None and match.group(1) == "PASS" for match in outcomes),
        sum(match is not None and match.group(1) == "FAIL" for match in outcomes),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--only", metavar="ROW", action="append", default=[])
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="suite command after --; defaults to the base fixture suite",
    )
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_offsets(text: str, node: ast.AST) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    return start, end


def mutation_span(source: str, arm: dict[str, object]) -> tuple[int, int]:
    tree = ast.parse(source)
    functions = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == arm["function"]
    ]
    if len(functions) != 1:
        raise RuntimeError(f"{arm['name']}: function anchor count={len(functions)}")
    wanted_type = getattr(ast, str(arm["node"]), None)
    if not isinstance(wanted_type, type) or not issubclass(wanted_type, ast.AST):
        raise RuntimeError(f"{arm['name']}: unknown AST node {arm['node']!r}")
    search_root: ast.AST = functions[0]
    context_node = arm.get("context_node")
    context_original = arm.get("context_original")
    if context_node is not None or context_original is not None:
        wanted_context_type = getattr(ast, str(context_node), None)
        if not isinstance(wanted_context_type, type) or not issubclass(wanted_context_type, ast.AST):
            raise RuntimeError(f"{arm['name']}: unknown context AST node {context_node!r}")
        contexts = [
            node for node in ast.walk(search_root)
            if isinstance(node, wanted_context_type) and ast.unparse(node) == context_original
        ]
        if len(contexts) != 1:
            raise RuntimeError(f"{arm['name']}: context AST anchor count={len(contexts)}")
        search_root = contexts[0]
    matches = [
        node for node in ast.walk(search_root)
        if isinstance(node, wanted_type) and ast.unparse(node) == arm["original"]
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{arm['name']}: AST anchor count={len(matches)}")
    return source_offsets(source, matches[0])


def clear_bytecode(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache)


def result_failures(output: str) -> set[str]:
    failed: set[str] = set()
    for line in output.splitlines():
        match = RESULT_DETAIL.match(line)
        if match and match.group("outcome") == "FAIL":
            failed.add(match.group("label").removeprefix("--relay-root ").removeprefix("--index "))
    return failed


def exact_error_sets(harness: ModuleType) -> dict[tuple[str, str], frozenset[str]]:
    """Measure every registered fixture's full error-message set.

    The fixture harness deliberately permits registrations with only an
    outcome/count oracle.  This independent universal oracle retains the
    exact observed messages for *all* registrations, so an arm cannot hide a
    changed message behind a weaker registered oracle.
    """
    lint = harness.load_linter()
    measured: dict[tuple[str, str], frozenset[str]] = {}
    for kind, rel, _expected in harness.EXPECTED:
        target = harness.FIXTURES / rel
        cwd_rel = harness.A5_CWD.get(rel)
        seam_rel = harness.A5_SEAM.get(rel)
        old_cwd = os.getcwd()
        if cwd_rel:
            os.chdir(harness.FIXTURES / cwd_rel)
        if seam_rel:
            os.chmod(harness.FIXTURES / seam_rel, 0)
        try:
            if kind == "file":
                result = lint.lint_file(target)
            elif kind == "template":
                result = lint.lint_file(target, template_mode=True)
            elif kind == "index":
                result = lint.lint_relay_index(target / "INDEX.md")
            elif kind == "index-rel":
                result = lint.lint_relay_index(Path("INDEX.md"))
            else:
                result = lint.lint_relay_root(target)
        finally:
            if seam_rel:
                os.chmod(harness.FIXTURES / seam_rel, 0o644)
            os.chdir(old_cwd)
        measured[(kind, rel)] = frozenset(result.errors)
    return measured


def xroot_registration_count(harness: ModuleType) -> int:
    """Count runtime-generated xroot registrations once for the denominator."""
    with tempfile.TemporaryDirectory(prefix="mtbattery-xroot-count.") as raw:
        return len(harness.xroot_cases(Path(raw)))


def normalized_target_tree(target: Path) -> dict[str, bytes]:
    """Return a fixture target as a relative-path-to-byte mapping."""
    if target.is_file():
        return {target.name: target.read_bytes()}
    return {
        member.relative_to(target).as_posix(): member.read_bytes()
        for member in sorted(target.rglob("*"))
        if member.is_file()
    }


def validate_count_controls(
    spec: ModuleType,
    harness: ModuleType,
) -> tuple[dict[str, dict[str, str]], list[str]]:
    """Validate explicit, arm-indexed count-control/twin declarations."""
    errors: list[str] = []
    raw = getattr(spec, "DECLARED_COUNT_CONTROLS", None)
    if not isinstance(raw, dict):
        return {}, ["CONTROL declaration missing or not a mapping"]

    arm_names = {str(arm.get("name")) for arm in spec.ARMS}
    registrations: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for kind, rel, expected in harness.EXPECTED:
        registrations[rel].append((kind, expected))

    valid: dict[str, dict[str, str]] = {}
    for arm_name, pairs in raw.items():
        if arm_name not in arm_names:
            errors.append(f"CONTROL declaration unknown arm={arm_name!r}")
            continue
        if not isinstance(pairs, dict) or not pairs:
            errors.append(f"CONTROL declaration arm={arm_name!r} must map control to twin")
            continue
        valid_pairs: dict[str, str] = {}
        for control, twin in pairs.items():
            pair_errors: list[str] = []
            structurally_resolvable = True
            control_regs = registrations.get(control, [])
            twin_regs = registrations.get(twin, [])
            if len(control_regs) != 1:
                pair_errors.append(f"control registration count={len(control_regs)}")
                structurally_resolvable = False
            if len(twin_regs) != 1:
                pair_errors.append(f"twin registration count={len(twin_regs)}")
                structurally_resolvable = False
            if control not in harness.EXPECTED_ERROR_COUNT or control in harness.EXPECTED_ERROR_SET:
                pair_errors.append("control must have EXPECTED_ERROR_COUNT and no EXPECTED_ERROR_SET")
                structurally_resolvable = False
            if twin not in harness.EXPECTED_ERROR_SET:
                pair_errors.append("twin must have EXPECTED_ERROR_SET")
                structurally_resolvable = False
            if len(control_regs) == 1 and len(twin_regs) == 1:
                control_kind, _control_expected = control_regs[0]
                twin_kind, _twin_expected = twin_regs[0]
                if control_kind != twin_kind:
                    pair_errors.append(
                        f"registration KIND differs control={control_kind!r} twin={twin_kind!r}"
                    )
                control_semantics = (
                    harness.A5_CWD.get(control),
                    harness.A5_SEAM.get(control),
                )
                twin_semantics = (
                    harness.A5_CWD.get(twin),
                    harness.A5_SEAM.get(twin),
                )
                if control_semantics != twin_semantics:
                    pair_errors.append(
                        "invocation semantics differ "
                        f"control={control_semantics!r} twin={twin_semantics!r}"
                    )
                control_tree = normalized_target_tree(harness.FIXTURES / control)
                twin_tree = normalized_target_tree(harness.FIXTURES / twin)
                if control_tree != twin_tree:
                    pair_errors.append("target path-to-byte mapping differs")
            if pair_errors:
                errors.extend(
                    f"CONTROL arm={arm_name} control={control!r} twin={twin!r}: {message}"
                    for message in pair_errors
                )
            if structurally_resolvable:
                # Keep the arm-local dynamic conditions independently testable
                # when an equivalence predicate fails.  Otherwise a KIND,
                # invocation, or mapping probe also trips the residual gate and
                # cannot establish an isolated differential.
                valid_pairs[control] = twin
        if valid_pairs:
            valid[arm_name] = valid_pairs
    return valid, errors


def generated_and_registered_by_kind() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Derive member identities from the generator and registrations independently."""
    generator = load_module("mtbattery_mtfixgen", TOOLS / "mtfixgen.py")
    harness = load_module("mtbattery_harness", TOOLS / "check-relay-lint-fixtures.py")
    registered: dict[str, set[str]] = defaultdict(set)
    generated: dict[str, set[str]] = defaultdict(set)
    for kind, rel, _expected in harness.EXPECTED:
        if rel.startswith("mastertier/"):
            registered[kind if kind in {"file", "template"} else "root"].add(rel)
    for member in generator.generated_members():
        if "/" in member:
            generated["root"].add(f"mastertier/{member.split('/', 1)[0]}")
        elif member.startswith("MTT") or member.startswith("DI1c-"):
            generated["template"].add(f"mastertier/{member}")
        else:
            generated["file"].add(f"mastertier/{member}")
    return dict(generated), dict(registered)


def validate_spec(spec: ModuleType, selected: list[dict[str, object]], full_run: bool) -> list[str]:
    errors: list[str] = []
    rows = set(spec.LEDGER_ROWS)
    def covered_rows(arm: dict[str, object]) -> tuple[str, ...]:
        covered = arm.get("rows", (arm.get("row"),))
        if not isinstance(covered, tuple) or not covered:
            return ()
        return tuple(str(row) for row in covered)

    arm_rows = [row for arm in spec.ARMS for row in covered_rows(arm)]
    counts = Counter(arm_rows)
    if full_run and set(arm_rows) != rows:
        errors.append(f"O1 uncovered={sorted(rows - set(arm_rows))} unknown={sorted(set(arm_rows) - rows)}")
    if full_run and any(counts[row] < 1 for row in rows):
        errors.append(f"O1 empty-row-coverage={sorted(row for row in rows if counts[row] < 1)}")
    if len(spec.ARMS) != len(set(arm.get("name") for arm in spec.ARMS)):
        errors.append("O1 duplicate arm name")

    source = (TOOLS / "relay-lint.py").read_text(encoding="utf-8")
    mutants: dict[str, str] = {}
    for arm in selected:
        actual_selector = (arm.get("function"), arm.get("node"), arm.get("original"))
        for row in covered_rows(arm):
            expected_selectors = ROW_SELECTORS.get(row, set())
            if actual_selector not in expected_selectors:
                errors.append(
                    f"O1 {arm.get('name')}: row={row} anchor-selector="
                    f"{actual_selector!r} expected-one-of={sorted(expected_selectors)!r}"
                )
        try:
            start, end = mutation_span(source, arm)
        except Exception as exc:
            errors.append(f"O1 {arm.get('name')}: cannot resolve mutant bytes: {exc}")
            continue
        mutant = source[:start] + str(arm.get("replacement")) + source[end:]
        earlier = mutants.get(mutant)
        if earlier is not None:
            errors.append(f"O1 duplicate-mutant-bytes={earlier},{arm.get('name')}")
        else:
            mutants[mutant] = str(arm.get("name"))

    if full_run:
        represented = {
            str(row): {
                (arm.get("function"), arm.get("node"), arm.get("original"))
                for arm in spec.ARMS if str(row) in covered_rows(arm)
            }
            for row in rows
        }
        for row, inventory in ROW_SELECTORS.items():
            missing = inventory - represented.get(row, set())
            if missing:
                errors.append(f"O1 row={row} unrepresented-reviewed-selectors={sorted(missing)!r}")
    return errors


def run_arm(
    arm: dict[str, object],
    command: tuple[str, ...],
    universal_baseline: dict[tuple[str, str], frozenset[str]],
) -> tuple[set[str], set[str], int, int, int]:
    source = (TOOLS / "relay-lint.py").read_text(encoding="utf-8")
    start, end = mutation_span(source, arm)
    mutant = source[:start] + str(arm["replacement"]) + source[end:]
    ast.parse(mutant)  # A non-importable mutant is never a kill.
    with tempfile.TemporaryDirectory(prefix="mtbattery-") as raw:
        scratch = Path(raw)
        shutil.copytree(TOOLS, scratch / "tools")
        (scratch / "tools" / "relay-lint.py").write_text(mutant, encoding="utf-8")
        clear_bytecode(scratch / "tools")
        # Parsing is not enough: import the exact copied mutant in an isolated
        # module namespace after cache removal.  Failure is a hard stop, never
        # counted as a kill.
        load_module(f"mtbattery_import_{arm['name'].replace('-', '_')}", scratch / "tools" / "relay-lint.py")
        scratch_harness = load_module(
            f"mtbattery_harness_{arm['name'].replace('-', '_')}",
            scratch / "tools" / "check-relay-lint-fixtures.py",
        )
        universal_observed = exact_error_sets(scratch_harness)
        universal_kills = {
            rel for key, expected_errors in universal_baseline.items()
            if universal_observed.get(key) != expected_errors
            for _kind, rel in [key]
        }
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run(command, cwd=scratch, capture_output=True, text=True, check=False, env=env)
        output = completed.stdout + completed.stderr
        passed, failed = parse_results(output)
        return result_failures(output), universal_kills, passed, failed, completed.returncode


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.spec.is_file():
        raise SystemExit(f"spec does not exist: {args.spec}")
    sys.dont_write_bytecode = True
    spec = load_module("mtbattery_spec", args.spec)
    command = tuple(args.command) or DEFAULT_SUITE
    selected = [
        arm for arm in spec.ARMS
        if not args.only or set(str(row) for row in arm.get("rows", (arm["row"],))) & set(args.only)
    ]
    errors = validate_spec(spec, selected, not args.only)
    harness = load_module("mtbattery_denominator", TOOLS / "check-relay-lint-fixtures.py")
    count_controls, control_errors = validate_count_controls(spec, harness)
    errors.extend(control_errors)
    xroot_registrations = xroot_registration_count(harness)
    registration_denominator = len(harness.EXPECTED) + xroot_registrations
    declared_arm_count = len(spec.ARMS)
    print(f"DENOMINATOR registrations={registration_denominator} declared_arms={declared_arm_count}")
    universal_baseline = exact_error_sets(harness)
    exact_message_oracles = xroot_registrations + sum(
        rel in harness.EXPECTED_ERROR_SET for _kind, rel, _expected in harness.EXPECTED
    )
    count_or_exit_oracles = registration_denominator - exact_message_oracles
    print(
        f"ORACLES exact-message={exact_message_oracles} "
        f"count-or-exit={count_or_exit_oracles} universal={len(universal_baseline)}"
    )
    generated, registered = generated_and_registered_by_kind()
    kinds = set(generated) | set(registered)
    for kind in sorted(kinds):
        difference = generated.get(kind, set()) ^ registered.get(kind, set())
        print(f"O2 KIND={kind} GENERATED={len(generated.get(kind, set()))} REGISTERED={len(registered.get(kind, set()))} DIFF={len(difference)}")
        if difference:
            errors.append(f"O2 {kind} symmetric-difference={sorted(difference)}")
    attempted: list[str] = []
    completed: list[str] = []
    for arm in selected:
        attempted.append(str(arm["name"]))
        try:
            observed, universal, passed, failed, code = run_arm(arm, command, universal_baseline)
        except Exception as exc:
            print(f"ARM={arm['name']} ROW={arm['row']} HARD-FAIL={exc}")
            errors.append(f"{arm['name']}: {exc}")
            continue
        completed.append(str(arm["name"]))
        arm_rows = {str(row) for row in arm.get("rows", (arm["row"],))}
        if not any(row.startswith("XR") for row in arm_rows):
            # Increment 9 is a frozen ledger over the committed static matrix.
            # Runtime-generated xroot registrations belong to Increment 10 and
            # do not retroactively enlarge historical arm kill declarations.
            observed = {label for label in observed if not label.startswith("xroot/")}
        declared = set(arm["kills"])
        universal_extra = universal - observed
        for control, twin in count_controls.get(str(arm["name"]), {}).items():
            condition_errors: list[str] = []
            if control not in universal:
                condition_errors.append("control is not universally killed")
            if control in observed:
                condition_errors.append("control does not survive its registered oracle")
            if twin not in observed:
                condition_errors.append("twin is not killed by its registered oracle")
            if condition_errors:
                errors.extend(
                    f"CONTROL arm={arm['name']} control={control!r} twin={twin!r}: {message}"
                    for message in condition_errors
                )
            else:
                universal_extra.discard(control)
        print(
            f"ARM={arm['name']} ROW={arm['row']} ROW_ANCHOR={arm['function']} ANCHOR=1 IMPORT=ok "
            f"KILLED={sorted(observed)} EXPECTED={sorted(declared)} "
            f"UNIVERSAL_EXACT={sorted(universal)} UNIVERSAL_MINUS_REGISTERED={sorted(universal_extra)} "
            f"PASS={passed} FAIL={failed} TOTAL={passed + failed}/{registration_denominator} exit={code}"
        )
        if passed + failed != registration_denominator:
            errors.append(
                f"A2 {arm['name']} incomplete-denominator="
                f"{passed + failed}/{registration_denominator}"
            )
        if observed != declared:
            errors.append(f"O3 {arm['name']} observed={sorted(observed)} declared={sorted(declared)}")
        if universal_extra:
            errors.append(
                f"A2 universal-exact-minus-registered {arm['name']}={sorted(universal_extra)}"
            )
        if not observed:
            errors.append(f"survivor {arm['name']}")
    if not args.only and len(attempted) != declared_arm_count:
        errors.append(f"run completeness attempted={len(attempted)} declared={declared_arm_count}")
    if not args.only and len(completed) != declared_arm_count:
        errors.append(f"run completeness completed={len(completed)} declared={declared_arm_count}")
    print(
        f"ARMS attempted={len(attempted)} completed={len(completed)} declared={declared_arm_count} "
        f"SURVIVORS={sum(error.startswith('survivor ') for error in errors)} ERRORS={len(errors)}"
    )
    for error in errors:
        print(f"ERROR {error}")
    return 7 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
