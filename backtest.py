"""Chronological simulation. A signal at bar close may enter only at the next open."""
import math
from strategy import indicators, rule_signal


def wilson_interval(wins, count):
    if not count:
        return None
    z, p = 1.96, wins/count
    divisor = 1 + z*z/count
    center = (p + z*z/(2*count)) / divisor
    radius = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / divisor
    return [100*(center-radius), 100*(center+radius)]


def exit_price(position, bar):
    """Gaps fill at open; if stop and target touch in one bar, assume stop first."""
    d, stop, target = (position[k] for k in ("direction", "stop", "target"))
    if d*(bar["open"]-stop) <= 0:
        return bar["open"], "gap_stop"
    if d*(bar["open"]-target) >= 0:
        return target, "target"
    if (bar["low"] <= stop if d == 1 else bar["high"] >= stop):
        return stop, "stop"
    if (bar["high"] >= target if d == 1 else bar["low"] <= target):
        return target, "target"
    return None


def simulate(bars, seconds, *, threshold=85, fee_bps=10, slippage_bps=5,
             risk_pct=0.5, allow_short=True, start=200, end=None, values=None):
    values = indicators(bars) if values is None else values
    end = len(bars) if end is None else end
    start = max(200, start)
    fee, slip = fee_bps/10000, slippage_bps/10000
    equity = peak = 10000.0
    drawdown = 0.0
    position = None
    trades, curve = [], []

    def liquidated_value(raw):
        filled = raw * (1 - position["direction"] * slip)
        return equity + position["qty"] * (position["direction"]*(filled-position["entry"]) - filled*fee)

    for i in range(start, end):
        bar = bars[i]
        signal = rule_signal(bars, values, i-1, threshold)
        fresh = bar["time"] - (bars[i-1]["time"] + seconds) <= 2*seconds
        if position is None and equity > 0 and fresh:
            side = signal["signal"]
            if side == "BUY" or (side == "SELL" and allow_short):
                d = 1 if side == "BUY" else -1
                reference = bars[i-1]["close"]
                distance = values["atr"][i-1] * 1.5
                stop, target = reference-d*distance, reference+d*distance*2
                entry = bar["open"] * (1+d*slip)
                if min(stop, target) > 0 and d*(entry-stop) > 0 and d*(target-entry) > 0:
                    stop_fill = stop*(1-d*slip)
                    risk_per_unit = d*(entry-stop_fill) + fee*(entry+stop_fill)
                    qty = min(equity*risk_pct/100/risk_per_unit, equity/(entry*(1+fee)))
                    before = equity
                    equity -= qty*entry*fee
                    position = {"direction": d, "entry": entry, "qty": qty, "stop": stop, "target": target,
                                "opened": bar["time"], "before": before, "index": i}
        if position:
            opposite = fresh and signal["signal"] == ("SELL" if position["direction"] == 1 else "BUY")
            if opposite:
                open_only = {**bar, "high": bar["open"], "low": bar["open"]}
                outcome = exit_price(position, open_only) or (bar["open"], "opposite_signal")
            else:
                outcome = exit_price(position, bar)
            if outcome is None and (i-position["index"] >= 47 or i == end-1):
                outcome = (bar["close"], "sample_end" if i == end-1 else "time_exit")
            if outcome:
                raw, why = outcome
                equity = liquidated_value(raw)
                trades.append({"entry_time": position["opened"], "exit_bar_time": bar["time"],
                               "side": "BUY" if position["direction"] == 1 else "SELL", "reason": why,
                               "pnl": equity-position["before"], "return_pct": (equity/position["before"]-1)*100})
                position = None
        marked = liquidated_value(bar["close"]) if position else equity
        peak = max(peak, marked)
        drawdown = max(drawdown, (peak-marked)/peak*100)
        curve.append({"time": bar["time"]+seconds, "equity": marked})
    wins = sum(t["pnl"] > 0 for t in trades)
    gains = sum(max(0, t["pnl"]) for t in trades)
    losses = -sum(min(0, t["pnl"]) for t in trades)
    count = len(trades)
    sampled = curve[::max(1, len(curve)//120)]
    if curve and sampled[-1] is not curve[-1]:
        sampled.append(curve[-1])
    return {"trades": count, "wins": wins, "win_rate_pct": wins/count*100 if count else None,
            "win_rate_interval": wilson_interval(wins, count), "net_return_pct": (equity/10000-1)*100,
            "max_drawdown_pct": drawdown, "profit_factor": gains/losses if losses else None,
            "profit_factor_status": "finite" if losses else "no_losses" if gains else "no_trades",
            "average_trade_pct": sum(t["return_pct"] for t in trades)/count if count else None,
            "enough_trades": count >= 30, "start_time": bars[start]["time"] if start < end else None,
            "end_time": bars[end-1]["time"]+seconds if start < end else None,
            "recent_trades": trades[-20:], "equity_curve": sampled}


def evaluate(bars, seconds, context=None, funding=None, **settings):
    values = {**indicators(bars), "context": context, "funding": funding}
    split = max(200, int(len(bars)*0.7))
    earlier = simulate(bars, seconds, end=split, values=values, **settings)
    holdout = simulate(bars, seconds, start=split, values=values, **settings)
    return {"earlier": earlier, "holdout": holdout, "split_index": split,
            "method": "fixed_rules_chronological_70_30", "bars": len(bars),
            "assumptions": {**settings, "initial_equity": 10000, "max_notional_multiple": 1,
                            "max_holding_bars": 48, "drawdown_sampling": "bar_close_liquidation_value",
                            "same_bar_exit": "stop_first", "funding_included": False,
                            "higher_timeframe_context": context is not None, "higher_bars_closed_only": True,
                            "funding_rate_filter": funding is not None, "funding_settled_only": True,
                            "volatility_regime_filter": True, "weekend_filter": True,
                            "live_only_guards_not_backtested": ["order_book_spread", "macro_events"]}}
