"""Fixed, causal technical rules; scores are rule coverage, not probabilities."""
import math
import time
from datetime import datetime, timezone

VERSION = "trend-pullback-v4"
TIMEFRAMES = {"M15": 900, "H1": 3600, "H4": 14400}
HIGHER = {"M15": "H1", "H1": "H4", "H4": "D1"}
ALL_TIMEFRAMES = {**TIMEFRAMES, "D1": 86400}
VOLUME_WINDOW = 20
FUNDING_LIMIT = 0.0003      # 0.03% per 8h settlement: three times Binance's neutral 0.01% baseline
ATR_REGIME_WINDOW = 100
ATR_REGIME = (0.5, 2.0)     # ATR14 relative to its 100-bar mean
MANDATORY = (0, 1, 4, 5)


def ema(values, period):
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(result[-1] + alpha * (value - result[-1]))
    return result


def wilder_series(values, period=14):
    out = [None] * len(values)
    if len(values) >= period:
        out[period - 1] = sum(values[:period]) / period
        for i in range(period, len(values)):
            out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def validate_bars(bars, minimum=200):
    if len(bars) < minimum:
        raise ValueError(f"ต้องมีแท่งราคาปิดอย่างน้อย {minimum} แท่ง")
    previous = None
    for b in bars:
        if any(not isinstance(b.get(k), (int, float)) or not math.isfinite(b[k]) for k in ("time", "open", "high", "low", "close")):
            raise ValueError("ข้อมูลราคามีค่าที่ไม่ถูกต้อง")
        if not 0 < b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]:
            raise ValueError("ข้อมูล OHLC ไม่ถูกต้อง")
        volume = b.get("volume")
        if volume is not None and (not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume < 0):
            raise ValueError("ข้อมูลปริมาณซื้อขายไม่ถูกต้อง")
        if previous is not None and b["time"] <= previous:
            raise ValueError("เวลาแท่งราคาต้องเรียงและไม่ซ้ำ")
        previous = b["time"]


def indicators(bars):
    validate_bars(bars)
    closes = [b["close"] for b in bars]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gains = wilder_series([max(c, 0) for c in changes])
    losses = wilder_series([max(-c, 0) for c in changes])
    rsi = [None] + [None if g is None else 50 if g == l == 0 else 100 if l == 0 else 100 - 100 / (1 + g/l) for g, l in zip(gains, losses)]
    ranges = [max(b["high"] - b["low"], abs(b["high"] - a["close"]), abs(b["low"] - a["close"])) for a, b in zip(bars, bars[1:])]
    return {"fast": ema(closes, 20), "slow": ema(closes, 50), "trend": ema(closes, 200),
            "rsi": rsi, "atr": [None] + wilder_series(ranges)}


def volume_pressure(bars, i, window=VOLUME_WINDOW):
    """Share (%) of volume traded on up-closing bars over the last `window` closed bars; None when volume is missing."""
    if i + 1 < window:
        return None
    up = down = 0.0
    for b in bars[i + 1 - window:i + 1]:
        volume = b.get("volume")
        if volume is None:
            return None
        if b["close"] > b["open"]:
            up += volume
        elif b["close"] < b["open"]:
            down += volume
    total = up + down
    return up / total * 100 if total > 0 else None


def atr_ratio(values, i, window=ATR_REGIME_WINDOW):
    """ATR14 at bar i divided by its mean over the last `window` bars (inclusive); None until enough history."""
    if i + 1 < window + 14:
        return None
    recent = [a for a in values["atr"][i + 1 - window:i + 1] if a is not None]
    if len(recent) < window:
        return None
    mean = sum(recent) / window
    return values["atr"][i] / mean if mean > 0 else None


def funding_series(bars, seconds, funding):
    """Per base bar: the latest funding settlement (list of {time, rate}) at or before the bar close; None before the first."""
    if funding is None:
        return None
    settlements = sorted(funding, key=lambda f: f["time"])
    out, j = [], -1
    for b in bars:
        close_time = b["time"] + seconds
        while j + 1 < len(settlements) and settlements[j + 1]["time"] <= close_time:
            j += 1
        out.append(None if j < 0 else settlements[j]["rate"])
    return out


