"""
XAU Market Microstructure Engine V21 — بوت تيليجرام مبني بالكامل على المحرك
اللي بعثه المستخدم (XAU_V21_PWA_MarketTargets.zip). هذا استبدال كامل للكود
القديم، ماشي إضافة — كل الاستراتيجيات السابقة (Breakout, Pullback, Reversal,
S/R, MPE, VAB, ZLR...) تم حذفها بالكامل كيما طُلب.

منقول من JavaScript لبايثون بأمانة قدر الإمكان، نفس المعادلات:
  - Volume Profile (POC/VAH/VAL)               → priceBins()
  - Order Flow تقريبي (Chaikin-style)           → qflow()
  - بنية السوق اللحظية (Swing/Displacement)     → immediateStructure()
  - تنبؤ 3 شموع بنظام نقاط مركّب                → forecast()
  - إجهاد وفشل الاختراق (Exhaustion/Failed)      → bullExhaust/bearExhaust...
  - مناطق سيولة مؤسساتية (قمم/قيعان متقاربة)     → institutionalLiquidity()
  - خطة صفقة من مناطق سيولة حقيقية (لا نسب ثابتة) → marketPlan()
  - آلة حالة Break → Retest → Confirm            → update_timing() (محفوظة
    بين كل تشغيلة عبر state.json بدل متغير JS في الذاكرة)

ما تم إسقاطه عمداً (بصراحة، بلا تمويه):
  - طبقة "Live Tick Shift" الأصلية (liveBearShift/liveBullShift/liveMove)
    كانت مبنية على تدفق Ticks مستمر داخل متصفح مفتوح طول الوقت. بوتنا يخدم
    دقيقة بدقيقة عبر GitHub Actions بلا اتصال مستمر، فتم تقريبها باستدعاء
    Kraken Ticker (آخر سعر تداول حقيقي لحظة التشغيل) مقارنة بآخر شمعة
    مغلقة — تقريب معقول، ماشي تدفق Ticks حي حقيقي.
  - التقويم الاقتصادي (Macro Calendar) كان Hardcoded بتواريخ 2026 محددة في
    الملف الأصلي، ميزة واجهة فقط (عرض على الشاشة)، ما تم نقلها.
  - تتبع الدقة كان عبر localStorage (متصفح)؛ استبدلناه بتتبع مشابه داخل
    state.json (لكل أفق تنبؤ +1/+2/+3).
  - مصدر البيانات: الملف الأصلي يستعمل Biquote.io (XAU/USD حقيقي). ما
    قدرناش نتأكد من توفر/استقرار هذا الـ API من سيرفرات GitHub Actions
    (بلا توثيق رسمي)، فبقينا على Kraken (PAXG/USD) كيما كان بوتنا القديم.
"""

import os
import json
import requests
from datetime import datetime, timezone

# ------------------------------------------------------------------
# الإعدادات
# ------------------------------------------------------------------
BOT_SYMBOLS = os.environ.get("BOT_SYMBOLS", "XBTUSD|BTC/USDT,PAXGUSD|GOLD (PAXG)")
INTERVAL = os.environ.get("BOT_INTERVAL", "1m")
LOOKBACK_BARS = int(os.environ.get("BOT_LOOKBACK_BARS", "80"))  # نفس n=80 الأصلي
WEEKEND_FILTER = os.environ.get("BOT_WEEKEND_FILTER", "1") == "1"
# الرموز اللي تتبع عطلة سوق الفوركس التقليدي (الذهب فقط افتراضياً — البيتكوين والكريبتو عموماً يتداولو 24/7 بلا عطلة)
WEEKEND_FILTER_SYMBOLS = {s.strip().upper() for s in os.environ.get("BOT_WEEKEND_FILTER_SYMBOLS", "PAXGUSD").split(",") if s.strip()}
# دخول سريع: يدخل عند أول اختراق مؤكد (BREAK) بلا انتظار Retest كامل، بشرط
# ضغط وتوقع قويين متفقين. أسرع، لكن خطر الدخول في اختراق وهمي أعلى قليلاً.
FAST_ENTRY = os.environ.get("BOT_FAST_ENTRY", "1") == "1"
MAX_CONSECUTIVE_LOSSES = int(os.environ.get("BOT_MAX_CONSECUTIVE_LOSSES", "3"))
CIRCUIT_BREAKER_PAUSE_HOURS = float(os.environ.get("BOT_CIRCUIT_BREAKER_PAUSE_HOURS", "4"))
HOURLY_UPDATE_MINUTES = int(os.environ.get("BOT_HOURLY_UPDATE_MINUTES", "60"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "state.json"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
KRAKEN_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


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
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
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


def is_forex_market_closed(now=None):
    """سوق الذهب الحقيقي يقفل من جمعة 21:00 UTC لحد أحد 22:00 UTC تقريباً."""
    now = now or datetime.now(timezone.utc)
    weekday = now.weekday()
    if weekday == 4 and now.hour >= 21:
        return True
    if weekday == 5:
        return True
    if weekday == 6 and now.hour < 22:
        return True
    return False


# ------------------------------------------------------------------
# جلب البيانات من Kraken
# ------------------------------------------------------------------
def get_klines(pair, interval, limit=300):
    minutes = KRAKEN_INTERVAL_MINUTES.get(interval)
    if minutes is None:
        raise ValueError(f"فترة زمنية غير مدعومة: {interval}")
    resp = requests.get(KRAKEN_OHLC_URL, params={"pair": pair, "interval": minutes}, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    if raw.get("error"):
        raise RuntimeError("; ".join(raw["error"]) or "خطأ من Kraken")
    result = raw.get("result", {})
    ohlc_key = next((k for k in result if k != "last"), None)
    if not ohlc_key:
        raise RuntimeError(f"لا توجد بيانات: {pair}")
    candles = [
        {
            "open_time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]), "volume": float(row[6]),
        }
        for row in result[ohlc_key]
    ]
    return candles[-limit:] if limit else candles


def get_ticker_price(pair):
    """آخر سعر تداول حقيقي لحظة التشغيل — تقريبنا لـ 'px' (السعر الحي) في الملف الأصلي."""
    try:
        resp = requests.get(KRAKEN_TICKER_URL, params={"pair": pair}, timeout=10)
        resp.raise_for_status()
        raw = resp.json()
        if raw.get("error"):
            return None
        result = raw.get("result", {})
        key = next(iter(result), None)
        if not key:
            return None
        return float(result[key]["c"][0])
    except Exception as e:
        log(f"تعذر جلب سعر Ticker: {e}")
        return None


# ------------------------------------------------------------------
# أدوات رياضية أساسية (منقولة حرفياً من الملف الأصلي)
# ------------------------------------------------------------------
def mean(a):
    return sum(a) / len(a) if a else 0.0


def ema(values, period):
    if not values:
        return 0.0
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i]["high"] - candles[i]["low"],
            abs(candles[i]["high"] - candles[i - 1]["close"]),
            abs(candles[i]["low"] - candles[i - 1]["close"]),
        )
        trs.append(tr)
    return mean(trs[-period:])


def slope(values, n=8):
    if len(values) < n:
        return 0.0
    x = values[-n:]
    mx = (n - 1) / 2
    my = mean(x)
    num = sum((i - mx) * (x[i] - my) for i in range(n))
    den = sum((i - mx) ** 2 for i in range(n))
    return num / den if den else 0.0


def qflow(candles):
    """ضغط تدفق أوامر تقريبي (Chaikin-style) — نفس معادلة qflow() الأصلية."""
    den = num = 0.0
    for c in candles[-20:]:
        r = max(c["high"] - c["low"], 1e-9)
        body = (c["close"] - c["open"]) / r
        w = c.get("volume", 1) or 1
        num += body * w
        den += w
    return max(-100.0, min(100.0, 100 * num / den)) if den else 0.0


def price_bins(candles):
    """Volume Profile: POC + VAH + VAL — نفس معادلة priceBins() الأصلية (40 بن، 70% منطقة قيمة)."""
    lo = min(c["low"] for c in candles)
    hi = max(c["high"] for c in candles)
    bins = 40
    step = (hi - lo) / bins or 0.1
    v = [0.0] * bins
    for c in candles:
        p = (c["high"] + c["low"] + c["close"]) / 3
        idx = max(0, min(bins - 1, int((p - lo) / step)))
        v[idx] += c.get("volume", 1) or 1
    poc_idx = v.index(max(v))
    poc = lo + (poc_idx + 0.5) * step
    total = sum(v)
    target = total * 0.70
    order = sorted(range(bins), key=lambda i: -v[i])
    acc, sel = 0.0, []
    for i in order:
        if acc >= target:
            break
        acc += v[i]
        sel.append(i)
    val = lo + min(sel) * step
    vah = lo + (max(sel) + 1) * step
    return {"lo": lo, "hi": hi, "step": step, "poc": poc, "val": val, "vah": vah}


def wick_info(c):
    r = max(c["high"] - c["low"], 1e-9)
    body = abs(c["close"] - c["open"])
    upper = c["high"] - max(c["open"], c["close"])
    lower = min(c["open"], c["close"]) - c["low"]
    return {"r": r, "body": body, "upper": upper, "lower": lower,
            "upper_pct": upper / r, "lower_pct": lower / r, "body_pct": body / r}


def flow_divergence(candles):
    c = [x["close"] for x in candles]
    n = min(6, len(c))
    if n < 6:
        return {"bull": False, "bear": False}
    pc = c[-1] - c[-n]
    f1 = qflow(candles[-n:])
    f2 = qflow(candles[-min(12, len(candles)):-n]) if len(candles) > n else 0.0
    return {"bull": pc > 0 and f1 < f2 - 8, "bear": pc < 0 and f1 > f2 + 8}


def calc_efficiency(candles, lookback=20):
    """
    نسبة الكفاءة الاتجاهية (Efficiency Ratio) — منقولة من مؤشر MST:
    صافي الحركة (بداية-نهاية) ÷ مجموع الحركة الكلي (كل الخطوات). قريبة من
    1 = سعر يمشي بخط نظيف (ترند حقيقي)، قريبة من 0 = تذبذب بلا هدف واضح.
    """
    if len(candles) < lookback + 1:
        return 0.0
    closes = [c["close"] for c in candles]
    net_move = abs(closes[-1] - closes[-1 - lookback])
    total_move = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - lookback, len(closes)))
    return net_move / total_move if total_move > 0 else 0.0


