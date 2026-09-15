"""
بوت تداول مستقل بملف واحد — مصمم للعمل داخل GitHub Actions أو أي بيئة cron.
يفحص السوق مرة واحدة عند كل تشغيل (مناسب لجدولة GitHub Actions)، يحلل
بتقاطع المتوسطات المتحركة + مشاعر الأخبار، وينفّذ صفقة إذا لزم.

كل الإعدادات تأتي من متغيرات البيئة (Secrets في GitHub) — لا يوجد ملف .env هنا.
"""
import os
import sys
from datetime import datetime, timezone

MARKET = os.environ.get("BOT_MARKET", "crypto")          # stocks | crypto | forex
SYMBOL = os.environ.get("BOT_SYMBOL", "BTC/USDT")
QTY = float(os.environ.get("BOT_QTY", "0.001"))
SHORT_WINDOW = int(os.environ.get("BOT_SHORT_WINDOW", "10"))
LONG_WINDOW = int(os.environ.get("BOT_LONG_WINDOW", "30"))
LIVE = os.environ.get("BOT_LIVE", "false").lower() == "true"
USE_NEWS = os.environ.get("BOT_USE_NEWS", "true").lower() == "true"
NEWS_VETO_THRESHOLD = float(os.environ.get("BOT_NEWS_VETO_THRESHOLD", "0.3"))

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_IS_PAPER = "paper" in ALPACA_BASE_URL

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY", "")
BINANCE_TESTNET = os.environ.get("BINANCE_TESTNET", "true").lower() == "true"

OANDA_API_TOKEN = os.environ.get("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
OANDA_ENVIRONMENT = os.environ.get("OANDA_ENVIRONMENT", "practice")

EXN_API_KEY = os.environ.get("EXN_API_KEY", "")
EXN_PRIVATE_KEY = os.environ.get("EXN_PRIVATE_KEY", "")
EXN_ACCOUNT_ID = os.environ.get("EXN_ACCOUNT_ID", "")
EXN_API_BASE_URL = os.environ.get("EXN_API_BASE_URL", "https://api.exness.com")
FOREX_PROVIDER = os.environ.get("FOREX_PROVIDER", "exness")  # exness | oanda

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POSITIVE_WORDS = ["surge", "soar", "rally", "gain", "beat", "growth", "upgrade",
                   "bullish", "profit", "record high", "outperform", "strong",
                   "rise", "jump", "boost"]
NEGATIVE_WORDS = ["plunge", "crash", "drop", "loss", "miss", "downgrade",
                   "bearish", "decline", "fall", "sell-off", "underperform",
                   "weak", "cut", "lawsuit", "investigation", "fraud"]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def notify(msg):
    """يرسل إشعار تليجرام إن كانت البيانات مُعدّة. لا يوقف البوت إن فشل الإرسال."""
    log(msg)
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        log(f"تعذر إرسال إشعار تليجرام: {e}")


def _exn_b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _exn_load_private_key():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = EXN_PRIVATE_KEY
    try:
        key_bytes = base64.b64decode(raw)
        if len(key_bytes) == 32:
            return Ed25519PrivateKey.from_private_bytes(key_bytes)
    except Exception:
        pass
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))


