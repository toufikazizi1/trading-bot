"""
بوت إشارات اختراق متعدد الفلاتر (Breakout Signal Bot) — يرسل توصية عبر
تيليجرام فقط، بدون تنفيذ أي صفقة فعلية.

الفلاتر المطبّقة قبل إرسال أي إشارة:
  1. اختراق قمة/قاع سابق (Breakout أساسي)
  2. تأكيد استمرار الاختراق (Confirm Bars)
  3. اتجاه المتوسط المتحرك الأسي EMA200 (فلتر الاتجاه العام)
  4. تحقق الفريم الأعلى (Higher Timeframe Validation) — اتجاه الساعة يتفق
  5. مؤشر RSI (تجنب مناطق التشبع الشرائي/البيعي)
  6. حجم التداول (Volume) أعلى من المتوسط
  7. فلتر الجسم/الظل (Body & Wick) — الشمعة ذات جسم حقيقي قوي
  8. ارتباط الذهب بالبتكوين (Correlation Check) — تحذير فقط، لا يمنع الإشارة
  9. قاطع الدائرة (Circuit Breaker) — يوقف الإشارات مؤقتاً بعد خسائر متتالية

SL/TP يُحسبان ديناميكياً بواسطة ATR بدل نسبة ثابتة.

⚠️ هذا البوت لا يضمن نجاح أي صفقة. كل ما سبق يقلل نسبة الإشارات
الخاطئة فقط، ولا يلغيها. التداول يحمل مخاطرة دائماً.

مصدر البيانات: CryptoCompare API العام (لا يحتاج مفتاح API، ولا يحظر أي
منطقة جغرافية — بعكس Binance الذي يحظر الوصول من نطاقات IP الأمريكية
التي تشتغل منها سيرفرات GitHub Actions الافتراضية).
"""
import os
import sys
import json
import requests
from datetime import datetime, timezone

# ------------------------------------------------------------------
# الإعدادات (عبر متغيرات البيئة، مع قيم افتراضية معقولة)
# ------------------------------------------------------------------
# صيغة كل رمز: FSYM|LABEL  — عدة رموز مفصولة بفاصلة (FSYM = رمز العملة بـ CryptoCompare)
BOT_SYMBOLS = os.environ.get(
    "BOT_SYMBOLS", "BTC|BTC/USDT,PAXG|GOLD (PAXG)"
)

