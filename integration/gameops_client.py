"""游戏服务端的接入示例，仅使用 Python 标准库。"""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GameOpsError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GameOpsClient:
    def __init__(self, address, token, timeout=5):
        parsed = urlsplit(address)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('请输入平台服务地址，例如 http://127.0.0.1:8765')
        local_http = parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')
        if parsed.scheme != 'https' and not local_http:
            raise ValueError('远程接入请使用 HTTPS 地址，本机支持 HTTP。')
        if not token or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError('请从接入管理复制完整游戏密钥。')
        self.url = address.rstrip('/') + '/api/v1/'
        self.token = token
        self.timeout = timeout
        self.http = build_opener(NoRedirect())

    def _call(self, name, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        try:
            request = Request(self.url + name, data=body, headers=headers)
            with self.http.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                message = json.loads(error.read(65536)).get('error', error.reason)
            except (ValueError, AttributeError):
                message = error.reason
            raise GameOpsError(f'HTTP {error.code} · {message}', error.code) from error
        except (URLError, OSError) as error:
            raise GameOpsError(f'无法连接平台或未收到完整响应 · {error}') from error
        except ValueError as error:
            raise GameOpsError('平台响应不是有效 JSON，请检查服务地址。') from error

    def event(self, kind, payload):
        if kind not in ('chat', 'reports', 'levels', 'matches', 'receipts'):
            raise ValueError('未知事件类型')
        return self._call(kind, payload)

    def pending(self):
        return self._call('commands')['items']