def detect_liquidity_sweep(candles, sweep_len=10):
    """
    سحب السيولة (Liquidity Sweep) — منقول من مؤشر MST: السعر يكسر قمة/قاع
    أخير بسرعة (يصطاد أوامر الوقف) ثم يرجع يغلق داخل المدى القديم — نمط
    انعكاس كلاسيكي، مختلف عن مناطق السيولة الثابتة (Swing clusters).
    """
    if len(candles) < sweep_len + 2:
        return {"buy_sweep": False, "sell_sweep": False}
    last = candles[-1]
    window = candles[-1 - sweep_len:-1]
    prev_high = max(c["high"] for c in window)
    prev_low = min(c["low"] for c in window)
    buy_sweep = last["high"] > prev_high and last["close"] < prev_high
    sell_sweep = last["low"] < prev_low and last["close"] > prev_low
    return {"buy_sweep": buy_sweep, "sell_sweep": sell_sweep}


def detect_absorption(candles, lookback=20):
    """
    الامتصاص (Absorption) — منقول من مؤشر MST: حجم تداول كبير (>1.4x
    المتوسط) بجسم شمعة صغير (<35% من المدى) — يعني بيع/شراء كبير حصل بلا
    ما يحرك السعر فعلياً، إشارة إجهاد/استيعاب كلاسيكية.
    """
    if len(candles) < lookback + 1:
        return False
    last = candles[-1]
    volumes = [c.get("volume", 1) for c in candles[-1 - lookback:-1]]
    avg_vol = mean(volumes) if volumes else 0
    rel_vol = (last.get("volume", 1) / avg_vol) if avg_vol > 0 else 1
    rng = max(last["high"] - last["low"], 1e-9)
    body_ratio = abs(last["close"] - last["open"]) / rng
    return rel_vol > 1.4 and body_ratio < 0.35


def momentum_state(candles):
    c = [x["close"] for x in candles]
    a = atr(candles, 14) or 1
    if len(c) < 10:
        return {"slow": False, "accel": 0.0}
    s1 = slope(c[-5:], 5) / a
    s2 = slope(c[-10:-5], 5) / a
    slow = (s1 > 0 and s2 > 0 and s1 < s2 * 0.55) or (s1 < 0 and s2 < 0 and s1 > s2 * 0.55)
    return {"slow": slow, "accel": s1 - s2}


def range_state(candles, vp, a):
    z = candles[-6:]
    if len(z) < 5:
        return {"consolidating": False, "above": False, "below": False, "range": 0, "expansion": False}
    highs = [x["high"] for x in z]
    lows = [x["low"] for x in z]
    rng = max(highs) - min(lows)
    bodies = [abs(x["close"] - x["open"]) for x in z]
    avg_body = mean(bodies)
    avg_range = mean([x["high"] - x["low"] for x in z])
    small = rng <= max(a * 1.35, 0.8) and avg_body <= max(a * 0.42, 0.25) and avg_range <= max(a * 0.75, 0.45)
    last = z[-1]
    above = last["close"] > vp["vah"]
    below = last["close"] < vp["val"]
    expansion = (last["high"] - last["low"]) > max(avg_range * 1.25, a * 0.75)
    return {"consolidating": small, "above": above, "below": below, "range": rng, "expansion": expansion}


def rejection_at(candles, level, side):
    z = candles[-1]
    w = wick_info(z)
    if side == "upper":
        return z["high"] >= level and w["upper_pct"] >= 0.38 and z["close"] < level + w["r"] * 0.15
    return z["low"] <= level and w["lower_pct"] >= 0.38 and z["close"] > level - w["r"] * 0.15


