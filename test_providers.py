import json
import os
import threading
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch
from http.server import ThreadingHTTPServer
import providers
from bot import get_config, make_handler


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

    @patch.dict(os.environ, {'TWELVE_DATA_API_KEY':'test-secret'})
    @patch('providers.get_json')
    def test_gold_utc_sort_and_closed_filter(self, fetch):
        fetch.return_value = {'values':[{'datetime':'2026-01-01 02:00:00','open':'100','high':'102','low':'99','close':'101'}, {'datetime':'2026-01-01 00:00:00','open':'100','high':'102','low':'99','close':'101'}]}
        now = datetime(2026,1,1,2,30,tzinfo=timezone.utc).timestamp()
        bars = providers.gold_bars('H1', now)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]['time'], now-9000)
        self.assertEqual(fetch.call_args.args[1]['timezone'], 'UTC')
        self.assertNotIn('volume', bars[0])

    @patch.dict(os.environ, {'TWELVE_DATA_API_KEY':''})
    def test_missing_gold_key_is_explicit(self):
        with self.assertRaisesRegex(providers.FeedError, 'TWELVE_DATA_API_KEY'):
            providers.gold_bars('H1')

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
        self.assertEqual(len(data['assets']), 2)
        for a in data['assets']:
            self.assertNotIn('error', a)
            self.assertTrue(0 <= a['score'] <= 100)
            self.assertIn('holdout', a['backtest'])
            self.assertEqual(len(a['checks']), 7)
            self.assertIn(a['higher_timeframe'], ('H4',))
            self.assertIsNone(a['context_error'])
            self.assertIsNotNone(a['context'])
            self.assertIsNotNone(a['volume_pressure_pct'])
            self.assertTrue(a['backtest']['assumptions']['higher_timeframe_context'])
        json.dumps(data, allow_nan=False)

    def test_host_and_port_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PORT', None)  # hosts such as Render export PORT even during build
            self.assertEqual((get_config([]).host, get_config([]).port), ('127.0.0.1', 8766))
        with patch.dict(os.environ, {'PORT': '10000'}):
            self.assertEqual(get_config([]).port, 10000)
            self.assertEqual(get_config(['--port', '9000', '--host', '0.0.0.0']).port, 9000)

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
