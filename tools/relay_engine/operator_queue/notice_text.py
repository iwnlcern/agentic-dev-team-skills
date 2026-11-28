"""Lifecycle notice text and lossless pins, built from committed outbox rows."""
from collections.abc import Mapping
from dataclasses import dataclass

FREE_VALUE_MAX_BYTES = 512

SETTLEMENT_SENTENCES = {('settlement_finished', 'start'): 'After a sudden stop, the program finished an approval change in <project> when it restarted; whoever asked for it was told its outcome was not yet known, or had not yet been answered.', ('settlement_undone', 'start'): 'After a sudden stop, the program undid an unfinished approval change in <project> when it restarted; it is in effect only if it was asked for again and recorded since.', ('settlement_undone', 'running'): 'An approval change in <project> that was not completed has been undone; it is in effect only if it was asked for again and recorded since.', ('settlement_undone', 'stop'): 'When the program stopped, it undid an approval change in <project> that was not completed; it is in effect only if it was asked for again and recorded since.', ('settlement_undone', 'resolve'): "On your answer that this project's store was not put back from a backup, the program discarded an unfinished approval change in <project>; it is in effect only if it was asked for again and recorded since."}

EVENT_PHRASES = {'slip-issued': 'permission given', 'slip-revoked': 'permission withdrawn', 'branch-bound': 'branch tied to a bundle', 'claim-recorded': 'approval claim recorded', 'verification-approved': 'review approved', 'verification-objected': 'review objection', 'waiver': 'waiver', 'reviewer-assigned': 'reviewer assigned', 'verification-interrupted': 'review interrupted', 'reviewer-withheld': 'reviewer held'}

SERVICE_SENTENCES = {'engine_stopped': 'v3 was stopped for every project at <time>.', 'engine_started': 'v3 was started again at <time>.', 'project_stopped': "<project> was stopped at <time>. A stopped project's agents cannot send relays, get approvals, or commit or merge where the engine's permission is needed.", 'project_started': '<project> was started again at <time>.', 'engine_was_down': 'v3 was not running from <time> to <time>; nothing was lost. Run adt engine status if it stops again.', 'engine_updated': 'v3 was updated from <from> to <to> at <time>.', 'engine_update_failed': 'v3 could not update from <from> to <to>, and went back to <from> at <time>.', 'engine_restart_requested': "This project's top seat asked v3 to restart at <time>."}

SEAT_SENTENCES = {'seat_registered': "Seat <seat> was registered in <project>; its agent can now sign in with the seat's key.", 'seat_replaced': 'Seat <seat> in <project> was given a new key; its old key stopped working at once.', 'seat_stood_down': 'Seat <seat> in <project> was stood down; no key signs in to it now.'}

REVIEWER_UNDONE_PHRASE = "undoing it left the earlier reviewer, if any, assigned at that time"
SLIP_UNDONE_PHRASE = "undoing it left the seat's earlier permission for that bundle and repository, if any, in force at that time"
UNSEAT_SENTENCES = {
    'terminal': '<seat> was unseated from the terminal at <time>. Its window can no longer send relays, commit or merge as that agent until it signs in again.',
    'page': "<seat> was unseated from v3's page at <time>. Its window can no longer send relays, commit or merge as that agent until it signs in again.",
    'instructed': '<seat> was unseated at <time> by the top-level planner on your instruction <relay id>. Its window can no longer send relays, commit or merge as that agent until it signs in again.',
}
MERGE_ALARM_SENTENCE = 'PR #<pr> was merged onto <landed target>, but its go-ahead allowed only <allowed target>. If this merge should not stand, revert its merge commit from your own terminal: <full commit id>'


CAUSE_PHRASES: Mapping[str, str] = {
    'folder_unusable': "the approval-seal folder or this project's files in it are missing or cannot be read or written",
    'counter_unusable': "the approval counter is damaged or does not match this project's ledger",
    'high_water_unusable': "the record of what has been written out is damaged or does not match this project's ledger",
    'pointer_unset': "setting up this project's approval counter did not finish",
    'ledger_chain_mismatch': "this project's ledger does not match its approval record, as after a restore from a backup",
    'pointer_damaged': "the record of which approval history this project is on is damaged or was replaced from outside v3",
}
_POINTER_DAMAGED_SUFFIX = (
    "If v3 cannot read which approval history a project is on, run adopt-restored from the item that names it; "
    "as after any restore through Store's restore command, every go-ahead and approval in the store is then "
    "cancelled and every project held until you end the hold."
)
_SEAL_RING_TEXT = (
    "Approvals cannot be recorded; anything you cancel now stays in force until you cancel it again after a fix. "
    "To bar its agents' approvals now: adt engine stop --project <project>"
)


