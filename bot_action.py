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

مصدر البيانات: Kraken Public API (لا يحتاج مفتاح API، ولا يحظر أي
نطاق جغرافي — بعكس Binance/Bybit اللي يحظرو IPs أمريكية اللي تشتغل
منها سيرفرات GitHub Actions الافتراضية، وبعكس CryptoCompare اللي
سكرات خدمتها المجانية).
"""
import os
import sys
import json
import requests
from datetime import datetime, timezone

# ------------------------------------------------------------------
# الإعدادات (عبر متغيرات البيئة، مع قيم افتراضية معقولة)
# ------------------------------------------------------------------
# صيغة كل رمز: PAIR|LABEL — عدة رموز مفصولة بفاصلة
# PAIR = رمز الزوج بصيغة Kraken (مثال: XBTUSD لبتكوين، PAXGUSD للذهب)
# ملاحظة: الافتراضي دروك الذهب وحدو فقط (بطلب المستخدم — تركيز أعلى)
BOT_SYMBOLS = os.environ.get(
    "BOT_SYMBOLS", "PAXGUSD|GOLD (PAXG)"
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
ALLOW_COUNTER_TREND = os.environ.get("BOT_ALLOW_COUNTER_TREND", "1") == "1"
SR_SIGNAL = os.environ.get("BOT_SR_SIGNAL", "1") == "1"
SR_PIVOT_LENGTH = int(os.environ.get("BOT_SR_PIVOT_LENGTH", "5"))
SR_TOLERANCE_ATR = float(os.environ.get("BOT_SR_TOLERANCE_ATR", "0.15"))
MPE_SIGNAL = os.environ.get("BOT_MPE_SIGNAL", "1") == "1"
MPE_LOOKBACK = int(os.environ.get("BOT_MPE_LOOKBACK", "30"))
MPE_SCORE_THRESHOLD = int(os.environ.get("BOT_MPE_SCORE_THRESHOLD", "5"))
MPE_VP_BINS = int(os.environ.get("BOT_MPE_VP_BINS", "20"))
VAB_SIGNAL = os.environ.get("BOT_VAB_SIGNAL", "1") == "1"
VAB_LOOKBACK = int(os.environ.get("BOT_VAB_LOOKBACK", "40"))
HOURLY_UPDATE_MINUTES = int(os.environ.get("BOT_HOURLY_UPDATE_MINUTES", "60"))
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("BOT_MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE_HOURS = float(os.environ.get("BOT_CIRCUIT_BREAKER_PAUSE_HOURS", "4"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# تحويل الفريم إلى عدد الدقائق اللي يفهمها Kraken
KRAKEN_INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


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
# جلب البيانات من Kraken
# ------------------------------------------------------------------
def get_klines(pair, interval, limit=300):
    """
    يرجع قائمة شموع: كل شمعة dict فيها open, high, low, close, volume.
    pair: رمز الزوج بصيغة Kraken (مثال: XBTUSD).
    interval: "15m" أو "1h" (الفترتان المستخدمتان بهذا البوت فقط).
    """
    minutes = KRAKEN_INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"فترة زمنية غير مدعومة: {interval}")

    params = {"pair": pair, "interval": minutes}
    resp = requests.get(KRAKEN_OHLC_URL, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    if raw.get("error"):
        raise RuntimeError("; ".join(raw["error"]) or "خطأ غير معروف من Kraken")

    result = raw.get("result", {})
    # النتيجة تجي تحت مفتاح باسم الزوج الداخلي متاع Kraken (ماشي بالضرورة
    # نفس النص اللي بعثناه)، فنلقاو أول مفتاح غير "last"
    ohlc_key = next((k for k in result if k != "last"), None)
    if not ohlc_key:
        raise RuntimeError(f"لا توجد بيانات لهذا الزوج: {pair}")

    candles = []
    for row in result[ohlc_key]:
        candles.append({
            "open_time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })

    return candles[-limit:] if limit else candles


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


def check_volume(candles, mult=None):
    if len(candles) < LOOKBACK + 1:
        return True  # بيانات غير كافية، لا نمنع الإشارة بسبب هذا وحده
    volumes = [c["volume"] for c in candles[-1 - LOOKBACK:-1]]
    avg_volume = sum(volumes) / len(volumes)
    current_volume = candles[-1]["volume"]
    if avg_volume == 0:
        return True
    return current_volume >= avg_volume * (mult if mult is not None else VOLUME_MULT)


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


def evaluate_pullback_signal(closes, candles, ema, atr):
    """
    مصدر إشارة ثالث (Pullback): يُستخدم كي يكون RSI متطرف (مباع/مشترى بزيادة)
    فيمنع الاختراق و ZLR. بدل ما يبقى ينتظر، يستنى ارتداد لـ EMA20
    (أو RSI يرجع لمنطقة 40-60) ثم يدخل مع الاتجاه العام (EMA200)
    بشرط شمعة تأكيد في اتجاه الصفقة.
    """
    if len(closes) < 40:
        return None, None

    trend_direction = "buy" if closes[-1] > ema else "sell"
    ema20 = calc_ema(closes, 20)
    rsi_now = calc_rsi(closes, RSI_PERIOD)
    last = candles[-1]

    # RSI خلال آخر 12 شمعة — لازم كان متطرف في اتجاه الترند
    rsis = [calc_rsi(closes[:len(closes) - k], RSI_PERIOD) for k in range(0, 12)]

    if trend_direction == "sell":
        was_extreme = min(rsis) <= 30
        # لازم السعر يكون قريب فعلاً من EMA20 (جهة صحيحة)، مش بعيد في قمة موجة
        near_ema20 = ema20 - 0.4 * atr <= closes[-1] <= ema20 + 0.2 * atr
        rsi_not_too_recovered = rsi_now <= 55  # يمنع الدخول كي RSI يكون طالع بزاف (قرب قمة)
        candle_ok = last["close"] < last["open"]
    else:
        was_extreme = max(rsis) >= 70
        near_ema20 = ema20 - 0.2 * atr <= closes[-1] <= ema20 + 0.4 * atr
        rsi_not_too_recovered = rsi_now >= 45  # يمنع الدخول كي RSI يكون نازل بزاف (قرب قاع)
        candle_ok = last["close"] > last["open"]

    if not was_extreme:
        return None, None
    if not near_ema20:
        return None, "Pullback: ننتظر السعر يرجع أقرب فعلاً لـ EMA20"
    if not rsi_not_too_recovered:
        return None, "Pullback: RSI ابتعد بزاف عن منطقة الارتداد، فات وقت الدخول"
    # السعر لازم يبقى في الجهة الصحيحة من EMA200
    if not candle_ok:
        return None, "Pullback: ننتظر شمعة تأكيد في اتجاه الترند"
    if trend_direction == "sell" and rsi_now <= 30:
        return None, None
    if trend_direction == "buy" and rsi_now >= 70:
        return None, None

    return trend_direction, "Pullback بعد تطرف RSI — دخول مع الترند العام"


def calc_adx(candles, period=14):
    """
    قوة الاتجاه (ADX) — ما بين 0 و100. تحت 20 يعني سوق عرضي بلا اتجاه واضح،
    فوق 25 يعني اتجاه قوي يستاهل المتابعة.
    """
    if len(candles) < period * 2:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up_move = candles[i]["high"] - candles[i - 1]["high"]
        down_move = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        trs.append(tr)

    def smooth(values, period):
        if len(values) < period:
            return []
        first = sum(values[:period])
        out = [first]
        for v in values[period:]:
            out.append(out[-1] - (out[-1] / period) + v)
        return out

    tr_s = smooth(trs, period)
    plus_s = smooth(plus_dm, period)
    minus_s = smooth(minus_dm, period)
    if not tr_s or not plus_s or not minus_s:
        return 0.0

    n = min(len(tr_s), len(plus_s), len(minus_s))
    dx_values = []
    for i in range(n):
        tr_v = tr_s[i] or 1e-9
        plus_di = 100 * (plus_s[i] / tr_v)
        minus_di = 100 * (minus_s[i] / tr_v)
        denom = (plus_di + minus_di) or 1e-9
        dx = 100 * abs(plus_di - minus_di) / denom
        dx_values.append(dx)

    if not dx_values:
        return 0.0
    tail = dx_values[-period:] if len(dx_values) >= period else dx_values
    return sum(tail) / len(tail)


def calc_volume_profile(candles, bins=20, value_area_pct=0.70):
    """
    Volume Profile — يوزّع حجم التداول على مستويات سعرية داخل آخر شموع.
    يرجع POC (Point of Control — أكبر تركّز حجم)، ومنطقة القيمة كاملة:
    VAH (Value Area High) و VAL (Value Area Low) — الحدود اللي تحوي
    70% من الحجم حول الـ POC (نفس منطق Volume Profile الاحترافي).
    """
    if len(candles) < 5:
        return None
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    if hi <= lo:
        return None
    bin_size = (hi - lo) / bins
    volumes = [0.0] * bins

    for c in candles:
        mid = (c["high"] + c["low"]) / 2
        idx = int((mid - lo) / bin_size)
        idx = max(0, min(bins - 1, idx))
        volumes[idx] += c["volume"]

    total_volume = sum(volumes) or 1e-9
    poc_idx = volumes.index(max(volumes))
    poc_price = lo + (poc_idx + 0.5) * bin_size

    # بناء منطقة القيمة: نوسّع من الـ POC للخارج (يمين/يسار) نختار الأكبر حجم
    # في كل خطوة، حتى نوصل لنسبة value_area_pct من الحجم الكلي.
    included = {poc_idx}
    acc_volume = volumes[poc_idx]
    low_i, high_i = poc_idx, poc_idx
    while acc_volume < total_volume * value_area_pct and (low_i > 0 or high_i < bins - 1):
        vol_below = volumes[low_i - 1] if low_i > 0 else -1
        vol_above = volumes[high_i + 1] if high_i < bins - 1 else -1
        if vol_above >= vol_below:
            high_i += 1
            acc_volume += volumes[high_i]
        else:
            low_i -= 1
            acc_volume += volumes[low_i]
        included.add(low_i)
        included.add(high_i)

    val_price = lo + low_i * bin_size
    vah_price = lo + (high_i + 1) * bin_size

    return {
        "poc": poc_price, "vah": vah_price, "val": val_price,
        "range_low": lo, "range_high": hi,
    }


def calc_order_flow_pressure(candles, lookback=14):
    """
    تقريب لـ Order Flow بدون بيانات Tick: يحسب أين أغلق السعر داخل مدى كل شمعة
    (قرب القمة = ضغط شراء، قرب القاع = ضغط بيع) مرجّح بالحجم — نفس مبدأ
    Chaikin Money Flow. يرجع قيمة بين -1 (ضغط بيع قوي) و +1 (ضغط شراء قوي).
    """
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    total_vol = sum(c["volume"] for c in recent)
    if total_vol == 0:
        return 0.0
    flow_sum = 0.0
    for c in recent:
        full_range = c["high"] - c["low"]
        if full_range == 0:
            continue
        mfm = ((c["close"] - c["low"]) - (c["high"] - c["close"])) / full_range
        flow_sum += mfm * c["volume"]
    return flow_sum / total_vol


def evaluate_value_area_breakout(closes, candles, atr):
    """
    اختراق منطقة القيمة (Value Area Breakout) — منقول من فكرة
    Break → Retest → Confirmation: ما يدخلش فور الاختراق (تجنب "مطاردة
    السعر")، يستنى:
    1) شمعة تخترق وتُغلق خارج VAH/VAL
    2) شمعة ترتد وتختبر الحافة (Retest) بلا ما تدخل القيمة من جديد
    3) شمعة تأكيد تكمل في نفس الاتجاه، مدعومة بـ RSI وضغط Order Flow وحجم
    """
    if not VAB_SIGNAL or len(candles) < VAB_LOOKBACK + 5:
        return None, None

    vp = calc_volume_profile(candles[-VAB_LOOKBACK:], MPE_VP_BINS)
    if vp is None:
        return None, None

    vah, val = vp["vah"], vp["val"]
    break_c, retest_c, confirm_c = candles[-3], candles[-2], candles[-1]
    rsi_now = calc_rsi(closes, RSI_PERIOD)
    flow = calc_order_flow_pressure(candles, 14)
    band = max(0.3 * atr, 0.05)

    # BUY: اختراق فوق VAH + Retest + تأكيد استمرار
    if (break_c["close"] > vah
            and retest_c["low"] <= vah + band and retest_c["close"] >= vah - band * 0.5
            and confirm_c["close"] > vah and confirm_c["close"] > break_c["close"]
            and rsi_now < 75 and flow > 0):
        if not check_volume(candles):
            return None, "Value Area Breakout شراء: حجم التداول ضعيف"
        return "buy", f"اختراق مؤكد فوق VAH ({vah:,.2f}) بعد Retest وتأكيد استمرار"

    # SELL: كسر تحت VAL + Retest + تأكيد استمرار
    if (break_c["close"] < val
            and retest_c["high"] >= val - band and retest_c["close"] <= val + band * 0.5
            and confirm_c["close"] < val and confirm_c["close"] < break_c["close"]
            and rsi_now > 25 and flow < 0):
        if not check_volume(candles):
            return None, "Value Area Breakdown بيع: حجم التداول ضعيف"
        return "sell", f"كسر مؤكد تحت VAL ({val:,.2f}) بعد Retest وتأكيد استمرار"

    return None, None


def evaluate_market_prediction_signal(closes, candles, ema, atr):
    """
    Market Prediction Engine — مصدر إشارة مستقل يجمع عدة عناصر في نقاط (Score):
    اتجاه EMA200، RSI، زخم MACD، موقع السعر من Volume Profile (POC)، وضغط
    Order Flow (تقريبي). يبعث إشارة غير إذا النقاط الإجمالية وصلت حد معيّن.

    ADX هنا بوابة (Gate) وليس نقطة إضافية — سوق بلا اتجاه واضح (ADX ضعيف)
    يمنع الإشارة كاملة، بدل ما يزيد نقطة تكرر نفس اتجاه EMA200.

    شرط إضافي إجباري: الزخم القريب (EMA20) لازم يتفق مع اتجاه الإشارة —
    هذا يمنع الدخول لما يكون الاتجاه العام الطويل (EMA200) لسه قديم/متأخر
    بينما السعر بدا فعلياً ينعكس في آخر الشموع.
    """
    if not MPE_SIGNAL or len(closes) < max(MPE_LOOKBACK, 40):
        return None, None, 0

    adx = calc_adx(candles, 14)
    if adx < 18:
        return None, None, 0  # سوق بلا اتجاه واضح — المحرك ما يحسبش أصلاً

    rsi_now = calc_rsi(closes, RSI_PERIOD)
    flow = calc_order_flow_pressure(candles, MPE_LOOKBACK)
    vp = calc_volume_profile(candles[-MPE_LOOKBACK:], MPE_VP_BINS)
    price = closes[-1]
    ema20 = calc_ema(closes, 20)

    macd_line, signal_line = calc_macd_series(closes)
    macd_bullish = False
    macd_bearish = False
    if macd_line and signal_line and len(macd_line) >= len(signal_line):
        hist = [m - sgn for m, sgn in zip(macd_line[-len(signal_line):], signal_line)]
        if len(hist) >= 2:
            macd_bullish = hist[-1] > hist[-2] and hist[-1] > 0
            macd_bearish = hist[-1] < hist[-2] and hist[-1] < 0

    bull_score = 0
    bear_score = 0

    # 1) اتجاه EMA200
    if price > ema:
        bull_score += 1
    else:
        bear_score += 1

    # 2) RSI (منطقة صحية، بعيدة عن التشبع)
    if 50 <= rsi_now < 70:
        bull_score += 1
    elif 30 < rsi_now <= 50:
        bear_score += 1

    # 3) زخم MACD
    if macd_bullish:
        bull_score += 1
    if macd_bearish:
        bear_score += 1

    # 4) ضغط Order Flow (تقريبي)
    if flow > 0.15:
        bull_score += 1
    elif flow < -0.15:
        bear_score += 1

    # 5) موقع السعر من Volume Profile POC
    if vp is not None:
        if price > vp["poc"]:
            bull_score += 1
        elif price < vp["poc"]:
            bear_score += 1

    threshold = min(MPE_SCORE_THRESHOLD, 5)

    if bull_score >= threshold and bull_score > bear_score:
        if price < ema20:
            return None, None, bull_score  # الزخم القريب عكس الإشارة — رفض
        return "buy", f"Market Prediction: نقاط شراء {bull_score}/5 (ADX={adx:.0f}, Flow={flow:+.2f})", bull_score
    if bear_score >= threshold and bear_score > bull_score:
        if price > ema20:
            return None, None, bear_score  # الزخم القريب عكس الإشارة — رفض
        return "sell", f"Market Prediction: نقاط بيع {bear_score}/5 (ADX={adx:.0f}, Flow={flow:+.2f})", bear_score

    return None, None, max(bull_score, bear_score)


def check_macd_momentum(closes, direction):
    """MACD لازم يبدا يرتد في نفس اتجاه الإشارة (الهيستوغرام يتحسن مقارنة بآخر 3 شموع)."""
    macd_line, signal_line = calc_macd_series(closes)
    if not macd_line or not signal_line or len(macd_line) < 4:
        return True  # بيانات غير كافية، لا نمنع الإشارة بسبب هذا وحده
    hist = [m - s for m, s in zip(macd_line[-len(signal_line):], signal_line)]
    if len(hist) < 3:
        return True
    if direction == "buy":
        return hist[-1] > hist[-2] or hist[-1] > hist[-3]
    return hist[-1] < hist[-2] or hist[-1] < hist[-3]


def find_pivots(candles, left=3, right=3):
    """
    يكتشف القمم والقيعان المحلية (Pivots) — كل قمة/قاع لازم يكون أعلى/أدنى
    من "left" شمعة قبلو و"right" شمعة بعدو. أساس تحليل بنية السوق (ZigZag).
    """
    pivots = []
    n = len(candles)
    for i in range(left, n - right):
        window = candles[i - left:i + right + 1]
        high_i, low_i = candles[i]["high"], candles[i]["low"]
        if high_i == max(c["high"] for c in window):
            pivots.append((i, high_i, "high"))
        if low_i == min(c["low"] for c in window):
            pivots.append((i, low_i, "low"))
    return pivots


def classify_market_structure(candles, left=3, right=3):
    """
    يصنف آخر قمة وآخر قاع حسب بنية السوق:
    HH (قمة أعلى) / LH (قمة أقل) — HL (قاع أعلى) / LL (قاع أقل).
    يرجع dict: {"last_high": "HH"/"LH"/None, "last_low": "HL"/"LL"/None}
    """
    pivots = find_pivots(candles, left, right)
    highs = [p for p in pivots if p[2] == "high"]
    lows = [p for p in pivots if p[2] == "low"]

    result = {"last_high": None, "last_low": None}
    if len(highs) >= 2:
        result["last_high"] = "HH" if highs[-1][1] > highs[-2][1] else "LH"
    if len(lows) >= 2:
        result["last_low"] = "HL" if lows[-1][1] > lows[-2][1] else "LL"
    return result


def evaluate_support_resistance_signal(candles, atr, ema):
    """
    مصدر إشارة مستقل — فلتر واحد بس (بلا أي فلتر تأكيد إضافي):
    كي يوصل السعر لمستوى مقاومة (أعلى قمة في آخر N شمعة) → بيع.
    كي يوصل السعر لمستوى دعم (أدنى قاع في آخر N شمعة) → شراء.
    يعتمد على الشموع السابقة فقط (بلا انتظار شموع لاحقة للتأكيد)،
    فالإشارة تنطلق فوراً من أول شمعة تلامس المستوى، بلا أي تأخير.
    """
    if not SR_SIGNAL or len(candles) < SR_PIVOT_LENGTH + 5:
        return None, None

    window = candles[-1 - SR_PIVOT_LENGTH:-1]  # آخر N شمعة، بلا الشمعة الحالية
    resistance = max(c["high"] for c in window)
    support = min(c["low"] for c in window)
    price = candles[-1]["close"]
    tol = SR_TOLERANCE_ATR * atr

    closes_all = [c["close"] for c in candles]
    rsi_now = calc_rsi(closes_all, RSI_PERIOD)
    # EMA200 بطيء جداً (50+ ساعة) وممكن يبقى يقول "هابط" حتى بعد ما السعر
    # يرتد فعلياً ويبدا صعود قوي قريب. EMA20 يمسك الزخم القريب فيمنع هذا الفخ.
    ema20 = calc_ema(closes_all, 20)

    trend_up_long = price > ema
    trend_up_short = price > ema20

    if abs(price - resistance) <= tol:
        if rsi_now < 45:
            return None, f"عند المقاومة لكن RSI منخفض ({rsi_now:.1f}) — احتمال ترند صاعد قوي، البيع خطر"
        if trend_up_long or trend_up_short:
            return None, "عند المقاومة لكن الاتجاه (طويل أو قريب) صاعد — البيع عكس الترند، مرفوض"
        return "sell", f"السعر عند مستوى المقاومة ({resistance:,.2f}) — مع الترند الهابط (طويل وقريب)"
    if abs(price - support) <= tol:
        if rsi_now > 55:
            return None, f"عند الدعم لكن RSI مرتفع ({rsi_now:.1f}) — احتمال ترند هابط قوي، الشراء خطر"
        if not trend_up_long or not trend_up_short:
            return None, "عند الدعم لكن الاتجاه (طويل أو قريب) هابط — الشراء عكس الترند، مرفوض"
        return "buy", f"السعر عند مستوى الدعم ({support:,.2f}) — مع الترند الصاعد (طويل وقريب)"

    return None, None


def evaluate_reversal_signal(closes, candles, ema, atr, symbol=None):
    """
    إشارة عكس الترند (مخاطرة أعلى): كي يكون السعر تحت EMA200 ويطلع من منطقة
    مباع بزيادة، تعطي شراء (والعكس). شروط صارمة: RSI كان متطرف ثم ارتد،
    السعر تجاوز EMA20، وشمعة تأكيد قوية في اتجاه الصفقة.
    """
    if not ALLOW_COUNTER_TREND or len(closes) < 40:
        return None, None

    trend_direction = "buy" if closes[-1] > ema else "sell"
    direction = "sell" if trend_direction == "buy" else "buy"

    ema20 = calc_ema(closes, 20)
    rsi_now = calc_rsi(closes, RSI_PERIOD)
    rsi_prev = calc_rsi(closes[:-1], RSI_PERIOD)
    rsis = [calc_rsi(closes[:len(closes) - k], RSI_PERIOD) for k in range(0, 12)]
    last = candles[-1]

    if direction == "buy":
        if min(rsis) > 30:
            return None, None
        if not (35 <= rsi_now <= 65 and rsi_now > rsi_prev):
            return None, "Reversal شراء: ننتظر RSI يرتد فوق 35"
        if closes[-1] <= ema20:
            return None, "Reversal شراء: ننتظر السعر يتجاوز EMA20"
    else:
        if max(rsis) < 70:
            return None, None
        if not (35 <= rsi_now <= 65 and rsi_now < rsi_prev):
            return None, "Reversal بيع: ننتظر RSI ينزل تحت 65"
        if closes[-1] >= ema20:
            return None, "Reversal بيع: ننتظر السعر يكسر EMA20"

    if not check_body_wick(last, direction):
        return None, "Reversal: شمعة التأكيد ضعيفة"
    if not check_macd_momentum(closes, direction):
        return None, "Reversal: MACD ما يدعمش الارتداد بعد"
    if not check_volume(candles, mult=1.0):
        return None, "Reversal: حجم التداول ضعيف"
    if symbol is not None and not check_higher_timeframe(symbol, direction):
        return None, "Reversal: الفريم الأعلى (1h) ما يدعمش الاتجاه"

    structure = classify_market_structure(candles)
    if direction == "buy" and structure["last_low"] == "LL":
        return None, "Reversal: بنية السوق ما زالت LL (قاع أقل) — الهبوط لسه قوي"
    if direction == "sell" and structure["last_high"] == "HH":
        return None, "Reversal: بنية السوق ما زالت HH (قمة أعلى) — الصعود لسه قوي"

    return direction, "ارتداد عكس الترند بعد تطرف RSI (مؤكد بـ RSI+MACD+حجم+فريم أعلى+بنية السوق)"



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

    src = open_signal.get("source", "breakout")
    stats = symbol_state.setdefault("source_stats", {}).setdefault(src, {"wins": 0, "losses": 0})

    if hit_tp:
        symbol_state["consecutive_losses"] = 0
        symbol_state["open_signal"] = None
        stats["wins"] += 1
        log(f"✅ آخر إشارة {symbol_state.get('label','')} لامست TP1 — تصفير عداد الخسائر")
    elif hit_sl:
        symbol_state["consecutive_losses"] = symbol_state.get("consecutive_losses", 0) + 1
        symbol_state["open_signal"] = None
        stats["losses"] += 1
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
    elif source == "vab":
        title = "اختراق منطقة القيمة 📊 (Value Area Breakout)"
        filters_line = "الشروط: اختراق مغلق خارج VAH/VAL ✅ | Retest موثق ✅ | تأكيد استمرار ✅ | RSI ✅ | Order Flow ✅ | حجم ✅"
    elif source == "mpe":
        title = f"Market Prediction Engine 🔮 ({reason})" if reason else "Market Prediction Engine 🔮"
        filters_line = (
            "المحرك يجمع: اتجاه EMA200 + RSI + زخم MACD + Volume Profile (POC) + "
            "Order Flow تقريبي في نظام نقاط ✅ | بوابة ADX (قوة اتجاه كافية) ✅ | "
            "الزخم القريب (EMA20) متفق مع الإشارة ✅"
        )
    elif source == "sr":
        if direction == "sell":
            icon = "🔻"
            title = "Resistance بيع 🔻"
        else:
            icon = "⬆️"
            title = "Support شراء ⬆️"
        filters_line = "الفلتر: السعر عند مستوى دعم/مقاومة محلي (فريم 15 دقيقة) + RSI + اتجاه EMA200 (طويل) + EMA20 (قريب) — الاثنين لازم يتفقو ⚠️"
    elif source == "reversal":
        title = "ارتداد عكس الترند ⚠️ (مخاطرة أعلى)"
        filters_line = "الشروط: RSI ارتد ✅ | MACD ✅ | حجم ✅ | فريم أعلى ✅ | بنية السوق (HH/HL/LH/LL) ✅ | تجاوز EMA20 ✅ | شمعة تأكيد ✅ | ⚠️ عكس اتجاه EMA200"
    elif source == "pullback":
        title = "ارتداد مع الترند (Pullback)"
        filters_line = "الفلاتر المجتازة: اتجاه EMA200 ✅ | RSI متطرف ثم ارتد ✅ | قرب EMA20 ✅ | شمعة تأكيد ✅"
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


SOURCE_LABELS_AR = {
    "breakout": "اختراق", "zlr": "ZLR", "sr": "دعم/مقاومة", "mpe": "Market Prediction",
    "vab": "اختراق منطقة القيمة", "pullback": "Pullback", "reversal": "Reversal",
}


def format_accuracy_summary(source_stats):
    lines = []
    for src, st in source_stats.items():
        total = st.get("wins", 0) + st.get("losses", 0)
        if total < 3:
            continue
        pct = round(st["wins"] / total * 100)
        name = SOURCE_LABELS_AR.get(src, src)
        lines.append(f"{name}: {pct}% ({st['wins']}/{total})")
    if not lines:
        return ""
    return "📊 دقة المصادر حتى الآن: " + " | ".join(lines) + "\n\n"


def format_hourly_update(label, price, ema, rsi, trend_word, source_stats=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    accuracy_line = format_accuracy_summary(source_stats) if source_stats else ""
    return (
        f"🕐 <b>تحديث دوري (بدون توصية دخول)</b>\n"
        f"الرمز: {label}\n"
        f"التاريخ: {ts}\n\n"
        f"السعر الحالي: {price:,.2f}\n"
        f"الاتجاه العام (EMA{EMA_PERIOD}): {trend_word}\n"
        f"RSI: {rsi:.1f}\n\n"
        f"{accuracy_line}"
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

    def send_signal(direction, source, swing=None, reason=""):
        if direction == "buy":
            sl = current_price - atr * ATR_SL_MULT
            tp1 = current_price + atr * ATR_TP1_MULT
            tp2 = current_price + atr * ATR_TP2_MULT
            if swing is not None:
                sl = swing - 0.2 * atr
                sl = min(sl, current_price - 0.5 * atr)
                sl = max(sl, current_price - 2 * atr)
        else:
            sl = current_price + atr * ATR_SL_MULT
            tp1 = current_price - atr * ATR_TP1_MULT
            tp2 = current_price - atr * ATR_TP2_MULT
            if swing is not None:
                sl = swing + 0.2 * atr
                sl = max(sl, current_price + 0.5 * atr)
                sl = min(sl, current_price + 2 * atr)

        corr = None
        if other_closes is not None:
            corr = check_correlation(closes, other_closes, direction)

        notify(format_strong_signal(label, direction, current_price, sl, tp1, tp2, atr, rsi, corr, source=source, reason=reason))

        symbol_state["open_signal"] = {
            "direction": direction, "entry": current_price,
            "sl": sl, "tp1": tp1, "tp2": tp2, "source": source,
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

    # مصدر مستقل: الدعم/المقاومة (فلتر واحد فقط، بلا أي تأكيد إضافي)
    sr_recent = minutes_since((symbol_state.get("open_signal") or {}).get("opened_at"))
    if sr_recent is None or sr_recent >= 30:
        sr_direction, sr_reason = evaluate_support_resistance_signal(candles, atr, ema)
        if sr_direction is not None:
            send_signal(sr_direction, source="sr")
            return
        if sr_reason:
            log(f"{label}: لا S/R — {sr_reason}")

    # مصدر مستقل: Market Prediction Engine (نظام نقاط مركّب: EMA+RSI+MACD+ADX+Volume Profile+Order Flow)
    mpe_recent = minutes_since((symbol_state.get("open_signal") or {}).get("opened_at"))
    if mpe_recent is None or mpe_recent >= 30:
        mpe_direction, mpe_reason, mpe_score = evaluate_market_prediction_signal(closes, candles, ema, atr)
        if mpe_direction is not None:
            send_signal(mpe_direction, source="mpe", reason=f"{mpe_score}/6 نقاط")
            return
        if mpe_score:
            log(f"{label}: لا MPE — نقاط غير كافية ({mpe_score}/6)")

    # مصدر مستقل: اختراق منطقة القيمة (Value Area Breakout — Break/Retest/Confirm)
    vab_recent = minutes_since((symbol_state.get("open_signal") or {}).get("opened_at"))
    if vab_recent is None or vab_recent >= 20:
        vab_direction, vab_reason = evaluate_value_area_breakout(closes, candles, atr)
        if vab_direction is not None:
            send_signal(vab_direction, source="vab")
            return
        if vab_reason:
            log(f"{label}: لا VAB — {vab_reason}")

    # المصدر الثالث: Pullback (يعالج حالة RSI المتطرف اللي تمنع الاختراق و ZLR)
    recent = minutes_since((symbol_state.get("open_signal") or {}).get("opened_at"))
    if recent is None or recent >= 90:
        pb_direction, pb_reason = evaluate_pullback_signal(closes, candles, ema, atr)
        if pb_direction is not None:
            send_signal(pb_direction, source="pullback")
            return
        if pb_reason:
            log(f"{label}: لا Pullback — {pb_reason}")

        rv_direction, rv_reason = evaluate_reversal_signal(closes, candles, ema, atr, symbol)
        if rv_direction is not None:
            send_signal(rv_direction, source="reversal")
            return
        if rv_reason:
            log(f"{label}: لا Reversal — {rv_reason}")

    elapsed = minutes_since(symbol_state.get("last_sent"))
    if elapsed is None or elapsed >= HOURLY_UPDATE_MINUTES:
        trend_word = "صاعد (فوق EMA)" if current_price > ema else "هابط (تحت EMA)"
        notify(format_hourly_update(label, current_price, ema, rsi, trend_word, symbol_state.get("source_stats")))
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
