"""独立游戏服务进程：产生游戏事件，将处罚写入自己维护的模拟账号库，并上报执行结果。"""

import argparse
import getpass
import hashlib
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from gameops_client import GameOpsClient, GameOpsError


def open_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, started INTEGER NOT NULL, done INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS freezes (
            run_id TEXT PRIMARY KEY, command_id TEXT NOT NULL UNIQUE, player_id TEXT NOT NULL,
            expires_at INTEGER NOT NULL, applied_at INTEGER NOT NULL
        );
    ''')
    return db


def current_run(db, scope):
    with db:
        db.execute('BEGIN IMMEDIATE')
        run = db.execute('SELECT * FROM runs WHERE scope=? AND done=0 ORDER BY started LIMIT 1', (scope,)).fetchone()
        if run:
            return run
        identifier = uuid4().hex[:16]
        db.execute('INSERT INTO runs(id,scope,started) VALUES(?,?,?)', (identifier, scope, int(time.time())))
        return db.execute('SELECT * FROM runs WHERE id=?', (identifier,)).fetchone()


def freeze_player(db, run_id, command):
    """账号冻结和执行流水同库提交。以固定到期时间冻结，重复拉取同一命令不会续期。"""
    now = int(time.time())
    with db:
        db.execute('BEGIN IMMEDIATE')
        original = db.execute('SELECT * FROM freezes WHERE run_id=?', (run_id,)).fetchone()
        if original:
            if original['command_id'] != command['command_id']:
                raise ValueError('同一模拟对局收到不同处罚编号，需核对。')
            return original
        if command['action'] != 'freeze' or command['expires_at'] <= now:
            raise ValueError('命令已到期或动作不受支持。')
        db.execute('INSERT INTO freezes VALUES(?,?,?,?,?)', (
            run_id, command['command_id'], command['player_id'], command['expires_at'], now))
        return db.execute('SELECT * FROM freezes WHERE run_id=?', (run_id,)).fetchone()


def simulate(client, db, scope):
    run = current_run(db, scope)
    run_id, started = run['id'], run['started']
    player, match = 'sim-player-' + run_id, 'sim-match-' + run_id
    print('本次联调玩家', player, '对局', match)

    def send(kind, label, stamp, **fields):
        return client.event(kind, {'event_id': f'{run_id}-{label}', 'occurred_at': stamp,
                                   'player_id': player, **fields})

    send('levels', 'level', started - 120, level=31)
    chat = send('chat', 'chat', started - 100, text='联调消息：代练接单')
    print('聊天结果', chat['decision'], '命中词条', chat['hits'])
    for n in range(15):
        send('reports', f'report-{n}', started - 90 + n,
             reporter_id=f'sim-reporter-{run_id}-{n}', category='cheating')
    outcome = send('matches', 'match', started, match_id=match, game_type=2,
                   kd=2, kill_num=20, average_rank=20)
    print('局后结果', outcome['decision'], '举报量', outcome['report_count'], '命中组', outcome['hits'])

    command_id = outcome['command_id']
    if command_id:
        applied = db.execute('SELECT * FROM freezes WHERE run_id=?', (run_id,)).fetchone()
        if applied is None:
            # 此模拟器只执行本次模拟对局中产生的命令；共享游戏中的其他命令交给对应的执行端。
            command = next((c for c in client.pending() if c['command_id'] == command_id
                            and c['player_id'] == player and c['match_id'] == match), None)
            if command is None:
                print('该命令未出现在待执行列表中。请查看处罚状态及队列积压情况。')
                print('如已到期，结束本次演练后可再次运行生成新对局。')
                if input('输入 end 结束本次演练，直接回车保留重试：').strip() != 'end':
                    return
            else:
                applied = freeze_player(db, run_id, command)
        if applied is not None:
            receipt = client.event('receipts', {'event_id': f'{run_id}-receipt', 'command_id': command_id,
                'status': 'succeeded', 'message': '模拟游戏账号库已记录冻结', 'occurred_at': applied['applied_at']})
            can_login = applied['expires_at'] <= int(time.time())
            print('模拟账号登录检查', '允许登录' if can_login else '冻结中，暂时无法登录')
            print('处罚回执', receipt['command_id'], receipt['status'])
    with db:
        db.execute('UPDATE runs SET done=1 WHERE id=?', (run_id,))
    print('联调事件已提交。请在运营后台选择对应游戏查看。')


def main():
    parser = argparse.ArgumentParser(description='游戏服务侧联调，通过 HTTP 调用安全运营平台。')
    parser.add_argument('--url', default=os.getenv('GAMEOPS_URL', 'http://127.0.0.1:8765'), help='平台根地址')
    parser.add_argument('--state', type=Path, default=Path(__file__).parent / 'data' / 'simulator.sqlite3',
                        help='模拟游戏账号和恢复记录文件')
    args = parser.parse_args()
    token = os.getenv('GAMEOPS_GAME_TOKEN') or getpass.getpass('粘贴本次联调游戏的接入密钥并回车（输入隐藏）：')
    client = GameOpsClient(args.url, token.strip())
    scope = hashlib.sha256((client.url + '|' + client.token).encode()).hexdigest()
    with closing(open_state(args.state)) as db:
        simulate(client, db, scope)


if __name__ == '__main__':
    try:
        main()
    except (GameOpsError, ValueError, OSError, sqlite3.Error, EOFError) as error:
        print('联调中断：', error, file=sys.stderr)
        print('修正配置后用相同游戏密钥重新运行，可恢复未完成演练。', file=sys.stderr)
        sys.exit(1)