def seal_texts(cause: str, chain_id: str) -> Mapping[str, str]:
    sentence = (
        f"Approvals in <project> cannot be recorded: {CAUSE_PHRASES[cause]}. "
        "Work already approved carries on; new permissions, withdrawals, reviews, "
        "waivers, reviewer changes and the first commit on a new branch wait until it is fixed."
    )
    if cause == 'pointer_damaged':
        sentence += ' ' + _POINTER_DAMAGED_SUFFIX
    return {
        'sentence': sentence,
        'subject': f'{cause} (chain {fit_value(chain_id)})' if chain_id != 'none' else cause,
        'caused_by': 'the approval seal',
        'ring_text': _SEAL_RING_TEXT,
    }


def backup_texts() -> Mapping[str, str]:
    return {
        'sentence': (
            "The approval-seal folder on this machine may be included in backups. Approvals record as usual. "
            "If an old backup of this folder alone is put back, approvals may stop being recorded until it is fixed. "
            "Each start tries again to exclude it."
        ),
        'subject': 'the approval-seal folder',
        'caused_by': 'the approval seal',
    }


def backup_pins() -> Mapping:
    return {'folder': 'approval_seal'}


def fit_value(value: str) -> str:
    value = value.replace('\x00', '\ufffd')
    data = value.encode('utf-8')
    if len(data) <= FREE_VALUE_MAX_BYTES:
        return value
    return data[:FREE_VALUE_MAX_BYTES - 3].decode('utf-8', errors='ignore') + '…'


