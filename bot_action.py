"""
بوت إشارات اختراق (Breakout Signal Bot) — يرسل توصية عبر تيليجرام فقط،
بدون تنفيذ أي صفقة فعلية. يحدد أعلى قمة وأقل قاع خلال فترة سابقة (Lookback)،
ويرسل إشارة شراء لو السعر كسر أعلى قمة، أو إشارة بيع لو كسر أقل قاع.
يجلب الأسعار من CoinGecko (مجاني، بدون مفتاح API).
"""
import os
import sys
import requests
from datetime import datetime, timezone

BOT_SYMBOL = os.environ.get("BOT_SYMBOL", "bitcoin")
BOT_SYMBOL_LABEL = os.environ.get("BOT_SYMBOL_LABEL", "BTC/USDT")
LOOKBACK = int(os.environ.get("BOT_LOOKBACK", "24"))          # عدد النقاط لتحديد القمة/القاع
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


def breakout_signal(closes):
    if len(closes) < LOOKBACK + 2:
        return "hold", None, None

    current = closes[-1]
    window = closes[-1 - LOOKBACK:-1]
    highest = max(window)
    lowest = min(window)

    if current > highest:
        return "buy", highest, lowest
    if current < lowest:
        return "sell", highest, lowest
    return "hold", highest, lowest


def format_signal_card(signal, entry_price, highest, lowest):
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
        f"{icon} <b>إشارة {label} (اختراق)</b>\n"
        f"الرمز: {BOT_SYMBOL_LABEL}\n"
        f"التاريخ: {ts}\n\n"
        f"نقطة الدخول: {entry_price:,.2f}\n"
        f"أعلى قمة سابقة: {highest:,.2f}\n"
        f"أقل قاع سابق: {lowest:,.2f}\n\n"
        f"TP1: {tp1:,.2f}\n"
        f"TP2: {tp2:,.2f}\n"
        f"SL: {sl:,.2f}\n\n"
        f"⚠️ إشارة اختراق تلقائية مبنية على كسر أعلى قمة أو أقل قاع سابق — "
        f"وليست نصيحة مالية. التنفيذ يدوي بقرارك."
    )


def main():
    log(f"بدء الفحص — الرمز: {BOT_SYMBOL_LABEL}")
    try:
        closes = get_closes()
    except Exception as e:
        notify(f"⚠️ فشل جلب الأسعار لـ {BOT_SYMBOL_LABEL}: {e}")
        sys.exit(1)

    signal, highest, lowest = breakout_signal(closes)
    log(f"إشارة الاختراق: {signal} (قمة={highest}, قاع={lowest})")

    if signal == "hold":
        log("لا يوجد اختراق جديد — لا إشارة.")
        return

    entry_price = closes[-1]
    notify(format_signal_card(signal, entry_price, highest, lowest))


if __name__ == "__main__":
    main()
