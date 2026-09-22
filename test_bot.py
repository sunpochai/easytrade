import unittest
from bot import analyze, demo_bars, ema
from strategy import context_series, indicators, volume_pressure


def trending(bars, direction):
    price = 1000
    for i, b in enumerate(bars):
        opening = price
        price += direction * (3 if i % 2 else -2)
        b.update(open=opening, close=price, low=min(opening, price)-1, high=max(opening, price)+1, volume=10 if i % 2 else 5)
    return bars


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.bars = demo_bars("BTCUSD", 3600)
        self.now = self.bars[-1]["time"] + 3600

    def test_demo_finite_levels(self):
        a = analyze(self.bars, 3600, self.now)
        self.assertTrue(0 <= a["rsi"] <= 100)
        self.assertGreater(a["atr"], 0)
        if a["signal"] != "WAIT":
            self.assertAlmostEqual(abs(a["target"]-a["price"]), 2*abs(a["stop"]-a["price"]))

    def test_stale_disables_signal(self):
        a = analyze(self.bars, 3600, self.now + 10801)
        self.assertEqual(a["signal"], "WAIT")
        self.assertIsNone(a["stop"])

    def test_invalid_and_open_bars_rejected(self):
        with self.assertRaises(ValueError):
            analyze(self.bars, 3600, self.now - 1)
        self.bars[-1]["close"] = float('nan')
        with self.assertRaises(ValueError):
            analyze(self.bars, 3600, self.now)

    def test_flat_market(self):
        for b in self.bars:
            for k in ("open", "high", "low", "close"):
                b[k] = 100
        a = analyze(self.bars, 3600, self.now)
        self.assertEqual((a["rsi"], a["atr"], a["signal"]), (50, 0, "WAIT"))

    def test_known_ema(self):
        self.assertEqual(ema([1, 2, 3], 3), [1, 1.5, 2.25])

    def with_context(self, bars, direction):
        higher = trending(demo_bars("BTCUSD", 14400), direction)
        return {**indicators(bars), "context": context_series(bars, 3600, higher, 14400)}

    def test_buy_sell_risk_direction(self):
        for direction, expected in [(1, 'BUY'), (-1, 'SELL')]:
            bars = trending(self.bars, direction)
            a = analyze(bars, 3600, self.now, values=self.with_context(bars, direction))
            self.assertEqual(a['signal'], expected)
            self.assertEqual(a['score'], 100)
            self.assertEqual(a['context']['bias'], direction)
            self.assertGreater((a['price'] - a['stop'])*direction, 0)
            self.assertGreater((a['target'] - a['price'])*direction, 0)

    def test_no_higher_timeframe_context_means_wait(self):
        bars = trending(self.bars, 1)
        a = analyze(bars, 3600, self.now)
        self.assertEqual(a['signal'], 'WAIT')
        self.assertIsNone(a['context'])
        self.assertEqual(a['checks'][5]['note'], 'ไม่มีข้อมูลภาพใหญ่ที่ปิดแล้ว')
        opposite = analyze(bars, 3600, self.now, values=self.with_context(bars, -1))
        self.assertEqual(opposite['signal'], 'WAIT')
        self.assertEqual(opposite['context']['bias'], -1)

    def test_volume_pressure_and_missing_volume(self):
        bars = trending(self.bars, 1)
        self.assertAlmostEqual(volume_pressure(bars, len(bars)-1), 200/3)
        for b in bars:
            b.pop('volume')
        a = analyze(bars, 3600, self.now, values=self.with_context(bars, 1))
        self.assertIsNone(a['volume_pressure_pct'])
        self.assertEqual(a['score'], 90)
        self.assertEqual(a['signal'], 'BUY')
        self.assertEqual(a['checks'][6]['note'], 'ไม่มีข้อมูลปริมาณจากแหล่งนี้')
        bars[-1]['volume'] = -1
        with self.assertRaises(ValueError):
            analyze(bars, 3600, self.now)

    def test_context_uses_only_closed_higher_bars(self):
        bars = trending(self.bars, 1)
        higher = trending(demo_bars("BTCUSD", 14400), 1)[-260:]  # only 60 H4 bars beyond warm-up cover the H1 history
        context = context_series(bars, 3600, higher, 14400)
        self.assertEqual(len(context), len(bars))
        for b, ctx in zip(bars, context):
            if ctx is not None:
                self.assertLessEqual(ctx['bar_close'], b['time'] + 3600)
                self.assertGreater(ctx['bar_close'] + 14400, b['time'] + 3600)
        self.assertIsNone(context[0])
        self.assertIsNotNone(context[-1])

if __name__ == '__main__':
    unittest.main()