def immediate_structure(candles, a):
    """تحول بنية لحظي (HH/HL أو LH/LL أو كسر Swing) — نفس immediateStructure() الأصلية."""
    if len(candles) < 5:
        return {"bull": False, "bear": False, "level": None, "score": 0, "reason": "بيانات قصيرة"}
    z = candles[-5:]
    h = [x["high"] for x in z]
    l = [x["low"] for x in z]
    c = [x["close"] for x in z]
    bear_seq = h[2] < h[1] and h[3] <= h[2] and l[2] < l[1] and l[3] <= l[2]
    bull_seq = l[2] > l[1] and l[3] >= l[2] and h[2] > h[1] and h[3] >= h[2]
    prior_low = min(l[0], l[1], l[2])
    prior_high = max(h[0], h[1], h[2])
    break_bear = z[4]["close"] < prior_low or (z[3]["close"] < prior_low and z[4]["close"] <= z[3]["close"])
    break_bull = z[4]["close"] > prior_high or (z[3]["close"] > prior_high and z[4]["close"] >= z[3]["close"])
    body4, body3 = wick_info(z[4]), wick_info(z[3])
    disp_bear = z[4]["close"] < z[4]["open"] and body4["body_pct"] >= 0.55 and (z[4]["high"] - z[4]["low"]) >= a * 0.75
    disp_bull = z[4]["close"] > z[4]["open"] and body4["body_pct"] >= 0.55 and (z[4]["high"] - z[4]["low"]) >= a * 0.75
    last3, last2 = c[4] - c[2], c[4] - c[3]
    accel_bear = last3 < -a * 0.45 and last2 < 0
    accel_bull = last3 > a * 0.45 and last2 > 0
    bear = break_bear or ((bear_seq or accel_bear) and z[4]["close"] < z[4]["open"])
    bull = break_bull or ((bull_seq or accel_bull) and z[4]["close"] > z[4]["open"])
    score = (
        (2 if break_bear else 1) + (1 if disp_bear else 0) + (1 if accel_bear else 0) if bear
        else (2 if break_bull else 1) + (1 if disp_bull else 0) + (1 if accel_bull else 0) if bull else 0
    )
    return {
        "bull": bull and not bear, "bear": bear and not bull,
        "level": prior_low if bear else prior_high if bull else None,
        "score": score, "disp_bull": disp_bull, "disp_bear": disp_bear,
        "reason": "LH/LL أو كسر Swing هابط" if bear else "HH/HL أو كسر Swing صاعد" if bull else "لا تحول لحظي مؤكد",
    }


def forecast(candles, h):
    """تنبؤ الأفق h (1/2/3 شموع قدام) بنظام نقاط مركّب — نفس forecast() الأصلية."""
    c = [x["close"] for x in candles]
    last = c[-1]
    a = atr(candles, 14) or 1
    if len(c) < 20:
        return {"dir": "NEUTRAL", "score": 0.0, "price": last, "move": 0.0, "confidence": 0.0, "regime": "INSUFFICIENT"}

    e8, e20, e50 = ema(c, 8), ema(c, 20), ema(c, 50)
    fl = qflow(candles) / 100
    sl = slope(c, min(12, len(c))) / a
    mom6 = (last - c[-7]) / a
    mom3 = (last - c[-4]) / a
    lc, prev = candles[-1], candles[-2]
    w = wick_info(lc)
    rng = max(lc["high"] - lc["low"], 1e-9)
    body = (lc["close"] - lc["open"]) / rng
    prev_range = max(prev["high"] - prev["low"], 1e-9)
    range_exp = max(0.45, min(2.2, rng / max(prev_range, a * 0.55)))
    trend20 = (e20 - e50) / a
    short_trend = (e8 - e20) / a
    vp = price_bins(candles)
    vp_span = max(vp["vah"] - vp["val"], a)
    vp_pos = max(-1.0, min(1.0, (last - vp["poc"]) / vp_span))
    close_pos = ((last - lc["low"]) / rng) * 2 - 1
    seq5 = sum((1 if c[-5 + i] > c[-5 + i - 1] else (-1 if c[-5 + i] < c[-5 + i - 1] else 0)) for i in range(1, 5)) / 4

    def tanh(x):
        import math
        return math.tanh(x)

    slope_n, mom_n, mom3_n = tanh(sl * 0.75), tanh(mom6 * 0.58), tanh(mom3 * 0.72)
    trend_n, short_n = tanh(trend20 * 0.72), tanh(short_trend * 0.72)
    flow_n = max(-1.0, min(1.0, fl))
    body_n = max(-1.0, min(1.0, body))
    seq_n = max(-1.0, min(1.0, seq5))
    value_n = max(-1.0, min(1.0, -vp_pos * 0.34))
    close_n = max(-1.0, min(1.0, close_pos * 0.22))
    rejection = (0.24 if w["lower_pct"] > 0.45 else 0) + (-0.24 if w["upper_pct"] > 0.45 else 0)

    import math
    sign_src = body_n or mom3_n or short_n or trend_n or seq_n or 1
    expansion_dir = math.copysign(1, sign_src) if sign_src else 1
    expansion = (0.09 * expansion_dir if range_exp > 1.18 else -0.04 * expansion_dir if range_exp < 0.70 else 0)

    raw_base = (slope_n * .13 + mom_n * .20 + mom3_n * .12 + trend_n * .15 + short_n * .11 +
                flow_n * .12 + body_n * .08 + seq_n * .08 + value_n * .06 + close_n * .04 +
                rejection + expansion)

    disagreement = abs(short_n - trend_n)
    if abs(trend_n) > 0.28 and (1 if (short_n or trend_n) >= 0 else -1) == (1 if trend_n >= 0 else -1):
        regime = "TRENDING"
    elif disagreement > 0.75:
        regime = "TRANSITION"
    elif range_exp < 0.72 and abs(raw_base) < 0.45:
        regime = "COMPRESSION"
    elif range_exp > 1.25 and abs(raw_base) > 0.30:
        regime = "EXPANSION"
    else:
        regime = "BALANCED"

    raw = raw_base
    if regime == "TRANSITION":
        raw *= 0.78
    if regime == "COMPRESSION":
        raw *= 0.72
    if regime == "TRENDING" and (raw >= 0) == (trend_n >= 0):
        raw *= 1.08
    raw *= 1 if h == 1 else 0.88 if h == 2 else 0.76

    threshold = 0.14 if h == 1 else 0.18 if h == 2 else 0.22
    direction = "BULLISH" if raw > threshold else "BEARISH" if raw < -threshold else "NEUTRAL"

    features = [slope_n, mom_n, mom3_n, trend_n, short_n, flow_n, body_n, seq_n, value_n]
    feat_mean = mean(features)
    variance = mean([(v - feat_mean) ** 2 for v in features])
    agreement = max(0.0, 1 - variance ** 0.5)
    strength = min(1.0, abs(raw_base) * 1.65)
    confidence = 47 + strength * 35 + agreement * 13 - (h - 1) * 6
    if regime == "TRANSITION":
        confidence -= 9
    if regime == "COMPRESSION":
        confidence -= 7
    if direction == "NEUTRAL":
        confidence -= 12
    if abs(body_n) > 0.55 and (body_n >= 0) == (raw_base >= 0):
        confidence += 3
    confidence = max(18.0, min(90.0, confidence))

    directional = max(0.18, min(1.0, abs(raw) * 1.35))
    mult = 0.44 if h == 1 else 0.58 if h == 2 else 0.70
    move = a * (mult * (0.70 + directional * 0.58))
    if direction == "NEUTRAL":
        move = a * mult * 0.34
    signed = 1 if direction == "BULLISH" else -1 if direction == "BEARISH" else 0

    return {"dir": direction, "score": raw, "price": last + signed * move, "move": move,
            "confidence": confidence, "regime": regime}


