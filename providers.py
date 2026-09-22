"""Read-only HTTPS market adapters. No exchange credentials or order endpoints."""
import json
import random
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from strategy import ALL_TIMEFRAMES as TIMEFRAMES, validate_bars

MT5_LOCK = threading.Lock()
CACHE_LOCK = threading.Lock()
CACHE = {}
LOCKS = {}


class FeedError(RuntimeError):
    pass


def get_json(base, params):
    request = Request(base + "?" + urlencode(params), headers={"User-Agent": "EasyTrade/2.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=12) as response:
            payload = response.read(3_000_001)
        if len(payload) > 3_000_000:
            raise FeedError("API ส่งข้อมูลเกินขนาดที่รองรับ")
        return json.loads(payload)
    except HTTPError as exc:
        if exc.code in (418, 429):
            raise FeedError("API จำกัดความถี่ กรุณารอสักครู่แล้วลองใหม่") from None
        raise FeedError(f"API ตอบ HTTP {exc.code}: ตรวจสอบสิทธิ์ข้อมูลและการเข้าถึงจากเครือข่าย") from None
    except (URLError, TimeoutError, OSError):
        raise FeedError("ติดต่อ API ไม่ได้ ตรวจสอบอินเทอร์เน็ตหรือข้อจำกัดเครือข่าย") from None
    except (ValueError, UnicodeError):
        raise FeedError("API ตอบข้อมูลในรูปแบบที่ไม่รองรับ") from None


def binance_bars(timeframe, now=None):
    now = time.time() if now is None else now
    interval = {"M15": "15m", "H1": "1h", "H4": "4h", "D1": "1d"}[timeframe]
    payload = get_json("https://data-api.binance.vision/api/v3/klines", {"symbol": "BTCUSDT", "interval": interval, "limit": 1000})
    if not isinstance(payload, list):
        raise FeedError("Binance ไม่ส่งข้อมูลแท่งราคา")
    try:
        return [{"time": int(r[0])/1000, "open": float(r[1]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4]),
                 "volume": float(r[5])}
                for r in payload if int(r[0])/1000 + TIMEFRAMES[timeframe] <= now and int(r[6])/1000 < now]
    except (ValueError, TypeError, IndexError):
        raise FeedError("รูปแบบแท่งราคาของ Binance ไม่ถูกต้อง") from None


FUTURES = "https://fapi.binance.com"
SPOT = "https://data-api.binance.vision"


def binance_funding(now=None):
    """Settled BTCUSDT perpetual funding rates (8h), oldest first; about 333 days at limit 1000."""
    now = time.time() if now is None else now
    payload = get_json(FUTURES + "/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 1000})
    if not isinstance(payload, list):
        raise FeedError("Binance Futures ไม่ส่งข้อมูล funding")
    try:
        rows = [{"time": int(r["fundingTime"]) / 1000, "rate": float(r["fundingRate"])} for r in payload]
    except (KeyError, ValueError, TypeError):
        raise FeedError("รูปแบบ funding ของ Binance ไม่ถูกต้อง") from None
    rows = [r for r in rows if r["time"] <= now]
    rows.sort(key=lambda r: r["time"])
    if not rows:
        raise FeedError("ยังไม่มี funding ที่ชำระแล้ว")
    return rows


def binance_open_interest():
    """Open interest now versus 24 hours ago (Binance keeps only 30 days of hourly history)."""
    payload = get_json(FUTURES + "/futures/data/openInterestHist", {"symbol": "BTCUSDT", "period": "1h", "limit": 25})
    if not isinstance(payload, list) or len(payload) < 2:
        raise FeedError("Binance Futures ไม่ส่งข้อมูล open interest")
    try:
        first, last = float(payload[0]["sumOpenInterest"]), float(payload[-1]["sumOpenInterest"])
        return {"open_interest_btc": last, "value_usd": float(payload[-1]["sumOpenInterestValue"]),
                "change_24h_pct": (last / first - 1) * 100 if first > 0 else None,
                "time": int(payload[-1]["timestamp"]) / 1000, "hours": len(payload) - 1}
    except (KeyError, ValueError, TypeError):
        raise FeedError("รูปแบบ open interest ของ Binance ไม่ถูกต้อง") from None


def binance_depth(band_pct=0.5):
    """Spot order book: spread in bps and bid/ask depth (BTC) within ±band_pct of the mid price."""
    payload = get_json(SPOT + "/api/v3/depth", {"symbol": "BTCUSDT", "limit": 500})
    try:
        bids = [(float(p), float(q)) for p, q in payload["bids"]]
        asks = [(float(p), float(q)) for p, q in payload["asks"]]
        best_bid, best_ask = bids[0][0], asks[0][0]
    except (KeyError, ValueError, TypeError, IndexError):
        raise FeedError("รูปแบบ order book ของ Binance ไม่ถูกต้อง") from None
    if not 0 < best_bid <= best_ask:
        raise FeedError("order book ของ Binance ไม่สอดคล้อง")
    mid = (best_bid + best_ask) / 2
    band = mid * band_pct / 100
    bid_depth = sum(q for p, q in bids if p >= mid - band)
    ask_depth = sum(q for p, q in asks if p <= mid + band)
    total = bid_depth + ask_depth
    return {"mid": mid, "spread_bps": (best_ask - best_bid) / mid * 10000, "bid_depth_btc": bid_depth,
            "ask_depth_btc": ask_depth, "bid_share_pct": bid_depth / total * 100 if total > 0 else None,
            "band_pct": band_pct, "time": time.time()}


def binance_eth_btc(days=20):
    """ETH/BTC change over the last `days` closed daily bars: rising means money rotating away from BTC."""
    payload = get_json(SPOT + "/api/v3/klines", {"symbol": "ETHBTC", "interval": "1d", "limit": days + 2})
    if not isinstance(payload, list) or len(payload) < days + 2:
        raise FeedError("Binance ไม่ส่งข้อมูล ETH/BTC")
    try:
        closes = [float(r[4]) for r in payload[:-1]]  # drop the open daily bar
    except (ValueError, TypeError, IndexError):
        raise FeedError("รูปแบบ ETH/BTC ของ Binance ไม่ถูกต้อง") from None
    return {"ratio": closes[-1], "change_pct": (closes[-1] / closes[-1 - days] - 1) * 100, "days": days}


def coingecko_dominance():
    payload = get_json("https://api.coingecko.com/api/v3/global", {})
    try:
        share = payload["data"]["market_cap_percentage"]
        return {"btc_pct": float(share["btc"]), "eth_pct": float(share.get("eth", 0.0)),
                "time": float(payload["data"]["updated_at"])}
    except (KeyError, ValueError, TypeError):
        raise FeedError("รูปแบบข้อมูล CoinGecko ไม่ถูกต้อง") from None


def demo_funding(seconds, count=1000):
    """Synthetic 8h settlements covering the demo bar range; deterministic like demo_bars."""
    rng = random.Random("funding" + str(seconds))
    end = int(time.time()) // 28800 * 28800
    span = max(1, count * seconds // 28800 + 2)
    return [{"time": end - (span - i) * 28800, "rate": rng.gauss(0.0001, 0.0001)} for i in range(span)]


def demo_live():
    rng = random.Random("live")
    return {"open_interest": {"open_interest_btc": 110000.0, "value_usd": 9.4e9, "change_24h_pct": rng.uniform(-3, 3),
                              "time": time.time(), "hours": 24},
            "depth": {"mid": 60000.0, "spread_bps": 0.2, "bid_depth_btc": 40.0, "ask_depth_btc": 35.0, "bid_share_pct": 53.3,
                      "band_pct": 0.5, "time": time.time()},
            "eth_btc": {"ratio": 0.032, "change_pct": rng.uniform(-5, 5), "days": 20},
            "dominance": {"btc_pct": 57.0, "eth_pct": 12.0, "time": time.time()}}


def demo_bars(symbol, seconds, count=1000):
    rng = random.Random(symbol + str(seconds))
    end = int(time.time()) // seconds * seconds
    price = 60000 if symbol.startswith("BTC") else 2500
    bars = []
    for i in range(count):
        opening = price
        price *= 1 + rng.gauss(0.00025, 0.003)
        spread = opening * rng.uniform(0.001, 0.004)
        bars.append({"time": end-(count-i)*seconds, "open": opening, "close": price,
                     "high": max(opening, price)+spread, "low": min(opening, price)-spread,
                     "volume": rng.uniform(50, 150) * (1 + 40 * abs(price/opening - 1))})
    return bars


def mt5_bars(symbol, timeframe):
    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise FeedError("ติดตั้ง MetaTrader5: python -m pip install -r requirements-mt5.txt") from None
    with MT5_LOCK:
        if not mt5.initialize():
            raise FeedError("เชื่อมต่อ MT5 ไม่สำเร็จ กรุณาเปิดโปรแกรมและล็อกอินบัญชี")
        try:
            if not mt5.symbol_select(symbol, True):
                raise FeedError("ไม่พบสัญลักษณ์ใน MT5 ตรวจสอบชื่อที่โบรกเกอร์ใช้")
            rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, "TIMEFRAME_"+timeframe), 1, 1000)
            if rates is None:
                raise FeedError("MT5 อ่านประวัติราคาไม่ได้")
            return [{**{k: float(r[k]) for k in ("time", "open", "high", "low", "close")}, "volume": float(r["tick_volume"])}
                    for r in rates]
        finally:
            mt5.shutdown()


def cached(key, ttl, loader):
    """Serve one loader call per key per ttl seconds; failures are cached for 60 s and re-raised, never replaced."""
    with CACHE_LOCK:
        lock = LOCKS.setdefault(key, threading.Lock())
    with lock:
        entry = CACHE.get(key)
        if entry and time.monotonic() < entry["expires"]:
            if entry.get("error"):
                raise FeedError(entry["error"])
            return entry["value"], entry["fetched"]
        try:
            value = loader()
        except (FeedError, ValueError) as exc:
            CACHE[key] = {"expires": time.monotonic()+60, "error": str(exc)}
            raise FeedError(str(exc)) from None
        fetched = time.time()
        CACHE[key] = {"expires": time.monotonic()+ttl, "value": value, "fetched": fetched}
        return value, fetched


def load_bars(source, asset, timeframe, broker_symbol):
    def loader():
        if source == "demo":
            bars = demo_bars(asset, TIMEFRAMES[timeframe])
        elif source == "mt5":
            bars = mt5_bars(broker_symbol, timeframe)
        else:
            bars = binance_bars(timeframe)
        validate_bars(bars)
        return bars
    return cached((source, asset, timeframe, broker_symbol), 300 if source == "api" else 60, loader)


def load_funding(source, seconds):
    """Funding history for the rules; demo is synthetic, api and mt5 both read Binance Futures (BTC-wide signal)."""
    if source == "demo":
        return cached(("demo", "funding", seconds), 60, lambda: demo_funding(seconds))
    return cached(("binance", "funding"), 300, binance_funding)


LIVE_LOADERS = {"open_interest": binance_open_interest, "depth": binance_depth,
                "eth_btc": binance_eth_btc, "dominance": coingecko_dominance}
LIVE_TTL = {"open_interest": 300, "depth": 60, "eth_btc": 900, "dominance": 900}


def load_live(source):
    """Display-only and live-guard data; each item fails separately and is reported as an error string."""
    if source == "demo":
        return {name: {"value": value, "fetched": time.time()} for name, value in demo_live().items()}
    out = {}
    for name, loader in LIVE_LOADERS.items():
        try:
            value, fetched = cached(("binance", "live", name), LIVE_TTL[name], loader)
            out[name] = {"value": value, "fetched": fetched}
        except FeedError as exc:
            out[name] = {"error": str(exc)}
    return out
