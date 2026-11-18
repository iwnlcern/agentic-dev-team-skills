"""P7 instruction/distribution guards; P1-P6 require retained agent outputs.

These checks catch dropped carrier duties and contradictory waits, not scheduling
behavior. The controlled mutations never modify the repository or call an agent.
"""

import re
import unittest

from test_kit_stage_a import KitTreeCase, ROOT


PLANNERS = ('master-planner', 'orchestrator-planner', 'pair-planner')
REVIEWERS = ('master-reviewer', 'orchestrator-reviewer', 'pair-implementer')
TIERS = {
    'master-planner': ('adt-master',),
    'master-reviewer': ('adt-master',),
    'orchestrator-planner': ('adt-master', 'adt-orchestrator'),
    'orchestrator-reviewer': ('adt-master', 'adt-orchestrator'),
    'pair-planner': ('adt-master', 'adt-orchestrator', 'adt-pair'),
    'pair-implementer': ('adt-master', 'adt-orchestrator', 'adt-pair'),
}
ROLE_SECTION = '## Ready work and real dependencies'
PROTOCOL = 'shared/protocol.md'
MOVES = 'skills/orchestrator-planner/orchestration-moves.md'
TEMPLATE = 'skills/orchestrator-planner/handoff-templates.md'

# Independently enumerated obligations: a duty in another role cannot cover it.
PLANNER_RULES = {
    'readiness': r'decomposition, dispatch and each received substantive unblock.*ready independent authorized work.*actual prerequisite edges',
    'advance': r'Advance ready independent work.*scope, authority and available capacity',
    'wait': r'For every wait.*conflicting operation.*artifact/contract version.*why.*dependent increment.*release condition',
    'narrow': r'Serialize only the affected increment.*unaffected.*authorized',
    'events': r'Reassess relevant readiness on received dependency, collision or capacity events.*do not poll.*timer.*survey capacity',
    'holds': r'Preserve exact source holds, resource limits and bound dependencies/commit boundaries.*light work.*no automatic exemption',
}
REVIEW_RULES = {
    'balanced': r'Challenge both unnecessary ordering/idle authorized work and unsafe overlap',
    'evidence': r'For each finding.*actual prerequisite.*conflicting operation.*contract/version.*reason.*release.*smallest remedy',
    'limits': r'Preserve exact authority, source holds and resource/capacity limits.*no new actor.*approval gate',
    'event_review': r'Check readiness after received substantive events.*no polling.*task acknowledgment',
}
PAIR_RULES = {
    'execution': r'Implementer owns execution.*valid dispatched plan.*without a fresh Planner task acknowledgment',
    'boundaries': r'Do not reorder bound dependencies or commit boundaries.*scope/lock/grant/resource changes.*existing.*route',
    'single_capacity': r'One authorized execution capacity.*no second Implementer',
}
COLLISION_RULES = {
    'operations': r'Every category.*actual conflicting operation or required-contract/version invalidation',
    'category_limit': r'Component, migration, canonical semantics, asserting test, generated artifact, API/event contract and consumer labels alone.*do not serialize',
    'readonly': r'Two read-only uses of a pinned consumer.*no collision',
    'invalidation': r'A separate-file change.*invalidate.*required contract',
    'writers': r'Shared writes.*write/read.*write/write.*unsafe',
    'release': r'Name the artifact/version.*operation.*reason.*dependent increment.*release condition',
    'narrow': r'Serialize only that increment.*unaffected.*own authority.*capacity',
}
TEMPLATE_RULES = {
    'ready': r'Ready independent work:.*owner.*current authority/capacity',
    'wait': r'Actual waits:.*operation or required artifact/contract version.*reason.*dependent increment.*release',
    'events': r'Received-event reassessment:.*dependency/collision/capacity.*no polling.*task-ack gate',
}
PROTOCOL_RULES = {
    'common': r'Advance ready work; justify narrow waits/releases; reassess events; review idle/unsafe work\.',
}
FORBIDDEN = (
    r'Serialize when teams touch:',
    r'Shared files, migrations, generated schemas, downstream contracts, or the same asserting test must serialize\.',
    r'Require a fresh Planner task acknowledgment before ready execution\.',
    r'Ignore an exact source hold to advance light work\.',
)


def role_path(role):
    return f'skills/{role}/SKILL.md'


def required_carriers():
    """Literal tier inventory, independent of generator metadata/discovery."""
    result = {PROTOCOL: PROTOCOL_RULES, MOVES: COLLISION_RULES,
              TEMPLATE: TEMPLATE_RULES}
    for role in PLANNERS + REVIEWERS:
        rules = dict(PLANNER_RULES if role in PLANNERS else REVIEW_RULES)
        if role in ('pair-planner', 'pair-implementer'):
            rules.update(PAIR_RULES)
        path = role_path(role)
        result[path] = rules
        for tier in TIERS[role]:
            result[f'plugins/{tier}/{path}'] = rules
    for tier in ('adt-master', 'adt-orchestrator'):
        for path, rules in ((MOVES, COLLISION_RULES), (TEMPLATE, TEMPLATE_RULES)):
            result[f'plugins/{tier}/{path}'] = rules
    for role in ('domain-planner', 'domain-reviewer') + PLANNERS + REVIEWERS:
        for tier in TIERS.get(role, ('adt-master',)):
            result[f'plugins/{tier}/skills/{role}/protocol.md'] = PROTOCOL_RULES
    for role in ('pair-planner', 'pair-implementer'):
        result[f'skills/variants/{role}/SKILL.md'] = result[role_path(role)]
    return result