# ------------------------------------------------------------------
# مناطق السيولة المؤسساتية + خطة الصفقة (منقولة من institutionalLiquidity/marketPlan)
# ------------------------------------------------------------------
def institutional_liquidity(candles, vp, a, px):
    if len(candles) < 10:
        return {"zones": [], "summary": "بيانات غير كافية."}
    n = min(100, len(candles))
    w = candles[-n:]
    tol = max(0.18, a * 0.22)
    raw = []

    def add(price, type_, weight, why):
        if price is not None:
            raw.append({"price": price, "type": type_, "weight": weight, "why": why})

    add(vp["poc"], "HVN / POC", 7, "أعلى عقدة حجم تقريبية")
    add(vp["vah"], "VAH", 5, "حافة قيمة علوية")
    add(vp["val"], "VAL", 5, "حافة قيمة سفلية")

    swing_highs, swing_lows = [], []
    for i in range(2, len(w) - 2):
        z = w[i]
        if z["high"] >= w[i - 1]["high"] and z["high"] >= w[i - 2]["high"] and z["high"] >= w[i + 1]["high"] and z["high"] >= w[i + 2]["high"]:
            swing_highs.append(z["high"])
            add(z["high"], "Swing High", 3, "قمة محلية قد تتجمع فوقها السيولة")
        if z["low"] <= w[i - 1]["low"] and z["low"] <= w[i - 2]["low"] and z["low"] <= w[i + 1]["low"] and z["low"] <= w[i + 2]["low"]:
            swing_lows.append(z["low"])
            add(z["low"], "Swing Low", 3, "قاع محلي قد تتجمع تحته السيولة")

    def cluster_levels(items, type_, why):
        items_sorted = sorted(items)
        clusters = []
        for price in items_sorted:
            match = next((cl for cl in clusters if abs(cl["center"] - price) <= tol), None)
            if match is None:
                clusters.append({"prices": [price], "center": price})
            else:
                match["prices"].append(price)
                match["center"] = mean(match["prices"])
        for cl in clusters:
            if len(cl["prices"]) >= 2:
                strength = min(7.0, 4 + (len(cl["prices"]) - 2) * 0.75)
                add(cl["center"], type_, strength, f"{why} ({len(cl['prices'])} نقاط متقاربة)")

    cluster_levels(swing_highs, "Equal Highs", "قمم متقاربة — سيولة محتملة فوقها")
    cluster_levels(swing_lows, "Equal Lows", "قيعان متقاربة — سيولة محتملة تحتها")

    zones = []
    for r in raw:
        match = next((z for z in zones if abs(z["price"] - r["price"]) <= tol), None)
        if match is None:
            zones.append({"price": r["price"], "score": r["weight"], "types": [r["type"]], "why": [r["why"]]})
        else:
            match["price"] = (match["price"] + r["price"]) / 2
            match["score"] += r["weight"]
            if r["type"] not in match["types"]:
                match["types"].append(r["type"])
            if r["why"] not in match["why"]:
                match["why"].append(r["why"])

    for z in zones:
        z["distance"] = abs(z["price"] - px)
        z["score"] = min(20.0, z["score"] + max(0.0, 3 - z["distance"] / max(a, 0.01)))
        z["types"] = z["types"][:4]
        z["why"] = " • ".join(z["why"][:2])

    zones.sort(key=lambda z: -z["score"])
    top = zones[:6]
    summary = (
        "أقوى مناطق السيولة: " + " • ".join(f"{z['price']:,.2f}" for z in top[:3]) +
        f" • السعر الحالي {px:,.2f} • ATR {a:.2f}"
    ) if top else "لا توجد مناطق سيولة كافية."
    return {"zones": top, "summary": summary}


def market_plan(a, px, decision, liquidity):
    if decision not in ("BUY", "SELL"):
        return None
    zones = liquidity.get("zones", [])
    min_gap = max(a * 0.32, 0.35)
    side = 1 if decision == "BUY" else -1
    directional = [z for z in zones if (z["price"] > px + min_gap if side > 0 else z["price"] < px - min_gap)]
    directional.sort(key=lambda z: z["price"] if side > 0 else -z["price"])
    chosen = []
    for z in directional:
        if not any(abs(c["price"] - z["price"]) < max(a * 0.28, 0.30) for c in chosen):
            chosen.append(z)
        if len(chosen) >= 3:
            break
    fallback = [a * 0.75, a * 1.35, a * 2.05]
    for i in range(3):
        if i >= len(chosen):
            price = px + side * fallback[i]
            if i > 0:
                prev_dist = abs(chosen[i - 1]["price"] - px) if i - 1 < len(chosen) else 0
                price = (max(abs(price - px), prev_dist + a * 0.30)) * side + px
            chosen.append({"price": price, "types": ["ATR extension"], "why": "امتداد مبني على التذبذب الحالي (ATR)"})

    sl_candidates = [z for z in zones if (z["price"] < px - min_gap if side > 0 else z["price"] > px + min_gap)]
    sl_candidates.sort(key=lambda z: -z["price"] if side > 0 else z["price"])
    sl_base = sl_candidates[0] if sl_candidates else None
    sl = (sl_base["price"] - side * a * 0.18) if sl_base else (px - side * a * 0.90)
    sl = min(sl, px - a * 0.45) if side > 0 else max(sl, px + a * 0.45)

    tps = [z["price"] for z in chosen[:3]]
    risk = abs(px - sl) or 1e-9
    return {
        "entry": px, "sl": sl,
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "risk": risk,
        "tp_reasons": [c.get("why") or "/".join(c.get("types", [])) or "منطقة سوقية" for c in chosen[:3]],
    }


# ------------------------------------------------------------------
# آلة حالة التوقيت Break → Retest → Confirm (محفوظة في state.json)
# ------------------------------------------------------------------
def interval_seconds():
    return KRAKEN_INTERVAL_MINUTES.get(INTERVAL, 1) * 60


