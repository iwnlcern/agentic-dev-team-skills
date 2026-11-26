"""Independent, text-only loopback fixture for console browser walks.

Scenario entries are matched repeatedly by exact arguments. An entry's optional
``after`` mapping replaces the named operations' entries after its response,
allowing a walk to describe subsequent reads without running the engine.
"""

from collections.abc import Mapping
from copy import deepcopy
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import threading
from urllib.parse import urlsplit


POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
READS = {'queue.list', 'queue.get', 'engine.status', 'checkout.list', 'seat.list',
         'project.settings.get', 'authority.team_reviewer', 'authority.live_slips', 'authority.events'}
ACTS = {'queue.answer', 'queue.open', 'queue.dismiss', 'queue.dismiss_all',
        'engine.stop', 'engine.project_stop', 'engine.project_start',
        'checkout.register', 'checkout.unregister', 'seat.unseat',
        'project.settings.set', 'authority.assign_reviewer', 'slip.revoke'}


class ScenarioRefused(ValueError):
    """The fixture cannot represent a scenario on the text-only wire."""


def _text_only(value):
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_text_only(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _text_only(v) for k, v in value.items())
    return False


def _scenario(value):
    if not isinstance(value, Mapping):
        raise ScenarioRefused('scenario must map operation names to entries')
    for name, entries in value.items():
        if not isinstance(name, str) or not isinstance(entries, list):
            raise ScenarioRefused('operation entries must be lists')
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) - {'args', 'status', 'body', 'after'}
                    or not {'args', 'status', 'body'} <= set(entry)
                    or not isinstance(entry['args'], dict)
                    or not _text_only(entry['args']) or not _text_only(entry['body'])
                    or type(entry['status']) is not int or not 100 <= entry['status'] <= 599):
                raise ScenarioRefused('invalid or non-text scenario entry')
            if 'after' in entry:
                _scenario(entry['after'])
    return deepcopy(dict(value))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class FixtureService:
    """Serve fixture page bytes and scripted operations, with disposable sessions."""

    def __init__(self, scenario: Mapping, page_dir: str):
        self._scenario = _scenario(scenario)
        self._page_dir = Path(page_dir)
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._down = False
        self._requests = []
        self._lookups = []
        self.restart()

    def restart(self):
        with self._lock:
            self._cookie_name = 'adt_session_' + secrets.token_hex(4)
            self._sessions = {}
            self._launch = secrets.token_urlsafe(32)

    def start(self) -> str:
        if self._server is not None:
            raise RuntimeError('fixture already started')
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def send_error(self, code, message=None, explain=None):
                fixture._answer(self, 403, {'error': 'refused'})

            def do_GET(self):
                fixture._handle(self)

            def do_POST(self):
                fixture._handle(self)

            def do_HEAD(self):
                fixture._handle(self)

            do_OPTIONS = do_HEAD
            do_PUT = do_HEAD
            do_PATCH = do_HEAD
            do_DELETE = do_HEAD
            do_TRACE = do_HEAD
            do_CONNECT = do_HEAD

        self._pages = {}
        for route, name, mime in [('/', 'index.html', 'text/html'),
                ('/page.js', 'page.js', 'text/javascript'), ('/page.css', 'page.css', 'text/css')]:
            path = self._page_dir / name
            if path.is_file():
                self._pages[route] = (path.read_bytes(), mime + '; charset=utf-8')
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={'poll_interval': 0.02}, daemon=True)
        self._thread.start()
        self.origin = 'http://127.0.0.1:' + str(self._server.server_port)
        return self.origin

    def stop(self):
        server, thread = self._server, self._thread
        if server is not None:
            server.shutdown()
            server.server_close()
            thread.join()
            self._server = self._thread = None

    def launch_url(self, item_id: str | None = None) -> str:
        with self._lock:
            return self.origin + '/#launch=' + self._launch + ('' if item_id is None else '&item=' + item_id)

    def set_down(self, down: bool):
        with self._lock:
            self._down = down

    def requests(self) -> list[Mapping]:
        with self._lock:
            return deepcopy(self._requests)

    def lookups(self) -> list[str]:
        with self._lock:
            return list(self._lookups)

    def _answer(self, handler, status, body, mime='application/json; charset=utf-8', cookie=None):
        raw = body if isinstance(body, bytes) else _json(body)
        handler.send_response_only(status)
        for name, value in [('Cache-Control', 'no-store'), ('Content-Security-Policy', POLICY),
                ('X-Frame-Options', 'DENY'), ('X-Content-Type-Options', 'nosniff'),
                ('Connection', 'close'), ('Referrer-Policy', 'no-referrer'),
                ('Content-Type', mime), ('Content-Length', str(len(raw)))]:
            handler.send_header(name, value)
        if cookie is not None:
            handler.send_header('Set-Cookie', cookie)
        handler.end_headers()
        if handler.command != 'HEAD':
            handler.wfile.write(raw)
        handler.close_connection = True

    def _handle(self, handler):
        with self._lock:
            self._route(handler)

    def _route(self, handler):
        if self._down:
            handler.close_connection = True
            handler.connection.shutdown(socket.SHUT_RDWR)
            return

        def refuse():
            self._answer(handler, 403, {'error': 'refused'})

        def one(name):
            values = handler.headers.get_all(name, [])
            return values[0] if len(values) == 1 else None

        parsed = urlsplit(handler.path)
        path = parsed.path
        document = handler.command == 'GET' and path in self._pages and not parsed.query
        session = path == '/api/session' and not parsed.query
        read = handler.command == 'GET' and path.startswith('/api/read/')
        act = handler.command == 'POST' and path.startswith('/api/op/') and not parsed.query
        op = path.rsplit('/', 1)[-1]
        if not (document or (session and handler.command in ('GET', 'POST'))
                or (read and op in READS) or (act and op in ACTS)):
            return refuse()
        if one('Host') != urlsplit(self.origin).netloc:
            return refuse()
        origin = handler.headers.get_all('Origin', [])
        if (origin and origin != [self.origin]) or (handler.command == 'POST' and not origin):
            return refuse()
        if document:
            raw, mime = self._pages[path]
            return self._answer(handler, 200, raw, mime)
        sites = handler.headers.get_all('Sec-Fetch-Site', [])
        if sites and sites != ['same-origin']:
            return refuse()
        cookie_values = []
        for header in handler.headers.get_all('Cookie', []):
            for pair in header.split(';'):
                key, sep, value = pair.strip().partition('=')
                if sep and key == self._cookie_name:
                    cookie_values.append(value)
        token = self._sessions.get(cookie_values[0]) if len(cookie_values) == 1 else None
        body = None
        if handler.command == 'POST':
            length = one('Content-Length')
            if (one('Content-Type') != 'application/json' or not length
                    or not length.isascii() or not length.isdigit() or int(length) > 16384
                    or handler.headers.get_all('Transfer-Encoding')):
                return refuse()
            raw = handler.rfile.read(int(length))
            try:
                body = json.loads(raw, object_pairs_hook=_unique,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(body, dict) or raw != _json(body):
                    return refuse()
            except (ValueError, TypeError, UnicodeError):
                return refuse()
        if session and handler.command == 'POST':
            if set(body) != {'launch'} or not isinstance(body['launch'], str) or not body['launch'].isascii() or not hmac.compare_digest(body['launch'], self._launch):
                return refuse()
            session_id, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[session_id] = token
            self._launch = secrets.token_urlsafe(32)
            return self._answer(handler, 200, {'csrf_token': token}, cookie=
                self._cookie_name + '=' + session_id + '; HttpOnly; SameSite=Strict; Path=/')
        if token is None:
            return refuse()
        if session:
            return self._answer(handler, 200, {'csrf_token': token})
        if act:
            self._requests.append({'operation': op, 'headers': dict(handler.headers), 'body': deepcopy(body)})
            csrf = one('X-ADT-CSRF')
            if csrf is None or not csrf.isascii() or not hmac.compare_digest(csrf, token):
                return refuse()
            if not _text_only(body):
                return refuse()
            args = body
        else:
            args = {}
            if not parsed.query.isascii() or '%' in parsed.query or '+' in parsed.query:
                return refuse()
            for pair in parsed.query.split('&') if parsed.query else []:
                if pair.count('=') != 1:
                    return refuse()
                key, value = pair.split('=')
                if not key or key in args:
                    return refuse()
                args[key] = value
        self._lookups.append(op)
        for entry in self._scenario.get(op, []):
            if args == entry['args']:
                self._answer(handler, entry['status'], entry['body'])
                if 'after' in entry:
                    self._scenario.update(deepcopy(entry['after']))
                return
        refuse()


if __name__ == '__main__':
    import argparse
    import signal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin-file', required=True)
    parser.add_argument('--page-dir', required=True)
    parser.add_argument('--scenario')
    options = parser.parse_args()
    scenario = json.loads(Path(options.scenario).read_text()) if options.scenario else {}
    fixture = FixtureService(scenario, options.page_dir)
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    signal.signal(signal.SIGINT, lambda *_: done.set())
    try:
        origin = fixture.start()
        Path(options.origin_file).write_text(origin + '\n')
        done.wait()
    finally:
        fixture.stop()
