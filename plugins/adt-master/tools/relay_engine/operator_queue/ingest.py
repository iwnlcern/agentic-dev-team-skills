"""Question reads and anchored catalogue ingests within caller-owned jobs."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from sqlite3 import Connection
from typing import TYPE_CHECKING

from relay_engine import queries
from relay_engine.operator_queue import component, kinds, records

if TYPE_CHECKING:
    from relay_engine.api_writer import StorePort


def read_questions(ledger_conn: Connection, project_id: str, after_handle: int, limit: int):
    return queries.operator_questions(ledger_conn, project_id, after_handle, limit)


def _has_item(conn, relay_id):
    return records._one(conn, 'SELECT item_id FROM queue_items WHERE source_relay_id=?',
                        (relay_id,)) is not None


def ingest_questions(catalogue_conn: Connection, rows: Sequence[queries.QuestionRow],
                     project_id: str, cursor: records.Cursor, *, check_rewind: bool, now_ms: int):
    conn = catalogue_conn
    if (not records.route_active(conn, project_id)
            or records.read_cursor(conn, project_id, 'question') != cursor):
        return records.Stale()
    if check_rewind and cursor.position >= 1:
        if (not rows or rows[0].handle != cursor.position
                or rows[0].relay_id != cursor.anchor_id
                or not (_has_item(conn, rows[0].relay_id) or rows[0].answered)):
            return records.Reread()
    raised = 0
    principal = records.InternalWriter()
    for row in rows:
        if (check_rewind and row.handle <= cursor.position) or row.answered:
            continue
        existed = _has_item(conn, row.relay_id)
        pins = dict(project_id=project_id, relay_id=row.relay_id,
                    body_sha256=hashlib.sha256(row.body).hexdigest())
        # A reread has a distinct receipt scope but shares the original occurrence.
        operation_key = records._json([project_id, cursor.position, cursor.epoch,
                                       cursor.anchor_id, row.relay_id])
        request = records.RaiseRequest('question', None, project_id,
                  {'subject': row.subject, 'body': row.body.decode('utf-8')}, pins,
                  {'project_id': project_id, 'relay_id': row.relay_id}, operation_key,
                  actor_address=row.sender_address)
        result = records.raise_item(conn, principal, request, now_ms, enabled=kinds.ENABLED_TAGS)
        if isinstance(result, records.QueueRefusal):
            raise ValueError('native question could not be raised: ' + result.cause_kind)
        if not existed:
            choices, reason = kinds.parse_choices(row.body)
            records._write(conn, 'UPDATE queue_items SET asking_seat_id=?,answer_to=?,source_handle=?,'
                           'source_relay_id=?,choices=?,choices_reason=? WHERE item_id=?',
                           (row.sender_seat_id, row.sender_address, row.handle, row.relay_id,
                            records._json(list(choices)) if choices else None, reason, result.item_id))
            raised += 1
    position, anchor = ((rows[-1].handle, rows[-1].relay_id) if rows else (0, None))
    if raised or (position, anchor) != (cursor.position, cursor.anchor_id):
        # This job holds the catalogue writer; the preflight cursor cannot move meanwhile.
        if not records.advance_cursor(conn, project_id, 'question', cursor, position, anchor_id=anchor):
            raise RuntimeError('question cursor changed inside its catalogue job')
    return raised


def question_ingest_pass(port: records.JobPort, project_ids: Sequence[str], now_ms: int, *, limit: int = 100) -> int:
    if type(limit) is not int or not 1 <= limit < 500:
        raise ValueError('question pass limit is an integer from 1 to 499')
    raised = 0
    for project_id in project_ids:
        try:
            cursor = port.read_catalogue(lambda conn: records.read_cursor(conn, project_id, 'question'))
            rows = port.read_ledger(project_id, lambda conn: read_questions(
                conn, project_id, max(cursor.position - 1, 0), limit + 1))
            result = port.catalogue(lambda conn: ingest_questions(
                conn, rows, project_id, cursor, check_rewind=True, now_ms=now_ms))
            if isinstance(result, records.Reread):
                rows = port.read_ledger(project_id, lambda conn: read_questions(conn, project_id, 0, limit + 1))
                result = port.catalogue(lambda conn: ingest_questions(
                    conn, rows, project_id, cursor, check_rewind=False, now_ms=now_ms))
            if isinstance(result, int):
                raised += result
        except records.JobUnavailable:
            continue
    return raised


class LifecycleRowRefused(Exception):
    """Abort the caller's job; the pass may retry the committed prefix separately."""
    def __init__(self, position: int, row_id: str, refusal: records.QueueRefusal):
        self.position = position
        self.row_id = row_id
        self.refusal = refusal
        super().__init__(refusal.cause_kind)


