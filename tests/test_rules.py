"""Business regressions for the game-facing service. No external dependencies."""

import itertools
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import domain


class GameOpsRules(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        domain.init_db(self.connection)
        self.now = 1800000000
        clock = patch.object(domain.time, 'time', return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.events = itertools.count(1)

    def event(self, kind, *, game='a', **payload):
        payload.setdefault('event_id', f'e-{next(self.events)}')
        with self.connection:
            return domain.game_event(self.connection, game, kind, payload)

    def report(self, player, reporter, *, category='cheating', **kwargs):
        return self.event('reports', player_id=player, reporter_id=reporter, category=category, **kwargs)

    def play(self, player, match, **changes):
        args = dict(player_id=player, match_id=match, game_type=2, level=31, kd=2, kill_num=20, average_rank=20)
        args.update(changes)
        return self.event('matches', **args)

    def policy(self, **changes):
        with self.connection:
            previous = domain.admin_get(self.connection, 'a', 'policy')
            return domain.admin_set(self.connection, 'a', 'policy', dict(version=previous['version'], **changes))

    def test_five_groups_accept_exact_thresholds_in_both_modes(self):
        cases = [(10, 30, 5, 10, 19, [1]), (10, 30, 1.5, 10, 20, [2]),
                 (15, 31, 2, 20, 0, [3]), (20, 31, 2, 10, 0, [4]),
                 (30, 31, 2, 5, 0, [5]), (10, 30, 5, 10, 20, [1, 2])]
        for index, (reports, level, kd, kills, average, expected) in enumerate(cases):
            player = f'p-{index}'
            with self.subTest(group=expected):
                for i in range(reports - 1):
                    self.report(player, str(i))
                values = dict(level=level, kd=kd, kill_num=kills, average_rank=average)
                self.assertEqual(self.play(player, 'before', **values)['decision'], 'clear')
                self.report(player, 'last')
                for game_type in (2, 5):
                    result = self.play(player, f'mode-{game_type}', game_type=game_type, **values)
                    self.assertEqual(result['hits'], expected)
                    self.assertIsNotNone(result['command_id'])
        self.assertEqual(len(domain.pending(self.connection, 'a')['items']), 2 * len(cases))

    def test_below_performance_thresholds_and_other_modes(self):
        for i in range(10):
            self.report('p', str(i))
        safe_stats = [
            dict(kd=4.99, average_rank=19), dict(kd=1.49), dict(kd=1.5, average_rank=19.99),
            dict(level=31), dict(kill_num=9),
        ]
        for i, change in enumerate(safe_stats):
            stats = dict(level=30, kd=5, kill_num=10, average_rank=20)
            stats.update(change)
            self.assertEqual(self.play('p', f'case-{i}', **stats)['decision'], 'clear')
        self.assertEqual(self.play('p', 'other-mode', level=30, kd=5, game_type=0)['decision'], 'out_of_scope')
        self.policy(enabled=False)
        self.assertEqual(self.play('p', 'disabled', level=30, kd=5)['decision'], 'disabled')
        self.assertFalse(domain.pending(self.connection, 'a')['items'])

    def test_window_report_scope_and_distinct_reporters(self):
        self.report('p', 'old', occurred_at=self.now - 86400)
        self.report('p', 'x', occurred_at=self.now - 86399)
        self.report('p', 'x', occurred_at=self.now)
        self.report('p', 'future', occurred_at=self.now + 1)
        self.report('p', 'bad-chat', category='spam')
        self.report('other-player', 'different')
        self.report('p', 'different-game', game='b')
        self.assertEqual(self.play('p', 'all')['report_count'], 3)
        self.policy(report_mode='reporters', report_category='cheating')
        self.assertEqual(self.play('p', 'filtered')['report_count'], 1)

    def test_historical_level_and_tenant_isolation(self):
        self.event('levels', player_id='p', level=30, occurred_at=self.now - 1)
        self.event('levels', player_id='p', level=31, occurred_at=self.now + 1)
        self.event('levels', player_id='p', level=99, game='b')
        for i in range(10):
            self.report('p', str(i))
        payload = dict(player_id='p', match_id='r', game_type=5, kd=5, kill_num=10, average_rank=19)
        hit = self.event('matches', **payload)
        self.assertEqual((hit['level'], hit['hits']), (30, [1]))
        clear = self.event('matches', game='b', **payload)
        self.assertEqual((clear['level'], clear['report_count'], clear['hits']), (99, 0, []))
        self.assertEqual(domain.admin_get(self.connection, 'b', 'overview')['penalty_count'], 0)
        with self.assertRaises(domain.DomainError) as error:
            self.event('receipts', game='b', command_id=hit['command_id'], status='succeeded')
        self.assertEqual(error.exception.status, 404)

    def test_replays_policy_changes_and_conflicting_settlements(self):
        for i in range(15):
            self.report('p', str(i), event_id=f'r-{i}')
        self.report('p', '0', event_id='r-0')
        result = self.play('p', 'm', event_id='stable')
        self.assertEqual(result['report_count'], 15)
        self.policy(enabled=False)
        self.assertEqual(self.play('p', 'm', event_id='stable'), result)
        for new_event, new_kills in [('stable', 19), ('new-event', 20)]:
            with self.assertRaises(domain.DomainError) as error:
                self.play('p', 'm', event_id=new_event, kill_num=new_kills)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(domain.pending(self.connection, 'a')['items']), 1)
        self.assertEqual(len(domain.admin_get(self.connection, 'a', 'matches')['items']), 1)

    def test_failed_delivery_requires_explicit_retry_and_preserves_expiry(self):
        for i in range(15):
            self.report('p', str(i))
        result = self.play('p', 'm')
        command = domain.pending(self.connection, 'a')['items'][0]
        cid = result['command_id']
        self.assertEqual((command['command_id'], command['expires_at']), (cid, self.now + 21600))
        self.event('receipts', event_id='fail', command_id=cid, status='failed')
        self.assertFalse(domain.pending(self.connection, 'a')['items'])
        self.clock.return_value = self.now + 100
        domain.admin_set(self.connection, 'a', 'penalties', dict(action='retry', command_id=cid))
        self.assertEqual(domain.pending(self.connection, 'a')['items'][0]['expires_at'], command['expires_at'])
        self.event('receipts', event_id='fail', command_id=cid, status='failed')
        self.assertTrue(domain.pending(self.connection, 'a')['items'])
        self.event('receipts', command_id=cid, status='succeeded')
        self.assertFalse(domain.pending(self.connection, 'a')['items'])
        self.assertEqual(domain.admin_get(self.connection, 'a', 'penalties')['items'][0]['status'], 'succeeded')

    def test_expired_command_cannot_be_pulled(self):
        for i in range(15):
            self.report('p', str(i))
        self.play('p', 'm')
        self.clock.return_value += 21600
        self.assertFalse(domain.pending(self.connection, 'a')['items'])
        self.assertEqual(domain.admin_get(self.connection, 'a', 'penalties')['items'][0]['status'], 'expired')

    def test_wordlists_and_message_decisions_are_scoped_to_game(self):
        def set_word(term, match_type, enabled):
            domain.admin_set(self.connection, 'a', 'words', dict(term=term, match_type=match_type, enabled=enabled))
        set_word('spam', 'contains', True)
        set_word('ad', 'exact', True)
        msg = dict(event_id='chat-one', player_id='p', text='spam!')
        self.assertEqual(self.event('chat', **msg)['decision'], 'block')
        self.event('chat', **msg)
        self.assertEqual(self.event('chat', game='b', **msg)['decision'], 'allow')
        self.assertEqual(self.event('chat', player_id='p', text='bad')['decision'], 'allow')
        self.assertEqual(self.event('chat', player_id='p', text='ad')['decision'], 'block')
        set_word('spam', 'contains', False)
        self.assertEqual(self.event('chat', **msg)['decision'], 'block')
        self.assertEqual(self.event('chat', player_id='p', text='spam!')['decision'], 'allow')
        self.assertEqual(domain.admin_get(self.connection, 'a', 'overview')['block_count'], 2)
        self.assertEqual(domain.admin_get(self.connection, 'b', 'overview')['block_count'], 0)

    def test_allowlist_is_exact_only_and_overrides_substring_blocks_in_one_game(self):
        def change(term, list_type, match_type):
            return domain.admin_set(self.connection, 'a', 'words', dict(term=term, list_type=list_type, match_type=match_type, enabled=True))
        change('spam', 'block', 'contains')
        with self.assertRaises(domain.DomainError):
            change('reported spam', 'allow', 'contains')
        change('reported spam', 'allow', 'exact')
        chat = lambda text: self.event('chat', player_id='p', text=text)
        self.assertEqual(chat('reported spam'), dict(decision='allow', hits=[], allowed_by='reported spam'))
        self.assertEqual(chat('reported spam!')['decision'], 'block')
        domain.admin_set(self.connection, 'b', 'words', dict(term='spam', match_type='contains', enabled=True))
        self.assertEqual(self.event('chat', game='b', player_id='p', text='reported spam')['decision'], 'block')
        change('reported spam', 'block', 'exact')
        self.assertEqual(chat('reported spam'), dict(decision='block', hits=['reported spam','spam'], allowed_by=None))
        words = domain.admin_get(self.connection, 'a', 'words')['items']
        self.assertEqual([(w['term'],w['list_type']) for w in words], [('reported spam','block'),('spam','block')])

    def test_observe_records_evidence_without_creating_or_later_replaying_a_command(self):
        self.policy(enforcement='observe')
        for i in range(15):
            self.report('p', str(i))
        observed = self.play('p', 'm', event_id='settled')
        self.assertEqual((observed['decision'], observed['hits'], observed['command_id']), ('observed', [3], None))
        self.assertFalse(domain.pending(self.connection, 'a')['items'])
        overview = domain.admin_get(self.connection, 'a', 'overview')
        self.assertEqual((overview['enforcement'], overview['penalty_count']), ('observe', 0))
        self.assertEqual(domain.admin_get(self.connection, 'a', 'matches')['items'][0]['decision'], 'observed')
        self.policy(enforcement='enforce')
        self.assertEqual(self.play('p', 'm', event_id='settled'), observed)
        self.assertEqual(self.play('p', 'next')['decision'], 'hit')
        self.assertEqual(len(domain.pending(self.connection, 'a')['items']), 1)

    def test_previous_word_and_policy_storage_migrate_idempotently(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
          CREATE TABLE ops_words(game TEXT, term TEXT, match_type TEXT, enabled INTEGER, hits INTEGER, PRIMARY KEY(game,term));
          INSERT INTO ops_words VALUES('g','saved','contains',1,4);
          CREATE TABLE ops_policy(game TEXT PRIMARY KEY, document TEXT);
          INSERT INTO ops_policy VALUES('g','{"enabled":true,"version":12,"report_mode":"events","report_category":"","rules":[]}');
        """)
        domain.init_db(db)
        domain.init_db(db)
        word = domain.admin_get(db, 'g', 'words')['items'][0]
        self.assertEqual((word['term'], word['list_type'], word['hits']), ('saved', 'block', 4))
        settings = domain.policy(db, 'g')
        self.assertEqual((settings['version'], settings['enforcement']), (12, 'enforce'))


if __name__ == '__main__':
    unittest.main()
