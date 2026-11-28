"""Service-owned relay admission identity and context construction."""
import base64
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import re
import sqlite3

from relay_engine import addresses, errors, header, slips, store_layout, store_steps, submit
from relay_engine.api_protocol import ComponentRefusal, SeatBinding
from relay_engine.api_sessions import BindingUnavailable, require_current_binding, seat_address
from relay_engine.api_writer import JobOutcome, ReceiverRequired, StorePort, TransferJob, WriterUnavailable
from relay_engine.store_evidence import Capture, LedgerEvidence
from relay_engine.submit import SeatRef, SubmitContext, TableDirectory, party_columns


@dataclass(frozen=True)
class ServiceContext:
    port: StorePort
    clock: Callable[[], int]
    store_root: str
    project_id: str

    def submit_context(self, conn: sqlite3.Connection, project_id: str,
                       sender: SeatRef, submission_key: str, *,
                       sender_check: Callable[[sqlite3.Connection], None] | None = None
                       ) -> SubmitContext:
        party_seat_id, party_special = party_columns(sender.seat_id)
        capture = Capture('snapshot', 'host-observed', party_seat_id, party_special, None)
        return SubmitContext(sender=sender, git_state=None, now_ms=self.clock,
            directory=TableDirectory(conn, project_id), submission_key=submission_key,
            after_admit=slips.on_admitted, after_admit_writes=slips.AFTER_ADMIT_WRITES,
            after_admit_reads_denied=slips.AFTER_ADMIT_READS_DENIED,
            store=LedgerEvidence(conn, self.store_root, project_id, capture),
            sender_check=sender_check)


def sender_ref(conn: sqlite3.Connection, binding: SeatBinding) -> SeatRef:
    address = seat_address(conn, binding.seat_id)
    cursor = conn.execute('SELECT s.role,t.id,t.project_id FROM seats s'
        ' JOIN team_instances t ON t.id=s.team_instance_id WHERE s.id=?', (binding.seat_id,))
    try:
        role, team_instance_id, project_id = cursor.fetchone()
    finally:
        cursor.close()
    return SeatRef(binding.seat_id, address, role, team_instance_id, project_id)


def map_refusal(error: errors.EngineError) -> ComponentRefusal:
    code = error.code
    if code not in errors.ERRORS or re.fullmatch(r'E-[A-Z0-9-]{1,63}', code) is None:
        return ComponentRefusal('E_API_INTERNAL', 'handler_fault')
    if code == 'E-SUBMISSION-KEY-REUSED':
        return ComponentRefusal('E_API_CONFLICT', 'submission_key_reused')
    if code == 'E-SENDER-NOT-CURRENT':
        return ComponentRefusal('E_API_AUTH', error.params['reason'])
    if code == 'E-ADMISSION-HOOK':
        return ComponentRefusal('E_API_UNAVAILABLE', 'authority_unavailable')
    if code == 'E-ADMISSION-COMMIT':
        return ComponentRefusal('E_API_UNAVAILABLE', 'admission_not_stored')
    if code == 'E-ADMISSION-UNKNOWN':
        return ComponentRefusal('E_API_UNAVAILABLE', 'admission_unknown')
    return ComponentRefusal('E_API_ADMISSION', error.cls, {'engine_code': code})


@dataclass(frozen=True)
class _SubmitJob:
    context: ServiceContext
    args: Mapping
    binding: SeatBinding

    @property
    def files(self) -> frozenset[str]:
        return frozenset({self.context.port.ledger_file(self.context.project_id)})

    def run(self, conn):
        data = base64.b64decode(self.args['draft_base64'], validate=True)
        return _admitted_result(submit.submit(conn, self.context_for(conn), data))

    def context_for(self, conn):
        try:
            sender = sender_ref(conn, self.binding)
        except ValueError as error:
            if str(error) != 'seat_address_unavailable':
                raise
            require_current_binding(conn, self.binding)
            raise
        return self.context.submit_context(conn, self.context.project_id, sender,
            self.args['submission_key'], sender_check=lambda current: require_current_binding(current, self.binding))


def _admitted_result(admitted):
    return {'relay_id': admitted.relay_id, 'handle': str(admitted.handle),
            'created_at': str(admitted.created_at_ms)}


