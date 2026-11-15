"""Closed queue policy, canonical condition keys and offered choices."""
from collections.abc import Mapping
from dataclasses import dataclass
import re
from types import MappingProxyType

from relay_engine.jcs import jcs_encode


KINDS = frozenset({'question', 'cross_project_permission', 'merge_go_ahead', 'verification_held', 'notice'})
NOTICE_SUBKINDS = frozenset({
    'seat_registered', 'seat_replaced', 'seat_stood_down', 'seat_unseated',
    'engine_restart_requested', 'engine_updated', 'engine_update_failed', 'doorbell_off',
    'notifier_unhealthy', 'operator_act_carried', 'worker_stalled', 'bridge_refused',
    'panel_key_enrolled', 'answer_faulted', 'instruction_carried', 'seat_record_missing',
    'seat_ambiguous', 'backup_missed',
    'settlement_finished', 'settlement_undone', 'engine_stopped', 'engine_started',
    'project_stopped', 'project_started', 'engine_was_down', 'merge_target_changed',
    'approval_seal_unusable', 'approval_seal_backup_included',
})
STATES = frozenset({'pending', 'applying', 'resolved', 'superseded', 'refused', 'faulted',
                    'dismissed', 'withdrawn', 'cleared', 'retired'})
EVENT_KINDS = frozenset({'raised', 'answered', 'applied', 'superseded', 'refused', 'faulted',
    'withdrawn', 'opened', 'dismissed', 'dismissed_all', 'cleared', 'clear_requested',
    'condition_closed', 'retired'})
NOTIFICATION_STATES = frozenset({'pending', 'delivered', 'retired'})
RETIRE_REASONS = frozenset({'project_undone'})
NO_PROJECT_SUBKINDS = frozenset({'notifier_unhealthy', 'worker_stalled', 'bridge_refused',
    'panel_key_enrolled', 'engine_updated', 'engine_update_failed', 'instruction_carried',
    'seat_record_missing', 'seat_ambiguous', 'backup_missed', 'engine_stopped',
    'engine_started', 'engine_was_down', 'approval_seal_backup_included'})
NO_CLEAR_SUBKINDS = frozenset({'bridge_refused', 'panel_key_enrolled', 'instruction_carried',
                             'seat_record_missing', 'seat_ambiguous', 'merge_target_changed'})
OPERATOR_CLOSED_SUBKINDS = frozenset({'instruction_carried'})
STALL_WORKERS = ('queue_reconcile', 'queue_question_ingest', 'queue_lifecycle_ingest', 'engine_liveness', 'store_backup_daily')
SURFACES = frozenset({'page', 'frank_panel'})
ENABLED_TAGS = frozenset({'pr1', 'pr3'})


@dataclass(frozen=True)
class RaiserRow:
    kind: str
    subkind: str | None
    entry: str
    principal: str
    enabled_in: str
    keys: frozenset[str] | None


