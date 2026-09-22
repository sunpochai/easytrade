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


def load_bars(source, asset, timeframe, broker_symbol):
    key = (source, asset, timeframe, broker_symbol)
    with CACHE_LOCK:
        lock = LOCKS.setdefault(key, threading.Lock())
    with lock:
        entry = CACHE.get(key)
        if entry and time.monotonic() < entry["expires"]:
            if entry.get("error"):
                raise FeedError(entry["error"])
            return entry["bars"], entry["fetched"]
        try:
            if source == "demo":
                bars = demo_bars(asset, TIMEFRAMES[timeframe])
            elif source == "mt5":
                bars = mt5_bars(broker_symbol, timeframe)
            else:
                bars = binance_bars(timeframe)
            validate_bars(bars)
        except (FeedError, ValueError) as exc:
            CACHE[key] = {"expires": time.monotonic()+60, "error": str(exc)}
            raise FeedError(str(exc)) from None
        fetched = time.time()
        CACHE[key] = {"expires": time.monotonic()+(300 if source == "api" else 60), "bars": bars, "fetched": fetched}
        return bars, fetched
