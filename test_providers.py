import json
import os
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import providers
from bot import active_event, build_asset, get_config, live_guards, load_events, make_handler


class ProviderTests(unittest.TestCase):
    def tearDown(self):
        providers.CACHE.clear()

    @patch('providers.get_json')
    def test_binance_filters_unclosed_candle(self, fetch):
        fetch.return_value = [[0, '100', '102', '99', '101', '3', 3599999], [3600000, '101', '102', '99', '100', '3', 7199999]]
        bars = providers.binance_bars('H1', now=3601)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]['time'], 0)
        self.assertEqual(bars[0]['close'], 101)
        self.assertEqual(bars[0]['volume'], 3)

    @patch('providers.get_json')
    def test_binance_daily_interval(self, fetch):
        fetch.return_value = []
        providers.binance_bars('D1', now=0)
        self.assertEqual(fetch.call_args.args[1]['interval'], '1d')

    @patch('providers.urlopen')
    def test_http_errors_never_expose_key(self, fetch):
        fetch.side_effect = HTTPError('https://example.test/?apikey=secret123', 401, 'secret123', {}, None)
        with self.assertRaises(providers.FeedError) as caught:
            providers.get_json('https://example.test/', {'apikey':'secret123'})
        self.assertNotIn('secret123', str(caught.exception))

    @patch('providers.binance_bars')
    def test_cache_and_failure_do_not_fall_back_to_demo(self, fetch):
        fetch.return_value = providers.demo_bars('BTC', 3600)
        a, _ = providers.load_bars('api', 'BTC', 'H1', 'BTCUSD')
        b, _ = providers.load_bars('api', 'BTC', 'H1', 'BTCUSD')
        self.assertEqual(a, b)
        self.assertEqual(fetch.call_count, 1)
        providers.CACHE.clear()
        fetch.side_effect = providers.FeedError('offline')
        with self.assertRaisesRegex(providers.FeedError, 'offline'):
            providers.load_bars('api', 'BTC', 'H1', 'BTCUSD')


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(get_config(['--source','demo'])))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_demo_analysis_has_scores_backtest_and_no_nan(self):
        with urlopen(self.base+'/api/analysis') as response:
            data=json.load(response)
        self.assertEqual(data['source'], 'demo')
        self.assertEqual(len(data['assets']), 1)
        self.assertEqual(data['assets'][0]['asset'], 'BTC')
        for a in data['assets']:
            self.assertNotIn('error', a)
            self.assertTrue(0 <= a['score'] <= 100)
            self.assertIn('holdout', a['backtest'])
            self.assertEqual(len(a['checks']), 10)
            self.assertIn(a['higher_timeframe'], ('H4',))
            self.assertIsNone(a['context_error'])
            self.assertIsNone(a['funding_error'])
            self.assertIsNotNone(a['context'])
            self.assertIsNotNone(a['volume_pressure_pct'])
            self.assertIsNotNone(a['funding_rate'])
            self.assertTrue(a['backtest']['assumptions']['higher_timeframe_context'])
            self.assertTrue(a['backtest']['assumptions']['funding_rate_filter'])
            self.assertEqual(len(a['guards']), 2)
            self.assertEqual(set(a['live']), {'open_interest', 'depth', 'eth_btc', 'dominance'})
        json.dumps(data, allow_nan=False)

    def test_host_and_port_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PORT', None)  # hosts such as Render export PORT even during build
            self.assertEqual((get_config([]).host, get_config([]).port), ('127.0.0.1', 8766))
        with patch.dict(os.environ, {'PORT': '10000'}):
            self.assertEqual(get_config([]).port, 10000)
            self.assertEqual(get_config(['--port', '9000', '--host', '0.0.0.0']).port, 9000)

    def test_funding_history_settled_only(self):
        with patch('providers.get_json') as fetch:
            fetch.return_value = [{"fundingTime": 28800000, "fundingRate": "0.0002"}, {"fundingTime": 0, "fundingRate": "0.0001"},
                                  {"fundingTime": 57600000, "fundingRate": "0.0003"}]
            rows = providers.binance_funding(now=30000)
        self.assertEqual([r["rate"] for r in rows], [0.0001, 0.0002])
        self.assertEqual(fetch.call_args.args[1]["symbol"], "BTCUSDT")

    def test_depth_metrics(self):
        with patch('providers.get_json') as fetch:
            fetch.return_value = {"bids": [["100", "2"], ["99.6", "3"], ["90", "50"]], "asks": [["100.1", "1"], ["100.4", "1"], ["120", "50"]]}
            depth = providers.binance_depth()
        self.assertAlmostEqual(depth["spread_bps"], 0.1 / 100.05 * 10000)
        self.assertEqual((depth["bid_depth_btc"], depth["ask_depth_btc"]), (5, 2))
        self.assertAlmostEqual(depth["bid_share_pct"], 500 / 7)

    def test_live_data_failures_are_isolated(self):
        with patch('providers.binance_depth', side_effect=providers.FeedError('book down')), \
             patch('providers.binance_open_interest', return_value={"open_interest_btc": 1.0, "value_usd": 1.0, "change_24h_pct": 0.0, "time": 0, "hours": 24}), \
             patch('providers.binance_eth_btc', return_value={"ratio": 0.03, "change_pct": 1.0, "days": 20}), \
             patch('providers.coingecko_dominance', side_effect=providers.FeedError('gecko down')):
            providers.LIVE_LOADERS.update(depth=providers.binance_depth, open_interest=providers.binance_open_interest,
                                          eth_btc=providers.binance_eth_btc, dominance=providers.coingecko_dominance)
            try:
                live = providers.load_live('api')
            finally:
                providers.LIVE_LOADERS.update(depth=providers.binance_depth, open_interest=providers.binance_open_interest,
                                              eth_btc=providers.binance_eth_btc, dominance=providers.coingecko_dominance)
        self.assertEqual(live['depth'], {'error': 'book down'})
        self.assertEqual(live['dominance'], {'error': 'gecko down'})
        self.assertEqual(live['eth_btc']['value']['days'], 20)
        guards = live_guards(get_config(['--source', 'demo']), live, 0)
        self.assertFalse(guards[0]['pass'])
        self.assertIn('book down', guards[0]['note'])

    def test_event_window_and_wide_spread_veto_live_signal(self):
        calendar = load_events('events.json')
        fomc = next(e for e in calendar['events'] if e['time'] > 1780000000)
        self.assertEqual(calendar['before'], 3600)
        self.assertIsNone(active_event(calendar, fomc['time'] - 3601))
        self.assertEqual(active_event(calendar, fomc['time'] - 3600)['name'], 'FOMC statement')
        self.assertEqual(active_event(calendar, fomc['time'] + 7200)['name'], 'FOMC statement')
        self.assertIsNone(active_event(calendar, fomc['time'] + 7201))
        self.assertEqual(load_events('no-such-file.json')['events'], [])
        config = get_config(['--source', 'demo'])
        fresh = lambda *a, **k: {"signal": "BUY", "action": "BUY", "reason": "x", "stop": 1, "target": 2,
                                 "target_move_pct": 1, "stop_move_pct": 1, "score": 100, "checks": []}
        with patch('bot.analyze', side_effect=fresh):
            inside = build_asset(config, 'BTC', 'demo', 'H1', now=fomc['time'])
            self.assertEqual(inside['action'], 'WAIT')
            self.assertIsNone(inside['stop'])
            self.assertIn('FOMC', inside['reason'])
            with patch('providers.demo_live', return_value={**providers.demo_live(), "depth": {**providers.demo_live()["depth"], "spread_bps": 9.0}}):
                wide = build_asset(config, 'BTC', 'demo', 'H1', now=fomc['time'] + 86400)
            self.assertEqual(wide['action'], 'WAIT')
            self.assertIn('9.00 bps', wide['reason'])
            clear = build_asset(config, 'BTC', 'demo', 'H1', now=fomc['time'] + 86400)
            self.assertEqual(clear['action'], 'BUY')

    def test_invalid_options_400(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base+'/api/analysis?source=bad')
        self.assertEqual(caught.exception.code, 400)

    def test_static_and_health(self):
        for path in ('/', '/style.css', '/app.js', '/favicon.svg', '/api/health'):
            with urlopen(self.base+path) as response:
                self.assertEqual(response.status, 200)
        with urlopen(self.base+'/manifest.webmanifest') as response:
            self.assertEqual(json.load(response)['display'], 'standalone')
        for path in ('/sw.js', '/icon-192.png', '/icon-512.png', '/apple-touch-icon.png'):
            with urlopen(self.base+path) as response:
                self.assertEqual(response.status, 200)
        with urlopen(self.base+'/icon-192.png') as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertTrue(response.read().startswith(b'\x89PNG'))
        with urlopen(self.base+'/api/health') as response:
            self.assertFalse(json.load(response)['trading_enabled'])


if __name__ == '__main__':
    unittest.main()
