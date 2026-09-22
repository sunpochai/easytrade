"""EasyTrade: read-only market recommendations and chronological backtests."""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backtest import evaluate
from providers import FeedError, demo_bars, load_bars
from strategy import ALL_TIMEFRAMES, HIGHER, TIMEFRAMES, VERSION, analyze, context_series, ema, indicators

ROOT = Path(__file__).resolve().parent
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml"), "/sw.js": ("sw.js", "text/javascript"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}


def build_asset(config, asset, source, timeframe):
    broker = config.btc_symbol if asset == "BTC" else config.gold_symbol
    symbol = ("BTCUSDT" if asset == "BTC" else "XAU/USD") if source == "api" else broker
    feed = ("Binance Spot" if asset == "BTC" else "Twelve Data") if source == "api" else source.upper()
    allow_short = not (asset == "BTC" and source != "mt5")
    metadata = {"asset": asset, "symbol": symbol, "feed": feed,
                "quote": "USDT" if source == "api" and asset == "BTC" else "USD", "allow_short": allow_short}
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
        values = {**indicators(bars), "context": context}
        result = analyze(bars, TIMEFRAMES[timeframe], threshold=config.min_score, allow_short=allow_short, values=values)
        report = evaluate(bars, TIMEFRAMES[timeframe], context=context, **settings)
        if context_error:
            result["reason"] = context_error + " จึงไม่ให้สัญญาณจนกว่าจะมีข้อมูลกรอบใหญ่"
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
                "strategy_version": VERSION, "higher_timeframe": higher, "context_error": context_error}
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
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        assets = list(pool.map(lambda asset: build_asset(config, asset, source, timeframe), ("BTC", "GOLD")))
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
    parser.add_argument("--gold-symbol", default="XAUUSD")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; use 0.0.0.0 only behind a hosting platform")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8766")), help="Defaults to $PORT when a host sets it")
    parser.add_argument("--min-score", type=int, choices=range(70, 101), default=85)
    parser.add_argument("--fee-bps", type=float, default=10, help="Assumed commission per side, basis points")
    parser.add_argument("--slippage-bps", type=float, default=5, help="Assumed adverse slippage plus half-spread per side")
    parser.add_argument("--risk-pct", type=float, default=0.5, help="Simulated stop risk as percent of equity")
    config = parser.parse_args(args)
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
