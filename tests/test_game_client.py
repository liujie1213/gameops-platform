"""Exercise the sample game's HTTP integration, including an interrupted receipt."""

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'integration')]
import service
import simulate
from gameops_client import GameOpsClient, GameOpsError


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class GameClientIntegration(unittest.TestCase):
    def test_observe_then_enforce_and_resume_receipt(self):
        with tempfile.TemporaryDirectory() as tempdir:
            folder = Path(tempdir)
            with make_server('127.0.0.1', 0, None, handler_class=QuietHandler) as http:
                origin = f'http://127.0.0.1:{http.server_port}'
                http.set_app(service.application(folder / 'ops.db', 'test-admin', origin))
                thread = threading.Thread(target=http.serve_forever, daemon=True)
                thread.start()
                try:
                    self.exercise(origin, folder)
                finally:
                    http.shutdown()
                    thread.join()

    def exercise(self, origin, folder):
        admin = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

        def api(path, obj=None):
            content = None if obj is None else json.dumps(obj).encode()
            req = Request(origin + path, data=content, headers={'Content-Type': 'application/json'})
            with admin.open(req, timeout=5) as response:
                return json.load(response)

        api('/api/login', {'password': 'test-admin'})
        game = api('/api/admin/games', {'name': 'test-game'})
        base = '/api/admin/games/' + game['id']
        client = GameOpsClient(origin, game['token'])
        api(base + '/words', {'term': '代练接单', 'list_type': 'block', 'match_type': 'contains', 'enabled': True})

        with closing(simulate.open_state(folder / 'accounts.db')) as accounts, redirect_stdout(io.StringIO()):
            simulate.simulate(client, accounts, game['id'])
            observation = api(base + '/matches')['items'][0]
            self.assertEqual((observation['decision'], observation['report_count'], observation['hits']), ('observed', 15, [3]))
            self.assertEqual(api(base + '/overview')['block_count'], 1)
            self.assertEqual(client.pending(), [])

            version = api(base + '/policy')['version']
            api(base + '/policy', {'enforcement': 'enforce', 'version': version})

            class Interrupted(GameOpsClient):
                def event(self, kind, payload):
                    result = super().event(kind, payload)
                    if kind == 'receipts':
                        raise GameOpsError('receipt response lost')
                    return result

            with self.assertRaises(GameOpsError):
                simulate.simulate(Interrupted(origin, game['token']), accounts, game['id'])
            freeze = tuple(accounts.execute('SELECT * FROM freezes').fetchone())
            simulate.simulate(client, accounts, game['id'])
            self.assertEqual([tuple(r) for r in accounts.execute('SELECT * FROM freezes')], [freeze])
            self.assertEqual(accounts.execute('SELECT COUNT(*) FROM runs WHERE done=1').fetchone()[0], 2)
            penalties = api(base + '/penalties')['items']
            self.assertEqual(len(penalties), 1)
            command = penalties[0]
            self.assertEqual(command['status'], 'succeeded')
            self.assertEqual(command['expires_at'] - command['created_at'], 6 * 3600)
            self.assertEqual(api(base + '/overview')['report_count'], 30)
            self.assertEqual(client.pending(), [])


if __name__ == '__main__':
    unittest.main()