def _exn_request(method, path, body=None, idempotency_key=""):
    import hashlib
    import json as _json
    import requests

    body_bytes = _json.dumps(body, separators=(",", ":")).encode() if body else b""
    ts = int(time.time() * 1000)
    body_hash = _exn_b64url(hashlib.sha256(body_bytes).digest())
    payload = {
        "api_key": EXN_API_KEY, "idempotency_key": idempotency_key,
        "timestamp": ts, "sign_version": 1, "method": method.upper(),
        "path": path, "body_hash": body_hash,
    }
    payload_bytes = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = _exn_load_private_key().sign(payload_bytes)
    headers = {
        "EXN-API-KEY": EXN_API_KEY, "EXN-IDEMPOTENCY-KEY": idempotency_key,
        "EXN-TIMESTAMP": str(ts), "EXN-SIGN-VERSION": "1",
        "EXN-DATA": _exn_b64url(payload_bytes), "EXN-SIGN": _exn_b64url(signature),
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    resp = requests.request(method, f"{EXN_API_BASE_URL}{path}", headers=headers,
                             data=body_bytes or None, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_closes():
    if MARKET == "stocks":
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockBarsRequest(
            symbol_or_symbols=SYMBOL.upper(),
            timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=60),
        )
        bars = client.get_stock_bars(req)
        return [float(b.close) for b in bars.data.get(SYMBOL.upper(), [])]

    if MARKET == "crypto":
        import ccxt
        exchange = ccxt.binance({
            "apiKey": BINANCE_API_KEY,
            "secret": BINANCE_SECRET_KEY,
            "enableRateLimit": True,
        })
        if BINANCE_TESTNET:
            exchange.set_sandbox_mode(True)
        ohlcv = exchange.fetch_ohlcv(SYMBOL.upper(), timeframe="1d", limit=60)
        return [c[4] for c in ohlcv]

    if MARKET == "forex":
        if FOREX_PROVIDER == "exness":
            from_ts = int(time.time() * 1000) - 60 * 24 * 3600 * 1000
            path = (f"/v1/market-data/accounts/{EXN_ACCOUNT_ID}/candles"
                     f"?instrument={SYMBOL.upper()}&timeframe=D1&price=bid&from={from_ts}&count=60")
            data = _exn_request("GET", path)
            return [c["close"] for c in data.get("candles", [])]
        else:
            import oandapyV20
            import oandapyV20.endpoints.instruments as instruments
            env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
            client = oandapyV20.API(access_token=OANDA_API_TOKEN, environment=env)
            params = {"count": 60, "granularity": "D", "price": "M"}
            r = instruments.InstrumentsCandles(instrument=SYMBOL.upper(), params=params)
            resp = client.request(r)
            return [float(c["mid"]["c"]) for c in resp["candles"] if c["complete"]]

    raise ValueError(f"سوق غير معروف: {MARKET}")


def moving_average(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def crossover_signal(closes):
    if len(closes) < LONG_WINDOW + 1:
        return "hold", None, None
    short_now = moving_average(closes, SHORT_WINDOW)
    long_now = moving_average(closes, LONG_WINDOW)
    short_prev = moving_average(closes[:-1], SHORT_WINDOW)
    long_prev = moving_average(closes[:-1], LONG_WINDOW)
    if None in (short_now, long_now, short_prev, long_prev):
        return "hold", short_now, long_now
    if short_prev <= long_prev and short_now > long_now:
        return "buy", short_now, long_now
    if short_prev >= long_prev and short_now < long_now:
        return "sell", short_now, long_now
    return "hold", short_now, long_now


def get_news_sentiment():
    if not USE_NEWS:
        return 0.0, []
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        client = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = NewsRequest(symbols=SYMBOL.upper().replace("/", ""), limit=15)
        resp = client.get_news(req)
        news_list = resp.news if hasattr(resp, "news") else resp.data.get("news", [])
        headlines = [n.headline.lower() for n in news_list]
    except Exception as e:
        log(f"تعذر جلب الأخبار ({e}) — سيُتجاهل تحليل الأخبار لهذه الدورة.")
        return 0.0, []

    if not headlines:
        return 0.0, []
    pos = sum(sum(1 for w in POSITIVE_WORDS if w in h) for h in headlines)
    neg = sum(sum(1 for w in NEGATIVE_WORDS if w in h) for h in headlines)
    total = pos + neg
    return ((pos - neg) / total if total else 0.0), headlines


def place_order(side):
    if MARKET == "stocks":
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_IS_PAPER)
        req = MarketOrderRequest(
            symbol=SYMBOL.upper(), qty=QTY,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(req)
        return {"id": str(order.id), "status": order.status.value}

    if MARKET == "crypto":
        import ccxt
        exchange = ccxt.binance({
            "apiKey": BINANCE_API_KEY, "secret": BINANCE_SECRET_KEY,
            "enableRateLimit": True,
        })
        if BINANCE_TESTNET:
            exchange.set_sandbox_mode(True)
        order = exchange.create_order(symbol=SYMBOL.upper(), type="market", side=side, amount=QTY)
        return {"id": order.get("id"), "status": order.get("status")}

    if MARKET == "forex":
        if FOREX_PROVIDER == "exness":
            import uuid
            path = f"/v1/trading/accounts/{EXN_ACCOUNT_ID}/positions"
            idempotency_key = f"bot-{uuid.uuid4().hex[:24]}"
            body = {"instrument": SYMBOL.upper(), "side": side.lower(), "volume": str(QTY)}
            return _exn_request("POST", path, body=body, idempotency_key=idempotency_key)
        else:
            import oandapyV20
            import oandapyV20.endpoints.orders as orders
            env = "practice" if OANDA_ENVIRONMENT == "practice" else "live"
            client = oandapyV20.API(access_token=OANDA_API_TOKEN, environment=env)
            signed_units = abs(QTY) if side == "buy" else -abs(QTY)
            data = {"order": {"type": "MARKET", "instrument": SYMBOL.upper(),
                                "units": str(signed_units), "timeInForce": "FOK",
                                "positionFill": "DEFAULT"}}
            r = orders.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=data)
            return client.request(r)


def main():
    mode = "حقيقي" if LIVE else "تجريبي"
    log(f"بدء الفحص — السوق: {MARKET} | الرمز: {SYMBOL} | الوضع: {mode}")

    if not LIVE:
        log("تنبيه: BOT_LIVE ليست true — لن يتم تنفيذ أي صفقة حقيقية مهما كانت الإشارة.")

    try:
        closes = get_closes()
    except Exception as e:
        notify(f"⚠️ فشل جلب الأسعار لـ {SYMBOL}: {e}")
        sys.exit(1)

    signal, short_ma, long_ma = crossover_signal(closes)
    log(f"الإشارة الفنية: {signal} (قصير={short_ma}, طويل={long_ma})")

    if signal == "hold":
        log("لا يوجد تقاطع جديد — لا إجراء.")
        return

    sentiment, headlines = get_news_sentiment()
    log(f"مشاعر الأخبار: {round(sentiment, 2)} ({len(headlines)} خبر)")

    if signal == "buy" and sentiment <= -NEWS_VETO_THRESHOLD:
        notify(f"🔵 إشارة شراء فنية على {SYMBOL} لكن الأخبار سلبية بقوة (مشاعر={round(sentiment,2)}) — تم التجاهل.")
        return
    if signal == "sell" and sentiment >= NEWS_VETO_THRESHOLD:
        notify(f"🔴 إشارة بيع فنية على {SYMBOL} لكن الأخبار إيجابية بقوة (مشاعر={round(sentiment,2)}) — تم التجاهل.")
        return

    icon = "🟢" if signal == "buy" else "🔻"
    if not LIVE:
        notify(f"{icon} [تجريبي] إشارة {signal.upper()} على {SYMBOL} — كان سيُنفَّذ بكمية {QTY} (الوضع تجريبي، لم يُنفَّذ فعلياً).")
        return

    try:
        result = place_order(signal)
        notify(f"{icon} تم تنفيذ صفقة {signal.upper()} على {SYMBOL} بكمية {QTY} ({MARKET}).\nالتفاصيل: {result}")
    except Exception as e:
        notify(f"❌ فشل تنفيذ صفقة {signal.upper()} على {SYMBOL}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