def _route(conn, name):
    # The turn proxy deliberately denies PRAGMA. Only routing fields are needed.
    with closing(conn.execute('SELECT project_id,state FROM project_routes WHERE name=?',
                              (name,))) as cursor:
        row = cursor.fetchone()
    return row[0] if row and row[1] == 'active' else None


def _stopped(conn):
    with closing(conn.execute("SELECT project_id FROM engine_stops"
                              " WHERE scope='project' AND ended_at IS NULL")) as cursor:
        return {project for project, in cursor.fetchall()}


def _classify(port, sender, data):
    try:
        draft = header.parse(data)
    except errors.EngineError:
        return {}
    by_name = {}
    for field, role, group in (('TO', 'to', draft.to), ('CC', 'cc', draft.cc)):
        for address in group:
            name = addresses.parse_address(address).project
            if name:
                by_name.setdefault(name, ([], []))[0].append((address, field, role))
    for spelling in (draft.parent, *draft.answers):
        if spelling and (name := spelling.partition('@')[2]):
            by_name.setdefault(name, ([], []))[1].append(spelling)
    if not by_name:
        return {}

    def resolve(conn):
        foreign = {}
        for name, (targets, spellings) in by_name.items():
            project = _route(conn, name)
            if project not in (None, sender):
                foreign[project] = name, tuple(targets), tuple(spellings)
        return foreign

    return port.read_catalogue(resolve)


def _run_transfer(turn, job, data, foreign):
    from relay_engine import xproject

    sender = job.context.project_id
    for project, (name, _, _) in foreign.items():
        if _route(turn.catalogue, name) != project:
            raise submit.RouteChanged()
    stopped = _stopped(turn.catalogue)
    checks = {}
    for project in store_layout.declared_order(False, foreign):
        _, targets, spellings = foreign[project]

        def check(conn, project=project, targets=targets, spellings=spellings):
            return submit.check_project(conn, project, sender, job.binding.seat_id,
                                        targets, spellings, release=xproject.release)

        is_stopped = project in stopped
        proof = turn.read_receiver(project, 'required', check,
                                   route='stopped' if is_stopped else 'served')
        checks[project] = replace(proof, stopped=is_stopped)
    turn.open_sender_writes()
    result = submit.admit_in_turn(turn.sender, job.context_for(turn.sender), data, checks)
    return JobOutcome(True, result)


def _transfer_result(outcome):
    if outcome.commit:
        if isinstance(outcome.value, submit.TurnRefusal):
            raise outcome.value.error
        return _admitted_result(outcome.value)
    if outcome.fault in ('commit_unknown', 'stop_commit_unknown'):
        return ComponentRefusal('E_API_UNAVAILABLE', 'admission_unknown')
    if outcome.fault in ('commit_failed', 'stop_unwritten', 'receiver_required'):
        return ComponentRefusal('E_API_UNAVAILABLE', 'admission_not_stored')
    return ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')


class SubmitService:
    def __init__(self, context: ServiceContext):
        self.context = context

    def run(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal:
        try:
            job = _SubmitJob(self.context, dict(args), binding)
            port, sender = self.context.port, self.context.project_id
            data = base64.b64decode(args['draft_base64'], validate=True)
            foreign = _classify(port, sender, data)
            if not foreign:
                return port.submit_callee(sender, job)

            def replay(handles):
                conn = handles[sender]
                return submit.replay_in_snapshot(conn, job.context_for(conn), data)

            prior = port.read_declared(store_steps.Declared(None, {sender: 'read'}), replay)
            if prior is not None:
                return _admitted_result(prior)
            return _transfer_result(port.submit_transfer(
                TransferJob(sender, lambda turn: _run_transfer(turn, job, data, foreign))))
        except (ReceiverRequired, submit.RouteChanged):
            return ComponentRefusal('E_API_UNAVAILABLE', 'admission_not_stored')
        except errors.EngineError as error:
            return map_refusal(error)
        except (WriterUnavailable, BindingUnavailable, sqlite3.Error):
            return ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
        except Exception:
            return ComponentRefusal('E_API_INTERNAL', 'handler_fault')