def ingest_lifecycle(catalogue_conn, rows, project_id: str, cursor: records.Cursor,
                     *, check_rewind: bool, now_ms: int) -> int | records.Stale | records.Reread:
    from relay_engine import lifecycle_outbox as outbox
    from relay_engine.operator_queue import notice_text

    conn = catalogue_conn
    if (not records.route_active(conn, project_id)
            or records.read_cursor(conn, project_id, 'lifecycle') != cursor):
        return records.Stale()
    if check_rewind and cursor.position >= 1:
        anchor = next((row for row in rows if row.position == cursor.position), None)
        if anchor is None or anchor.row_id != cursor.anchor_id:
            return records.Reread()
    builders = {
        outbox.SeatRow: (notice_text.seat_texts, notice_text.seat_pins),
        outbox.SettlementRow: (notice_text.settlement_texts, notice_text.settlement_pins),
        outbox.RestartRow: (notice_text.restart_texts, notice_text.restart_pins),
        outbox.MergeAlarmRow: (notice_text.merge_alarm_texts, notice_text.merge_alarm_pins),
        outbox.UnseatRow: (notice_text.unseat_texts, notice_text.unseat_pins),
    }
    raised = 0
    position, anchor_id = (cursor.position, cursor.anchor_id) if check_rewind else (0, None)
    principal = records.InternalWriter()
    for row in rows:
        if check_rewind and row.position <= cursor.position:
            continue
        instructed = (isinstance(row, outbox.InstructedActRow)
                      or isinstance(row, outbox.UnseatRow) and row.instruction_relay_id is not None)
        if instructed and 'pr3c' not in kinds.ENABLED_TAGS:
            break
        texts, pins = builders[type(row)]
        key = {'project_id': project_id}
        if isinstance(row, outbox.MergeAlarmRow):
            key['slip_id'] = row.slip_id
        else:
            key['row_id'] = row.row_id
        actor = None if isinstance(row, outbox.SettlementRow) else row.seat_address
        request = records.RaiseRequest('notice', row.subkind, project_id, texts(row),
            notice_text.fit_integers(pins(row)), notice_text.fit_integers(key),
            records._json(['lifecycle', project_id, cursor.epoch, cursor.position,
                           cursor.anchor_id, check_rewind, row.row_id]), actor_address=actor)
        before = records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0]
        result = records.raise_item(conn, principal, request, now_ms, enabled=kinds.ENABLED_TAGS)
        if isinstance(result, records.QueueRefusal):
            raise LifecycleRowRefused(row.position, row.row_id, result)
        raised += records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0] - before
        position, anchor_id = row.position, row.row_id
    if (position, anchor_id) != (cursor.position, cursor.anchor_id):
        if not records.advance_cursor(conn, project_id, 'lifecycle', cursor, position, anchor_id=anchor_id):
            raise RuntimeError('lifecycle cursor changed inside its catalogue job')
    return raised