def is_weekend(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).weekday() >= 5


def context_series(bars, seconds, higher_bars, higher_seconds):
    """Per base bar: state of the latest higher-timeframe bar that closed no later than the base bar closed.

    Uses only closed higher bars, so the backtest never sees a higher-timeframe bar before it finished."""
    values = indicators(higher_bars)
    out, j = [], -1
    for b in bars:
        close_time = b["time"] + seconds
        while j + 1 < len(higher_bars) and higher_bars[j + 1]["time"] + higher_seconds <= close_time:
            j += 1
        if j < 199:
            out.append(None)
            continue
        close, mid, long, rsi = higher_bars[j]["close"], values["slow"][j], values["trend"][j], values["rsi"][j]
        bias = 1 if close > mid > long else -1 if close < mid < long else 0
        out.append({"bias": bias, "rsi": rsi, "close": close, "ema50": mid, "ema200": long,
                    "bar_close": higher_bars[j]["time"] + higher_seconds})
    return out


def rule_signal(bars, values, i, threshold=85):
    price = bars[i]["close"]
    fast, slow, trend = (values[k][i] for k in ("fast", "slow", "trend"))
    atr, rsi = values["atr"][i], values["rsi"][i]
    empty = {"signal": "WAIT", "score": 0, "buy_score": 0, "sell_score": 0, "checks": [], "bias": "NEUTRAL"}
    if i < 199 or atr is None or atr <= 0:
        return empty
    context = values.get("context")
    ctx = context[i] if context and i < len(context) else None
    funding = values.get("funding")
    rate = funding[i] if funding and i < len(funding) else None
    pressure = volume_pressure(bars, i)
    ratio = atr_ratio(values, i)
    weekend = is_weekend(bars[i]["time"])
    candidates = []
    for direction, side in ((1, "BUY"), (-1, "SELL")):
        checks = [
            {"name": "ราคาและ EMA20/50/200 เรียงตามแนวโน้ม", "weight": 25,
             "pass": price > fast > slow > trend if direction == 1 else price < fast < slow < trend},
            {"name": "RSI อยู่ในช่วง 50–68 / 32–50 ตามทิศทาง", "weight": 10,
             "pass": 50 <= rsi <= 68 if direction == 1 else 32 <= rsi <= 50},
            {"name": "EMA50 เคลื่อนตามแนวโน้ม 5 แท่ง", "weight": 5,
             "pass": direction * (slow - values["slow"][i-5]) > 0},
            {"name": "ระยะ EMA20 กับ EMA50 อย่างน้อย 0.25 ATR", "weight": 5,
             "pass": abs(fast - slow) >= 0.25 * atr},
            {"name": "ราคาไม่ห่าง EMA20 เกิน 1.5 ATR", "weight": 10,
             "pass": abs(price - fast) <= 1.5 * atr},
            {"name": "ภาพใหญ่กรอบถัดขึ้นไป: ราคา EMA50 EMA200 เรียงตามทิศทาง และ RSI ไม่สุดโต่ง (≤70 / ≥30)", "weight": 15,
             "pass": ctx is not None and ctx["bias"] == direction and (ctx["rsi"] <= 70 if direction == 1 else ctx["rsi"] >= 30),
             "note": None if ctx is not None else "ไม่มีข้อมูลภาพใหญ่ที่ปิดแล้ว"},
            {"name": "ปริมาณซื้อขาย 20 แท่ง: ฝั่งตามทิศทางมากกว่าฝั่งตรงข้าม", "weight": 10,
             "pass": pressure is not None and (pressure > 50 if direction == 1 else pressure < 50),
             "note": None if pressure is not None else "ไม่มีข้อมูลปริมาณจากแหล่งนี้"},
            {"name": "Funding rate ล่าสุด (Binance Futures) ไม่แออัดฝั่งเดียวกับสัญญาณ: BUY ≤ +0.03% / SELL ≥ −0.03% ต่อ 8 ชม.", "weight": 10,
             "pass": rate is not None and (rate <= FUNDING_LIMIT if direction == 1 else rate >= -FUNDING_LIMIT),
             "note": None if rate is not None else "ไม่มีข้อมูล funding ที่ชำระแล้ว"},
            {"name": "ความผันผวนไม่ผิดปกติ: ATR14 อยู่ระหว่าง 0.5–2 เท่าของค่าเฉลี่ย 100 แท่ง", "weight": 5,
             "pass": ratio is not None and ATR_REGIME[0] <= ratio <= ATR_REGIME[1],
             "note": None if ratio is not None else "ประวัติ ATR ยังไม่ครบ 100 แท่ง"},
            {"name": "แท่งนี้ไม่ได้เปิดในวันเสาร์–อาทิตย์ (UTC) ซึ่งสภาพคล่องเบาบาง", "weight": 5, "pass": not weekend},
        ]
        score = sum(c["weight"] for c in checks if c["pass"])
        ready = all(checks[k]["pass"] for k in MANDATORY) and score >= threshold
        candidates.append({"signal": side if ready else "WAIT", "score": score, "checks": checks, "bias": side})
    best = max(candidates, key=lambda x: x["score"])
    return {**best, "buy_score": candidates[0]["score"], "sell_score": candidates[1]["score"]}


