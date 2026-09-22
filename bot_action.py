"""
بوت إشارات تداول متعدد المؤشرات — يحلل السوق ويرسل توصية عبر تيليجرام فقط،
بدون تنفيذ أي صفقة فعلية. يجمع ثلاث مؤشرات: تقاطع المتوسطات المتحركة، RSI،
وتحليل القمم/القيعان (دعم ومقاومة)، ويطلب توافق بينها قبل إرسال إشارة.
يجلب الأسعار من CoinGecko (مجاني، بدون مفتاح API).
"""
import os
import sys
import requests
from datetime import datetime, timezone

BOT_SYMBOL = os.environ.get("BOT_SYMBOL", "bitcoin")
BOT_SYMBOL_LABEL = os.environ.get("BOT_SYMBOL_LABEL", "BTC/USDT")
SHORT_WINDOW = int(os.environ.get("BOT_SHORT_WINDOW", "10"))
LONG_WINDOW = int(os.environ.get("BOT_LONG_WINDOW", "30"))
RSI_PERIOD = int(os.environ.get("BOT_RSI_PERIOD", "14"))
RSI_OVERBOUGHT = float(os.environ.get("BOT_RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = float(os.environ.get("BOT_RSI_OVERSOLD", "30"))
SWING_LOOKBACK = int(os.environ.get("BOT_SWING_LOOKBACK", "40"))
TP1_PCT = float(os.environ.get("BOT_TP1_PCT", "0.5"))
TP2_PCT = float(os.environ.get("BOT_TP2_PCT", "1.2"))
SL_PCT = float(os.environ.get("BOT_SL_PCT", "0.7"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}")


def notify(msg):
    log(msg)
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


def get_closes():
    # days=1 يعطي بيانات كل ~5 دقائق تلقائيًا من CoinGecko
    url = f"https://api.coingecko.com/api/v3/coins/{BOT_SYMBOL}/market_chart"
    params = {"vs_currency": "usd", "days": "1"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    prices = data.get("prices", [])
    return [p[1] for p in prices]


def moving_average(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def crossover_signal(closes):
    if len(closes) < LONG_WINDOW + 1:
        return "hold"
    short_now = moving_average(closes, SHORT_WINDOW)
    long_now = moving_average(closes, LONG_WINDOW)
    short_prev = moving_average(closes[:-1], SHORT_WINDOW)
    long_prev = moving_average(closes[:-1], LONG_WINDOW)
    if None in (short_now, long_now, short_prev, long_prev):
        return "hold"
    if short_prev <= long_prev and short_now > long_now:
        return "buy"
    if short_prev >= long_prev and short_now < long_now:
        return "sell"
    return "hold"


def calc_rsi(closes, period):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def support_resistance(closes, lookback):
    recent = closes[-lookback:] if len(closes) >= lookback else closes
    return min(recent), max(recent)


def format_signal_card(signal, entry_price, rsi, support, resistance):
    icon = "🟢⬆️" if signal == "buy" else "🔴⬇️"
    label = "شراء" if signal == "buy" else "بيع"
    if signal == "buy":
        tp1 = entry_price * (1 + TP1_PCT / 100)
        tp2 = entry_price * (1 + TP2_PCT / 100)
        sl = entry_price * (1 - SL_PCT / 100)
    else:
        tp1 = entry_price * (1 - TP1_PCT / 100)
        tp2 = entry_price * (1 - TP2_PCT / 100)
        sl = entry_price * (1 + SL_PCT / 100)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"{icon} <b>إشارة {label}</b>\n"
        f"الرمز: {BOT_SYMBOL_LABEL}\n"
        f"التاريخ: {ts}\n\n"
        f"نقطة الدخول: {entry_price:,.2f}\n"
        f"TP1: {tp1:,.2f}\n"
        f"TP2: {tp2:,.2f}\n"
        f"SL: {sl:,.2f}\n\n"
        f"RSI: {rsi:.1f}\n"
        f"الدعم: {support:,.2f} | المقاومة: {resistance:,.2f}\n\n"
        f"⚠️ إشارة فنية تعليمية مبنية على تقاطع المتوسطات + RSI + الدعم/المقاومة — "
        f"وليست نصيحة مالية. التنفيذ يدوي بقرارك."
    )


def main():
    log(f"بدء الفحص — الرمز: {BOT_SYMBOL_LABEL}")
    try:
        closes = get_closes()
    except Exception as e:
        notify(f"⚠️ فشل جلب الأسعار لـ {BOT_SYMBOL_LABEL}: {e}")
        sys.exit(1)

    ma_signal = crossover_signal(closes)
    rsi = calc_rsi(closes, RSI_PERIOD)
    support, resistance = support_resistance(closes, SWING_LOOKBACK)
    entry_price = closes[-1]

    log(f"MA={ma_signal} RSI={rsi} دعم={support} مقاومة={resistance}")

    if ma_signal == "hold" or rsi is None:
        log("لا يوجد توافق كافٍ بين المؤشرات — لا إشارة.")
        return

    # نطلب توافق: تقاطع صاعد + RSI ليس بمنطقة تشبع شرائي متطرفة (والعكس للبيع)
    if ma_signal == "buy" and rsi >= RSI_OVERBOUGHT:
        log(f"تقاطع شراء لكن RSI بمنطقة تشبع شرائي ({rsi:.1f}) — تم تجاهل الإشارة.")
        return
    if ma_signal == "sell" and rsi <= RSI_OVERSOLD:
        log(f"تقاطع بيع لكن RSI بمنطقة تشبع بيعي ({rsi:.1f}) — تم تجاهل الإشارة.")
        return

    notify(format_signal_card(ma_signal, entry_price, rsi, support, resistance))


if __name__ == "__main__":
    main()    
    