INTERVAL = os.environ.get("BOT_INTERVAL", "15m")          # الفريم الأساسي
HTF_INTERVAL = os.environ.get("BOT_HTF_INTERVAL", "1h")   # الفريم الأعلى للتحقق
LOOKBACK = int(os.environ.get("BOT_LOOKBACK", "24"))
CONFIRM_BARS = int(os.environ.get("BOT_CONFIRM_BARS", "2"))
EMA_PERIOD = int(os.environ.get("BOT_EMA_PERIOD", "200"))
ATR_PERIOD = int(os.environ.get("BOT_ATR_PERIOD", "14"))
RSI_PERIOD = int(os.environ.get("BOT_RSI_PERIOD", "14"))
VOLUME_MULT = float(os.environ.get("BOT_VOLUME_MULT", "1.2"))
BODY_RATIO_MIN = float(os.environ.get("BOT_BODY_RATIO_MIN", "0.5"))
ATR_SL_MULT = float(os.environ.get("BOT_ATR_SL_MULT", "1.5"))
ATR_TP1_MULT = float(os.environ.get("BOT_ATR_TP1_MULT", "1.5"))
ATR_TP2_MULT = float(os.environ.get("BOT_ATR_TP2_MULT", "3.0"))
HOURLY_UPDATE_MINUTES = int(os.environ.get("BOT_HOURLY_UPDATE_MINUTES", "60"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("BOT_MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE_HOURS = float(os.environ.get("BOT_CIRCUIT_BREAKER_PAUSE_HOURS", "4"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"
CC_HISTOMINUTE_URL = "https://min-api.cryptocompare.com/data/v2/histominute"
CC_HISTOHOUR_URL = "https://min-api.cryptocompare.com/data/v2/histohour"


# ------------------------------------------------------------------
# أدوات عامة
# ------------------------------------------------------------------
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def notify(msg):
    log(msg.replace("\n", " | "))
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"تعذر إرسال إشعار تليجرام: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def parse_symbols():
    result = []
    for item in BOT_SYMBOLS.split(","):
        item = item.strip()
        if not item:
            continue
        if "|" in item:
            sym, label = item.split("|", 1)
        else:
            sym, label = item, item
        result.append((sym.strip(), label.strip()))
    return result


# ------------------------------------------------------------------
# جلب البيانات من CryptoCompare
# ------------------------------------------------------------------
def _parse_cc_response(raw):
    data = raw.get("Data", {}).get("Data", [])
    candles = []
    for d in data:
        candles.append({
            "open_time": d["time"],
            "open": float(d["open"]),
            "high": float(d["high"]),
            "low": float(d["low"]),
            "close": float(d["close"]),
            # volumefrom = حجم التداول بوحدة العملة نفسها (BTC مثلاً)
            "volume": float(d.get("volumefrom", 0)),
        })
    return candles


def get_klines(fsym, interval, limit=300):
    """
    يرجع قائمة شموع: كل شمعة dict فيها open, high, low, close, volume.
    interval: "15m" أو "1h" (الفترتان المستخدمتان بهذا البوت فقط).
    """
    if interval.endswith("m"):
        aggregate = int(interval[:-1])
        url = CC_HISTOMINUTE_URL
    elif interval.endswith("h"):
        aggregate = int(interval[:-1])
        url = CC_HISTOHOUR_URL
    else:
        raise ValueError(f"فترة زمنية غير مدعومة: {interval}")

    params = {"fsym": fsym, "tsym": "USD", "limit": limit, "aggregate": aggregate}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    if raw.get("Response") == "Error":
        raise RuntimeError(raw.get("Message", "خطأ غير معروف من CryptoCompare"))
    return _parse_cc_response(raw)


# ------------------------------------------------------------------
# المؤشرات الفنية
# ------------------------------------------------------------------
def calc_ema(values, period):
    if len(values) < period:
        period = len(values)
    if period == 0:
        return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema_series(values, period):
    """يرجع سلسلة كاملة من قيم EMA (بدل قيمة أخيرة فقط) — لازمة لحساب MACD."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    series = [ema]
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
        series.append(ema)
    return series


def calc_macd_series(closes, fast=12, slow=26, signal=9):
    """يرجع (macd_line, signal_line) كسلسلتين متزامنتين بالطول."""
    ema_fast = calc_ema_series(closes, fast)
    ema_slow = calc_ema_series(closes, slow)
    if not ema_fast or not ema_slow:
        return [], []
    ema_fast_aligned = ema_fast[-len(ema_slow):]
    macd_line = [f - s for f, s in zip(ema_fast_aligned, ema_slow)]
    signal_line_full = calc_ema_series(macd_line, signal)
    if not signal_line_full:
        return macd_line, []
    macd_aligned = macd_line[-len(signal_line_full):]
    return macd_aligned, signal_line_full


def detect_zero_line_reversal(macd_line, trend_direction, lookback=5, near_zero_ratio=0.3):
    """
    يكتشف نمط ارتداد الخط الصفري (Zero Line Reversal): ماكد يقترب من الصفر
    بنفس جهة الترند العام دون أن يعبره بالكامل، ثم يرتد مبتعداً عنه —
    يعتبر تأكيداً على استمرار الاتجاه، وليس انعكاساً.
    """
    if len(macd_line) < lookback + 1:
        return False
    recent = macd_line[-lookback:]
    span = max(abs(v) for v in recent) or 1e-9
    near_zero_threshold = span * near_zero_ratio

    if trend_direction == "buy":
        if any(v <= 0 for v in recent):
            return False  # لازم يبقى فوق الصفر طول الفترة (بدون عبور)
        min_val = min(recent[:-1])
        if min_val > near_zero_threshold:
            return False  # ما اقترب كفاية من الصفر
        return recent[-1] > recent[-2] and recent[-1] > min_val
    else:
        if any(v >= 0 for v in recent):
            return False
        max_val = max(recent[:-1])
        if abs(max_val) > near_zero_threshold:
            return False
        return recent[-1] < recent[-2] and recent[-1] < max_val


def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        period = len(candles) - 1
    if period < 1:
        return 0.0
    trs = []
    for i in range(-period, 0):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs)


# ------------------------------------------------------------------
# الفلاتر
# ------------------------------------------------------------------
def find_breakout_level(closes):
    if len(closes) < LOOKBACK + 2:
        return None, None, None
    current = closes[-1]
    window = closes[-1 - LOOKBACK:-1]
    highest = max(window)
    lowest = min(window)
    if current > highest:
        return "buy", highest, lowest
    if current < lowest:
        return "sell", highest, lowest
    return None, highest, lowest


def is_breakout_confirmed(closes, direction):
    needed = LOOKBACK + CONFIRM_BARS + 1
    if len(closes) < needed:
        return False
    for i in range(CONFIRM_BARS):
        idx = -1 - i
        window = closes[idx - LOOKBACK:idx]
        level_high = max(window)
        level_low = min(window)
        price = closes[idx]
        if direction == "buy" and price <= level_high:
            return False
        if direction == "sell" and price >= level_low:
            return False
    return True


def check_body_wick(candle, direction):
    """يتحقق إن جسم الشمعة قوي نسبة للظل — حركة حقيقية لا مجرد رفرفة."""
    body = abs(candle["close"] - candle["open"])
    full_range = candle["high"] - candle["low"]
    if full_range == 0:
        return False
    body_ratio = body / full_range
    is_bullish_body = candle["close"] >= candle["open"]
    if direction == "buy" and not is_bullish_body:
        return False
    if direction == "sell" and is_bullish_body:
        return False
    return body_ratio >= BODY_RATIO_MIN


def check_volume(candles):
    if len(candles) < LOOKBACK + 1:
        return True  # بيانات غير كافية، لا نمنع الإشارة بسبب هذا وحده
    volumes = [c["volume"] for c in candles[-1 - LOOKBACK:-1]]
    avg_volume = sum(volumes) / len(volumes)
    current_volume = candles[-1]["volume"]
    if avg_volume == 0:
        return True
    return current_volume >= avg_volume * VOLUME_MULT


def check_higher_timeframe(symbol, direction):
    try:
        htf_candles = get_klines(symbol, HTF_INTERVAL, limit=EMA_PERIOD + 5)
        htf_closes = [c["close"] for c in htf_candles]
        htf_ema = calc_ema(htf_closes, EMA_PERIOD)
        htf_price = htf_closes[-1]
        if direction == "buy":
            return htf_price > htf_ema
        return htf_price < htf_ema
    except Exception as e:
        log(f"تعذر التحقق من الفريم الأعلى: {e} — سيتم تجاوز هذا الفلتر بحذر")
        return True


def check_correlation(btc_closes, gold_closes, direction, points=10):
    """يقارن اتجاه حركة البتكوين مقابل الذهب — تحذير فقط، لا يمنع الإشارة."""
    if len(btc_closes) < points + 1 or len(gold_closes) < points + 1:
        return None
    btc_change = (btc_closes[-1] - btc_closes[-1 - points]) / btc_closes[-1 - points]
    gold_change = (gold_closes[-1] - gold_closes[-1 - points]) / gold_closes[-1 - points]
    same_direction = (btc_change > 0) == (gold_change > 0)
    return {
        "btc_change_pct": btc_change * 100,
        "gold_change_pct": gold_change * 100,
        "aligned": same_direction,
    }


def evaluate_zlr_signal(closes, candles, ema):
    """
    مصدر إشارة ثانٍ مستقل عن الاختراق: يكتشف نمط ارتداد الخط الصفري
    لمؤشر MACD في اتجاه الترند العام (EMA200)، مع تطبيق فلاتر RSI،
    حجم التداول، وجسم الشمعة كتأكيد — بدون اشتراط اختراق قمة/قاع.
    """
    trend_direction = "buy" if closes[-1] > ema else "sell"

    if len(closes) < 35:
        return None, "بيانات غير كافية لحساب MACD (ZLR)"

    macd_line, signal_line = calc_macd_series(closes)
    if not macd_line:
        return None, "تعذر حساب MACD"

    if not detect_zero_line_reversal(macd_line, trend_direction):
        return None, None  # لا يوجد نمط حالياً — ليس خطأ، فقط لا إشارة

    rsi = calc_rsi(closes, RSI_PERIOD)
    if trend_direction == "buy" and rsi >= 70:
        return None, f"ZLR صاعد لكن RSI مرتفع ({rsi:.1f})"
    if trend_direction == "sell" and rsi <= 30:
        return None, f"ZLR هابط لكن RSI منخفض ({rsi:.1f})"
    if not check_volume(candles):
        return None, "ZLR لكن حجم التداول ضعيف"
    if not check_body_wick(candles[-1], trend_direction):
        return None, "ZLR لكن جسم الشمعة ضعيف"

    return trend_direction, "نمط ارتداد الخط الصفري (MACD ZLR) مؤكد باتجاه الترند العام"


# ------------------------------------------------------------------
# قاطع الدائرة (Circuit Breaker) — يتحقق من نتائج آخر إشارة قبل يرسل جديدة
# ------------------------------------------------------------------
def check_open_signal_outcome(symbol_state, current_price):
    """يتحقق هل آخر إشارة مفتوحة لمست TP أو SL، ويحدّث عدّاد الخسائر."""
    open_signal = symbol_state.get("open_signal")
    if not open_signal:
        return
    direction = open_signal["direction"]
    tp1 = open_signal["tp1"]
    sl = open_signal["sl"]

    hit_tp = (direction == "buy" and current_price >= tp1) or \
             (direction == "sell" and current_price <= tp1)
    hit_sl = (direction == "buy" and current_price <= sl) or \
             (direction == "sell" and current_price >= sl)

    if hit_tp:
        symbol_state["consecutive_losses"] = 0
        symbol_state["open_signal"] = None
        log(f"✅ آخر إشارة {symbol_state.get('label','')} لامست TP1 — تصفير عداد الخسائر")
    elif hit_sl:
        symbol_state["consecutive_losses"] = symbol_state.get("consecutive_losses", 0) + 1
        symbol_state["open_signal"] = None
        log(f"❌ آخر إشارة {symbol_state.get('label','')} لامست SL — عداد الخسائر = {symbol_state['consecutive_losses']}")


def is_circuit_broken(symbol_state):
    pause_until = symbol_state.get("pause_until")
    if pause_until:
        now = datetime.now(timezone.utc)
        until = datetime.fromisoformat(pause_until)
        if now < until:
            return True, until
        symbol_state["pause_until"] = None
    return False, None


def maybe_trigger_circuit_breaker(symbol_state, label):
    if symbol_state.get("consecutive_losses", 0) >= MAX_CONSECUTIVE_LOSSES:
        now = datetime.now(timezone.utc)
        pause_until = now.timestamp() + CIRCUIT_BREAKER_PAUSE_HOURS * 3600
        pause_until_iso = datetime.fromtimestamp(pause_until, tz=timezone.utc).isoformat()
        symbol_state["pause_until"] = pause_until_iso
        symbol_state["consecutive_losses"] = 0
        notify(
            f"⛔ <b>قاطع الدائرة (Circuit Breaker) — {label}</b>\n"
            f"تم إيقاف الإشارات مؤقتاً بعد {MAX_CONSECUTIVE_LOSSES} خسائر متتالية.\n"
            f"سيُستأنف الفحص تلقائياً بعد {CIRCUIT_BREAKER_PAUSE_HOURS} ساعة."
        )


# ------------------------------------------------------------------
# تنسيق الرسائل
# ------------------------------------------------------------------
def format_strong_signal(label, direction, entry, sl, tp1, tp2, atr, rsi, corr,
                          source="breakout", reason=""):
    icon = "🟢⬆️" if direction == "buy" else "🔴⬇️"
    word = "شراء" if direction == "buy" else "بيع"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if source == "zlr":
        title = "استمرار اتجاه — MACD Zero Line Reversal"
        filters_line = "الفلاتر المجتازة: اتجاه EMA200 ✅ | RSI ✅ | حجم تداول ✅ | جسم شمعة قوي ✅"
    else:
        title = "اختراق مؤكّد بعدة فلاتر"
        filters_line = (
            "الفلاتر المجتازة: EMA200 ✅ | فريم أعلى ✅ | حجم تداول ✅ | "
            "جسم شمعة قوي ✅ | تأكيد استمرار ✅"
        )

    corr_line = ""
    if corr is not None:
        align_icon = "✅" if corr["aligned"] else "⚠️"
        corr_line = (
            f"{align_icon} ترابط الذهب: BTC {corr['btc_change_pct']:+.2f}% / "
            f"GOLD {corr['gold_change_pct']:+.2f}%\n"
        )

    return (
        f"{icon} <b>إشارة {word} — {title}</b>\n"
        f"الرمز: {label}\n"
        f"التاريخ: {ts}\n\n"
        f"نقطة الدخول: {entry:,.2f}\n"
        f"SL: {sl:,.2f} (ديناميكي عبر ATR)\n"
        f"TP1: {tp1:,.2f}\n"
        f"TP2: {tp2:,.2f}\n\n"
        f"ATR: {atr:,.2f} | RSI: {rsi:.1f}\n"
        f"{corr_line}"
        f"{filters_line}\n\n"
        f"⚠️ إشارة مبنية على تحليل متعدد الفلاتر، لكنها ليست ضمان نجاح "
        f"ولا نصيحة مالية. أي صفقة تحمل مخاطرة، والتنفيذ يدوي بقرارك."
    )


def format_hourly_update(label, price, ema, rsi, trend_word):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🕐 <b>تحديث دوري (بدون توصية دخول)</b>\n"
        f"الرمز: {label}\n"
        f"التاريخ: {ts}\n\n"
        f"السعر الحالي: {price:,.2f}\n"
        f"الاتجاه العام (EMA{EMA_PERIOD}): {trend_word}\n"
        f"RSI: {rsi:.1f}\n\n"
        f"لا يوجد اختراق مؤكد حالياً يجتاز كل الفلاتر — هذا تحديث حالة "
        f"سوق فقط، وليس إشارة دخول."
    )


# ------------------------------------------------------------------
# معالجة رمز واحد
# ------------------------------------------------------------------
def minutes_since(iso_str):
    if not iso_str:
        return None
    last = datetime.fromisoformat(iso_str)
    now = datetime.now(timezone.utc)
    return (now - last).total_seconds() / 60


def process_symbol(symbol, label, state, other_closes=None):
    symbol_state = state.setdefault(symbol, {"label": label})
    symbol_state["label"] = label

    breaker_active, until = is_circuit_broken(symbol_state)
    if breaker_active:
        log(f"{label}: قاطع الدائرة فعّال حتى {until} — تخطي الفحص")
        return

    try:
        candles = get_klines(symbol, INTERVAL, limit=max(LOOKBACK + CONFIRM_BARS + 5, EMA_PERIOD + 5))
    except Exception as e:
        log(f"{label}: فشل جلب البيانات: {e}")
        return

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    # تحديث نتيجة آخر إشارة مفتوحة (لأجل قاطع الدائرة)
    check_open_signal_outcome(symbol_state, current_price)
    maybe_trigger_circuit_breaker(symbol_state, label)

    direction, highest, lowest = find_breakout_level(closes)
    ema = calc_ema(closes, EMA_PERIOD)
    rsi = calc_rsi(closes, RSI_PERIOD)
    atr = calc_atr(candles, ATR_PERIOD)

    signal_ok = False
    reject_reason = None

    if direction is None:
        reject_reason = "لا يوجد اختراق حالياً"
    elif not is_breakout_confirmed(closes, direction):
        reject_reason = f"اختراق غير مؤكد (يحتاج {CONFIRM_BARS} تشغيلات متتالية)"
    elif direction == "buy" and current_price < ema:
        reject_reason = f"اختراق صاعد لكن السعر تحت EMA{EMA_PERIOD}"
    elif direction == "sell" and current_price > ema:
        reject_reason = f"اختراق هابط لكن السعر فوق EMA{EMA_PERIOD}"
    elif direction == "buy" and rsi >= 70:
        reject_reason = f"RSI مرتفع ({rsi:.1f}) — مشترى بزيادة"
    elif direction == "sell" and rsi <= 30:
        reject_reason = f"RSI منخفض ({rsi:.1f}) — مباع بزيادة"
    elif not check_volume(candles):
        reject_reason = "حجم التداول ضعيف نسبة للمتوسط"
    elif not check_body_wick(candles[-1], direction):
        reject_reason = "جسم الشمعة ضعيف نسبة للظل (حركة غير حاسمة)"
    elif not check_higher_timeframe(symbol, direction):
        reject_reason = f"الفريم الأعلى ({HTF_INTERVAL}) لا يدعم نفس الاتجاه"
    else:
        signal_ok = True

    def send_signal(direction, source):
        if direction == "buy":
            sl = current_price - atr * ATR_SL_MULT
            tp1 = current_price + atr * ATR_TP1_MULT
            tp2 = current_price + atr * ATR_TP2_MULT
        else:
            sl = current_price + atr * ATR_SL_MULT
            tp1 = current_price - atr * ATR_TP1_MULT
            tp2 = current_price - atr * ATR_TP2_MULT

        corr = None
        if other_closes is not None:
            corr = check_correlation(closes, other_closes, direction)

        notify(format_strong_signal(label, direction, current_price, sl, tp1, tp2, atr, rsi, corr, source=source))

        symbol_state["open_signal"] = {
            "direction": direction, "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        symbol_state["last_sent"] = datetime.now(timezone.utc).isoformat()
        log(f"{label}: تم إرسال إشارة {direction} (المصدر: {source})")

    if signal_ok:
        send_signal(direction, source="breakout")
        return

    log(f"{label}: لا اختراق — {reject_reason}")

    # المصدر الثاني: نمط ارتداد الخط الصفري لمؤشر MACD (ZLR)
    zlr_direction, zlr_reason = evaluate_zlr_signal(closes, candles, ema)
    if zlr_direction is not None:
        send_signal(zlr_direction, source="zlr")
        return
    if zlr_reason:
        log(f"{label}: لا ZLR — {zlr_reason}")

    elapsed = minutes_since(symbol_state.get("last_sent"))
    if elapsed is None or elapsed >= HOURLY_UPDATE_MINUTES:
        trend_word = "صاعد (فوق EMA)" if current_price > ema else "هابط (تحت EMA)"
        notify(format_hourly_update(label, current_price, ema, rsi, trend_word))
        symbol_state["last_sent"] = datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# البرنامج الرئيسي
# ------------------------------------------------------------------
def main():
    symbols = parse_symbols()
    log(f"بدء الفحص — الرموز: {[s[1] for s in symbols]}")

    state = load_state()

    # نجلب إغلاقات كل الرموز مسبقاً لاستخدامها بفلتر الترابط (Correlation)
    closes_by_symbol = {}
    for symbol, label in symbols:
        try:
            candles = get_klines(symbol, INTERVAL, limit=50)
            closes_by_symbol[symbol] = [c["close"] for c in candles]
        except Exception:
            closes_by_symbol[symbol] = None

    for symbol, label in symbols:
        others = [v for k, v in closes_by_symbol.items() if k != symbol and v is not None]
        other_closes = others[0] if others else None
        process_symbol(symbol, label, state, other_closes=other_closes)

    save_state(state)


if __name__ == "__main__":
    main()

    
