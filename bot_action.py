"""
بوت إشارات تداول (Signal Bot) — يحلل السوق ويرسل توصية عبر تيليجرام فقط،
بدون تنفيذ أي صفقة فعلية. يعتمد على تقاطع المتوسطات المتحركة كإشارة فنية،
ويحسب نقاط دخول/جني أرباح/وقف خسارة تقديرية بناءً على نسب مئوية.
يجلب الأسعار من CoinGecko (مجاني، بدون مفتاح API، بدون قيود جغرافية).
"""
import os
import sys
import requests
from datetime import datetime, timezone

BOT_SYMBOL = os.environ.get("BOT_SYMBOL", "bitcoin")          # معرف العملة في CoinGecko
BOT_SYMBOL_LABEL = os.environ.get("BOT_SYMBOL_LABEL", "BTC/USDT")  # للعرض فقط بالرسالة
SHORT_WINDOW = int(os.environ.get("BOT_SHORT_WINDOW", "10"))
LONG_WINDOW = int(os.environ.get("BOT_LONG_WINDOW", "30"))
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
    url = f"https://api.coingecko.com/api/v3/coins/{BOT_SYMBOL}/market_chart"
    params = {"vs_currency": "usd", "days": "90", "interval": "daily"}
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


def format_signal_card(signal, entry_price):
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
        f"⚠️ إشارة فنية تعليمية بناءً على تقاطع المتوسطات المتحركة — "
        f"وليست نصيحة مالية. التنفيذ يدوي بقرارك."
    )


def main():
    log(f"بدء الفحص — الرمز: {BOT_SYMBOL_LABEL}")
    try:
        closes = get_closes()
    except Exception as e:
        notify(f"⚠️ فشل جلب الأسعار لـ {BOT_SYMBOL_LABEL}: {e}")
        sys.exit(1)

    signal, short_ma, long_ma = crossover_signal(closes)
    log(f"الإشارة الفنية: {signal} (قصير={short_ma}, طويل={long_ma})")

    if signal == "hold":
        log("لا يوجد تقاطع جديد — لا إشارة.")
        return

    entry_price = closes[-1]
    notify(format_signal_card(signal, entry_price))


if __name__ == "__main__":
    main()    
    