def break_retest_evidence(candles, lc, px, a, side, trigger, flow, forecast_score):
    band = max(0.30, a * 0.20)
    confirm = max(0.35, a * 0.12)
    recent = candles[-5:]

    def body_pct(z):
        rng = max(z["high"] - z["low"], 1e-9)
        return abs(z["close"] - z["open"]) / rng

    bull_break = any(z["close"] > trigger + a * 0.08 and body_pct(z) >= 0.35 and z["close"] > z["open"] for z in recent)
    bear_break = any(z["close"] < trigger - a * 0.08 and body_pct(z) >= 0.35 and z["close"] < z["open"] for z in recent)
    bull_retest = side == "BUY" and trigger - band <= px <= trigger + band and lc["low"] <= trigger + band and lc["close"] >= lc["open"] and flow > 4 and forecast_score > 0.08
    bear_retest = side == "SELL" and trigger - band <= px <= trigger + band and lc["high"] >= trigger - band and lc["close"] <= lc["open"] and flow < -4 and forecast_score < -0.08
    bull_confirm = side == "BUY" and px > trigger + confirm and flow > 6 and forecast_score > 0.12
    bear_confirm = side == "SELL" and px < trigger - confirm and flow < -6 and forecast_score < -0.12
    return {
        "bull_break_closed": bull_break, "bear_break_closed": bear_break,
        "bull_retest": bull_retest, "bear_retest": bear_retest,
        "bull_confirm": bull_confirm, "bear_confirm": bear_confirm,
        "band": band, "confirm": confirm,
    }


def update_timing(timing, px, a, side, trigger, closed_break, retest_evidence, confirm_evidence):
    import time
    if not side or trigger is None or a <= 0:
        return {"side": side, "stage": "IDLE", "trigger": trigger, "band": 0, "reason": "لا يوجد Trigger صالح."}

    band = max(0.35, a * 0.18)
    now = time.time()
    max_retest_age = max(90, interval_seconds() * 4)

    if timing.get("side") != side or timing.get("trigger") is None or abs(timing["trigger"] - trigger) > a * 0.65:
        timing = {"side": side, "stage": "PRE-TRIGGER", "trigger": trigger, "band": band,
                  "broken_at": 0, "retest_seen": False, "confirmed_at": 0}
    else:
        timing["band"] = band

    t = timing["trigger"]
    if timing["stage"] == "BREAK" and timing.get("broken_at", 0) > 0 and now - timing["broken_at"] > max_retest_age and not timing.get("retest_seen"):
        timing["stage"] = "MISSED"

    if side == "SELL":
        if timing["stage"] == "BREAK" and px < t - max(a * 0.65, 1.2) and not timing.get("retest_seen"):
            timing["stage"] = "MISSED"
        elif timing["stage"] == "PRE-TRIGGER" and closed_break:
            timing["stage"] = "BREAK"
            timing["broken_at"] = now
        elif timing["stage"] == "BREAK" and retest_evidence:
            timing["stage"] = "RETEST"
            timing["retest_seen"] = True
        elif timing["stage"] == "RETEST" and confirm_evidence:
            timing["stage"] = "CONFIRMED ENTRY"
            timing["confirmed_at"] = now
    else:  # BUY
        if timing["stage"] == "BREAK" and px > t + max(a * 0.65, 1.2) and not timing.get("retest_seen"):
            timing["stage"] = "MISSED"
        elif timing["stage"] == "PRE-TRIGGER" and closed_break:
            timing["stage"] = "BREAK"
            timing["broken_at"] = now
        elif timing["stage"] == "BREAK" and retest_evidence:
            timing["stage"] = "RETEST"
            timing["retest_seen"] = True
        elif timing["stage"] == "RETEST" and confirm_evidence:
            timing["stage"] = "CONFIRMED ENTRY"
            timing["confirmed_at"] = now

    reasons = {
        "PRE-TRIGGER": f"ننتظر إغلاق شمعة خارج {t:,.2f}.",
        "BREAK": f"تم تجاوز {t:,.2f}؛ ننتظر Retest ولا نطارد السعر.",
        "RETEST": "السعر داخل منطقة التأكيد؛ ننتظر استمراراً واضحاً.",
        "CONFIRMED ENTRY": f"تأكيد Break + Retest + استمرار. Trigger {t:,.2f}.",
        "MISSED": "فات وقت الدخول — لا مطاردة.",
    }
    timing["reason"] = reasons.get(timing["stage"], "—")
    timing["closed_break"] = bool(closed_break)
    return timing


# ------------------------------------------------------------------
# محرك القرار الرئيسي (منقول من دالة calc() الأصلية)
# ------------------------------------------------------------------
def classify_move_path(work, px, a, vp, flow, raw_f, macro_structure, imm, fd, ms, sweep=None, absorption=False):
    """
    مصنّف مسار الحركة — يفرق بين 3 حالات عند أي حركة عكس الاتجاه الأساسي:
      REVERSAL   انعكاس حقيقي: كسر بنيوي + انقلاب ضغط + استمرار (نقاط >=70)
      CORRECTION تصحيح مؤقت: البنية الأساسية سليمة، ما نعكسش القرار غلط
      MOVE_END   توقف/إجهاد: بلا دليل كافٍ على انعكاس حقيقي، نوقف بلا تأكيد
    منقول من classifyMovePath() في الملف اللي بعثه المستخدم.
    """
    if len(work) < 8 or a <= 0:
        return {"state": "WAIT", "direction": None, "confidence": 0, "reason": "بيانات غير كافية لتصنيف نهاية الحركة."}

    bars = work[-8:]
    bull_trend = macro_structure == "BULLISH" or imm.get("bull")
    bear_trend = macro_structure == "BEARISH" or imm.get("bear")
    score0 = raw_f[0]["score"] if raw_f else 0
    score1 = raw_f[1]["score"] if len(raw_f) > 1 else score0

    if bull_trend and not bear_trend:
        main_dir = "UP"
    elif bear_trend and not bull_trend:
        main_dir = "DOWN"
    elif score0 > 0.12:
        main_dir = "UP"
    elif score0 < -0.12:
        main_dir = "DOWN"
    else:
        main_dir = None

    if main_dir is None:
        return {"state": "WAIT", "direction": None, "confidence": 0, "reason": "الاتجاه الأساسي غير حاسم؛ لا نصنف التصحيح/الانعكاس."}

    flow_flip = flow < -8 if main_dir == "UP" else flow > 8
    flow_still = flow > -4 if main_dir == "UP" else flow < 4
    momentum_flip = score0 < -0.12 if main_dir == "UP" else score0 > 0.12
    forecast_flip = (score0 < -0.10 and score1 < 0) if main_dir == "UP" else (score0 > 0.10 and score1 > 0)
    persistence = sum(1 for z in bars[-3:] if (z["close"] < z["open"] if main_dir == "UP" else z["close"] > z["open"]))

    recent_lows = [z["low"] for z in bars[-6:]]
    recent_highs = [z["high"] for z in bars[-6:]]
    anchor_low = min(z["low"] for z in bars[-5:-1])
    anchor_high = max(z["high"] for z in bars[-5:-1])
    break_anchor_down = main_dir == "UP" and px < anchor_low - a * 0.08
    break_anchor_up = main_dir == "DOWN" and px > anchor_high + a * 0.08
    break_anchor = break_anchor_down or break_anchor_up

    value_lost = (px < vp["poc"] - a * 0.12) if main_dir == "UP" else (px > vp["poc"] + a * 0.12)
    value_extreme = (px >= vp["vah"] - a * 0.25) if main_dir == "UP" else (px <= vp["val"] + a * 0.25)

    exhaustion = (value_extreme and (ms["slow"] or fd["bull"] or flow < 6 or absorption)) if main_dir == "UP" \
        else (value_extreme and (ms["slow"] or fd["bear"] or flow > -6 or absorption))

    # سحب سيولة عكس الاتجاه الأساسي (اصطياد وقف خسارة) — دليل انعكاس كلاسيكي
    sweep = sweep or {"buy_sweep": False, "sell_sweep": False}
    sweep_against_trend = sweep["buy_sweep"] if main_dir == "UP" else sweep["sell_sweep"]

    reversal_score = 0
    if break_anchor: reversal_score += 35
    if flow_flip: reversal_score += 25
    if momentum_flip: reversal_score += 15
    if forecast_flip: reversal_score += 10
    if value_lost: reversal_score += 10
    if persistence >= 2: reversal_score += 10
    if exhaustion: reversal_score += 5
    if sweep_against_trend: reversal_score += 15

    counter_bars = sum(1 for z in bars[-3:] if (z["close"] < z["open"] if main_dir == "UP" else z["close"] > z["open"]))
    correction_evidence = (counter_bars >= 1 or exhaustion) and not break_anchor and (flow_still or not flow_flip) and not forecast_flip
    correction_score = 0
    if correction_evidence: correction_score += 35
    if exhaustion: correction_score += 25
    if not break_anchor: correction_score += 20
    if flow_still: correction_score += 10
    if counter_bars >= 1: correction_score += 10

    if reversal_score >= 70:
        direction = "DOWN" if main_dir == "UP" else "UP"
        return {"state": "REVERSAL", "direction": direction, "confidence": min(99, reversal_score),
                "reason": f"انعكاس حقيقي عالي الثقة: كسر بنيوي {'✔' if break_anchor else '—'} + "
                          f"انقلاب ضغط {'✔' if flow_flip else '—'} + استمرار {'✔' if persistence >= 2 else '—'}"
                          f"{' + فقد POC' if value_lost else ''}."}
    if correction_score >= 65:
        return {"state": "CORRECTION", "direction": main_dir, "confidence": min(95, correction_score),
                "reason": f"تصحيح داخل الاتجاه: الحركة المضادة لم تكسر البنية الرئيسية، والضغط/التوقع "
                          f"الأساسي ما زال يحمي {'الصعود' if main_dir == 'UP' else 'الهبوط'}."}
    if exhaustion or (value_extreme and (ms["slow"] or fd["bull"] or fd["bear"])):
        return {"state": "MOVE_END", "direction": main_dir,
                "confidence": min(90, 55 + (20 if exhaustion else 0) + (0 if break_anchor else 15)),
                "reason": "نهاية/توقف محتملة للحركة الحالية قرب منطقة سوقية مهمة، لكن لا توجد أدلة كافية لتسميتها انعكاساً حقيقياً."}
    return {"state": "CONTINUATION", "direction": main_dir, "confidence": 55,
            "reason": f"الاتجاه {'صاعد' if main_dir == 'UP' else 'هابط'} ما زال قائماً؛ لم يظهر بعد تصحيح أو انعكاس عالي الثقة."}


