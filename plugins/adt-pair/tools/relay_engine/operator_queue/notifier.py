"""Pure display arguments and bounded, durable outbox delivery."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import json

from relay_engine import store_layout
from relay_engine.operator_queue import kinds, records
from relay_engine.operator_queue.records import JobPort


_MARK = " … [cut: the whole message is on v3's page]"


@dataclass(frozen=True)
class Notification:
    item_id: str
    revision: int
    title: str
    text: str


@dataclass(frozen=True)
class NotifyReport:
    delivered: int = 0
    failed: int = 0


class DeliveryCancelled(Exception):
    """Shutdown interrupted a send, leaving its outbox entry eligible to retry."""


class NotifierIssueUnavailable(Exception):
    """No eligible native issue, so the pass must leave outbox and health untouched."""


class KnownNotifierFailure(Exception):
    """An acknowledged failure before any possible external effect."""

    def __init__(self, error_class):
        self.error_class = error_class
        super().__init__(error_class)


def notification_title(project_name: str | None, actor: str) -> str:
    return f"{project_name if project_name is not None else 'v3'}: {actor}"


def _popup_text(kind, texts, popup):
    if kind == 'question':
        return texts['subject']
    if popup is not None:
        return popup
    return texts['ring_text'] if 'ring_text' in texts else texts['sentence']


def popup_time(at: str | int, tz: tzinfo | None = None) -> str:
    try:
        if isinstance(at, str):
            instant = datetime.fromisoformat(at)
            if instant.utcoffset() is None:
                raise ValueError('popup time needs a UTC offset')
        elif type(at) is int:
            instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=at)
        else:
            raise ValueError('popup time needs an ISO timestamp or integer milliseconds')
        local = instant.astimezone(tz)
    except (OverflowError, OSError) as error:
        raise ValueError('popup time outside the supported range') from error
    return (f'{local.year:04d}-{local.month:02d}-{local.day:02d} '
            f'{local.hour:02d}:{local.minute:02d}')


def due_notifications(conn, now_ms, limit: int) -> list[Notification]:
    rows = records._rows(conn, 'SELECT n.item_id,n.revision,i.project_id,i.actor_address,'
                         'i.raiser,i.kind,i.subkind,i.texts,i.popup_text,i.pins FROM queue_notifications n '
                         'JOIN queue_items i ON i.item_id=n.item_id '
                         "WHERE n.state='pending' AND n.next_attempt_at_ms<=? "
                         'ORDER BY i.raised_position,n.item_id,n.revision LIMIT ?',
                         (now_ms, max(0, limit)))
    notes = []
    for item, revision, project, actor, raiser, kind, subkind, texts, popup, pins in rows:
        name = None if project is None else store_layout.route(conn, project_id=project).name
        texts = json.loads(texts)
        text = _popup_text(kind, texts, popup)
        if subkind == 'approval_seal_unusable' and name is not None and 'ring_text' in texts:
            text = texts['ring_text'].replace('<project>', name)
        elif subkind == 'restore_command_started':
            text = texts['sentence'].replace('<time>', popup_time(json.loads(pins)['at']))
            if name is not None:
                text = text.replace('<project>', name)
        notes.append(Notification(item, revision, notification_title(name, actor if actor is not None else raiser),
                                  text))
    return notes


def _cut(text):
    raw = text.encode('utf-8')
    if len(raw) <= 200:
        return text
    return raw[:200 - len(_MARK.encode('utf-8'))].decode('utf-8', errors='ignore').rstrip() + _MARK


def alarm_argv(title: str, text: str, notifier: str) -> list[str]:
    if notifier == 'none':
        return []
    text = _cut(text)
    if notifier == 'osascript':
        return ['osascript', '-e', 'on run argv', '-e',
                'display notification (item 2 of argv) with title (item 1 of argv)',
                '-e', 'end run', '--', title, text]
    if notifier == 'terminal-notifier':
        return ['terminal-notifier', '-title', '\\' + title, '-message', '\\' + text]
    if notifier == 'notify-send':
        return ['notify-send', '--', title, text]
    raise ValueError('unknown notifier')


def notification_argv(note: Notification, notifier: str) -> list[str]:
    return alarm_argv(note.title, note.text, notifier)


def notify_delivered(conn, note, now_ms):
    records._write(conn, "UPDATE queue_notifications SET state='delivered',next_attempt_at_ms=NULL "
                   "WHERE item_id=? AND revision=? AND state='pending'", (note.item_id, note.revision))
    records._write(conn, 'INSERT INTO queue_notifier_health VALUES (1,0,?) '
                   'ON CONFLICT(id) DO UPDATE SET consecutive_failures=0,updated_at_ms=excluded.updated_at_ms',
                   (now_ms,))
    principal = records.InternalWriter()
    for occurrence in records.open_condition_items(conn, principal, None):
        if occurrence.subkind == 'notifier_unhealthy' and occurrence.condition_key == 'notifier':
            records.clear_condition(conn, principal, occurrence.item_id, occurrence.kind,
                                    occurrence.subkind, None, occurrence.condition_key, now_ms)


def notify_failed(conn, note, error_class, now_ms):
    row = records._one(conn, 'SELECT attempts FROM queue_notifications '
                       "WHERE item_id=? AND revision=? AND state='pending'", (note.item_id, note.revision))
    if row is not None:
        attempts = row[0] + 1
        delay = min(30000 * 2 ** min(attempts - 1, 5), 900000)
        records._write(conn, "UPDATE queue_notifications SET state='pending',attempts=?,last_error_class=?,"
                       "next_attempt_at_ms=? WHERE item_id=? AND revision=? AND state='pending'",
                       (attempts, error_class, now_ms + delay, note.item_id, note.revision))
    # Health measures the program even if undo retired this selected entry meanwhile.
    records._write(conn, 'INSERT INTO queue_notifier_health VALUES (1,1,?) '
                   'ON CONFLICT(id) DO UPDATE SET consecutive_failures=consecutive_failures+1,'
                   'updated_at_ms=excluded.updated_at_ms', (now_ms,))
    failures = records._one(conn, 'SELECT consecutive_failures FROM queue_notifier_health WHERE id=1')[0]
    if failures == 5:
        request = records.RaiseRequest('notice', 'notifier_unhealthy', None,
                                      {'sentence': kinds.fixed_text('notifier_unhealthy', 'notifier')}, {}, 'notifier',
                                      f'notifier-failure:{note.item_id}:{note.revision}:{failures}:{now_ms}')
        result = records.raise_item(conn, records.InternalWriter(), request, now_ms, enabled=kinds.ENABLED_TAGS)
        if isinstance(result, records.QueueRefusal):
            raise RuntimeError('notifier health notice refused: ' + result.cause_kind)


def notify_pass(port: JobPort, deliver: Callable[[list[str]], None], now_ms: int, *,
                notifier: str, limit: int = 5, offer=None, settled=None,
                stopping=lambda: False) -> NotifyReport:
    if notifier == 'none':
        return NotifyReport()
    delivered = failed = 0
    try:
        notes = port.read_catalogue(lambda conn: due_notifications(conn, now_ms, min(5, max(0, limit))))
        for note in notes:
            argv = notification_argv(note, notifier)
            try:
                if offer is None:
                    deliver(argv)
                else:
                    offer(note, argv)
            except (DeliveryCancelled, NotifierIssueUnavailable):
                break
            except Exception as exc:
                if stopping():
                    break
                if offer is None:
                    error_class = type(exc).__name__
                elif isinstance(exc, KnownNotifierFailure):
                    error_class = exc.error_class
                else:
                    break
                port.catalogue(lambda conn: notify_failed(conn, note, error_class, now_ms))
                failed += 1
            else:
                if stopping():
                    break
                port.catalogue(lambda conn: notify_delivered(conn, note, now_ms))
                delivered += 1
            if settled is not None:
                settled()
    except records.JobUnavailable as error:
        if error.reason != 'stopped':
            raise
    return NotifyReport(delivered, failed)