def lifecycle_ingest_pass(port: records.JobPort, project_ids: Sequence[str], now_ms: int,
                          *, limit: int = 100) -> int:
    from relay_engine import lifecycle_outbox as outbox

    raised = 0
    for project_id in project_ids:
        try:
            cursor = port.read_catalogue(lambda conn: records.read_cursor(conn, project_id, 'lifecycle'))
            rows = port.read_ledger(project_id, lambda conn: outbox.read_after(
                conn, project_id, max(cursor.position - 1, 0), limit=limit + 1))
            check_rewind = True
            # A rewind gets one fresh read; a refused job gets one prefix-only retry.
            while True:
                try:
                    result = port.catalogue(lambda conn: ingest_lifecycle(
                        conn, rows, project_id, cursor, check_rewind=check_rewind, now_ms=now_ms))
                except LifecycleRowRefused as failure:
                    prefix = tuple(row for row in rows if row.position < failure.position)
                    if not check_rewind or any(row.position > cursor.position for row in prefix):
                        try:
                            result = port.catalogue(lambda conn: ingest_lifecycle(
                                conn, prefix, project_id, cursor, check_rewind=check_rewind, now_ms=now_ms))
                        except LifecycleRowRefused:
                            break
                        if isinstance(result, int):
                            raised += result
                    break
                if isinstance(result, records.Reread) and check_rewind:
                    rows = port.read_ledger(project_id, lambda conn: outbox.read_after(
                        conn, project_id, 0, limit=limit + 1))
                    check_rewind = False
                    continue
                if isinstance(result, int):
                    raised += result
                break
        except records.JobUnavailable:
            continue
    return raised


from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class LogLine:
    seq: int
    id: str
    kind: str | None
    project_id: str | None
    at: str | int | None
    fields: Mapping


def _log_string(value) -> bool:
    return isinstance(value, str) and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def read_lifecycle_log(data: bytes) -> tuple[LogLine, ...]:
    lines = []
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b'\n'):
            continue
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if (not isinstance(row, dict) or row.get('format') != 'adt-lifecycle-v1'
                or type(row.get('seq')) is not int
                or not _log_string(row.get('id')) or not row['id']):
            continue
        lines.append(LogLine(row['seq'], row['id'], row.get('kind'), row.get('project_id'),
            row.get('at'), {key: value for key, value in row.items()
                           if key not in ('format', 'seq', 'id', 'kind', 'project_id', 'at')}))
    return tuple(lines)


def log_notice(line: LogLine) -> tuple[str, Mapping] | str:
    from relay_engine.operator_queue import notice_text

    if isinstance(line.kind, str) and not _log_string(line.kind):
        return 'bad_field:kind'
    if line.kind == 'outage':
        names = ('started_at', 'ended_at')
    elif line.kind == 'update':
        names = ('from', 'to', 'at', 'outcome')
    else:
        return 'unknown_kind:' + str(line.kind)
    values = dict(line.fields)
    if line.at is not None:
        values['at'] = line.at
    for name in names:
        if name not in values or values[name] is None:
            return 'missing_field:' + name
        value = values[name]
        if (name in ('from', 'to', 'outcome') and not isinstance(value, str)
                or name not in ('from', 'to', 'outcome') and type(value) not in (str, int)
                or isinstance(value, str) and not _log_string(value)):
            return 'bad_field:' + name
    if line.kind == 'outage':
        return 'engine_was_down', notice_text.fit_integers({name: values[name] for name in names})
    if values['outcome'] not in ('updated', 'failed'):
        return 'bad_field:outcome'
    subkind = 'engine_updated' if values['outcome'] == 'updated' else 'engine_update_failed'
    return subkind, notice_text.fit_integers({name: values[name] for name in ('at', 'from', 'to')})


def record_skip(catalogue_conn, source: str, row_ref: str, reason: str, now_ms: int) -> None:
    records._write(catalogue_conn, 'INSERT OR IGNORE INTO queue_ingest_skips '
                   '(source,row_ref,reason,skipped_at_ms) VALUES (?,?,?,?)',
                   (source, row_ref, reason, now_ms))


def _raise_store_notice(conn, subkind, project_id, key, pins, operation_key, now_ms):
    from relay_engine.operator_queue import notice_text

    request = records.RaiseRequest('notice', subkind, project_id,
        {'sentence': notice_text.SERVICE_SENTENCES[subkind]}, notice_text.fit_integers(pins),
        notice_text.fit_integers(key), operation_key)
    before = records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0]
    result = records.raise_item(conn, records.InternalWriter(), request, now_ms, enabled=kinds.ENABLED_TAGS)
    if isinstance(result, records.QueueRefusal):
        raise RuntimeError('service notice refused: ' + result.cause_kind)
    return records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0] - before


