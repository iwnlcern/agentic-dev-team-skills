"""Service-owned relay admission identity and context construction."""
import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re
import sqlite3

from relay_engine import errors, slips, submit
from relay_engine.api_protocol import ComponentRefusal, SeatBinding
from relay_engine.api_sessions import BindingUnavailable, require_current_binding, seat_address
from relay_engine.api_writer import StorePort, WriterUnavailable
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
        try:
            sender = sender_ref(conn, self.binding)
        except ValueError as error:
            if str(error) != 'seat_address_unavailable':
                raise
            require_current_binding(conn, self.binding)
            raise
        ctx = self.context.submit_context(conn, self.context.project_id, sender,
            self.args['submission_key'], sender_check=lambda current: require_current_binding(current, self.binding))
        admitted = submit.submit(conn, ctx, data)
        return {'relay_id': admitted.relay_id, 'handle': str(admitted.handle),
                'created_at': str(admitted.created_at_ms)}


class SubmitService:
    def __init__(self, context: ServiceContext):
        self.context = context

    def run(self, args: Mapping, binding: SeatBinding) -> Mapping | ComponentRefusal:
        try:
            return self.context.port.submit_callee(self.context.project_id,
                                                   _SubmitJob(self.context, dict(args), binding))
        except errors.EngineError as error:
            return map_refusal(error)
        except (WriterUnavailable, BindingUnavailable, sqlite3.Error):
            return ComponentRefusal('E_API_UNAVAILABLE', 'runtime_not_ready')
        except Exception:
            return ComponentRefusal('E_API_INTERNAL', 'handler_fault')
