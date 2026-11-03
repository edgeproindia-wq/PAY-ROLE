"""The office sync agent: reads a (fake) machine, sends new punches in batches, and moves forward only after the website accepted them."""
import datetime
import io
import json
import logging
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import TestCase

from .bio_agent import agent
from .bio_agent.agent import AgentError, Lock, http_request, load_config, load_state, run_once, save_state, select_new

D = datetime.datetime
KEY = 'bio_SECRET_KEY_VALUE_123'


class FakeReader:
    def __init__(self, records=None, error=None):
        self.records, self.error = records or [], error

    def read(self):
        if self.error:
            raise AgentError(self.error)
        return list(self.records)


class FakePost:
    """Records every request; answers with the queued (status, body) pairs, then 200 OK."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], list(answers or [])

    def __call__(self, cfg, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.answers.pop(0) if self.answers else (200, {'ok': True, 'new': len((payload or {}).get('punches', []))})


class AgentBase(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = {'device_ip': '192.168.1.50', 'device_port': 4370, 'device_password': 0, 'server_url': 'https://pay.example.com', 'api_key': KEY, 'first_run_days': 7,
                    'batch_size': 2, 'timeout_seconds': 5, 'state_file': os.path.join(self.dir, 'state.json'), 'lock_file': os.path.join(self.dir, 'agent.lock'),
                    'log_file': os.path.join(self.dir, 'agent.log')}
        self.now = D(2026, 11, 2, 12, 0, 0)

    def records(self, *items):
        return [(uid, D(2026, 11, 2, h, m, s)) for uid, h, m, s in items]


class SelectTests(AgentBase):
    def test_the_first_run_sends_only_the_last_few_days(self):
        recs = [('1', self.now - datetime.timedelta(days=30)), ('1', self.now - datetime.timedelta(days=3)), ('2', self.now - datetime.timedelta(hours=1))]
        self.assertEqual([t for t, _u in select_new(recs, None, self.now, 7)], [self.now - datetime.timedelta(days=3), self.now - datetime.timedelta(hours=1)])

    def test_after_that_only_punches_from_the_last_one_sent_and_oldest_first(self):
        recs = self.records(('3', 9, 5, 0), ('1', 9, 0, 0), ('2', 9, 0, 0), ('9', 8, 59, 59))
        got = select_new(recs, D(2026, 11, 2, 9, 0, 0), self.now, 7)
        self.assertEqual([(u, t.strftime('%H:%M:%S')) for t, u in got], [('1', '09:00:00'), ('2', '09:00:00'), ('3', '09:05:00')])      # the last second is re-sent, the website ignores repeats


class RunTests(AgentBase):
    def test_new_punches_are_sent_in_batches_and_the_state_moves_forward(self):
        reader, post = FakeReader(self.records(('1', 9, 0, 0), ('2', 9, 1, 0), ('3', 9, 2, 0), ('4', 9, 3, 0), ('5', 9, 4, 0))), FakePost()
        r = run_once(self.cfg, reader, post, self.now)
        self.assertEqual((r['read'], r['new'], r['sent'], len(post.calls)), (5, 5, 5, 3))
        self.assertEqual([len(c[2]['punches']) for c in post.calls], [2, 2, 1])
        self.assertEqual(post.calls[0][2]['punches'][0], {'id': '1', 'time': '2026-11-02T09:00:00'})
        self.assertEqual(load_state(self.cfg['state_file']), D(2026, 11, 2, 9, 4, 0))
        post2 = FakePost()
        run_once(self.cfg, reader, post2, self.now)                    # nothing new: only the last second is re-sent
        self.assertEqual([len(c[2]['punches']) for c in post2.calls], [1])

    def test_a_failed_batch_keeps_the_state_at_the_last_accepted_one(self):
        reader = FakeReader(self.records(('1', 9, 0, 0), ('2', 9, 1, 0), ('3', 9, 2, 0), ('4', 9, 3, 0)))
        post = FakePost([(200, {'ok': True}), (500, {'error': 'boom'})])
        with self.assertRaises(AgentError):
            run_once(self.cfg, reader, post, self.now)
        self.assertEqual(load_state(self.cfg['state_file']), D(2026, 11, 2, 9, 1, 0))
        post2 = FakePost()
        r = run_once(self.cfg, reader, post2, self.now)                  # next run continues from there
        self.assertEqual(r['sent'], 3)

    def test_a_refused_key_stops_with_a_clear_message_and_sends_nothing_more(self):
        post = FakePost([(401, {'ok': False})])
        with self.assertRaises(AgentError) as ctx:
            run_once(self.cfg, FakeReader(self.records(('1', 9, 0, 0))), post, self.now)
        self.assertIn('refused the key', str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIsNone(load_state(self.cfg['state_file']))

    def test_a_machine_that_cannot_be_read_changes_nothing(self):
        post = FakePost()
        with self.assertRaises(AgentError):
            run_once(self.cfg, FakeReader(error='Could not connect to the machine'), post, self.now)
        self.assertEqual((post.calls, load_state(self.cfg['state_file'])), ([], None))

    def test_an_unreadable_state_file_means_a_fresh_start_not_a_crash(self):
        with open(self.cfg['state_file'], 'w') as fh:
            fh.write('{broken')
        self.assertIsNone(load_state(self.cfg['state_file']))
        r = run_once(self.cfg, FakeReader(self.records(('1', 9, 0, 0))), FakePost(), self.now)
        self.assertEqual(r['sent'], 1)

    def test_a_website_that_says_not_ok_is_an_error(self):
        with self.assertRaises(AgentError):
            run_once(self.cfg, FakeReader(self.records(('1', 9, 0, 0))), FakePost([(200, {'ok': False, 'error': 'nope'})]), self.now)
        self.assertIsNone(load_state(self.cfg['state_file']))


class ConfigTests(AgentBase):
    def write(self, **changes):
        cfg = {'device_ip': '192.168.1.50', 'server_url': 'https://pay.example.com/', 'api_key': KEY}
        cfg.update(changes)
        path = os.path.join(self.dir, 'config.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({k: v for k, v in cfg.items() if v is not None}, fh)
        return path

    def test_defaults_are_filled_in_and_the_address_is_tidied(self):
        cfg = load_config(self.write())
        self.assertEqual((cfg['device_port'], cfg['batch_size'], cfg['first_run_days'], cfg['server_url']), (4370, 500, 7, 'https://pay.example.com'))
        self.assertTrue(cfg['state_file'].startswith(self.dir))

    def test_bad_configs_are_refused_with_a_plain_message(self):
        for changes, text in (({'api_key': None}, 'api_key'), ({'device_ip': ''}, 'device_ip'), ({'server_url': 'http://pay.example.com'}, 'https://'),
                              ({'device_port': 'abc'}, 'must be a number'), ({'batch_size': 0}, 'between 1 and 2000'), ({'batch_size': 5000}, 'between 1 and 2000')):
            with self.assertRaises(AgentError) as ctx:
                load_config(self.write(**changes))
            self.assertIn(text, str(ctx.exception))
        with self.assertRaises(AgentError):
            load_config(os.path.join(self.dir, 'missing.json'))
        bad = os.path.join(self.dir, 'bad.json')
        with open(bad, 'w') as fh:
            fh.write('{not json')
        with self.assertRaises(AgentError):
            load_config(bad)

    def test_plain_http_is_allowed_only_for_the_local_computer(self):
        self.assertEqual(load_config(self.write(server_url='http://127.0.0.1:8000'))['server_url'], 'http://127.0.0.1:8000')


class LockTests(AgentBase):
    def test_a_second_run_is_refused_while_one_is_running_and_the_lock_is_released(self):
        with Lock(self.cfg['lock_file']):
            with self.assertRaises(AgentError):
                with Lock(self.cfg['lock_file']):
                    pass
        with Lock(self.cfg['lock_file']):
            pass                                                   # released after the first run

    def test_a_lock_left_by_a_crash_is_replaced_after_ten_minutes(self):
        with open(self.cfg['lock_file'], 'w') as fh:
            fh.write('123')
        old = os.path.getmtime(self.cfg['lock_file']) - 3600
        os.utime(self.cfg['lock_file'], (old, old))
        with Lock(self.cfg['lock_file']):
            pass


class MainTests(AgentBase):
    def config_file(self, **changes):
        cfg = dict({'device_ip': '192.168.1.50', 'server_url': 'https://pay.example.com', 'api_key': KEY, 'batch_size': 100}, **changes)
        path = os.path.join(self.dir, 'config.json')
        with open(path, 'w') as fh:
            json.dump(cfg, fh)
        return path

    def tearDown(self):
        for h in list(agent.log.handlers):
            agent.log.removeHandler(h)
            h.close()

    def test_a_normal_run_logs_without_ever_writing_the_key(self):
        path = self.config_file()
        post = FakePost()
        code = agent.main(['--config', path], reader_factory=lambda cfg: FakeReader([('1', D.now())]), post=post)
        self.assertEqual(code, 0)
        text = open(os.path.join(self.dir, 'agent.log'), encoding='utf-8').read()
        self.assertIn('Sent 1 punch', text)
        self.assertNotIn(KEY, text)

    def test_a_failed_run_returns_1_and_logs_the_reason_not_the_key(self):
        path = self.config_file()
        code = agent.main(['--config', path], reader_factory=lambda cfg: FakeReader(error='Could not connect to the machine at 192.168.1.50:4370'), post=FakePost())
        self.assertEqual(code, 1)
        text = open(os.path.join(self.dir, 'agent.log'), encoding='utf-8').read()
        self.assertIn('Could not connect to the machine', text)
        self.assertNotIn(KEY, text)

    def test_test_mode_checks_both_ends_and_sends_nothing(self):
        path = self.config_file()
        post = FakePost([(200, {'ok': True, 'device': 'Hosur'})])
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = agent.main(['--config', path, '--test'], reader_factory=lambda cfg: FakeReader([('1', D(2026, 11, 2, 9, 0, 0))]), post=post)
        self.assertEqual(code, 0)
        self.assertIn('Machine: OK, 1 punch(es) stored, latest at 2026-11-02T09:00:00', out.getvalue())
        self.assertIn('device "Hosur"', out.getvalue())
        self.assertEqual([c[0] for c in post.calls], ['GET'])

    def test_test_mode_reports_a_refused_key(self):
        path = self.config_file()
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            code = agent.main(['--config', path, '--test'], reader_factory=lambda cfg: FakeReader([]), post=FakePost([(401, {})]))
        self.assertEqual(code, 1)

    def test_a_missing_config_prints_an_error_and_returns_1(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            self.assertEqual(agent.main(['--config', os.path.join(self.dir, 'nope.json')]), 1)
        self.assertIn('config file was not found', out.getvalue())


class _Handler(BaseHTTPRequestHandler):
    seen = []

    def _reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        type(self).seen.append((self.headers.get('Authorization'), json.loads(self.rfile.read(n))))
        if self.headers.get('Authorization') != 'Bearer ' + KEY:
            return self._reply(401, {'ok': False})
        self._reply(200, {'ok': True, 'new': 1})

    def do_GET(self):
        self._reply(200 if self.headers.get('Authorization') == 'Bearer ' + KEY else 401, {'ok': True, 'device': 'Local'})

    def log_message(self, *args):
        pass


class RealHttpTests(AgentBase):
    def setUp(self):
        super().setUp()
        _Handler.seen = []
        self.server = HTTPServer(('127.0.0.1', 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cfg['server_url'] = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        if not getattr(self, 'stopped', False):
            self.server.shutdown()
            self.server.server_close()

    def test_the_real_http_function_sends_the_key_in_the_header_and_reads_the_answer(self):
        status, body = http_request(self.cfg, 'POST', '/api/biometric/punches/', {'punches': [{'id': '1', 'time': '2026-11-02T09:00:00'}]})
        self.assertEqual((status, body['ok']), (200, True))
        self.assertEqual(_Handler.seen[0][0], 'Bearer ' + KEY)
        self.assertEqual(http_request(dict(self.cfg, api_key='wrong'), 'GET', '/api/biometric/punches/')[0], 401)

    def test_an_unreachable_website_is_a_plain_error(self):
        self.server.shutdown()
        self.server.server_close()
        self.stopped = True
        with self.assertRaises(AgentError) as ctx:
            http_request(self.cfg, 'GET', '/x')
        self.assertIn('Could not reach the website', str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_a_whole_run_over_real_http(self):
        reader = FakeReader([('76', D(2026, 11, 2, 9, 0, 0)), ('76', D(2026, 11, 2, 18, 0, 0))])
        r = run_once(self.cfg, reader, http_request, D(2026, 11, 2, 20, 0, 0))
        self.assertEqual(r['sent'], 2)
        self.assertEqual(len(_Handler.seen[0][1]['punches']), 2)