def contract_body(path, text):
    """Check the actual carrier's section, not incidental words elsewhere."""
    if path.endswith('/SKILL.md'):
        return text.partition(ROLE_SECTION + '\n')[2].split('\n## ', 1)[0]
    if path.endswith('orchestration-moves.md'):
        return text.partition('## Sequencing and collision management\n')[2].split('\n## ', 1)[0]
    if path.endswith('handoff-templates.md'):
        return text.partition('## Template D — PROCEED-TO-PLAN handoff\n')[2].split('## Template E', 1)[0]
    return text.partition('## Plan contract\n')[2].split('\n## ', 1)[0]


def check_parallel_guidance(texts: dict[str, str]) -> list[str]:
    failures = []
    for path, rules in required_carriers().items():
        if path not in texts:
            failures.append(f'{path}:missing-carrier')
            continue
        body = contract_body(path, texts[path])
        for name, pattern in rules.items():
            if not re.search(pattern, body):
                failures.append(f'{path}:{name}')
        for pattern in FORBIDDEN:
            if re.search(pattern, texts[path]):
                failures.append(f'{path}:contradictory-wait-or-hold')
    return failures


class TestParallelPlanning(KitTreeCase):
    def texts(self):
        return {p: (ROOT / p).read_text() for p in required_carriers()
                if (ROOT / p).is_file()}

    def assert_carriers(self, paths):
        wanted = set(paths)
        failures = check_parallel_guidance(self.texts())
        self.assertEqual([f for f in failures if f.split(':', 1)[0] in wanted], [])

    def test_planner_duties_in_each_carrier(self):
        # Break caught: a planner tier loses one readiness/edge/event/hold duty.
        self.assert_carriers(role_path(r) for r in PLANNERS)

    def test_matching_review_checks_both_failures(self):
        # Break caught: a reviewer tier checks only safety or only utilization.
        self.assert_carriers(role_path(r) for r in REVIEWERS)

    def test_collision_rules_use_operations(self):
        # Break caught: category labels return as blanket serialization rules.
        self.assert_carriers([MOVES, role_path('orchestrator-planner')])
        routing = self.canonical_text(role_path('orchestrator-planner')).split('## Routing rules', 1)[1].split('\n## ', 1)[0]
        self.assertRegex(routing, r'actual conflicting operation.*required-contract/version invalidation')
        self.assertRegex(routing, r'reason.*dependent increment.*release condition')

    def test_pair_execution_has_no_ack_gate(self):
        # Break caught: either Pair carrier adds a task go-ahead/capacity fiction.
        self.assert_carriers([role_path('pair-planner'), role_path('pair-implementer')])

    def test_template_and_protocol_contract(self):
        # Break caught: shared/template contract disappears or exceeds size cap.
        self.assert_carriers([PROTOCOL, TEMPLATE])
        self.assertLess(len((ROOT / PROTOCOL).read_bytes()), 15000)

    def test_generated_carriers_match_contract(self):
        # Break caught: a missing tier/copy or broken Pair derivation hides a duty.
        self.assertEqual(check_parallel_guidance(self.texts()), [])
        self.assertEqual(sum(p.startswith('plugins/') and p.endswith('/protocol.md')
                             for p in required_carriers()), 14)
        preamble = self.canonical_text('skills/variants/subordination-preamble.md')
        for role in ('pair-planner', 'pair-implementer'):
            text = self.canonical_text(role_path(role))
            front, body = text.split('\n---\n', 1)
            self.assertEqual(self.canonical_text(f'skills/variants/{role}/SKILL.md'),
                             front + '\n---\n\n' + preamble + '\n' + body)

    def test_controls_are_decisive(self):
        # Each isolated defect must fail at its own carrier while originals pass.
        original = self.texts()
        self.assertEqual(check_parallel_guidance(original), [])
        for path, rules in required_carriers().items():
            for name, pattern in rules.items():
                with self.subTest(path=path, duty=name):
                    body = contract_body(path, original[path])
                    match = re.search(pattern, body)
                    self.assertIsNotNone(match)
                    # Delete the entire duty line, not a phrase elsewhere.
                    line = next(l for l in body.splitlines() if re.search(pattern, l))
                    changed = dict(original)
                    changed[path] = original[path].replace(line, '', 1)
                    self.assertIn(f'{path}:{name}', check_parallel_guidance(changed))
            if path.startswith('plugins/'):
                changed = dict(original)
                del changed[path]
                self.assertIn(f'{path}:missing-carrier', check_parallel_guidance(changed))
        for path, defect in (
            (MOVES, 'Serialize when teams touch:'),
            (role_path('pair-planner'), 'Require a fresh Planner task acknowledgment before ready execution.'),
            (role_path('pair-implementer'), 'Ignore an exact source hold to advance light work.'),
        ):
            changed = dict(original)
            changed[path] += '\n' + defect + '\n'
            self.assertIn(f'{path}:contradictory-wait-or-hold', check_parallel_guidance(changed))


if __name__ == '__main__':
    unittest.main()
