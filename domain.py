"""Game-scoped moderation, gameplay decisions, command delivery, and audit history.

The HTTP layer authenticates the game and encloses each write in BEGIN IMMEDIATE.
All times are Unix seconds. Persisting immutable responses makes retries safe even
after an operator publishes a new policy or changes the word list.
"""

import hashlib
import json
import math
import time
import uuid


class DomainError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


RULES = [
    dict(id=1, reports=10, level=30, kd=5, kills=10, avg=None),
    dict(id=2, reports=10, level=30, kd=1.5, kills=10, avg=20),
    dict(id=3, reports=15, level=None, kd=2, kills=20, avg=None),
    dict(id=4, reports=20, level=None, kd=2, kills=10, avg=None),
    dict(id=5, reports=30, level=None, kd=2, kills=5, avg=None),
]
GAME_TYPES = (2, 5)
DURATION = 6 * 3600
DAY = 86400
FIELDS = {
    'chat': {'player_id', 'text'}, 'levels': {'player_id', 'level'},
    'reports': {'player_id', 'reporter_id', 'category'},
    'matches': {'player_id', 'match_id', 'game_type', 'kd', 'kill_num', 'average_rank', 'level'},
    'receipts': {'command_id', 'status', 'message'},
}


def init_db(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS ops_policy (
          game TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ops_words (
          game TEXT NOT NULL, term TEXT NOT NULL, match_type TEXT NOT NULL,
          enabled INTEGER NOT NULL, hits INTEGER NOT NULL DEFAULT 0,
          list_type TEXT NOT NULL DEFAULT 'block', PRIMARY KEY(game,term));
        CREATE TABLE IF NOT EXISTS ops_events (
          game TEXT NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL,
          occurred_at INTEGER NOT NULL, player TEXT NOT NULL, reporter TEXT, category TEXT,
          match_id TEXT, level INTEGER, blocked INTEGER NOT NULL, document TEXT NOT NULL,
          response TEXT NOT NULL, PRIMARY KEY(game,event_id));
        CREATE INDEX IF NOT EXISTS ops_timeline ON ops_events(game,kind,occurred_at DESC);
        CREATE INDEX IF NOT EXISTS ops_player_time ON ops_events(game,kind,player,occurred_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS ops_one_settlement ON ops_events(game,player,match_id)
          WHERE kind='matches';
        CREATE INDEX IF NOT EXISTS ops_chat_counts ON ops_events(game,kind,blocked);
        CREATE TABLE IF NOT EXISTS ops_penalties (
          game TEXT NOT NULL, command_id TEXT NOT NULL, player_id TEXT NOT NULL, match_id TEXT NOT NULL,
          status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          message TEXT NOT NULL, hits TEXT NOT NULL, PRIMARY KEY(game,command_id),
          UNIQUE(game,player_id,match_id));
        CREATE INDEX IF NOT EXISTS ops_delivery ON ops_penalties(game,status,created_at);
        CREATE TABLE IF NOT EXISTS ops_audit (
          id INTEGER PRIMARY KEY, game TEXT NOT NULL, at INTEGER NOT NULL, kind TEXT NOT NULL,
          summary TEXT NOT NULL, details TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ops_audit_game ON ops_audit(game,id DESC);
    ''')
    if 'list_type' not in {col[1] for col in db.execute('PRAGMA table_info(ops_words)')}:
        db.execute("ALTER TABLE ops_words ADD COLUMN list_type TEXT NOT NULL DEFAULT 'block'")


def encode(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise DomainError('数据格式不正确，请提供有效的 JSON。') from exc


def fields(payload, allowed):
    if not isinstance(payload, dict) or not set(payload) <= set(allowed):
        raise DomainError('请求含有未知字段，或请求体并非对象。')


def string(obj, key, limit=128, *, blank=False, raw=False):
    value = obj.get(key)
    if not isinstance(value, str) or len(value) > limit or (not blank and not value.strip()):
        raise DomainError(f'{key} 需要有效文本，最多 {limit} 个字符。')
    try:
        value.encode('utf-8')
    except UnicodeError as exc:
        raise DomainError(f'{key} 含有无效字符。') from exc
    return value if raw else value.strip()


def number(obj, key, *, integer=False, maximum=2**31-1):
    val = obj.get(key)
    valid = type(val) is int if integer else type(val) in (float, int)
    if not valid or not 0 <= val <= maximum or not math.isfinite(val):
        raise DomainError(f'{key} 需要 0 至 {maximum} 范围内的有效' + ('整数。' if integer else '数值。'))
    return val


def choice(obj, key, options):
    value = obj.get(key)
    if value not in options:
        raise DomainError(f'{key} 不在可选范围内。')
    return value


def boolean(obj, key):
    if type(obj.get(key)) is not bool:
        raise DomainError(f'{key} 需要 true 或 false。')
    return obj[key]


def policy(db, game):
    stored = db.execute('SELECT document FROM ops_policy WHERE game=?', (game,)).fetchone()
    if stored:
        document = json.loads(stored[0])
        document.setdefault('enforcement', 'enforce')
        return document
    return dict(enabled=True, enforcement='enforce', version=1, report_mode='events', report_category='',
                rules=[r.copy() for r in RULES], game_types=list(GAME_TYPES), duration_seconds=DURATION)


def audit(db, game, kind, summary, details):
    db.execute('INSERT INTO ops_audit(game,at,kind,summary,details) VALUES(?,?,?,?,?)',
               (game, int(time.time()), kind, summary, encode(details)))


def _matches(db, game, player, args, occurred_at, now):
    match = string(args, 'match_id')
    if db.execute("SELECT 1 FROM ops_events WHERE game=? AND kind='matches' AND player=? AND match_id=?",
                  (game, player, match)).fetchone():
        raise DomainError('同一玩家的这场对局已经结算，请用原 event_id 和原始请求重试。', 409)
    game_type = number(args, 'game_type', integer=True)
    kd = number(args, 'kd')
    kills = number(args, 'kill_num', integer=True)
    average = number(args, 'average_rank')
    rule_set = policy(db, game)
    if 'level' in args:
        level = number(args, 'level', integer=True)
    else:
        entry = db.execute("SELECT level FROM ops_events WHERE game=? AND kind='levels' AND player=? "
                           'AND occurred_at<=? ORDER BY occurred_at DESC,rowid DESC LIMIT 1',
                           (game, player, occurred_at)).fetchone()
        level = entry[0] if entry else None
        if level is None and rule_set['enabled'] and game_type in GAME_TYPES:
            raise DomainError('缺少结算时的玩家等级，请随结算提供 level 或先上报等级事件。')

    counter = 'COUNT(DISTINCT reporter)' if rule_set['report_mode'] == 'reporters' else 'COUNT(*)'
    sql = (f"SELECT {counter} FROM ops_events WHERE game=? AND kind='reports' AND player=? "
           'AND occurred_at>? AND occurred_at<=?')
    values = [game, player, occurred_at-DAY, occurred_at]
    if rule_set['report_category']:
        sql += ' AND category=?'
        values.append(rule_set['report_category'])
    count = db.execute(sql, values).fetchone()[0]
    command_id = None
    hits = []
    if game_type not in GAME_TYPES:
        decision = 'out_of_scope'
    elif not rule_set['enabled']:
        decision = 'disabled'
    else:
        for rule in rule_set['rules']:
            if (count >= rule['reports'] and kd >= rule['kd'] and kills >= rule['kills']
                    and (rule['level'] is None or level <= rule['level'])
                    and (rule['avg'] is None or average >= rule['avg'])):
                hits.append(rule['id'])
        decision = 'hit' if hits else 'clear'
    if hits and rule_set['enforcement'] == 'observe':
        decision = 'observed'
    elif hits:
        command_id = 'cmd_' + uuid.uuid4().hex
        db.execute('INSERT INTO ops_penalties(game,command_id,player_id,match_id,status,created_at,expires_at,message,hits) '
                   "VALUES(?,?,?,?,'pending',?,?,'',?)", (game, command_id, player, match, now, now+DURATION, encode(hits)))
    return dict(decision=decision, hits=hits, report_count=count, level=level, command_id=command_id,
                policy_version=rule_set['version'], enforcement=rule_set['enforcement'],
                report_mode=rule_set['report_mode'], report_category=rule_set['report_category'])


def _receipt(db, game, args):
    command_id = string(args, 'command_id')
    status = choice(args, 'status', ('succeeded', 'failed'))
    message = string(args, 'message', 500, blank=True) if 'message' in args else ''
    row = db.execute('SELECT status FROM ops_penalties WHERE game=? AND command_id=?', (game, command_id)).fetchone()
    if not row:
        raise DomainError('处罚指令不存在。', 404)
    if row[0] != 'pending' and row[0] != status:
        raise DomainError('处罚已收到另一种结果，请检查原回执或先重试失败指令。', 409)
    if row[0] == 'pending':
        db.execute('UPDATE ops_penalties SET status=?,message=? WHERE game=? AND command_id=?',
                   (status, message, game, command_id))
    return dict(command_id=command_id, status=status)


def game_event(db, game_id, kind, payload):
    if kind not in FIELDS:
        raise DomainError('未知事件类型。', 404)
    fields(payload, FIELDS[kind] | {'event_id', 'occurred_at'})
    event_id = string(payload, 'event_id')
    digest = hashlib.sha256(encode(payload).encode()).hexdigest()
    prior = db.execute('SELECT kind,digest,response FROM ops_events WHERE game=? AND event_id=?',
                       (game_id, event_id)).fetchone()
    if prior:
        if prior['kind'] != kind or prior['digest'] != digest:
            raise DomainError('同一 event_id 关联了不同的请求。', 409)
        return json.loads(prior['response'])

    now = int(time.time())
    occurred = number(payload, 'occurred_at', integer=True, maximum=now+60) if 'occurred_at' in payload else now
    player = '' if kind == 'receipts' else string(payload, 'player_id')
    doc = dict(payload, occurred_at=occurred)
    if player:
        doc['player_id'] = player
    blocked = 0
    if kind == 'chat':
        text = string(payload, 'text', 4096, raw=True)
        allowed = db.execute("SELECT term FROM ops_words WHERE game=? AND term=? AND enabled=1 "
                             "AND list_type='allow' AND match_type='exact'", (game_id, text)).fetchone()
        hits = []
        if not allowed:
            blocks = db.execute("SELECT term,match_type FROM ops_words WHERE game=? AND enabled=1 AND list_type='block' ORDER BY term", (game_id,))
            hits = [w['term'] for w in blocks if (text == w['term'] if w['match_type'] == 'exact' else w['term'] in text)]
        matched = [allowed[0]] if allowed else hits
        db.executemany('UPDATE ops_words SET hits=hits+1 WHERE game=? AND term=?', [(game_id, term) for term in matched])
        blocked = int(bool(hits))
        outcome = dict(decision='block' if hits else 'allow', hits=hits, allowed_by=allowed[0] if allowed else None)
        summary = '白库匹配，聊天已放行' if allowed else ('聊天已拦截' if hits else '聊天已放行')
    elif kind == 'levels':
        doc['level'] = number(payload, 'level', integer=True)
        outcome, summary = {'accepted': True}, '收到玩家等级'
    elif kind == 'reports':
        doc['reporter_id'] = string(payload, 'reporter_id')
        doc['category'] = string(payload, 'category', 80)
        outcome, summary = {'accepted': True}, '收到玩家举报'
    elif kind == 'matches':
        doc['match_id'] = string(payload, 'match_id')
        outcome = _matches(db, game_id, player, doc, occurred, now)
        summary = {'hit': '局后判定命中，已创建处罚指令', 'observed': '观察模式命中，已记录'}.get(outcome['decision'], '完成局后判定')
    else:
        outcome = _receipt(db, game_id, payload)
        summary = '游戏确认处罚已执行' if outcome['status'] == 'succeeded' else '游戏报告处罚失败'
    db.execute('INSERT INTO ops_events(game,event_id,kind,digest,occurred_at,player,reporter,category,match_id,level,blocked,document,response) '
               'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (game_id, event_id, kind, digest, occurred, player,
               doc.get('reporter_id'), doc.get('category'), doc.get('match_id'), doc.get('level'), blocked, encode(doc), encode(outcome)))
    audit(db, game_id, kind, summary, dict(doc, **{k:v for k,v in outcome.items() if k not in doc}))
    return outcome


def _command(row, now):
    result = dict(row)
    result.pop('game', None)
    result['hits'] = json.loads(result['hits'])
    result.update(action='freeze', duration_seconds=DURATION)
    if result['status'] == 'pending' and result['expires_at'] <= now:
        result['status'] = 'expired'
    return result


def pending(db, game_id):
    now = int(time.time())
    rows = db.execute("SELECT * FROM ops_penalties WHERE game=? AND status='pending' AND expires_at>? "
                      'ORDER BY created_at,rowid LIMIT 100', (game_id, now))
    return {'items': [_command(row, now) for row in rows]}


def admin_get(db, game_id, section):
    if section == 'policy':
        return policy(db, game_id)
    if section == 'words':
        rows = db.execute('SELECT term,match_type,enabled,hits,list_type FROM ops_words WHERE game=? ORDER BY term', (game_id,))
        return {'items': [dict(row, enabled=bool(row['enabled'])) for row in rows]}
    if section in ('reports', 'matches', 'chat'):
        rows = db.execute('SELECT document,response FROM ops_events WHERE game=? AND kind=? ORDER BY occurred_at DESC,rowid DESC LIMIT 200', (game_id, section))
        return {'items': [dict(json.loads(row[0]), **json.loads(row[1])) for row in rows]}
    if section == 'audit':
        rows = db.execute('SELECT id,at,kind,summary,details FROM ops_audit WHERE game=? ORDER BY id DESC LIMIT 200', (game_id,))
        return {'items': [dict(row, details=json.loads(row['details'])) for row in rows]}
    if section in ('penalties', 'overview'):
        limit = 10 if section == 'overview' else 200
        rows = db.execute('SELECT * FROM ops_penalties WHERE game=? ORDER BY created_at DESC,rowid DESC LIMIT ?', (game_id, limit))
        items = [_command(row, int(time.time())) for row in rows]
        if section == 'penalties':
            return {'items': items}
        metrics = db.execute('''SELECT
          (SELECT count(*) FROM ops_events WHERE game=?1 AND kind='matches') AS match_count,
          (SELECT count(*) FROM ops_events WHERE game=?1 AND kind='reports') AS report_count,
          (SELECT count(*) FROM ops_events WHERE game=?1 AND kind='chat' AND blocked=1) AS block_count,
          (SELECT count(*) FROM ops_words WHERE game=?1 AND enabled=1) AS word_count,
          (SELECT count(*) FROM ops_penalties WHERE game=?1) AS penalty_count''', (game_id,)).fetchone()
        settings = policy(db, game_id)
        return dict(metrics, enabled=settings['enabled'], enforcement=settings['enforcement'], recent=items)
    raise DomainError('未知管理页面。', 404)


def admin_set(db, game_id, section, payload):
    if section == 'words':
        fields(payload, {'term', 'match_type', 'enabled', 'list_type'})
        term = string(payload, 'term', 128)
        mode = choice(payload, 'match_type', ('exact', 'contains'))
        list_type = choice(payload, 'list_type', ('block', 'allow')) if 'list_type' in payload else 'block'
        if list_type == 'allow' and mode != 'exact':
            raise DomainError('白库词条仅支持整条文本完全匹配。')
        enabled = boolean(payload, 'enabled')
        db.execute('INSERT INTO ops_words(game,term,match_type,enabled,list_type) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(game,term) DO UPDATE SET match_type=excluded.match_type,'
                   'enabled=excluded.enabled,list_type=excluded.list_type', (game_id, term, mode, enabled, list_type))
        audit(db, game_id, 'words', '更新聊天词条', dict(term=term, match_type=mode, enabled=enabled, list_type=list_type))
        return {'saved': True}
    if section == 'policy':
        fields(payload, {'version', 'enabled', 'enforcement', 'report_category', 'report_mode', 'rules'})
        current = policy(db, game_id)
        if number(payload, 'version', integer=True) != current['version']:
            raise DomainError('策略已被其他操作更新，请刷新后重新编辑。', 409)
        updated = current.copy()
        if 'enabled' in payload:
            updated['enabled'] = boolean(payload, 'enabled')
        if 'enforcement' in payload:
            updated['enforcement'] = choice(payload, 'enforcement', ('enforce', 'observe'))
        if 'report_mode' in payload:
            updated['report_mode'] = choice(payload, 'report_mode', ('events', 'reporters'))
        if 'report_category' in payload:
            updated['report_category'] = string(payload, 'report_category', 80, blank=True)
        if 'rules' in payload:
            rules = payload['rules']
            if not isinstance(rules, list) or len(rules) != 5:
                raise DomainError('请提供完整的五组检测条件。')
            updated['rules'] = []
            for index, rule in enumerate(rules, 1):
                fields(rule, {'id', 'reports', 'level', 'kd', 'kills', 'avg'})
                if number(rule, 'id', integer=True) != index:
                    raise DomainError('检测组需按编号 1 至 5 排列。')
                updated['rules'].append(dict(id=index, reports=number(rule, 'reports', integer=True),
                    level=number(rule, 'level', integer=True) if rule.get('level') is not None else None,
                    kd=number(rule, 'kd'), kills=number(rule, 'kills', integer=True),
                    avg=number(rule, 'avg') if rule.get('avg') is not None else None))
        updated['version'] += 1
        db.execute('INSERT INTO ops_policy(game,document) VALUES(?,?) ON CONFLICT(game) DO UPDATE SET document=excluded.document', (game_id, encode(updated)))
        audit(db, game_id, 'policy', f"发布行为策略 v{updated['version']}", {'before': current, 'after': updated})
        return updated
    if section == 'penalties':
        fields(payload, {'action', 'command_id'})
        choice(payload, 'action', ('retry',))
        command_id = string(payload, 'command_id')
        row = db.execute('SELECT * FROM ops_penalties WHERE game=? AND command_id=?', (game_id, command_id)).fetchone()
        if not row:
            raise DomainError('处罚指令不存在。', 404)
        if row['status'] != 'failed' or row['expires_at'] <= int(time.time()):
            raise DomainError('只有执行失败且仍在有效期内的指令可以重试。', 409)
        db.execute("UPDATE ops_penalties SET status='pending', message='' WHERE game=? AND command_id=?", (game_id, command_id))
        audit(db, game_id, 'penalties', '失败指令已重新入队', {'command_id': command_id, 'expires_at': row['expires_at']})
        return dict(command_id=command_id, status='pending')
    raise DomainError('未知管理操作。', 404)
