"""Management and game HTTP endpoints for the single-node integration edition."""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import quote, urlsplit
from wsgiref.simple_server import WSGIServer, make_server

import domain

ROOT = Path(__file__).resolve().parent
SESSION_AGE = 8 * 3600
STATIC = {
    '/': ('static/index.html', 'text/html'),
    '/app.js': ('static/app.js', 'text/javascript'),
    '/style.css': ('static/style.css', 'text/css'),
    '/integration/guide': ('integration/接入指南.md', 'text/plain'),
    '/integration/openapi.json': ('integration/openapi.json', 'application/json'),
}


def digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def database(path):
    db = sqlite3.connect(path, timeout=6)
    db.row_factory = sqlite3.Row
    return db


def load_json(env):
    if env.get('CONTENT_TYPE', '').split(';')[0].strip() != 'application/json':
        raise domain.DomainError('需要 application/json 请求。', 415)
    length = int(env.get('CONTENT_LENGTH') or 0)
    if not 0 < length <= 65536:
        raise domain.DomainError('请求体应为 1 至 65536 字节。', 413)
    obj = json.loads(env['wsgi.input'].read(length).decode('utf-8'))
    if not isinstance(obj, dict):
        raise domain.DomainError('需要一个 JSON 对象。')
    return obj


def cookie_token(env):
    cookies = SimpleCookie()
    try:
        cookies.load(env.get('HTTP_COOKIE', ''))
    except CookieError:
        return ''
    item = cookies.get('gameops_session')
    return item.value if item else ''