def compute_engine(candles, px, symbol_state):
    work = candles
    c = [x["close"] for x in work]
    last = c[-1]
    e20, e50 = ema(c, 20), ema(c, min(50, len(c)))
    a = atr(work, 14) or 1.0
    flow = qflow(work)
    vp = price_bins(work)
    lc = work[-1]

    macro_structure = "BULLISH" if e20 > e50 else "BEARISH" if e20 < e50 else "NEUTRAL"
    imm = immediate_structure(work, a)
    structure = "BEARISH SHIFT" if imm["bear"] else "BULLISH SHIFT" if imm["bull"] else macro_structure

    raw_f = [forecast(work, h) for h in (1, 2, 3)]
    if imm["bear"]:
        for i, f in enumerate(raw_f):
            adj = 0.34 if i == 0 else 0.22 if i == 1 else 0.12
            f["score"] -= adj
            if f["score"] < -0.20:
                f["dir"] = "BEARISH"
    if imm["bull"]:
        for i, f in enumerate(raw_f):
            adj = 0.34 if i == 0 else 0.22 if i == 1 else 0.12
            f["score"] += adj
            if f["score"] > 0.20:
                f["dir"] = "BULLISH"

    px = px if px is not None else last
    live_move = px - last
    live_shock_bear = live_move <= -max(a * 0.55, 0.80) and px < last - a * 0.30
    live_shock_bull = live_move >= max(a * 0.55, 0.80) and px > last + a * 0.30
    live_poc_loss = px < vp["poc"] - a * 0.10 <= last
    live_poc_reclaim = px > vp["poc"] + a * 0.10 and last <= vp["poc"] + a * 0.10
    live_break_low = px < lc["low"] - a * 0.08
    live_break_high = px > lc["high"] + a * 0.08
    live_bear_shift = live_shock_bear and (live_poc_loss or live_break_low or px < vp["poc"])
    live_bull_shift = live_shock_bull and (live_poc_reclaim or live_break_high or px > vp["poc"])

    inside = vp["val"] <= px <= vp["vah"]
    near_vah = abs(px - vp["vah"]) <= max(a * 0.50, 0.25)
    near_val = abs(px - vp["val"]) <= max(a * 0.50, 0.25)
    close_above = lc["close"] > vp["vah"]
    close_below = lc["close"] < vp["val"]
    recent_above = any(z["close"] > vp["vah"] for z in work[-3:])
    recent_below = any(z["close"] < vp["val"] for z in work[-3:])

    rs = range_state(work, vp, a)
    fd = flow_divergence(work)
    ms = momentum_state(work)
    upper_reject = rejection_at(work, vp["vah"], "upper") or rejection_at(work, vp["poc"], "upper")
    lower_reject = rejection_at(work, vp["val"], "lower") or rejection_at(work, vp["poc"], "lower")

    # منقول من مؤشر MST: كفاءة الحركة (ترند نظيف مقابل تذبذب)، سحب
    # السيولة (اصطياد وقف خسارة كلاسيكي)، والامتصاص (حجم كبير/جسم صغير)
    efficiency = calc_efficiency(work, 20)
    sweep = detect_liquidity_sweep(work, 10)
    absorption = detect_absorption(work, 20)

    bull_exhaust = (near_vah or px > vp["vah"]) and (ms["slow"] or fd["bull"] or upper_reject or flow < 6 or absorption or sweep["buy_sweep"])
    strong_bear_cont = (
        (macro_structure == "BEARISH" or imm["bear"] or live_bear_shift)
        and (flow < -8 or raw_f[0]["score"] < -0.18)
        and (raw_f[0]["dir"] == "BEARISH" or imm["bear"] or live_bear_shift)
        and (close_below or recent_below or live_move < -max(a * 0.45, 0.60))
        and not fd["bull"]
    )
    strong_bull_cont = (
        (macro_structure == "BULLISH" or imm["bull"] or live_bull_shift)
        and (flow > 8 or raw_f[0]["score"] > 0.18)
        and (raw_f[0]["dir"] == "BULLISH" or imm["bull"] or live_bull_shift)
        and (close_above or recent_above or live_move > max(a * 0.45, 0.60))
        and not fd["bear"]
    )
    bear_exhaust = (near_val or px < vp["val"]) and not strong_bear_cont and ((ms["slow"] and flow > -6) or fd["bear"] or lower_reject or absorption or sweep["sell_sweep"])
    wi = wick_info(lc)
    failed_bull = close_above and ((px < vp["vah"] + a * 0.10 and wi["upper_pct"] > 0.42) or flow < 0 or fd["bull"])
    failed_bear = close_below and ((px > vp["val"] - a * 0.10 and wi["lower_pct"] > 0.42) or flow > 0 or fd["bear"])

    # كفاءة ضعيفة = سعر يتذبذب بلا هدف، حتى لو بدا فيه زخم آني — نمنع
    # الدخول "زخم" في هذي الحالة (تقليل الإشارات الكاذبة في سوق متذبذب)
    strong_bear_mom = not failed_bear and not bear_exhaust and efficiency > 0.40 and (
        strong_bear_cont or (macro_structure == "BEARISH" and flow < -10 and raw_f[0]["score"] < -0.15 and close_below)
    )
    strong_bull_mom = not failed_bull and not bull_exhaust and efficiency > 0.40 and (
        strong_bull_cont or (macro_structure == "BULLISH" and flow > 10 and raw_f[0]["score"] > 0.15 and close_above)
    )

    micro_bear = (imm["bear"] or live_bear_shift) and not bear_exhaust and not bull_exhaust
    micro_bull = (imm["bull"] or live_bull_shift) and not bull_exhaust and not bear_exhaust
    micro_bear_level = imm["level"] if imm["bear"] else (vp["poc"] if live_poc_loss else lc["low"])
    micro_bull_level = imm["level"] if imm["bull"] else (vp["poc"] if live_poc_reclaim else lc["high"])

    legacy_bull_break = recent_above and close_above and flow > 10 and raw_f[0]["dir"] == "BULLISH" and (macro_structure == "BULLISH" or micro_bull) and not bull_exhaust and not failed_bull
    legacy_bear_break = recent_below and close_below and flow < -10 and raw_f[0]["dir"] == "BEARISH" and (macro_structure == "BEARISH" or micro_bear) and not bear_exhaust and not failed_bear

    timing_side = "SELL" if (micro_bear or legacy_bear_break) else "BUY" if (micro_bull or legacy_bull_break) else None
    timing_trigger = (micro_bear_level if timing_side == "SELL" else micro_bull_level if timing_side == "BUY" else None)
    if timing_side == "SELL" and (timing_trigger is None or abs(timing_trigger - vp["val"]) > a * 1.5):
        timing_trigger = vp["val"]
    if timing_side == "BUY" and (timing_trigger is None or abs(timing_trigger - vp["vah"]) > a * 1.5):
        timing_trigger = vp["vah"]

    ev = break_retest_evidence(work, lc, px, a, timing_side, timing_trigger, flow, raw_f[0]["score"]) if timing_side else None
    bull_break = ((ev["bull_break_closed"] if ev else False) or legacy_bull_break) and flow > 5 and raw_f[0]["score"] > 0.08 and not bull_exhaust and not failed_bull
    bear_break = ((ev["bear_break_closed"] if ev else False) or legacy_bear_break) and flow < -5 and raw_f[0]["score"] < -0.08 and not bear_exhaust and not failed_bear

    closed_break_for_side = (ev["bull_break_closed"] if timing_side == "BUY" else ev["bear_break_closed"] if timing_side == "SELL" else False) if ev else False
    retest_for_side = (ev["bull_retest"] if timing_side == "BUY" else ev["bear_retest"] if timing_side == "SELL" else False) if ev else False
    confirm_for_side = (ev["bull_confirm"] if timing_side == "BUY" else ev["bear_confirm"] if timing_side == "SELL" else False) if ev else False

    timing = symbol_state.get("timing") or {}
    if timing_side:
        timing = update_timing(timing, px, a, timing_side, timing_trigger, closed_break_for_side, retest_for_side, confirm_for_side)
    else:
        timing = {"side": None, "stage": "IDLE", "trigger": None, "band": 0, "reason": "لا يوجد Trigger صالح."}
    symbol_state["timing"] = timing

    decision, reason = "WAIT", "لا توجد مطابقة كافية بين البنية والتدفق والتوقيت."
    if bull_exhaust:
        reason = "إجهاد شرائي قرب المقاومة/القيمة العليا؛ ننتظر إعادة بناء."
    elif bear_exhaust:
        reason = "إجهاد بيعي قرب الدعم/القيمة السفلى؛ ننتظر إعادة بناء."
    elif failed_bull:
        reason = "اختراق صاعد فقد القبول؛ لا مطاردة."
    elif failed_bear:
        reason = "كسر هابط فقد القبول؛ لا مطاردة."
    elif timing["stage"] == "CONFIRMED ENTRY" and timing["side"] == "BUY":
        decision, reason = "BUY", f"تأكيد BUY: Break + Retest + استمرار. Trigger {timing['trigger']:,.2f}."
    elif timing["stage"] == "CONFIRMED ENTRY" and timing["side"] == "SELL":
        decision, reason = "SELL", f"تأكيد SELL: Break + Retest + استمرار. Trigger {timing['trigger']:,.2f}."
    elif FAST_ENTRY and timing["stage"] == "BREAK" and timing["side"] == "BUY" and flow > 12 and raw_f[0]["score"] > 0.16:
        decision, reason = "BUY", f"⚡ دخول سريع عند أول اختراق مؤكد (بلا انتظار Retest). Trigger {timing['trigger']:,.2f}."
    elif FAST_ENTRY and timing["stage"] == "BREAK" and timing["side"] == "SELL" and flow < -12 and raw_f[0]["score"] < -0.16:
        decision, reason = "SELL", f"⚡ دخول سريع عند أول كسر مؤكد (بلا انتظار Retest). Trigger {timing['trigger']:,.2f}."
    elif strong_bull_mom:
        decision, reason = "BUY", "BUY زخم صاعد: الاتجاه والبنية والتدفق والتنبؤ متوافقون — زخم وليس مطاردة."
    elif strong_bear_mom:
        decision, reason = "SELL", "SELL زخم هابط: الاتجاه والبنية والتدفق والتنبؤ متوافقون — زخم وليس مطاردة."
    elif timing["stage"] == "MISSED":
        reason = timing["reason"]
    elif timing["stage"] == "RETEST":
        reason = f"RETEST {timing_side}: السعر داخل منطقة التأكيد؛ ننتظر استمراراً."
    elif timing["stage"] == "BREAK":
        reason = f"BREAK {timing_side}: تجاوز Trigger {timing['trigger']:,.2f}؛ ننتظر Retest."
    elif live_bull_shift or micro_bull:
        decision, reason = "BUY WATCH", f"تحول صاعد؛ ننتظر Break ثم Retest قرب {timing_trigger:,.2f}." if timing_trigger else "تحول صاعد مبكر."
    elif live_bear_shift or micro_bear:
        decision, reason = "SELL WATCH", f"تحول هابط؛ ننتظر Break ثم Retest قرب {timing_trigger:,.2f}." if timing_trigger else "تحول هابط مبكر."
    elif bull_break:
        decision, reason = "BUY WATCH", "اختراق صاعد؛ ننتظر Break/Retest صالح."
    elif bear_break:
        decision, reason = "SELL WATCH", "كسر هابط؛ ننتظر Break/Retest صالح."
    elif inside:
        reason = "السعر داخل منطقة القيمة؛ لا دخول من المنتصف."

    # مصنّف مسار الحركة — يمنع تحويل كل تصحيح بسيط لانعكاس، ويبدل القرار
    # فقط عند انعكاس حقيقي (كسر بنيوي + انقلاب ضغط + استمرار).
    path_state = classify_move_path(work, px, a, vp, flow, raw_f, macro_structure, imm, fd, ms, sweep, absorption)
    if path_state["state"] == "REVERSAL":
        new_decision = "BUY" if path_state["direction"] == "UP" else "SELL"
        decision = new_decision
        reason = f"🔄 {path_state['reason']} تم تغيير مسار التحليل إلى {new_decision}."
    elif path_state["state"] == "CORRECTION":
        keep = "BUY" if path_state["direction"] == "UP" else "SELL"
        if (keep == "BUY" and decision == "SELL") or (keep == "SELL" and decision == "BUY"):
            decision = "WAIT"
            reason = f"↩️ {path_state['reason']} لذلك لا نعكس الاتجاه بسبب التصحيح؛ ننتظر استمرار {keep}."
        elif decision == "WAIT" or "WATCH" in decision:
            reason = f"↩️ {path_state['reason']} الاتجاه الرئيسي لم ينتهِ بعد."
    elif path_state["state"] == "MOVE_END":
        if decision in ("BUY", "SELL"):
            decision = "WAIT"
            reason = f"⏸️ {path_state['reason']} لا نغيّر الاتجاه قبل ظهور دليل انعكاس حقيقي."

    liquidity = institutional_liquidity(work, vp, a, px)
    plan = market_plan(a, px, decision, liquidity)

    return {
        "price": px, "last_close": last, "atr": a, "flow": flow, "vp": vp,
        "structure": structure, "macro_structure": macro_structure,
        "decision": decision, "reason": reason, "timing": timing, "path_state": path_state,
        "efficiency": efficiency, "sweep": sweep, "absorption": absorption,
        "forecast": raw_f, "liquidity": liquidity, "plan": plan,
        "bull_exhaust": bull_exhaust, "bear_exhaust": bear_exhaust,
        "failed_bull": failed_bull, "failed_bear": failed_bear,
    }