def analyze(bars, seconds, now=None, threshold=85, allow_short=True, values=None):
    values = indicators(bars) if values is None else values
    now = time.time() if now is None else now
    age = now - (bars[-1]["time"] + seconds)
    if age < 0:
        raise ValueError("ข้อมูลมีแท่งราคาที่ยังไม่ปิดหรือเวลาอยู่ในอนาคต")
    result = rule_signal(bars, values, len(bars)-1, threshold)
    stale = age > seconds * 2
    reason = "รอ: แนวโน้ม โมเมนตัม ภาพใหญ่ funding ความผันผวน หรือจังหวะเข้าไม่ผ่านเกณฑ์"
    if stale:
        result["signal"] = "WAIT"
        reason = "ข้อมูลล่าช้าหรือตลาดปิด จึงระงับคำแนะนำ"
    elif result["signal"] != "WAIT":
        reason = "ผ่านเกณฑ์แนวโน้ม โมเมนตัม ภาพใหญ่ ปริมาณ และ funding ใช้ระดับด้านล่างเป็นแผนราคาอ้างอิง"
    action = result["signal"]
    if action == "SELL" and not allow_short:
        action = "REDUCE"
        reason = "แนวโน้มลง: พิจารณาลด BTC ที่ถืออยู่ หากไม่มีสถานะให้รอ (Spot ไม่เปิด Short)"
    price, atr = bars[-1]["close"], values["atr"][-1]
    direction = 1 if action == "BUY" else -1
    tradable = action in ("BUY", "SELL")
    stop = price - direction * atr * 1.5 if tradable else None
    target = price + direction * atr * 3 if tradable else None
    if tradable and min(stop, target) <= 0:
        result["signal"], action, reason = "WAIT", "WAIT", "ความผันผวนสูงเกินไป ระดับแผนราคาไม่ถูกต้อง"
        stop = target = None
    context = values.get("context")
    funding = values.get("funding")
    return {**result, "funding_rate": funding[-1] if funding else None, "atr_ratio": atr_ratio(values, len(bars)-1),
            "weekend": is_weekend(bars[-1]["time"]), "action": action, "reason": reason, "price": price, "ema20": values["fast"][-1],
            "ema50": values["slow"][-1], "ema200": values["trend"][-1], "rsi": values["rsi"][-1],
            "atr": atr, "stale": stale, "bar_close": bars[-1]["time"] + seconds, "stop": stop, "target": target,
            "target_move_pct": abs(target-price)/price*100 if target is not None else None,
            "stop_move_pct": abs(stop-price)/price*100 if stop is not None else None,
            "context": context[-1] if context else None, "volume_pressure_pct": volume_pressure(bars, len(bars)-1),
            "bars": bars[-120:], "fast": values["fast"][-120:], "slow": values["slow"][-120:]}