def ingest_log(catalogue_conn, lines: Sequence[LogLine], cursor: records.Cursor,
               now_ms: int) -> int | records.Stale:
    conn = catalogue_conn
    if records.read_cursor(conn, None, 'lifecycle_log') != cursor:
        return records.Stale()
    rewind = cursor.anchor_id is not None and not any(
        line.seq == cursor.position and line.id == cursor.anchor_id for line in lines)
    considered = [line for line in lines if rewind or line.seq > cursor.position]
    raised = 0
    for line in considered:
        notice = log_notice(line)
        if isinstance(notice, str):
            record_skip(conn, 'lifecycle_log', line.id, notice, now_ms)
        else:
            subkind, pins = notice
            raised += _raise_store_notice(conn, subkind, None, {'id': line.id}, pins,
                                           records._json(['lifecycle-log', line.id]), now_ms)
    if considered:
        last = considered[-1]
        if not records.advance_cursor(conn, None, 'lifecycle_log', cursor, last.seq, anchor_id=last.id):
            raise RuntimeError('log cursor changed inside its catalogue job')
    return raised


def ingest_stop_events(catalogue_conn, now_ms: int) -> int:
    from relay_engine import store_layout

    conn = catalogue_conn
    cursor = records.read_cursor(conn, None, 'stop_events')
    events = store_layout.stop_events_after(conn, cursor.position)
    raised = 0
    for event in events:
        if event.project_id is not None and not records.route_active(conn, event.project_id):
            continue
        subkind = ('engine_' if event.scope == 'store' else 'project_') + (
            'stopped' if event.kind == 'stop' else 'started')
        key = {'event_id': event.event_id}
        if event.project_id is not None:
            key['project_id'] = event.project_id
        raised += _raise_store_notice(conn, subkind, event.project_id, key,
            {'at': event.at, 'by': event.by}, records._json(['stop-event', str(event.event_id)]), now_ms)
    if events:
        if not records.advance_cursor(conn, None, 'stop_events', cursor, events[-1].event_id):
            raise RuntimeError('stop-event cursor changed inside its catalogue job')
    return raised


class StoreItemRefused(Exception):
    def __init__(self, row_id: int, refusal: records.QueueRefusal):
        self.row_id = row_id
        self.refusal = refusal
        super().__init__(f'store item {row_id}: {refusal.cause_kind}')


def ingest_store_items(catalogue_conn, now_ms: int, *, limit: int = 100, below: int | None = None) -> int:
    from relay_engine.operator_queue import notice_text

    conn = catalogue_conn
    cursor = records.read_cursor(conn, None, 'store_items')
    select = 'SELECT id,kind,project_id,subject,ends,at,fields FROM store_operator_items WHERE id>?'
    params = (cursor.position,)
    if below is not None:
        select += ' AND id<?'
        params += (below,)
    rows = records._rows(conn, select + ' ORDER BY id LIMIT ?', (*params, limit))
    raised = 0
    for row_id, kind, project, subject, ends, at, fields_json in rows:
        if ends is not None:
            binding = records._one(conn, 'SELECT item_id FROM queue_store_item_sources WHERE raise_row_id=?',
                                   (ends,))
            if binding is not None:
                records.clear_condition(conn, records.InternalWriter(), binding[0], 'notice',
                                        kind, project, subject, now_ms)
            continue
        if project is not None and not records.route_active(conn, project):
            continue
        fields = json.loads(fields_json)
        item = notice_text.StoreItemRow(row_id, kind, project, subject, None, at, fields)
        outcome = notice_text.store_item_texts(item)
        if isinstance(outcome, str):
            record_skip(conn, 'store_items', str(row_id), outcome, now_ms)
            continue
        texts, pins = outcome
        request = records.RaiseRequest('notice', kind, project, texts, pins, subject,
                                       records._json(['store-item', str(row_id)]))
        before = records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0]
        result = records.raise_item(conn, records.InternalWriter(), request, now_ms,
                                    enabled=kinds.ENABLED_TAGS, fields=notice_text.fit_integers(fields))
        if isinstance(result, records.QueueRefusal):
            raise StoreItemRefused(row_id, result)
        records._write(conn, 'INSERT OR IGNORE INTO queue_store_item_sources (raise_row_id,item_id) VALUES (?,?)',
                       (row_id, result.item_id))
        raised += records._one(conn, 'SELECT COUNT(*) FROM queue_items')[0] - before
    if rows and not records.advance_cursor(conn, None, 'store_items', cursor, rows[-1][0]):
        raise RuntimeError('store item cursor changed inside its catalogue job')
    return raised