# ------------------------------------------------------------------
# قاطع الدائرة (نفس فكرة البوت القديم — أمان إضافي)
# ------------------------------------------------------------------
def check_open_signal_outcome(symbol_state, price):
    open_signal = symbol_state.get("open_signal")
    if not open_signal:
        return
    direction, tp1, sl = open_signal["direction"], open_signal.get("tp1"), open_signal["sl"]
    if tp1 is None:
        return
    hit_tp = (direction == "BUY" and price >= tp1) or (direction == "SELL" and price <= tp1)
    hit_sl = (direction == "BUY" and price <= sl) or (direction == "SELL" and price >= sl)
    if hit_tp:
        symbol_state["consecutive_losses"] = 0
        symbol_state["open_signal"] = None
        log(f"✅ آخر إشارة {symbol_state.get('label','')} لامست TP1")
    elif hit_sl:
        symbol_state["consecutive_losses"] = symbol_state.get("consecutive_losses", 0) + 1
        symbol_state["open_signal"] = None
        log(f"❌ آخر إشارة {symbol_state.get('label','')} لامست SL — خسائر متتالية: {symbol_state['consecutive_losses']}")


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
        until = datetime.fromtimestamp(now.timestamp() + CIRCUIT_BREAKER_PAUSE_HOURS * 3600, tz=timezone.utc)
        symbol_state["pause_until"] = until.isoformat()
        symbol_state["consecutive_losses"] = 0
        notify(f"⛔ <b>قاطع الدائرة — {label}</b>\nتوقف مؤقت بعد {MAX_CONSECUTIVE_LOSSES} خسائر متتالية، لمدة {CIRCUIT_BREAKER_PAUSE_HOURS} ساعة.")


