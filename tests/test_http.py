"""Exercise the actual HTTP boundary and bundled game adapter on ephemeral loopback ports."""
import http.client
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import service


class SilentHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class GameHttpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.password = secrets.token_urlsafe(30)
        self.ticket = secrets.token_urlsafe(30)
        self.start_server()
        self.addCleanup(self.stop_server)

    def start_server(self):
        self.http = make_server('127.0.0.1', 0, None, server_class=service.LocalServer, handler_class=SilentHandler)
        self.port = self.http.server_port
        self.origin = f'http://127.0.0.1:{self.port}'
        self.http.set_app(service.application(self.data/'platform.sqlite3', self.password, self.origin, self.ticket))
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.03}, daemon=True)
        self.thread.start()

    def stop_server(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)

    def request(self, path, payload=None, *, cookie='', key='', expect=200, origin=None):
        headers = {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if cookie:
            headers['Cookie'] = cookie
        if key:
            headers['Authorization'] = 'Bearer ' + key
        if origin:
            headers['Origin'] = origin
        with closing(http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)) as connection:
            connection.request('GET' if payload is None else 'POST', path, body, headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            self.assertEqual(response.status, expect, f'{path}: unexpected HTTP status')
            return data, response.getheader('Set-Cookie', '')

    def login(self, credential):
        status, header = self.request('/api/login', {'password': credential})
        self.assertTrue(status['authenticated'])
        self.assertIn('HttpOnly', header)
        self.assertIn('SameSite=Strict', header)
        cookies = SimpleCookie(header)
        return 'gameops_session=' + cookies['gameops_session'].value

    def simulate(self, token, state):
        env = dict(os.environ, GAMEOPS_GAME_TOKEN=token, NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost', PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-B', str(ROOT/'integration'/'simulate.py'),
                                 '--url', self.origin, '--state', str(state)],
                                env=env, input='', capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, 'Independent simulator process did not complete')

    def test_login_ticket_session_and_logout(self):
        session, _ = self.request('/api/session')
        self.assertFalse(session['authenticated'])
        self.request('/api/admin/games', expect=401)
        self.request('/api/login', {'password': 'invalid'}, expect=401)
        cookie = self.login(self.ticket)
        self.request('/api/login', {'password': self.ticket}, expect=401)
        self.assertTrue(self.request('/api/session', cookie=cookie)[0]['authenticated'])
        self.request('/api/admin/games', {'name':'example'}, cookie=cookie, origin='https://other.invalid', expect=403)
        self.assertFalse(self.request('/api/logout', {}, cookie=cookie)[0]['authenticated'])
        self.request('/api/admin/games', cookie=cookie, expect=401)
        self.login(self.password)

    def test_rotation_and_game_scoped_wordlists(self):
        cookie = self.login(self.password)
        a, _ = self.request('/api/admin/games', {'name':'a'}, cookie=cookie)
        b, _ = self.request('/api/admin/games', {'name':'b'}, cookie=cookie)
        a_admin = '/api/admin/games/'+a['id']
        self.request(a_admin+'/words', {'term':'abc','enabled':True,'match_type':'contains'}, cookie=cookie)
        chat = dict(event_id='one', player_id='p', text=' abc ')
        self.assertEqual(self.request('/api/v1/chat', chat, key=a['token'])[0]['decision'], 'block')
        self.assertEqual(self.request('/api/v1/chat', chat, key=b['token'])[0]['decision'], 'allow')
        self.request(a_admin+'/words', key=b['token'], expect=401)
        rotated, _ = self.request(a_admin+'/rotate', {}, cookie=cookie)
        self.request('/api/v1/commands', key=a['token'], expect=401)
        self.request('/api/v1/commands', key=rotated['token'])
        self.assertEqual(self.request('/api/v1/chat', chat, key=rotated['token'])[0]['decision'], 'block')
        self.assertEqual(self.request(a_admin+'/overview', cookie=cookie)[0]['block_count'], 1)
        self.assertEqual(self.request('/api/admin/games/'+b['id']+'/overview', cookie=cookie)[0]['block_count'], 0)

    def test_independent_game_execution_and_restarted_platform(self):
        cookie = self.login(self.password)
        a, _ = self.request('/api/admin/games', {'name':'integration'}, cookie=cookie)
        b, _ = self.request('/api/admin/games', {'name':'other'}, cookie=cookie)
        admin = '/api/admin/games/'+a['id']
        settings = self.request(admin+'/policy', cookie=cookie)[0]
        self.assertEqual(settings['enforcement'], 'observe')
        account_db = self.data/'accounts.sqlite3'
        self.simulate(a['token'], account_db)
        observed = self.request(admin+'/matches', cookie=cookie)[0]['items']
        self.assertEqual([(m['decision'],m['hits'],m['command_id']) for m in observed], [('observed',[3],None)])
        self.assertEqual(self.request('/api/v1/commands', key=a['token'])[0]['items'], [])
        with closing(sqlite3.connect(account_db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM freezes').fetchone()[0], 0)
        self.request(admin+'/policy', {'version':settings['version'], 'enforcement':'enforce'}, cookie=cookie)
        self.simulate(a['token'], account_db)
        commands = self.request(admin+'/penalties', cookie=cookie)[0]['items']
        self.assertEqual(len(commands), 1)
        cmd = commands[0]
        self.assertEqual((cmd['status'],cmd['hits']), ('succeeded',[3]))
        with closing(sqlite3.connect(account_db)) as db:
            freezes = db.execute('SELECT command_id,player_id,expires_at FROM freezes').fetchall()
        self.assertEqual(freezes, [(cmd['command_id'], cmd['player_id'], cmd['expires_at'])])
        self.request('/api/v1/receipts', {'command_id':cmd['command_id'],'event_id':'unrelated','status':'succeeded'}, key=b['token'], expect=404)
        self.assertEqual(self.request('/api/admin/games/'+b['id']+'/penalties', cookie=cookie)[0]['items'], [])
        self.stop_server()
        self.start_server()
        cookie = self.login(self.password)
        overview = self.request(admin+'/overview', cookie=cookie)[0]
        self.assertEqual((overview['report_count'], overview['match_count'], overview['penalty_count']), (30,2,1))
        self.assertEqual(self.request(admin+'/penalties', cookie=cookie)[0]['items'][0], cmd)
        self.assertEqual(self.request('/api/v1/commands', key=a['token'])[0]['items'], [])


if __name__ == '__main__':
    unittest.main()