RAISER_MATRIX = (
    RaiserRow('question', None, 'internal', 'console_queue', 'pr1', None),
    RaiserRow('cross_project_permission', None, 'queue.raise', 'xproject_transfer', 'pr2', None),
    RaiserRow('merge_go_ahead', None, 'queue.raise', 'authority_service', 'pr2', None),
    RaiserRow('verification_held', None, 'queue.raise', 'authority_service', 'pr2', None),
    RaiserRow('notice', 'doorbell_off', 'queue.raise', 'engine_liveness', 'pr5', None),
    RaiserRow('notice', 'seat_registered', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'seat_replaced', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'seat_stood_down', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_restart_requested', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_updated', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_update_failed', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'notifier_unhealthy', 'internal', 'console_queue', 'pr1', frozenset({'notifier'})),
    RaiserRow('notice', 'notifier_unhealthy', 'queue.raise', 'supervision', 'pr1', frozenset({'notify_worker'})),
    RaiserRow('notice', 'worker_stalled', 'queue.raise', 'supervision', 'pr1', frozenset(STALL_WORKERS)),
    RaiserRow('notice', 'operator_act_carried', 'internal', 'console_queue', 'pr3c', None),
    RaiserRow('notice', 'answer_faulted', 'internal', 'console_queue', 'pr1', None),
    RaiserRow('notice', 'backup_missed', 'queue.raise', 'supervision', 'pr1', frozenset({'daily'})),
    RaiserRow('notice', 'settlement_finished', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'settlement_undone', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_stopped', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_started', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'project_stopped', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'project_started', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'engine_was_down', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'merge_target_changed', 'internal', 'console_queue', 'pr3', None),
    RaiserRow('notice', 'seat_unseated', 'internal', 'console_queue', 'pr3', None),
)

RING_SET = frozenset({'question', 'cross_project_permission', 'merge_go_ahead', 'verification_held',
    'notice/doorbell_off', 'notice/instruction_carried', 'notice/worker_stalled', 'notice/bridge_refused', 'notice/answer_faulted', 'notice/backup_missed', 'notice/merge_target_changed'})
FIXED_TEXTS: Mapping[tuple[str, str], str] = MappingProxyType({('worker_stalled', 'queue_question_ingest'): "v3 has stopped turning agents' questions into "
                                              'items, so new questions are not shown. Run adt '
                                              'engine status.',
 ('worker_stalled', 'queue_lifecycle_ingest'): 'v3 has stopped reading notices from projects, so '
                                               'new notices are not shown. Run adt engine status.',
 ('worker_stalled', 'queue_reconcile'): 'v3 has stopped finishing your answers, so an answer you '
                                        'gave may not have reached its agent. Run adt engine '
                                        'status.',
 ('worker_stalled', 'engine_liveness'): 'v3 has stopped checking whether agents get the relays '
                                        'sent to them, so an agent that does not is not shown. Run '
                                        'adt engine status.',
 ('bridge_refused', '*'): 'v3 refused a request to act as you that did not come from its own page '
                          'address; nothing was changed. If you did not just use a v3 page at '
                          'another address, another site may have tried.',
 ('notifier_unhealthy', 'notifier'): 'v3 could not show its pop-ups five times in a row, so new '
                                     'questions and notices may not pop up. They are still listed '
                                     "on v3's page. Run adt engine status.",
 ('notifier_unhealthy', 'notify_worker'): 'v3 has stopped showing pop-ups, so new questions and '
                                          "notices do not pop up. They are still listed on v3's "
                                          'page. Run adt engine status.',
 ('backup_missed', 'daily'): 'v3 has made no complete daily backup of its store yesterday or today, '
                              'so putting the store back from a backup could lose more than a day of work. '
                              'Run adt engine status.',
 ('worker_stalled', 'store_backup_daily'): 'v3 has stopped making its daily backup, so your store may go without one. '
                                          'Run adt engine status.'})
ALARM_TITLE = 'v3'
ALARM_TEXT = 'v3 notifications have stopped. Run adt engine status.'
ANSWER_FAULTED_TEXTS: Mapping[str, str] = MappingProxyType({
    'question': "v3 stopped an answer you gave: a different version had already been sent to this agent, which only a fault in v3 can cause. Open the question on v3's page to check it.",
})
PIN_TOKENS: Mapping[str, tuple[str, str]] = MappingProxyType({})


def raiser_row(kind: str, subkind: str | None, principal: str, *, enabled: frozenset[str]) -> RaiserRow | None:
    for row in RAISER_MATRIX:
        if (row.kind, row.subkind, row.principal) == (kind, subkind, principal) and row.enabled_in in enabled:
            return row
    return None


def answer_surfaces(kind: str, choice: str | None, *, frank_conformed: bool) -> frozenset[str]:
    if kind not in KINDS:
        return frozenset()
    if kind == 'merge_go_ahead' and not frank_conformed:
        return SURFACES - {'frank_panel'}
    return SURFACES


def fixed_choices(kind: str) -> tuple[str, ...]:
    return {
        'cross_project_permission': ('allow_always', 'allow_once', 'deny'),
        'merge_go_ahead': ('go_ahead', 'go_ahead_without_review'),
        'verification_held': ('waive',),
    }.get(kind, ())


def rings(kind: str, subkind: str | None) -> bool:
    return (f'notice/{subkind}' if kind == 'notice' else kind) in RING_SET


def fixed_text(subkind: str, condition_key: str) -> str | None:
    return FIXED_TEXTS.get((subkind, condition_key), FIXED_TEXTS.get((subkind, '*')))


def condition_key(kind: str, subkind: str | None, key: Mapping | str) -> str:
    return jcs_encode(dict(key) if isinstance(key, Mapping) else key).decode('utf-8')


def parse_choices(body: bytes) -> tuple[tuple[str, ...], str | None]:
    """Treat a malformed final block as display metadata, never a refused question."""
    if not isinstance(body, bytes):
        return (), None
    headers = tuple(re.finditer(br'(?:\A|\n)Choices:(?=\r?\n|\Z)', body))
    if not headers:
        return (), None
    block = body[headers[-1].end():]
    if b'\r\n' in block:
        return (), 'crlf'
    if block.endswith(b'\n\n'):
        return (), 'trailing_blank_line'
    if block.startswith(b'\n'):
        block = block[1:]
    if block.endswith(b'\n'):
        block = block[:-1]
    lines = block.split(b'\n')
    if len(lines) > 8:
        return (), 'too_many_labels'
    labels = []
    for line in lines:
        if not line.startswith(b'- ') or len(line) == 2:
            return (), 'empty_label'
        raw = line[2:]
        try:
            label = raw.decode('utf-8')
        except UnicodeDecodeError:
            return (), 'label_not_utf8'
        if len(raw) > 120:
            return (), 'label_too_long'
        labels.append(label)
    return tuple(labels), None


def act_lookup_args_valid(subkind: str, condition_key: object, pins: object) -> bool:
    if not isinstance(condition_key, Mapping) or not isinstance(pins, Mapping):
        return False
    key = condition_key
    if subkind == 'seat_record_missing':
        if set(key) != {'seat_id', 'session_digest'} or set(pins) != {'seat_id'}:
            return False
        seat = key['seat_id']
        if not isinstance(seat, str) or '\x00' in seat or pins['seat_id'] != seat:
            return False
        try:
            if not 1 <= len(seat.encode('utf-8')) <= 256:
                return False
        except UnicodeEncodeError:
            return False
    elif subkind == 'seat_ambiguous':
        if set(key) != {'host', 'session_digest'} or pins or key['host'] not in ('claude-code', 'codex'):
            return False
    else:
        return False
    digest = key['session_digest']
    return isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest) is not None