# ------------------------------------------------------------------
# تنسيق رسائل تيليجرام
# ------------------------------------------------------------------
def format_entry_signal(label, x):
    plan = x["plan"]
    icon = "🟢⬆️" if x["decision"] == "BUY" else "🔴⬇️"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tp_lines = ""
    for i, key in enumerate(("tp1", "tp2", "tp3"), start=1):
        if plan.get(key) is not None:
            tp_lines += f"TP{i}: {plan[key]:,.2f}\n"
    return (
        f"{icon} <b>XAU Engine V21 — {x['decision']}</b>\n"
        f"الرمز: {label}\n"
        f"التاريخ: {ts}\n\n"
        f"نقطة الدخول: {plan['entry']:,.2f}\n"
        f"SL: {plan['sl']:,.2f}\n"
        f"{tp_lines}\n"
        f"ATR: {x['atr']:.2f} | Order Flow: {x['flow']:+.1f} | البنية: {x['structure']}\n"
        f"مسار الحركة: {x['path_state']['state']} ({x['path_state']['confidence']}%)\n"
        f"السبب: {x['reason']}\n\n"
        f"⚠️ إشارة تحليلية (Volume Profile + Order Flow + Break/Retest/Confirm)، "
        f"ليست ضمان نجاح ولا نصيحة مالية. التنفيذ يدوي بقرارك."
    )


def format_status_update(label, x):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"🕐 <b>تحديث دوري (بدون توصية دخول)</b>\n"
        f"الرمز: {label}\n"
        f"التاريخ: {ts}\n\n"
        f"السعر: {x['price']:,.2f}\n"
        f"البنية: {x['structure']} (EMA: {x['macro_structure']})\n"
        f"القرار الحالي: {x['decision']}\n"
        f"المرحلة: {x['timing'].get('stage', '—')}\n"
        f"مسار الحركة: {x['path_state']['state']} ({x['path_state']['confidence']}%)\n"
        f"Order Flow: {x['flow']:+.1f} | ATR: {x['atr']:.2f}\n\n"
        f"{x['reason']}"
    )


# ------------------------------------------------------------------
# معالجة رمز واحد
# ------------------------------------------------------------------
def minutes_since(iso_str):
    if not iso_str:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso_str)).total_seconds() / 60


def process_symbol(symbol, label, state):
    symbol_state = state.setdefault(symbol, {"label": label})
    symbol_state["label"] = label

    if WEEKEND_FILTER and symbol.upper() in WEEKEND_FILTER_SYMBOLS and is_forex_market_closed():
        log(f"{label}: سوق الذهب الحقيقي مغلق (عطلة نهاية الأسبوع) — تخطي الفحص")
        return

    breaker_active, until = is_circuit_broken(symbol_state)
    if breaker_active:
        log(f"{label}: قاطع الدائرة فعّال حتى {until}")
        return

    try:
        candles = get_klines(symbol, INTERVAL, limit=max(LOOKBACK_BARS + 10, 250))
    except Exception as e:
        log(f"{label}: 
