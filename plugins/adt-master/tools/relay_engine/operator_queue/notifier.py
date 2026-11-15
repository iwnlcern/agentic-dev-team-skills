"""Pure display arguments and bounded, durable outbox delivery."""
from collections.abc import Callable
from dataclasses import dataclass
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


def notification_title(project_name: str | None, actor: str) -> str:
    return f"{project_name if project_name is not None else 'v3'}: {actor}"


def _popup_text(kind, texts, popup):
    if kind == 'question':
        return texts['subject']
    if popup is not None:
        return popup
    return texts['ring_text'] if 'ring_text' in texts else texts['sentence']


def due_notifications(conn, now_ms, limit: int) -> list[Notification]:
    rows = records._rows(conn, 'SELECT n.item_id,n.revision,i.project_id,i.actor_address,'
                         'i.raiser,i.kind,i.texts,i.popup_text FROM queue_notifications n '
                         'JOIN queue_items i ON i.item_id=n.item_id '
                         "WHERE n.state='pending' AND n.next_attempt_at_ms<=? "
                         'ORDER BY i.raised_position,n.item_id,n.revision LIMIT ?',
                         (now_ms, max(0, limit)))
    notes = []
    for item, revision, project, actor, raiser, kind, texts, popup in rows:
        name = None if project is None else store_layout.route(conn, project_id=project).name
        notes.append(Notification(item, revision, notification_title(name, actor if actor is not None else raiser),
                                  _popup_text(kind, json.loads(texts), popup)))
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
                notifier: str, limit: int = 5) -> NotifyReport:
    if notifier == 'none':
        return NotifyReport()
    notes = port.read_catalogue(lambda conn: due_notifications(conn, now_ms, min(5, max(0, limit))))
    delivered = failed = 0
    for note in notes:
        argv = notification_argv(note, notifier)
        try:
            deliver(argv)
        except Exception as exc:
            error_class = type(exc).__name__
            port.catalogue(lambda conn: notify_failed(conn, note, error_class, now_ms))
            failed += 1
        else:
            port.catalogue(lambda conn: notify_delivered(conn, note, now_ms))
            delivered += 1
    return NotifyReport(delivered, failed)
