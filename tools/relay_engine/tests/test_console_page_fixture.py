"""Loopback contract tests for the browser walk's independent fixture."""

import http.client
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit, parse_qs


POLICY = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
HEADERS = {'Cache-Control': 'no-store', 'Content-Security-Policy': POLICY,
           'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
           'Connection': 'close', 'Referrer-Policy': 'no-referrer'}
SUCCESS = {'request_id': '1', 'result': {'value': 'matched'}}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode()


class ConsolePageFixtureTests(unittest.TestCase):
    def module(self):
        # Import inside each test so the initial RED names all six behaviors.
        return importlib.import_module('relay_engine.tests.console_page_fixture')

    def fixture(self, module, scenario=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        page = Path(temp.name)
        for name, content in [('index.html', '<main>fixture</main>'),
                              ('page.js', 'void 0;'), ('page.css', 'main {}')]:
            (page / name).write_text(content)
        service = module.FixtureService(scenario or {}, str(page))
        origin = service.start()
        self.addCleanup(service.stop)
        return service, origin

    def request(self, origin, method, path, body=None, headers=None):
        url = urlsplit(origin)
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def session(self, service, origin):
        secret = parse_qs(urlsplit(service.launch_url()).fragment)['launch'][0]
        status, headers, body = self.request(origin, 'POST', '/api/session',
            encoded({'launch': secret}), {'Origin': origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        token = json.loads(body)['csrf_token']
        return cookie, token, secret

    def act(self, origin, cookie, token, args=None, operation='queue.answer'):
        return self.request(origin, 'POST', '/api/op/' + operation,
            encoded(args or {}), {'Cookie': cookie, 'X-ADT-CSRF': token,
                'Origin': origin, 'Content-Type': 'application/json'})

    def test_routes_and_headers(self):
        module = self.module()
        service, origin = self.fixture(module)
        for route, mime, content in [('/', 'text/html', b'<main>fixture</main>'),
                ('/page.js', 'text/javascript', b'void 0;'),
                ('/page.css', 'text/css', b'main {}')]:
            with self.subTest(route=route):
                status, headers, body = self.request(origin, 'GET', route,
                    headers={'Sec-Fetch-Site': 'cross-site'})
                self.assertEqual((status, body), (200, content))
                self.assertEqual(headers['Content-Type'], mime + '; charset=utf-8')
                self.assertEqual(headers['Content-Length'], str(len(content)))
                self.assertEqual(set(headers), set(HEADERS) | {'Content-Type', 'Content-Length'})
                for key, value in HEADERS.items():
                    self.assertEqual(headers[key], value)
        for method, path in [('GET', '/favicon.ico'), ('HEAD', '/'), ('PUT', '/'), ('BREW', '/')]:
            status, headers, body = self.request(origin, method, path)
            self.assertEqual(status, 403)
            self.assertEqual(body, b'' if method == 'HEAD' else b'{"error":"refused"}')
            for key, value in HEADERS.items():
                self.assertEqual(headers[key], value)
            self.assertNotIn('Server', headers)

    def test_session_and_csrf(self):
        module = self.module()
        service, origin = self.fixture(module, {'queue.answer': [{'args': {}, 'status': 200, 'body': SUCCESS}]})
        cookie, token, secret = self.session(service, origin)
        self.assertRegex(cookie, r'^adt_session_[0-9a-f]{8}=[A-Za-z0-9_-]{43}$')
        status, headers, body = self.request(origin, 'GET', '/api/session', headers={'Cookie': cookie})
        self.assertEqual((status, json.loads(body)), (200, {'csrf_token': token}))
        # A second exchange using the consumed secret cannot make another session.
        status, _, _ = self.request(origin, 'POST', '/api/session', encoded({'launch': secret}),
            {'Origin': origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        status, _, _ = self.request(origin, 'POST', '/api/session', encoded({'launch': '\u00e9'}),
            {'Origin': origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        for bad_cookie, bad_token in [(cookie, ''), (cookie, 'wrong'), ('', token),
                                       (cookie + '; ' + cookie, token)]:
            self.assertEqual(self.act(origin, bad_cookie, bad_token)[0], 403)
        status, _, body = self.act(origin, cookie, token)
        self.assertEqual((status, json.loads(body)), (200, SUCCESS))
        record = service.requests()[-1]
        self.assertEqual(record['operation'], 'queue.answer')
        self.assertEqual(record['body'], {})
        self.assertEqual(record['headers']['X-ADT-CSRF'], token)
        # Inspect attributes on a fresh launch rather than trusting the cookie name alone.
        fresh = service.launch_url()
        status, headers, _ = self.request(origin, 'POST', '/api/session',
            encoded({'launch': parse_qs(urlsplit(fresh).fragment)['launch'][0]}),
            {'Origin': origin, 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertTrue(headers['Set-Cookie'].endswith('; HttpOnly; SameSite=Strict; Path=/'))
        self.assertNotIn('Domain=', headers['Set-Cookie'])

    def test_reads_take_query_args(self):
        module = self.module()
        args = {'scope': 'machine', 'state': 'pending', 'page_size': '100'}
        after = {'request_id': '1', 'result': {'value': 'after'}}
        service, origin = self.fixture(module, {
            'queue.list': [{'args': args, 'status': 200, 'body': SUCCESS}],
            'queue.answer': [{'args': {}, 'status': 200, 'body': SUCCESS, 'after': {
                'queue.list': [{'args': args, 'status': 200, 'body': after}]}}]})
        cookie, token, _ = self.session(service, origin)
        path = '/api/read/queue.list?scope=machine&state=pending&page_size=100'
        status, _, body = self.request(origin, 'GET', path, headers={'Cookie': cookie})
        self.assertEqual((status, json.loads(body)), (200, SUCCESS))
        for suffix in ['&scope=machine', '&label=%20', '&label=+', '&extra=x']:
            self.assertEqual(self.request(origin, 'GET', path + suffix, headers={'Cookie': cookie})[0], 403)
        self.assertEqual(self.request(origin, 'GET', '/api/read/unknown', headers={'Cookie': cookie})[0], 403)
        self.assertEqual(self.act(origin, cookie, token)[0], 200)
        status, _, body = self.request(origin, 'GET', path, headers={'Cookie': cookie})
        self.assertEqual((status, json.loads(body)), (200, after))
        # Cookie/origin/fetch-site checks also cover reads, while documents need no session.
        self.assertEqual(self.request(origin, 'GET', path)[0], 403)
        for headers in [{'Cookie': cookie, 'Origin': 'http://elsewhere.invalid'},
                        {'Cookie': cookie, 'Sec-Fetch-Site': 'cross-site'},
                        {'Cookie': cookie, 'Host': 'localhost:1'}]:
            self.assertEqual(self.request(origin, 'GET', path, headers=headers)[0], 403)

    def test_wire_carries_text_only(self):
        module = self.module()
        for value in [1, True, False, {'nested': [1]}, {'nested': [True]}]:
            for field in ['args', 'body']:
                entry = {'args': {}, 'status': 200, 'body': SUCCESS}
                entry[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(module.ScenarioRefused):
                    module.FixtureService({'queue.answer': [entry]}, '.')
        entry = {'args': {'expected_revision': '3'}, 'status': 200,
                 'body': {'request_id': '1', 'result': {'revision': '1', 'more_remaining': 'true', 'truncated': 'false'}}}
        service, origin = self.fixture(module, {'queue.answer': [entry]})
        cookie, token, _ = self.session(service, origin)
        status, _, body = self.act(origin, cookie, token, {'expected_revision': 3})
        self.assertEqual(service.lookups(), [])
        self.assertEqual(service.requests()[-1]['body'], {'expected_revision': 3})
        self.assertEqual((status, body), (403, b'{"error":"refused"}'))
        status, _, body = self.act(origin, cookie, token, {'expected_revision': '3'})
        self.assertEqual(service.lookups(), ['queue.answer'])
        self.assertEqual((status, json.loads(body)), (200, entry['body']))
        for path in (Path(__file__).parent / 'console_page_walk/scenarios').glob('*.json'):
            module.FixtureService(json.loads(path.read_text()), '.')

    def test_act_body_must_be_canonical(self):
        module = self.module()
        operation = 'queue.answer'
        entry = {'args': {'choice': 'ok', 'expected_revision': '3'},
                 'status': 200, 'body': SUCCESS}
        service, origin = self.fixture(module, {operation: [entry]})
        cookie, token, _ = self.session(service, origin)
        headers = {'Cookie': cookie, 'X-ADT-CSRF': token,
                   'Origin': origin, 'Content-Type': 'application/json'}
        path = '/api/op/' + operation
        lookups = service.lookups
        for body in (b'{"choice": "ok","expected_revision":"3"}',
                     b'{"expected_revision":"3","choice":"ok"}'):
            with self.subTest(body=body):
                status, _, response = self.request(origin, 'POST', path, body, headers)
                self.assertEqual(lookups(), [])
                self.assertEqual((status, response), (403, b'{"error":"refused"}'))
        canonical = b'{"choice":"ok","expected_revision":"3"}'
        status, _, response = self.request(origin, 'POST', path, canonical, headers)
        self.assertEqual(lookups(), [operation])
        self.assertEqual((status, json.loads(response)), (200, SUCCESS))

    def test_typed_refusal_status(self):
        module = self.module()
        for cls, status in [('wire', 400), ('auth', 401), ('version', 409),
                ('conflict', 409), ('internal', 500), ('unavailable', 503), ('admission', 422)]:
            with self.subTest(error_class=cls):
                body = {'request_id': '1', 'error': {'class': cls, 'code': 'fixture_refusal'}}
                service, origin = self.fixture(module, {'queue.answer': [
                    {'args': {'choice': 'bad'}, 'status': status, 'body': body},
                    {'args': {}, 'status': 200, 'body': SUCCESS}]})
                cookie, token, _ = self.session(service, origin)
                actual, _, raw = self.act(origin, cookie, token, {'choice': 'bad'})
                self.assertEqual((actual, json.loads(raw)), (status, body))
                actual, _, raw = self.act(origin, cookie, token)
                self.assertEqual((actual, json.loads(raw)), (200, SUCCESS))
                service.stop()

    def test_restart_and_down(self):
        module = self.module()
        service, origin = self.fixture(module, {'queue.answer': [{'args': {}, 'status': 200, 'body': SUCCESS}]})
        cookie, token, _ = self.session(service, origin)
        old_url = service.launch_url('fixture-item')
        self.assertIn('&item=fixture-item', old_url)
        service.restart()
        self.assertNotEqual(service.launch_url(), old_url.split('&')[0])
        self.assertEqual(self.act(origin, cookie, token)[0], 403)
        self.assertEqual(self.request(origin, 'GET', '/api/session', headers={'Cookie': cookie})[0], 403)
        service.set_down(True)
        with self.assertRaises((OSError, http.client.HTTPException)):
            self.request(origin, 'GET', '/')
        service.set_down(False)
        self.assertEqual(self.request(origin, 'GET', '/')[0], 200)


if __name__ == '__main__':
    unittest.main()
