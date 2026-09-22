"""EasyTrade: read-only market recommendations and chronological backtests."""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backtest import evaluate
from providers import FeedError, demo_bars, load_bars, load_funding, load_live
from strategy import ALL_TIMEFRAMES, HIGHER, TIMEFRAMES, VERSION, analyze, context_series, ema, funding_series, indicators

ROOT = Path(__file__).resolve().parent
MAX_SPREAD_BPS = 5.0   # BTCUSDT normally trades under 1 bp; wider means the book is thin or the venue is stressed
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/sw.js": ("sw.js", "text/javascript"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}


def load_events(path):
    """Macro calendar: list of {name, time(sec)} plus the withholding window in seconds; missing file means no guard."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        events = [{"name": str(e["name"]), "time": datetime.fromisoformat(str(e["time"]).replace("Z", "+00:00")).timestamp()}
                  for e in raw.get("events", [])]
        return {"events": sorted(events, key=lambda e: e["time"]), "before": float(raw.get("window_before_min", 60)) * 60,
                "after": float(raw.get("window_after_min", 120)) * 60, "source": str(path)}
    except FileNotFoundError:
        return {"events": [], "before": 0, "after": 0, "source": None}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit(f"events file {path} มีรูปแบบไม่ถูกต้อง")


def active_event(calendar, now):
    for e in calendar["events"]:
        if e["time"] - calendar["before"] <= now <= e["time"] + calendar["after"]:
            return e
    return None


def next_event(calendar, now):
    return next((e for e in calendar["events"] if e["time"] > now), None)


def live_guards(config, live, now):
    """Checks that only make sense at read time; they can veto a signal but are never part of the backtest."""
    guards = []
    depth = live.get("depth", {})
    if "value" in depth:
        spread = depth["value"]["spread_bps"]
        guards.append({"name": f"สภาพคล่อง: สเปรด order book ไม่เกิน {MAX_SPREAD_BPS:g} bps", "pass": spread <= MAX_SPREAD_BPS,
                       "note": f"สเปรดตอนนี้ {spread:.2f} bps"})
    else:
        guards.append({"name": f"สภาพคล่อง: สเปรด order book ไม่เกิน {MAX_SPREAD_BPS:g} bps", "pass": False,
                       "note": "อ่าน order book ไม่ได้: " + depth.get("error", "")})
    event = active_event(config.calendar, now)
    upcoming = next_event(config.calendar, now)
    guards.append({"name": "ไม่อยู่ในช่วง 1 ชม. ก่อนถึง 2 ชม. หลังประกาศตัวเลขสำคัญ (events.json)", "pass": event is None,
                   "note": f"อยู่ในช่วง {event['name']}" if event else
                   (f"ถัดไป: {upcoming['name']} {datetime.fromtimestamp(upcoming['time'], timezone.utc):%Y-%m-%d %H:%M} UTC" if upcoming
                    else "ไม่มีเหตุการณ์ในปฏิทิน")})
    return guards


def build_asset(config, asset, source, timeframe, now=None):
    broker = config.btc_symbol
    symbol = "BTCUSDT" if source == "api" else broker
    feed = "Binance Spot" if source == "api" else source.upper()
    allow_short = source == "mt5"
    metadata = {"asset": asset, "symbol": symbol, "feed": feed,
                "quote": "USDT" if source == "api" else "USD", "allow_short": allow_short}
    try:
        bars, fetched = load_bars(source, asset, timeframe, broker)
        settings = {"threshold": config.min_score, "fee_bps": config.fee_bps, "slippage_bps": config.slippage_bps,
                    "risk_pct": config.risk_pct, "allow_short": allow_short}
        higher = HIGHER[timeframe]
        context_error = None
        try:
            higher_bars, _ = load_bars(source, asset, higher, broker)
            context = context_series(bars, TIMEFRAMES[timeframe], higher_bars, ALL_TIMEFRAMES[higher])
        except (FeedError, ValueError) as exc:
            context, context_error = None, f"โหลดภาพใหญ่ {higher} ไม่ได้: {exc}"
        funding_error = None
        try:
            settlements, _ = load_funding(source, TIMEFRAMES[timeframe])
            funding = funding_series(bars, TIMEFRAMES[timeframe], settlements)
        except (FeedError, ValueError) as exc:
            funding, funding_error = None, f"โหลด funding ไม่ได้: {exc}"
        values = {**indicators(bars), "context": context, "funding": funding}
        now = time.time() if now is None else now
        result = analyze(bars, TIMEFRAMES[timeframe], now=now, threshold=config.min_score, allow_short=allow_short, values=values)
        report = evaluate(bars, TIMEFRAMES[timeframe], context=context, funding=funding, **settings)
        if context_error:
            result["reason"] = context_error + " จึงไม่ให้สัญญาณจนกว่าจะมีข้อมูลกรอบใหญ่"
        live = load_live(source)
        guards = live_guards(config, live, now)
        vetoed = [g for g in guards if not g["pass"]]
        if vetoed and result["action"] != "WAIT":
            result.update(signal="WAIT", action="WAIT", stop=None, target=None, target_move_pct=None, stop_move_pct=None,
                          reason="เงื่อนไขผ่านแต่ระงับชั่วคราว: " + " · ".join(g["note"] or g["name"] for g in vetoed))
        h = report["holdout"]
        if source == "demo":
            evidence = "ข้อมูลสาธิต: ผลทดสอบนี้ใช้ตรวจการทำงานเท่านั้น"
        elif not h["enough_trades"]:
            evidence = f"หลักฐานยังน้อย: ช่วงทดสอบล่าสุดมี {h['trades']} เทรด ยังไม่ถึง 30 เทรด"
        elif h["net_return_pct"] <= 0:
            evidence = "ช่วงทดสอบล่าสุดยังไม่ทำกำไรสุทธิหลังต้นทุนสมมติ"
        else:
            evidence = "ช่วงทดสอบล่าสุดเป็นบวก แต่ไม่ได้ยืนยันกำไรในอนาคต"
        return {**metadata, **result, "fetched_at": fetched, "backtest": report, "evidence": evidence,
                "strategy_version": VERSION, "higher_timeframe": higher, "context_error": context_error,
                "funding_error": funding_error, "live": live, "guards": guards}
    except (FeedError, ValueError) as exc:
        return {**metadata, "error": str(exc)}


def make_handler(config):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request = urlsplit(self.path)
            if request.path == "/api/analysis":
                query = parse_qs(request.query)
                source = query.get("source", [config.source])[0]
                timeframe = query.get("timeframe", [config.timeframe])[0]
                if source not in ("api", "demo", "mt5") or timeframe not in TIMEFRAMES:
                    self.send_json(400, {"error": "source หรือ timeframe ไม่ถูกต้อง"})
                    return
                try:
                    assets = [build_asset(config, "BTC", source, timeframe)]
                    self.send_json(200, {"source": source, "timeframe": timeframe, "assets": assets,
                                        "min_score": config.min_score, "updated": datetime.now(timezone.utc).isoformat(),
                                        "settings": {"risk_pct": config.risk_pct, "fee_bps": config.fee_bps, "slippage_bps": config.slippage_bps}})
                except Exception:
                    self.send_json(503, {"error": "ประมวลผลไม่สำเร็จ กรุณาตรวจสอบข้อมูลและลองใหม่"})
            elif request.path == "/api/health":
                self.send_json(200, {"status": "ok", "version": VERSION, "trading_enabled": False})
            elif request.path in STATIC:
                filename, content_type = STATIC[request.path]
                charset = "" if content_type == "image/png" else "; charset=utf-8"
                self.send_body(200, (ROOT / filename).read_bytes(), content_type + charset)
            else:
                self.send_body(404, b"Not found", "text/plain")

        def send_json(self, status, value):
            self.send_body(status, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

        def send_body(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    return Handler


def get_config(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["api", "demo", "mt5"], default="api")
    parser.add_argument("--timeframe", choices=TIMEFRAMES, default="H1")
    parser.add_argument("--btc-symbol", default="BTCUSD")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 only behind a hosting platform")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")), help="Defaults to $PORT when a host sets it")
    parser.add_argument("--min-score", type=int, choices=range(70, 101), default=85)
    parser.add_argument("--fee-bps", type=float, default=10, help="Assumed commission per side, basis points")
    parser.add_argument("--slippage-bps", type=float, default=5, help="Assumed adverse slippage plus half-spread per side")
    parser.add_argument("--risk-pct", type=float, default=0.5, help="Simulated stop risk as percent of equity")
    parser.add_argument("--events", default=str(ROOT / "events.json"), help="Macro calendar JSON; signals pause around each entry")
    config = parser.parse_args(args)
    config.calendar = load_events(config.events)
    if not (0 <= config.fee_bps <= 100 and 0 <= config.slippage_bps <= 100 and 0 < config.risk_pct <= 2):
        parser.error("fee/slippage ต้องอยู่ในช่วง 0–100 bps และ risk-pct มากกว่า 0 ไม่เกิน 2")
    return config


if __name__ == "__main__":
    config = get_config()
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    print(f"EasyTrade: http://{config.host}:{config.port} | source={config.source} | {config.timeframe}", flush=True)
    if config.host != "127.0.0.1":
        print("คำเตือน: เปิดรับการเชื่อมต่อจากภายนอก หน้าเว็บไม่มีรหัสผ่าน ใครเข้าถึงพอร์ตนี้ได้ก็อ่านและใช้โควตา API ได้", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
