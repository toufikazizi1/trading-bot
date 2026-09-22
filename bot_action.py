"""
بوت إشارات اتجاه السوق (Trend Signal Bot) — يرسل توصية عبر تيليجرام فقط،
بدون تنفيذ أي صفقة فعلية. يقارن السعر الحالي بالسعر قبل فترة معينة:
لو السوق صاعد بنسبة كافية يرسل إشارة شراء، لو نازل يرسل إشارة بيع.
يجلب الأسعار من CoinGecko (مجاني، بدون مفتاح API).
"""
import os
import sys
import requests
from datetime import datetime, timezone

BOT_SYMBOL = os.environ.get("BOT_SYMBOL", "bitcoin")
BOT_SYMBOL_LABEL = os.environ.get("BOT_SYMBOL_LABEL", "BTC/USDT")
LOOKBACK = int(os.environ.get("BOT_LOOKBACK", "12"))          # عدد النقاط للمقارنة
TREND_THRESHOLD_PCT = float(os.environ.get("BOT_TREND_THRESHOLD_PCT", "0.3"))
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
    params = {"vs_currency": "usd", "days": "1"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    prices = data.get("prices", [])
    return [p[1] for p in prices]


def trend_signal(closes):
    if len(closes) < LOOKBACK + 1:
        return "hold"
    current = closes[-1]
    past = closes[-1 - LOOKBACK]
    change_pct = ((current - past) / past) * 100
    if change_pct >= TREND_THRESHOLD_PCT:
        return "buy"
    if change_pct <= -TREND_THRESHOLD_PCT:
        return "sell"
    return "hold"


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
        f"⚠️ إشارة اتجاه السوق التلقائية — وليست نصيحة مالية. "
        f"التنفيذ يدوي بقرارك، وقد تتكرر الإشارة طول استمرار الاتجاه."
    )


def main():
    log(f"بدء الفحص — الرمز: {BOT_SYMBOL_LABEL}")
    try:
        closes = get_closes()
    except Exception as e:
        notify(f"⚠️ فشل جلب الأسعار لـ {BOT_SYMBOL_LABEL}: {e}")
        sys.exit(1)

    signal = trend_signal(closes)
    log(f"اتجاه السوق: {signal}")

    if signal == "hold":
        log("لا يوجد اتجاه واضح كافٍ — لا إشارة.")
        return

    entry_price = closes[-1]
    notify(format_signal_card(signal, entry_price))


if __name__ == "__main__":
    main()