def application(db_path, admin_password, origin, login_ticket=''):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(database(db_path)) as db:
        db.executescript('''
           PRAGMA journal_mode=WAL;
           CREATE TABLE IF NOT EXISTS apps (
             id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, key_hash TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL);
           CREATE TABLE IF NOT EXISTS admin_sessions (
             sid TEXT PRIMARY KEY, credential_id TEXT NOT NULL, expires INTEGER NOT NULL);
        ''')
        domain.init_db(db)
    credential_id = digest(admin_password)
    ticket_hash = digest(login_ticket) if login_ticket else None
    ticket_lock = threading.Lock()
    ticket_expires = time.monotonic() + 90
    parts = urlsplit(origin)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path or parts.query or parts.fragment or parts.username:
        raise ValueError('GAMEOPS_ORIGIN 需要包含协议和主机的完整来源，例如 https://ops.example.com')
    if parts.scheme == 'http' and parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('远程部署需要 HTTPS 来源；本机开发使用 http://127.0.0.1:端口。')
    suffix = '; Path=/; HttpOnly; SameSite=Strict' + ('; Secure' if parts.scheme == 'https' else '')

    def dispatch(db, env, method, path, headers):
        nonlocal ticket_hash
        now = int(time.time())
        write = method == 'POST'
        if write and env.get('HTTP_ORIGIN', origin) != origin:
            raise domain.DomainError('请求来源不匹配。', 403)
        # Checking authentication inside the write transaction serializes key rotation and requests.
        payload = load_json(env) if write else {}
        db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        if path.startswith('/api/v1/'):
            auth = env.get('HTTP_AUTHORIZATION', '')
            token = auth[7:] if auth.startswith('Bearer ') else ''
            game = db.execute('SELECT id FROM apps WHERE key_hash=?', (digest(token),)).fetchone()
            if not game:
                raise domain.DomainError('游戏密钥无效。', 401)
            kind = path.removeprefix('/api/v1/')
            if kind == 'commands' and not write:
                return domain.pending(db, game['id'])
            if kind in domain.FIELDS and write:
                return domain.game_event(db, game['id'], kind, payload)
            raise domain.DomainError('游戏接口或请求方法不存在。', 404)
        if path == '/api/login' and write:
            password = domain.string(payload, 'password', 512)
            candidate = digest(password)
            valid = hmac.compare_digest(candidate, credential_id)
            with ticket_lock:
                if ticket_hash and time.monotonic() < ticket_expires and hmac.compare_digest(candidate, ticket_hash):
                    valid = True
                    ticket_hash = None
            if not valid:
                raise domain.DomainError('管理密码无效。', 401)
            sid = secrets.token_urlsafe(32)
            db.execute('DELETE FROM admin_sessions WHERE expires<=? OR sid=?', (now,digest(cookie_token(env))))
            db.execute('INSERT INTO admin_sessions VALUES(?,?,?)', (digest(sid),credential_id,now+SESSION_AGE))
            headers.append(('Set-Cookie','gameops_session='+sid+'; Max-Age='+str(SESSION_AGE)+suffix))
            return {'authenticated': True}
        sid_hash = digest(cookie_token(env))
        signed_in = db.execute('SELECT 1 FROM admin_sessions WHERE sid=? AND expires>? AND credential_id=?',
                               (sid_hash,now,credential_id)).fetchone() is not None
        if path == '/api/session' and not write:
            return {'authenticated': signed_in}
        if not signed_in:
            raise domain.DomainError('请先登录管理工作台。', 401)
        if path == '/api/logout' and write:
            db.execute('DELETE FROM admin_sessions WHERE sid=?', (sid_hash,))
            headers.append(('Set-Cookie','gameops_session=; Max-Age=0'+suffix))
            return {'authenticated': False}
        route = path.strip('/').split('/')
        if route[:3] != ['api','admin','games']:
            raise domain.DomainError('接口不存在。', 404)
        if len(route) == 3 and not write:
            return {'items': [dict(r) for r in db.execute('SELECT id,name,created_at FROM apps ORDER BY name')]}
        if len(route) == 3 and write:
            name = domain.string(payload, 'name', 80)
            if db.execute('SELECT 1 FROM apps WHERE name=?', (name,)).fetchone():
                raise domain.DomainError('已有同名游戏。', 409)
            game_id = secrets.token_hex(12)
            token = 'gs_'+secrets.token_urlsafe(32)
            db.execute('INSERT INTO apps VALUES(?,?,?,?)',(game_id,name,digest(token),now))
            domain.audit(db,game_id,'games','创建接入游戏',{'name':name})
            initial = dict(domain.policy(db, game_id), enforcement='observe')
            db.execute('INSERT INTO ops_policy(game,document) VALUES(?,?)', (game_id,domain.encode(initial)))
            return {'id':game_id,'name':name,'token':token}
        if len(route) != 5:
            raise domain.DomainError('接口不存在。', 404)
        game_id, section = route[3:]
        game = db.execute('SELECT name FROM apps WHERE id=?',(game_id,)).fetchone()
        if not game:
            raise domain.DomainError('游戏不存在。', 404)
        if write and section == 'rotate':
            token = 'gs_'+secrets.token_urlsafe(32)
            db.execute('UPDATE apps SET key_hash=? WHERE id=?',(digest(token),game_id))
            domain.audit(db,game_id,'games','轮换游戏密钥',{})
            return {'id':game_id,'name':game['name'],'token':token}
        return domain.admin_set(db,game_id,section,payload) if write else domain.admin_get(db,game_id,section)

    def app(env, start_response):
        path = env['PATH_INFO']
        method = env['REQUEST_METHOD']
        headers = [('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),
                   ('Referrer-Policy','no-referrer'),('Content-Security-Policy',
                   "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        mime = 'application/json'
        status = 200
        try:
            if env.get('HTTP_HOST','').lower() != parts.netloc.lower():
                raise domain.DomainError('请使用启动时配置的工作台地址。', 403)
            if method == 'GET' and path in STATIC:
                filename, mime = STATIC[path]
                body = (ROOT/filename).read_bytes()
            elif method == 'GET' and path == '/health':
                with closing(database(db_path)) as db:
                    db.execute('SELECT 1 FROM apps LIMIT 1').fetchone()
                body = b'{"ok":true,"service":"gameops"}'
            elif path.startswith('/api/') and method in ('POST','GET'):
                with closing(database(db_path)) as db, db:
                    result = dispatch(db,env,method,path,headers)
                    body = domain.encode(result).encode()
            else:
                raise domain.DomainError('资源不存在。',404)
        except domain.DomainError as exc:
            status, body = exc.status, domain.encode({'error':str(exc)}).encode()
        except (ValueError,UnicodeError,RecursionError):
            status, body = 400, domain.encode({'error':'请求数据格式无效。'}).encode()
        except sqlite3.OperationalError:
            status, body = 503, domain.encode({'error':'数据库暂不可用，请稍后重试。'}).encode()
        headers.extend([('Content-Type',mime+'; charset=utf-8'),('Content-Length',str(len(body)))])
        start_response(str(status)+' '+HTTPStatus(status).phrase, headers)
        return [body]
    return app


def create_hosted_app():
    """Factory for a single-node deployment behind an HTTPS proxy."""
    password = os.environ['GAMEOPS_ADMIN_PASSWORD']
    origin = os.environ['GAMEOPS_ORIGIN'].rstrip('/')
    if len(password)<24 or not origin.startswith('https://'):
        raise ValueError('独立部署需要 HTTPS 地址和至少 24 字符的随机管理员密码。')
    return application(os.environ['GAMEOPS_DB'], password, origin)


class LocalServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open',action='store_true')
    parser.add_argument('--local-name', choices=('127.0.0.1', 'localhost'), default='127.0.0.1')
    parser.add_argument('--port',type=int,default=int(os.environ.get('GAMEOPS_PORT','8765')))
    args = parser.parse_args()
    password = os.environ.get('GAMEOPS_ADMIN_PASSWORD') or secrets.token_urlsafe(24)
    ticket = secrets.token_urlsafe(32) if args.open else ''
    origin = f'http://{args.local_name}:{args.port}'
    db = os.environ.get('GAMEOPS_DB',ROOT/'data'/'gameops.sqlite3')
    app = application(db,password,origin,ticket)
    with make_server('127.0.0.1', args.port, app, server_class=LocalServer) as http:
        print(f'工作台：{origin}\n本次管理密码：{password}\n数据库：{db}\n关闭本窗口即可停止服务。',flush=True)
        if args.open:
            webbrowser.open(origin+'/#login='+quote(ticket))
        try:
            http.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    try:
        main()
    except OSError as error:
        if getattr(error, 'winerror', None) == 10048 or error.errno in (48, 98):
            raise SystemExit('该端口已在使用。如果工作台已启动，请使用原窗口；也可用 start.cmd --port 8766 指定另一端口。')
        raise
