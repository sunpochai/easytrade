import unittest
from bot import analyze, demo_bars, ema
from strategy import atr_ratio, context_series, funding_series, indicators, is_weekend, volume_pressure

END = 1771430400  # 2026-02-18 16:00 UTC (Wednesday): last bar close, so weekday rules are deterministic


def timed(bars, seconds, end=END):
    for i, b in enumerate(bars):
        b["time"] = end - (len(bars) - i) * seconds
    return bars


def trending(bars, direction):
    price = 1000
    for i, b in enumerate(bars):
        opening = price
        price += direction * (3 if i % 2 else -2)
        b.update(open=opening, close=price, low=min(opening, price)-1, high=max(opening, price)+1, volume=10 if i % 2 else 5)
    return bars


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.bars = timed(demo_bars("BTCUSD", 3600), 3600)
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

    def with_context(self, bars, direction, funding=0.0001):
        higher = trending(timed(demo_bars("BTCUSD", 14400), 14400), direction)
        settlements = [{"time": t, "rate": funding} for t in range(END - 1000 * 3600, END + 1, 28800)]
        return {**indicators(bars), "context": context_series(bars, 3600, higher, 14400),
                "funding": funding_series(bars, 3600, settlements)}

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

    def test_funding_series_uses_only_settled_rates(self):
        bars = trending(self.bars, 1)
        settlements = [{"time": bars[10]["time"] + 3600, "rate": 0.0001}, {"time": bars[20]["time"] + 3601, "rate": 0.0005}]
        series = funding_series(bars, 3600, settlements[::-1])
        self.assertIsNone(series[9])
        self.assertEqual(series[10], 0.0001)
        self.assertEqual(series[20], 0.0001)  # settled one second after this bar closed
        self.assertEqual(series[21], 0.0005)
        self.assertIsNone(funding_series(bars, 3600, None))

    def test_crowded_funding_blocks_same_side_only(self):
        bars = trending(self.bars, 1)
        crowded = analyze(bars, 3600, self.now, values=self.with_context(bars, 1, funding=0.0004))
        self.assertFalse(crowded['checks'][7]['pass'])
        self.assertEqual(crowded['score'], 90)
        self.assertEqual(crowded['funding_rate'], 0.0004)
        negative = analyze(bars, 3600, self.now, values=self.with_context(bars, 1, funding=-0.0009))
        self.assertTrue(negative['checks'][7]['pass'])
        missing = analyze(bars, 3600, self.now, values={**self.with_context(bars, 1), "funding": None})
        self.assertEqual(missing['checks'][7]['note'], 'ไม่มีข้อมูล funding ที่ชำระแล้ว')
        self.assertIsNone(missing['funding_rate'])

    def test_weekend_bar_loses_liquidity_points(self):
        bars = trending(self.bars, 1)
        self.assertFalse(is_weekend(bars[-1]['time']))
        saturday = timed(trending(self.bars, 1), 3600, end=END + 3 * 86400)  # last bar opens Saturday 15:00 UTC
        self.assertTrue(is_weekend(saturday[-1]['time']))
        a = analyze(saturday, 3600, saturday[-1]['time'] + 3600, values=self.with_context(saturday, 1))
        self.assertFalse(a['checks'][9]['pass'])
        self.assertTrue(a['weekend'])
        self.assertEqual(a['score'], 95)

    def test_volatility_regime(self):
        bars = trending(self.bars, 1)
        values = self.with_context(bars, 1)
        self.assertAlmostEqual(atr_ratio(values, len(bars) - 1), 1.0, places=1)
        self.assertIsNone(atr_ratio(values, 100))
        spike = bars[-1]
        spike['high'] = spike['close'] + 200
        values = self.with_context(bars, 1)
        a = analyze(bars, 3600, self.now, values=values)
        self.assertGreater(a['atr_ratio'], 2)
        self.assertFalse(a['checks'][8]['pass'])

    def test_context_uses_only_closed_higher_bars(self):
        bars = trending(self.bars, 1)
        higher = trending(timed(demo_bars("BTCUSD", 14400), 14400), 1)[-260:]  # only 60 H4 bars beyond warm-up cover the H1 history
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