def _retry_store_item_prefix(port: records.JobPort, now_ms: int, refused: StoreItemRefused) -> int:
    try:
        result = port.catalogue(lambda conn: ingest_store_items(conn, now_ms, below=refused.row_id))
    except (StoreItemRefused, records.JobUnavailable):
        return 0
    return result if isinstance(result, int) else 0


def _read_log_path(log_path: Path) -> tuple[LogLine, ...]:
    try:
        data = log_path.read_bytes()
    except FileNotFoundError:
        return ()
    return read_lifecycle_log(data)


@dataclass(frozen=True)
class StorePassResult:
    raised: int
    log_unreadable: str | None


def store_ingest_pass(port: records.JobPort, log_path: Path, now_ms: int) -> StorePassResult:
    raised = 0
    log_unreadable = None
    try:
        cursor = port.read_catalogue(lambda conn: records.read_cursor(conn, None, 'lifecycle_log'))
        try:
            lines = _read_log_path(log_path)
        except OSError as error:
            log_unreadable = type(error).__name__
        else:
            result = port.catalogue(lambda conn: ingest_log(conn, lines, cursor, now_ms))
            if isinstance(result, int):
                raised += result
    except records.JobUnavailable:
        pass
    try:
        result = port.catalogue(lambda conn: ingest_stop_events(conn, now_ms))
        if isinstance(result, int):
            raised += result
    except records.JobUnavailable:
        pass
    try:
        result = port.catalogue(lambda conn: ingest_store_items(conn, now_ms))
        if isinstance(result, int):
            raised += result
    except StoreItemRefused as refused:
        raised += _retry_store_item_prefix(port, now_ms, refused)
    except records.JobUnavailable:
        pass
    return StorePassResult(raised, log_unreadable)


class StartupIngestIncomplete(Exception):
    def __init__(self, source: str, cause: str):
        self.source = source
        self.cause = cause
        super().__init__(source + ': ' + cause)


def store_ingest_startup(port: records.JobPort, log_path: Path, now_ms: int) -> int:
    raised = 0
    for source in ('lifecycle_log', 'stop_events', 'store_items'):
        try:
            while True:
                before = port.read_catalogue(lambda conn: records.read_cursor(conn, None, source))
                if source == 'lifecycle_log':
                    lines = _read_log_path(log_path)
                    result = port.catalogue(lambda conn: ingest_log(conn, lines, before, now_ms))
                elif source == 'stop_events':
                    result = port.catalogue(lambda conn: ingest_stop_events(conn, now_ms))
                else:
                    result = port.catalogue(lambda conn: ingest_store_items(conn, now_ms))
                if isinstance(result, records.Stale):
                    raise StartupIngestIncomplete(source, 'stale')
                raised += result
                after = port.read_catalogue(lambda conn: records.read_cursor(conn, None, source))
                if after == before:
                    break
        except StoreItemRefused as refused:
            _retry_store_item_prefix(port, now_ms, refused)
            raise StartupIngestIncomplete(source, 'refused') from refused
        except records.JobUnavailable:
            raise StartupIngestIncomplete(source, 'job_unavailable') from None
    return raised


def startup_callback(log_path: Path) -> Callable[[StorePort], None]:
    def before_joins(port: StorePort) -> None:
        job_port = component.catalogue_only(component.PlatformJobPort.over_port(port))
        store_ingest_startup(job_port, log_path, time.time_ns() // 1_000_000)

    return before_joins


def project_start_callback() -> Callable[[StorePort], None]:
    def before_project_join(port: StorePort) -> None:
        job_port = component.catalogue_only(component.PlatformJobPort.over_port(port))
        now_ms = time.time_ns() // 1_000_000
        try:
            job_port.catalogue(lambda conn: ingest_stop_events(conn, now_ms))
        except records.JobUnavailable:
            raise StartupIngestIncomplete('stop_events', 'job_unavailable') from None

    return before_project_join
