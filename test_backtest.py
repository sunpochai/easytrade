import unittest
from unittest.mock import patch
from backtest import evaluate, exit_price, simulate, wilson_interval
from providers import demo_bars
from strategy import analyze, indicators, rule_signal


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.bars = [{"time": i*3600, "open": 100., "high": 101., "low": 99., "close": 100.} for i in range(205)]

    def test_stop_first_when_both_levels_hit(self):
        for p in [{"direction": 1, "stop": 98, "target": 104}, {"direction": -1, "stop": 102, "target": 96}]:
            result = exit_price(p, {"open": 100, "low": 95, "high": 105})
            self.assertEqual(result, (p['stop'], 'stop'))

    def test_gap_stop_is_not_filled_at_optimistic_stop(self):
        self.assertEqual(exit_price({"direction": 1, "stop": 98, "target": 104}, {"open": 90, "low": 89, "high": 100}), (90, 'gap_stop'))
        self.assertEqual(exit_price({"direction": -1, "stop": 102, "target": 96}, {"open": 110, "low": 100, "high": 111}), (110, 'gap_stop'))

    @patch('backtest.rule_signal')
    def test_signal_enters_next_open_and_fees_are_round_trip(self, signal):
        signal.side_effect = lambda bars, values, i, threshold: {'signal': 'BUY' if i == 199 else 'WAIT'}
        result = simulate(self.bars, 3600, fee_bps=10, slippage_bps=0, risk_pct=0.5)
        self.assertEqual(result['trades'], 1)
        trade = result['recent_trades'][0]
        self.assertEqual(trade['entry_time'], self.bars[200]['time'])
        # ATR=2, stop distance=3; per-unit risk includes both entry and stop fees.
        qty = 50 / (3 + .001*(100+97))
        self.assertAlmostEqual(trade['pnl'], -qty * 100 * .001 * 2)
        self.assertLess(result['net_return_pct'], 0)

    @patch('backtest.rule_signal')
    def test_slippage_costs_and_position_risk(self, signal):
        signal.return_value = {'signal': 'BUY'}
        free = simulate(self.bars, 3600, fee_bps=0, slippage_bps=0)
        costly = simulate(self.bars, 3600, fee_bps=10, slippage_bps=5)
        self.assertEqual(free['net_return_pct'], 0)
        self.assertLess(costly['net_return_pct'], free['net_return_pct'])
        self.bars[200]['low'] = 90
        stopped = simulate(self.bars[:201], 3600, fee_bps=10, slippage_bps=5)
        self.assertAlmostEqual(stopped['net_return_pct'], -.5)

    @patch('backtest.rule_signal')
    def test_spot_never_opens_short(self, signal):
        signal.return_value = {'signal': 'SELL'}
        result = simulate(self.bars, 3600, allow_short=False)
        self.assertEqual(result['trades'], 0)
        self.assertIsNone(result['win_rate_pct'])

    @patch('backtest.rule_signal')
    def test_opposite_signal_exits_at_open_before_intrabar_move(self, signal):
        signal.side_effect = lambda bars, values, i, threshold: {'signal': 'BUY' if i == 199 else 'SELL' if i == 200 else 'WAIT'}
        self.bars[201]['low'] = 90
        result = simulate(self.bars, 3600, fee_bps=0, slippage_bps=0, allow_short=False)
        self.assertEqual(result['recent_trades'][0]['reason'], 'opposite_signal')
        self.assertEqual(result['recent_trades'][0]['pnl'], 0)

    @patch('backtest.rule_signal')
    def test_stale_signal_not_entered_after_gap(self, signal):
        signal.side_effect = lambda bars, values, i, threshold: {'signal': 'BUY' if i == 199 else 'WAIT'}
        for b in self.bars[200:]:
            b['time'] += 100000
        self.assertEqual(simulate(self.bars, 3600)['trades'], 0)

    def test_indicators_and_rules_do_not_use_future_bars(self):
        bars = demo_bars('BTCUSD', 3600)
        full = indicators(bars)
        for n in (201, 400, 750):
            prefix = indicators(bars[:n])
            for key in full:
                self.assertEqual(prefix[key], full[key][:n])
            self.assertEqual(rule_signal(bars, full, n-1), rule_signal(bars[:n], prefix, n-1))

    def test_holdout_is_separate_and_not_selected(self):
        bars = demo_bars('BTCUSD', 3600)
        report = evaluate(bars, 3600)
        self.assertEqual(report['split_index'], 700)
        self.assertEqual(report['holdout']['start_time'], bars[700]['time'])
        self.assertTrue(all(t['entry_time'] >= bars[700]['time'] for t in report['holdout']['recent_trades']))

    def test_wilson_never_claims_certainty_with_small_sample(self):
        lo, hi = wilson_interval(1, 1)
        self.assertLess(lo, 21)
        self.assertAlmostEqual(hi, 100)
        self.assertIsNone(wilson_interval(0, 0))

    def test_spot_sell_has_no_short_plan(self):
        with patch('strategy.rule_signal', return_value={'signal':'SELL','score':100,'buy_score':0,'sell_score':100,'checks':[],'bias':'SELL'}):
            a = analyze(self.bars, 3600, now=205*3600, allow_short=False)
        self.assertEqual(a['action'], 'REDUCE')
        self.assertIsNone(a['target'])

    def test_equity_curve_is_chronological_sampled_and_starts_at_initial_equity(self):
        bars = demo_bars('BTCUSD', 3600)
        report = evaluate(bars, 3600)
        for period in (report['earlier'], report['holdout']):
            curve = period['equity_curve']
            self.assertLessEqual(len(curve), 242)
            self.assertEqual(curve[0]['time'], period['start_time'] + 3600)
            self.assertEqual(curve[-1]['time'], period['end_time'])
            self.assertEqual(len({p['time'] for p in curve}), len(curve))
            self.assertEqual(curve, sorted(curve, key=lambda p: p['time']))
            self.assertAlmostEqual(curve[-1]['equity'], 10000 * (1 + period['net_return_pct'] / 100))
        self.assertEqual(simulate(bars[:200], 3600)['equity_curve'], [])


if __name__ == '__main__':
    unittest.main()
