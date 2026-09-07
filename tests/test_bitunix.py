import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bitunix_client import BitunixClient, BitunixError, amount, utc_date
from eligibility import evaluate_lesson, ACTIVITY_PERIOD_MS
from mexc_client import ReferralData
from partner_client import PartnerAPIError
from storage import BotStorage, ExchangeUidAlreadyBoundError, UserExchangeUidConflictError
from test_manual_lesson_review import load_bot_module

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
REGISTERED = "2026-09-03T12:00:22Z"


class PartnerFixture:
    def __init__(self, volume=(), trades=(), registered=REGISTERED):
        self.volume = list(volume)
        self.trades = list(trades)
        self.registered = registered
        self.direct = True
        self.calls = []

    def request(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint.endswith('validateUser'):
            return {'result': self.direct}
        if endpoint.endswith('userList'):
            if 'uid' in params:
                return True
            return {'items': [{'uid': 123, 'registerTime': self.registered}], 'total': 1}
        rows = self.volume if endpoint.endswith('transAmountList') else self.trades
        start = (params['page'] - 1) * 100
        return {'items': rows[start:start + 100], 'total': len(rows)}


def volume(value, uid=123):
    return {'uid': uid, 'transCoinPair': 'BTCUSDT', 'tradeType': 'futures', 'transVolumeUsd': value}


def trade(value='20', at='2026-09-04T12:00:00Z', uid=123):
    return {'uid': uid, 'tradeAmount': value, 'ctime': at}


class BitunixAdapterTests(unittest.TestCase):
    def client(self, partner):
        return BitunixClient(partner=partner, now=lambda: NOW, interval=0)

    def test_empty_history_is_valid_zero_and_no_trade(self):
        result = self.client(PartnerFixture()).get_rebate_referral('123')
        self.assertEqual(result.trading_amount, 0)
        self.assertIsNone(result.first_trade_time)
        self.assertFalse(evaluate_lesson(2, result).is_eligible)

    def test_foreign_uid_returns_none_before_history_requests(self):
        fixture = PartnerFixture(); fixture.direct = False
        self.assertIsNone(self.client(fixture).get_rebate_referral('123'))
        self.assertEqual(len(fixture.calls), 1)

    def test_exact_thresholds_and_below(self):
        for value, lesson, eligible in [('299.9999', 3, False), ('300', 3, True), ('4999.99', 7, False), ('5000', 7, True)]:
            with self.subTest(value=value):
                result = self.client(PartnerFixture([volume(value)], [trade()])).get_rebate_referral('123')
                self.assertEqual(evaluate_lesson(lesson, result).is_eligible, eligible)
                self.assertTrue(evaluate_lesson(2, result).is_eligible)

    def test_missing_usd_does_not_use_ambiguous_trans_volume(self):
        row = {'uid': 123, 'transVolume': '999999'}
        result = self.client(PartnerFixture([row], [trade()])).get_rebate_referral('123')
        self.assertIsNone(result.trading_amount)
        self.assertFalse(evaluate_lesson(3, result).is_eligible)
        self.assertTrue(evaluate_lesson(2, result).is_eligible)

    def test_full_pagination_not_only_first_page(self):
        rows = [{**volume('2.99'), 'transCoinPair': str(n)} for n in range(101)]
        fixture = PartnerFixture(rows, [trade()])
        result = self.client(fixture).get_rebate_referral('123')
        self.assertEqual(result.trading_amount, Decimal('301.99'))
        self.assertEqual([p['page'] for e, p in fixture.calls if e.endswith('transAmountList')], [1, 2])

    def test_api_failure_does_not_become_zero_volume(self):
        fixture = Mock(); fixture.request.side_effect = PartnerAPIError(401)
        with self.assertRaises(BitunixError):
            self.client(fixture).get_rebate_referral('123')

    def test_incomplete_and_repeated_pages_fail(self):
        for items in [[], [volume('5')]]:
            client = self.client(PartnerFixture())
            with patch.object(client, '_request', return_value={'items': items, 'total': 2}):
                with self.assertRaises(BitunixError):
                    list(client._pages('transAmountList', {}))

    def test_changed_total_fails(self):
        client = self.client(PartnerFixture())
        with patch.object(client, '_request', side_effect=[{'items': [volume('5')], 'total': 2}, {'items': [volume('6')], 'total': 3}]):
            with self.assertRaises(BitunixError):
                list(client._pages('transAmountList', {}))

    def test_foreign_rows_fail_closed(self):
        for fixture in [PartnerFixture([volume('1000', 999)]), PartnerFixture([], [trade(uid=999)])]:
            with self.assertRaises(BitunixError):
                self.client(fixture).get_rebate_referral('123')

    def test_invalid_numbers_fail(self):
        for value in ['NaN', 'Infinity', '-1', '', None, True]:
            with self.subTest(value=value), self.assertRaises(BitunixError):
                amount(value)

    def test_trade_timestamps_are_real_sorted_extrema(self):
        fixture = PartnerFixture([], [trade(at='2026-09-06T01:00:00Z'), trade(at='2026-09-04T01:00:00Z')])
        result = self.client(fixture).get_rebate_referral('123')
        self.assertEqual(result.first_trade_time, int(utc_date('2026-09-04T01:00:00Z').timestamp() * 1000))
        self.assertEqual(result.last_trade_time, int(utc_date('2026-09-06T01:00:00Z').timestamp() * 1000))

    def test_zero_trade_does_not_qualify(self):
        result = self.client(PartnerFixture([], [trade('0')])).get_rebate_referral('123')
        self.assertIsNone(result.first_trade_time)

    def test_out_of_range_or_invalid_time_fails(self):
        for value in ['invalid', '2026-09-04T01:00:00', '2026-09-10T00:00:00Z']:
            with self.subTest(value=value), self.assertRaises(BitunixError):
                self.client(PartnerFixture([], [trade(at=value)])).get_rebate_referral('123')

    def test_history_covers_registration_beyond_180_days(self):
        fixture = PartnerFixture(registered='2025-01-01T00:00:00Z')
        self.client(fixture).get_rebate_referral('123')
        periods = [p for e, p in fixture.calls if e.endswith('transAmountList')]
        self.assertGreater(len(periods), 3)
        self.assertEqual(periods[0]['startTime'], '2025-01-01T00:00:00Z')
        self.assertEqual(utc_date(periods[-1]['endTime']), NOW)
        for i, period in enumerate(periods):
            self.assertLess(utc_date(period['endTime']) - utc_date(period['startTime']), timedelta(days=180))
            if i:
                self.assertEqual(utc_date(period['startTime']), utc_date(periods[i-1]['endTime']) + timedelta(seconds=1))


class ExchangeStorageTests(unittest.TestCase):
    def test_uid_namespace_and_immutable_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BotStorage(str(Path(tmp) / 'state.db'))
            with self.assertRaises(UserExchangeUidConflictError):
                store.bind_exchange_uid(1, '123')
            store.set_exchange(1, 'mexc'); store.bind_exchange_uid(1, '123')
            store.set_exchange(2, 'bitunix'); store.bind_exchange_uid(2, '123')
            store.set_exchange(3, 'bitunix')
            with self.assertRaises(ExchangeUidAlreadyBoundError):
                store.bind_exchange_uid(3, '123')
            with self.assertRaises(UserExchangeUidConflictError):
                store.set_exchange(2, 'mexc')
            with self.assertRaises(UserExchangeUidConflictError):
                store.bind_exchange_uid(2, '456')
            reopened = BotStorage(str(Path(tmp) / 'state.db'))
            self.assertEqual(reopened.get_user(2).exchange, 'bitunix')

    def test_selection_race_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BotStorage(str(Path(tmp) / 'state.db'))
            store.set_exchange(1, 'bitunix'); store.set_exchange(1, 'mexc')
            with self.assertRaises(UserExchangeUidConflictError):
                store.bind_exchange_uid(1, '123', exchange='bitunix')


class ExchangeFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = load_bot_module(Path(self.tmp.name) / 'state.db')
        self.module.exchange_clients['bitunix'] = object()
        self.module.storage.set_exchange(1, 'bitunix')
        self.referral = ReferralData('123', None, Decimal('5000'), 1000, 1000)
        self.module.get_referral_cached = Mock(side_effect=lambda *a, **k: self.referral)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seven_lessons_pdf_bonus_and_video_with_time_and_friends(self):
        m = self.module
        # All Telegram sends are fake; eligibility and SQLite state are real.
        self.assertTrue(m.issue_lesson_once(1, 1))
        for n in [2, 3]:
            m.check_lesson_with_uid(1, n, '123')
        self.assertEqual(m.storage.issued_lessons(1), (1, 2, 3))
        for user, exchange in [(2, 'mexc'), (3, 'bitunix')]:
            m.storage.assign_inviter(user, 1)
            m.storage.set_exchange(user, exchange)
            m.storage.bind_exchange_uid(user, str(100 + user))
            m.storage.mark_qualified(user)
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), text='')
        m.process_lesson_request(message, 4)
        confirmed = m.storage.get_user(1).activity_confirmed_at
        m.check_lesson_with_uid(1, 5, '123')
        self.assertFalse(m.storage.is_lesson_issued(1, 5))
        self.referral = ReferralData('123', None, Decimal('5000'), 1000, confirmed + ACTIVITY_PERIOD_MS)
        with patch.object(m.time, 'time', return_value=(confirmed + ACTIVITY_PERIOD_MS) / 1000):
            m.check_lesson_with_uid(1, 5, '123', force_refresh=True)
        m.process_lesson_request(message, 6)
        m.check_lesson_with_uid(1, 7, '123')
        self.assertEqual(m.storage.issued_lessons(1), tuple(range(1, 8)))
        self.assertEqual(len(m.bot.documents), 11)
        for n in range(1, 8):
            self.assertTrue(any(m.LESSON_VIDEO_URLS[n] in (d['caption'] or '') for d in m.bot.documents))
        m.check_lesson_with_uid(1, 7, '123')
        self.assertEqual(len(m.bot.documents), 11)
        self.assertFalse(any('MEXC подтвердил' in x['text'] for x in m.bot.messages))

    def test_no_trade_binds_uid_but_does_not_issue_or_qualify(self):
        m = self.module; m.storage.claim_lesson(1, 1)
        self.referral = ReferralData('123', None, Decimal(0), None, None)
        m.check_lesson_with_uid(1, 2, '123')
        self.assertEqual(m.storage.get_user(1).exchange_uid, '123')
        self.assertIsNone(m.storage.get_user(1).qualified_at)
        self.assertFalse(m.storage.is_lesson_issued(1, 2))

    def test_missing_volume_routes_to_manual_with_correct_exchange(self):
        m = self.module
        for n in [1, 2]: m.storage.claim_lesson(1, n)
        self.referral = ReferralData('123', None, None, 1000, 1000)
        m.check_lesson_with_uid(1, 3, '123')
        self.assertFalse(m.storage.is_lesson_issued(1, 3))
        self.assertTrue(any('Биржа: Bitunix' in x['text'] and '300 USD' in x['text'] for x in m.bot.messages))

    def test_foreign_uid_and_api_error_do_not_bind_or_award(self):
        m = self.module; m.storage.claim_lesson(1, 1)
        for response in [None, BitunixError()]:
            m.get_referral_cached = Mock(side_effect=response if isinstance(response, Exception) else None, return_value=None)
            m.check_lesson_with_uid(1, 2, '123')
            self.assertIsNone(m.storage.get_user(1).exchange_uid)
            self.assertFalse(m.storage.is_lesson_issued(1, 2))

    def test_out_of_order_direct_uid_cannot_skip_lessons(self):
        self.module.check_lesson_with_uid(1, 7, '123')
        self.module.get_referral_cached.assert_not_called()
        self.assertEqual(self.module.bot.documents, [])

    def test_cache_is_scoped_by_exchange(self):
        from test_manual_lesson_review import load_bot_module
        m = load_bot_module(Path(self.tmp.name) / 'cache.db')
        mexc, bitunix = Mock(), Mock()
        mexc.get_rebate_referral.return_value = 'mexc'
        bitunix.get_rebate_referral.return_value = 'bitunix'
        m.exchange_clients.update(mexc=mexc, bitunix=bitunix)
        self.assertEqual(m.get_referral_cached('123', exchange='mexc'), 'mexc')
        self.assertEqual(m.get_referral_cached('123', exchange='bitunix'), 'bitunix')
        m.get_referral_cached('123', exchange='mexc')
        mexc.get_rebate_referral.assert_called_once()