def fit_integers(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if -(2**53 - 1) <= value <= 2**53 - 1 else str(value)
    if isinstance(value, Mapping):
        return {key: fit_integers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [fit_integers(item) for item in value]
    return value


def settlement_texts(row) -> Mapping[str, str]:
    sentence = SETTLEMENT_SENTENCES[(row.subkind, row.occasion)]
    parts = []
    for event in row.events:
        part = fit_value(EVENT_PHRASES.get(event.kind, event.kind)) + ': ' + fit_value(event.subject)
        if row.subkind == 'settlement_undone':
            if event.kind == 'reviewer-assigned':
                part += ' (' + REVIEWER_UNDONE_PHRASE + ')'
            elif event.kind == 'slip-issued':
                part += ' (' + SLIP_UNDONE_PHRASE + ')'
        parts.append(part)
    subject = '; '.join(parts)
    if len(subject.encode('utf-8')) > 4096:
        kept = []
        for index, part in enumerate(parts):
            suffix = f'; and {len(parts) - index - 1} more, listed when you open it'
            candidate = '; '.join([*kept, part]) + suffix
            if len(candidate.encode('utf-8')) > 4096:
                break
            kept.append(part)
        subject = '; '.join(kept) + f'; and {len(parts) - len(kept)} more, listed when you open it'
    return {'sentence': sentence, 'subject': subject,
            'caused_by': 'your answer' if row.occasion == 'resolve' else 'the program'}


def settlement_pins(row) -> Mapping:
    return fit_integers({'row_id': row.row_id, 'position': row.position,
        'occasion': row.occasion, 'chain_id': row.chain_id,
        'runtime_instance_id': row.runtime_instance_id,
        'transaction_ordinal': row.transaction_ordinal,
        'events': [{'number': event.number, 'kind': event.kind, 'subject': event.subject}
                   for event in row.events]})


def seat_texts(row) -> Mapping[str, str]:
    return {'sentence': SEAT_SENTENCES[row.subkind].replace('<seat>', fit_value(row.seat_address), 1),
            'caused_by': {'occupant': "the project's top seat", 'operator': 'you',
                          'program': 'the program'}[row.actor_kind]}


def seat_pins(row) -> Mapping:
    pins = {'row_id': row.row_id, 'position': row.position,
            'team_instance_id': row.team_instance_id, 'seat_id': row.seat_id,
            'actor_kind': row.actor_kind}
    if row.old_occupant_id is not None:
        pins['old_occupant_id'] = row.old_occupant_id
    if row.new_occupant_id is not None:
        pins['new_occupant_id'] = row.new_occupant_id
    if row.actor_occupant_id is not None:
        pins['actor_occupant_id'] = row.actor_occupant_id
    return fit_integers(pins)


def unseat_texts(row) -> Mapping[str, str]:
    form = 'instructed' if row.instruction_relay_id is not None else row.surface
    # Split the template before filling so token-looking free values stay literal.
    text = UNSEAT_SENTENCES[form]
    import re
    values = {'<seat>': fit_value(row.seat_address),
              '<relay id>': fit_value(row.instruction_relay_id or '')}
    return {'sentence': re.sub(r'<seat>|<relay id>', lambda match: values[match[0]], text)}


def unseat_pins(row) -> Mapping:
    pins = {'row_id': row.row_id, 'position': row.position, 'seat_id': row.seat_id,
            'generation': row.generation, 'surface': row.surface,
            'carrying_seat_id': row.carrying_seat_id,
            'instruction_relay_id': row.instruction_relay_id, 'at': row.committed_at_ms}
    return fit_integers(pins)


def restart_texts(row) -> Mapping[str, str]:
    return {'sentence': SERVICE_SENTENCES['engine_restart_requested']}


def restart_pins(row) -> Mapping:
    return fit_integers({'row_id': row.row_id, 'position': row.position,
                        'seat_id': row.seat_id, 'actor_occupant_id': row.actor_occupant_id,
                        'at': row.committed_at_ms})


def merge_alarm_texts(row) -> Mapping[str, str]:
    import re
    values = {'<pr>': str(row.pr), '<landed target>': fit_value(row.landed_target),
              '<allowed target>': fit_value(row.allowed_target), '<full commit id>': row.merge_commit}
    return {'sentence': re.sub(r'<pr>|<landed target>|<allowed target>|<full commit id>',
                               lambda match: values[match[0]], MERGE_ALARM_SENTENCE),
            'subject': fit_value(row.slip_id), 'caused_by': 'the program'}


def merge_alarm_pins(row) -> Mapping:
    return fit_integers({'row_id': row.row_id, 'position': row.position,
                         'slip_id': row.slip_id, 'pr': row.pr, 'merge_commit': row.merge_commit,
                         'allowed_target': row.allowed_target, 'landed_target': row.landed_target})


@dataclass(frozen=True)
class StoreItemRow:
    id: int
    kind: str
    project_id: str | None
    subject: str
    ends: int | None
    at: str | int
    fields: Mapping


def store_item_texts(row: StoreItemRow) -> tuple[Mapping[str, str], Mapping] | str:
    """Build the display text and pins, or the first invalid field or text member."""
    from relay_engine.operator_queue.notifier import popup_time

    if row.kind == 'report_awaiting_confirmation':
        names = ('report_path',)
    elif row.kind == 'restore_command_started':
        names = ('command', 'scope', 'started_at')
    else:
        return f'unknown_kind:{row.kind}'

    fields = row.fields
    for name in names:
        if not isinstance(fields, Mapping) or name not in fields:
            return f'missing_field:{name}'
        value = fields[name]
        if name == 'command':
            if (not isinstance(value, str)
                    or (value not in ('restore', 'adopt-restored', 'new-folder pass', 'resolve-reservation')
                        and not (value.startswith('abandon ') and value[8:].strip()))):
                return 'bad_field:command'
        elif name == 'scope':
            ending = ', which is still being created'
            creating = (isinstance(value, str) and value.endswith(ending)
                        and len(value) > len(ending) and '<' not in value[:-len(ending)])
            if not (value == 'the whole store'
                    or (row.project_id is not None and value == 'project <project>')
                    or (row.project_id is None and creating)):
                return 'bad_field:scope'
        elif name == 'started_at':
            try:
                popup_time(value)
            except ValueError:
                return 'bad_field:started_at'
        elif not isinstance(value, str):
            return f'bad_field:{name}'

    if row.kind == 'report_awaiting_confirmation':
        sentence = (f'Projects wait until you confirm you have seen the report of restore job {row.subject}: '
                    f'run adt store confirm-report {row.subject}. The report is at {fields["report_path"]}.')
        pins = {}
    else:
        command, scope, at = fields['command'], fields['scope'], fields['started_at']
        sentence = f'A restore command started: {command} for {scope}, run at <time> after the operator check passed.'
        if command == 'resolve-reservation':
            if row.project_id is None:
                return 'bad_field:project_id'
            sentence += (" It answers the question about <project>'s unfinished changes and grants nothing: "
                         'a no undoes them, with a notice for each, unless the store was put back, when nothing '
                         'changes and <project> waits for adopt-restored; a yes or not sure starts adopt-restored.')
        elif command.startswith('abandon '):
            sentence += (f' It withdraws nothing and grants nothing; {scope} stays held until the step its '
                         'report names has run.')
        else:
            sentence += ' It will withdraw go-aheads and approvals there and grants none; what it did follows in its report.'
        pins = {'at': at}

    try:
        if not sentence or '\x00' in sentence or len(sentence.encode('utf-8')) > 4096:
            return 'bad_texts:sentence'
    except UnicodeEncodeError:
        return 'bad_texts:sentence'
    return {'sentence': sentence}, pins
