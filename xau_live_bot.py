#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAU Live Paper Bot — بوت لحظي تجريبي (Paper) للذهب XAUUSD

- يقرأ سعر وشموع XAUUSD الحيين من Biquote (نفس مصدر ملف V26).
- يشغّل نفس محرك البوت (bot_action.py: Volume Profile + Order Flow + تأكيد + فلتر الأخبار
  + فلتر الأطر الأكبر + فلتر "فات القطار") ويفتح/يغلق صفقات تجريبية لحاله. لا يلمس أي مال حقيقي.
- لوحة تحكم محلية: http://localhost:8080

التشغيل:   python xau_live_bot.py
اختبار على ملف قديم:   python xau_live_bot.py --replay FILE.csv --tf 15m
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import requests
except ImportError:
    print("ناقص requests — شغّل:  pip install requests")
    sys.exit(1)

import bot_action as eng  # محرك القرار (ملف bot_action.py لازم يكون في نفس المجلد)

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "paper_state.json")
TRADES_FILE = os.path.join(HERE, "paper_trades.csv")

# ----------------------------- الإعدادات -----------------------------
CFG = {
    "api": "https://biquote.io/api",
    "tf": "1m",                 # إطار الشموع الذي يقرر عليه المحرك (نفس بوت تيليجرام)
    "start_equity": 10000.0,    # رأس مال تجريبي بالدولار
    "risk_pct": 0.5,            # نسبة المخاطرة لكل صفقة من الرصيد
    "contract": 100.0,          # أونصة في اللوت الواحد
    "min_lot": 0.01,
    "max_leverage": 20.0,       # سقف الرافعة الفعلية: حجم الصفقة لا يتجاوز رصيد × 20
    "tick_seconds": 2.0,        # كل كم ثانية نقرأ السعر ونتابع الصفقة
    "engine_seconds": 5.0,      # كل كم ثانية نشغّل المحرك
    "max_tick_age": 30.0,       # سعر أقدم من هذا (ثانية) = لا نتداول
    "spread_fallback": 0.30,
    "htf_cache": 60.0,          # تخزين مؤقت لشموع الأطر الكبيرة
    "port": 8080,
}


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def to_epoch(v):
    if isinstance(v, (int, float)):
        return float(v / 1000 if v > 1e12 else v)
    s = str(v).strip()
    if s.isdigit():
        n = float(s)
        return n / 1000 if n > 1e12 else n
    if ("T" in s or " " in s) and not (s.endswith("Z") or re.search(r"[+-]\d\d:?\d\d$", s)):
        s = s.replace(" ", "T") + "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def aggregate(bars, sec):
    out, cur = [], None
    for z in bars:
        k = int(z["open_time"] // sec * sec)
        if cur is None or cur["open_time"] != k:
            if cur:
                out.append(cur)
            cur = {"open_time": k, "open": z["open"], "high": z["high"], "low": z["low"],
                   "close": z["close"], "volume": z["volume"]}
        else:
            cur["high"] = max(cur["high"], z["high"])
            cur["low"] = min(cur["low"], z["low"])
            cur["close"] = z["close"]
            cur["volume"] += z["volume"]
    if cur:
        out.append(cur)
    return out


# ----------------------------- مصادر البيانات -----------------------------
class BiquoteFeed:
    live = True

    def __init__(self):
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) XAU-Live-Paper-Bot"
        self.cache = {}

    def now(self):
        return time.time()

    def _get(self, path, params=None, timeout=8):
        r = self.http.get(CFG["api"] + path, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def tick(self):
        try:
            t = self._get("/XAUUSD", {"allowStale": "false"})
        except Exception:
            t = self._get("/XAUUSD", {"allowStale": "true"})
        mid = t.get("mid", t.get("price"))
        bid, ask = t.get("bid"), t.get("ask")
        if bid is None or ask is None:
            mid = float(mid)
            bid, ask = mid - CFG["spread_fallback"] / 2, mid + CFG["spread_fallback"] / 2
        bid, ask = float(bid), float(ask)
        return {"bid": bid, "ask": ask, "mid": float(mid) if mid is not None else (bid + ask) / 2,
                "age": float(t.get("quoteAgeSeconds") or 0)}

    def klines(self, interval, limit=300):
        ttl = 0 if interval == CFG["tf"] else CFG["htf_cache"]
        c = self.cache.get(interval)
        if c and time.time() - c[0] < ttl and len(c[1]) >= min(limit, c[2]):
            return c[1][-limit:]
        to = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        o = self._get("/XAUUSD/ohlc", {"interval": interval, "limit": limit, "to": to,
                                       "_": int(time.time() * 1000)})
        raw = o.get("bars") or []
        bars = []
        for z in raw:
            try:
                bars.append({
                    "open_time": int(to_epoch(z.get("openTime") or z.get("timestamp") or z.get("time"))),
                    "open": float(z["open"]), "high": float(z["high"]),
                    "low": float(z["low"]), "close": float(z["close"]),
                    "volume": float(z.get("volume") or z.get("tickVolume") or 0),
                })
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b["open_time"])
        self.cache[interval] = (time.time(), bars, limit)
        return bars[-limit:]


def load_replay_csv(path):
    """يقرأ ملف شموع (صيغة myfxbook: Date,Open,High,Low,Close — الأحدث أولاً، أو صيغة ISO)."""
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) < 5 or not r[0][:1].isdigit():
                continue
            ds = r[0].strip()
            try:
                if re.match(r"\d\d/\d\d/\d{4}", ds):
                    t = datetime.strptime(ds, "%m/%d/%Y %H:%M").replace(tzinfo=timezone.utc).timestamp()
                else:
                    t = to_epoch(ds)
                rows.append({"open_time": int(t), "open": float(r[1]), "high": float(r[2]),
                             "low": float(r[3]), "close": float(r[4]), "volume": 0.0})
            except ValueError:
                continue
    rows.sort(key=lambda b: b["open_time"])
    return rows


class ReplayFeed:
    """يعيد تشغيل شموع قديمة كأنها لحظية (4 نقاط سعر داخل كل شمعة) لاختبار البوت."""
    live = False

    def __init__(self, path, tf, start=130):
        self.bars = load_replay_csv(path)
        self.sec = TF_SECONDS[tf]
        self.i, self.k = start, 0
        if len(self.bars) <= start + 5:
            raise SystemExit("الملف قصير جداً للاختبار")

    def _seq(self, z):
        return [z["open"], z["low"], z["high"], z["close"]] if z["close"] >= z["open"] \
            else [z["open"], z["high"], z["low"], z["close"]]

    def now(self):
        return self.bars[self.i]["open_time"] + self.sec * (self.k + 1) / 4.0

    def tick(self):
        p = self._seq(self.bars[self.i])[self.k]
        h = CFG["spread_fallback"] / 2
        return {"bid": p - h, "ask": p + h, "mid": p, "age": 0.0}

    def advance(self):
        self.k += 1
        if self.k > 3:
            self.k = 0
            self.i += 1
        return self.i < len(self.bars)

    def klines(self, interval, limit=300):
        z = self.bars[self.i]
        seq = self._seq(z)[: self.k + 1]
        forming = {"open_time": z["open_time"], "open": z["open"], "high": max(seq), "low": min(seq),
                   "close": seq[-1], "volume": 0.0}
        base = self.bars[: self.i] + [forming]
        sec = TF_SECONDS[interval]
        if sec < self.sec:
            raise ValueError("إطار أصغر من إطار ملف الاختبار")
        res = base if sec == self.sec else aggregate(base, sec)
        return res[-limit:]


# ----------------------------- البوت -----------------------------
def fresh_state():
    return {"balance": CFG["start_equity"], "peak": CFG["start_equity"], "max_dd": 0.0,
            "position": None, "trades": [], "consec_losses": 0, "pause_until": 0, "sym": {}}


class Bot:
    def __init__(self, feed, persist=True):
        self.feed, self.persist = feed, persist
        self.lock = threading.Lock()
        self.st = fresh_state()
        if persist and os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    self.st.update(json.load(f))
                log("تم استرجاع الحالة السابقة (paper_state.json)")
            except Exception as e:
                log(f"تعذر قراءة الحالة، نبدأ من جديد: {e}")
        self.last_engine = 0.0
        self.view = {"status": "يبدأ...", "decision": "—", "stage": "—", "reason": "", "atr": 0, "flow": 0,
                     "bid": None, "ask": None, "updated": None, "blocked": ""}

    # ---------- حفظ ----------
    def save(self):
        if not self.persist:
            return
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log(f"تعذر حفظ الحالة: {e}")

    def append_trade(self, tr):
        self.st["trades"].append(tr)
        if not self.persist:
            return
        new = not os.path.exists(TRADES_FILE)
        try:
            with open(TRADES_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["opened", "closed", "side", "lots", "entry", "sl", "tp1", "tp2", "tp3",
                                "exits", "pnl_usd", "balance"])
                w.writerow([tr["opened"], tr["closed"], tr["side"], tr["lots"], tr["entry"], tr["sl0"],
                            *tr["tps"], tr["exits"], tr["pnl"], tr["balance"]])
        except Exception as e:
            log(f"تعذر كتابة paper_trades.csv: {e}")

    # ---------- الصفقة ----------
    def open_position(self, decision, x, t, now):
        d = 1 if decision == "BUY" else -1
        entry = t["ask"] if d > 0 else t["bid"]
        plan = x["plan"]
        sl = plan["sl"]
        tps = [plan.get("tp1"), plan.get("tp2"), plan.get("tp3")]
        if any(v is None for v in tps):
            return
        risk = (entry - sl) * d
        if risk <= 0 or any((tp - entry) * d <= 0 for tp in tps):
            log(f"{decision} تجاهل: مستويات غير منطقية (entry {entry:.2f}, SL {sl:.2f})")
            return
        bal = self.st["balance"]
        lots = bal * CFG["risk_pct"] / 100.0 / (risk * CFG["contract"])
        lots = min(lots, bal * CFG["max_leverage"] / (entry * CFG["contract"]))
        lots = math.floor(lots / CFG["min_lot"] + 1e-9) * CFG["min_lot"]
        if lots < CFG["min_lot"]:
            log(f"{decision} تجاهل: الحجم أقل من الحد الأدنى")
            return
        self.st["position"] = {
            "dir": d, "side": decision, "entry": entry, "sl": sl, "sl0": sl, "tps": tps,
            "vol": lots, "vol0": lots, "tp_hit": 0, "realized": 0.0, "exits": [],
            "opened": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": x["reason"],
        }
        log(f"▶ فتح {decision} {lots:.2f} لوت @ {entry:.2f} | SL {sl:.2f} | TP {tps[0]:.2f}/{tps[1]:.2f}/{tps[2]:.2f}")
        self.save()

    def close_part(self, vol, price, label):
        pos = self.st["position"]
        pnl = (price - pos["entry"]) * pos["dir"] * vol * CFG["contract"]
        self.st["balance"] += pnl
        pos["realized"] += pnl
        pos["vol"] -= vol
        pos["exits"].append(f"{label}@{price:.2f}")
        log(f"■ {label} {vol:.2f} لوت @ {price:.2f}  ربح/خسارة {pnl:+.2f}$")

    def finish(self, now):
        pos = self.st["position"]
        bal = self.st["balance"]
        self.st["peak"] = max(self.st["peak"], bal)
        self.st["max_dd"] = max(self.st["max_dd"], (self.st["peak"] - bal) / self.st["peak"] * 100)
        total = pos["realized"]
        self.append_trade({
            "opened": pos["opened"], "closed": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "side": pos["side"], "lots": round(pos["vol0"], 2), "entry": round(pos["entry"], 2),
            "sl0": round(pos["sl0"], 2), "tps": [round(v, 2) for v in pos["tps"]],
            "exits": " ".join(pos["exits"]), "pnl": round(total, 2), "balance": round(bal, 2)})
        if total < 0:
            self.st["consec_losses"] += 1
            if self.st["consec_losses"] >= eng.MAX_CONSECUTIVE_LOSSES:
                self.st["pause_until"] = now + eng.CIRCUIT_BREAKER_PAUSE_HOURS * 3600
                self.st["consec_losses"] = 0
                log(f"⛔ توقف مؤقت {eng.CIRCUIT_BREAKER_PAUSE_HOURS}س بعد {eng.MAX_CONSECUTIVE_LOSSES} خسائر متتالية")
        else:
            self.st["consec_losses"] = 0
        self.st["position"] = None
        log(f"● أُغلقت الصفقة: {total:+.2f}$ | الرصيد {bal:.2f}$")
        self.save()

    def manage_position(self, t, now):
        pos = self.st["position"]
        if not pos:
            return
        d = pos["dir"]
        px = t["bid"] if d > 0 else t["ask"]          # سعر الإغلاق الفعلي
        if (d > 0 and px <= pos["sl"]) or (d < 0 and px >= pos["sl"]):
            label = "SL" if pos["tp_hit"] == 0 else ("BE" if pos["tp_hit"] == 1 else "LOCK")
            self.close_part(pos["vol"], px, label)    # ستوب: ينفذ بالسعر الحالي (قد يكون أسوأ من SL)
            self.finish(now)
            return
        while pos["tp_hit"] < 3:
            tp = pos["tps"][pos["tp_hit"]]
            if not ((px >= tp) if d > 0 else (px <= tp)):
                break
            n = pos["tp_hit"] + 1
            vol = pos["vol"] if n == 3 else min(pos["vol"], pos["vol0"] / 3.0)
            self.close_part(vol, tp, f"TP{n}")
            pos["tp_hit"] = n
            if n == 1:
                pos["sl"] = pos["entry"]                # بعد TP1: الستوب لنقطة الدخول
            elif n == 2:
                pos["sl"] = pos["tps"][0]               # بعد TP2: الستوب عند TP1
            if pos["vol"] <= 1e-9:
                self.finish(now)
                return
        self.save()

    # ---------- القرار ----------
    def blocked_reason(self, decision, x, candles, px, now):
        age = now - candles[-1]["open_time"]
        if age > eng.interval_seconds() * eng.STALE_DATA_BARS:
            return f"بيانات قديمة ({age:.0f}ث)"
        if eng.LATE_MOVE_FILTER and x["atr"] > 0:
            recent = candles[-eng.LATE_MOVE_BARS:]
            moved = (max(z["high"] for z in recent) - px) if decision == "SELL" else (px - min(z["low"] for z in recent))
            if moved > x["atr"] * eng.LATE_MOVE_ATR:
                return f"فات القطار ({moved / x['atr']:.1f} ATR)"
        if self.feed.live and eng.NEWS_FILTER:
            nm = eng.news_blackout()
            if nm:
                return f"خبر: {nm}"
        if eng.HTF_FILTER and eng.htf_blocks("XAUUSD", decision):
            return "الأطر الأكبر تعاكس"
        return ""

    def run_engine(self, t, now):
        candles = self.feed.klines(CFG["tf"], 250)
        if len(candles) < 40:
            self.view["status"] = "شموع غير كافية"
            return
        px = t["mid"]
        x = eng.compute_engine(candles, px, self.st["sym"])
        v = self.view
        v.update(decision=x["decision"], stage=(x.get("timing") or {}).get("stage", "—"),
                 reason=x["reason"], atr=x["atr"], flow=x["flow"], blocked="")
        if x["decision"] in ("BUY", "SELL") and x["plan"] and self.st["position"] is None \
                and now >= self.st["pause_until"]:
            why = self.blocked_reason(x["decision"], x, candles, px, now)
            if why:
                v["blocked"] = why
                return
            self.open_position(x["decision"], x, t, now)

    def step(self):
        now = self.feed.now()
        t = self.feed.tick()
        v = self.view
        v.update(bid=t["bid"], ask=t["ask"], updated=now)
        if self.feed.live:
            if eng.is_forex_market_closed():
                v["status"] = "سوق الذهب مغلق (عطلة)"
                return
            if t["age"] > CFG["max_tick_age"]:
                v["status"] = f"السعر قديم ({t['age']:.0f}ث) — لا تداول"
                return
        v["status"] = "يخدم" if now >= self.st["pause_until"] else "متوقف مؤقتاً (خسائر متتالية)"
        self.manage_position(t, now)
        if (not self.feed.live) or now - self.last_engine >= CFG["engine_seconds"]:
            self.last_engine = now
            self.run_engine(t, now)

    # ---------- إحصائيات لللوحة ----------
    def snapshot(self):
        st = self.st
        tr = st["trades"]
        wins = [x["pnl"] for x in tr if x["pnl"] > 0]
        losses = [-x["pnl"] for x in tr if x["pnl"] < 0]
        pos = st["position"]
        unreal = 0.0
        if pos and self.view["bid"] is not None:
            px = self.view["bid"] if pos["dir"] > 0 else self.view["ask"]
            unreal = (px - pos["entry"]) * pos["dir"] * pos["vol"] * CFG["contract"]
        return {
            "view": self.view,
            "balance": round(st["balance"], 2), "equity": round(st["balance"] + unreal, 2),
            "start": CFG["start_equity"], "max_dd": round(st["max_dd"], 2),
            "n": len(tr), "win_rate": round(len(wins) / len(tr) * 100, 1) if tr else 0,
            "pf": round(sum(wins) / sum(losses), 2) if losses else (None if not wins else 99.0),
            "pnl": round(sum(x["pnl"] for x in tr), 2),
            "position": pos, "unreal": round(unreal, 2),
            "paused": time.time() < st["pause_until"],
            "trades": tr[-25:][::-1],
        }


# ----------------------------- اللوحة المحلية -----------------------------
PAGE = """<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>XAU Live Paper Bot</title>
<style>
body{font-family:Segoe UI,Tahoma,sans-serif;background:#0f1419;color:#e6e6e6;margin:0;padding:16px}
h1{font-size:20px;margin:0 0 12px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.c{background:#1a2129;border-radius:10px;padding:12px}.c small{color:#8b98a5;display:block;margin-bottom:4px}
.c b{font-size:20px}.up{color:#3ddc84}.dn{color:#ff5c5c}.w{background:#1a2129;border-radius:10px;padding:12px;margin-top:10px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:6px 8px;border-bottom:1px solid #2a343e;text-align:right;white-space:nowrap}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;background:#2a343e}</style>
<h1>🥇 XAU Live Paper Bot <span class="tag">تجريبي — بدون مال حقيقي</span></h1>
<div class="g" id="topbar"></div><div class="w" id="posbox"></div><div class="w" id="decbox"></div>
<div class="w"><b>آخر الصفقات</b><table id="trtbl"></table></div>
<script>
const f=(v,d=2)=>v==null?'—':Number(v).toFixed(d), cl=v=>v>0?'up':v<0?'dn':'';
async function R(){try{const s=await (await fetch('/api/state')).json(),v=s.view;
document.getElementById('topbar').innerHTML=`
<div class=c><small>الحالة</small><b>${v.status}</b></div>
<div class=c><small>Bid / Ask</small><b>${f(v.bid)} / ${f(v.ask)}</b></div>
<div class=c><small>الرصيد</small><b>${f(s.balance)}$</b></div>
<div class=c><small>الأسهم (مع المفتوحة)</small><b class="${cl(s.equity-s.start)}">${f(s.equity)}$</b></div>
<div class=c><small>إجمالي الربح</small><b class="${cl(s.pnl)}">${f(s.pnl)}$</b></div>
<div class=c><small>صفقات / نجاح</small><b>${s.n} / ${f(s.win_rate,1)}%</b></div>
<div class=c><small>PF</small><b>${s.pf==null?'—':s.pf}</b></div>
<div class=c><small>أكبر تراجع</small><b>${f(s.max_dd,1)}%</b></div>`;
const p=s.position;document.getElementById('posbox').innerHTML=p?`<b>صفقة مفتوحة:</b> ${p.side} ${f(p.vol)} لوت @ ${f(p.entry)} | SL ${f(p.sl)} | TP ${p.tps.map(x=>f(x)).join(' / ')} | تحقق TP: ${p.tp_hit}
 | عائم <b class="${cl(s.unreal)}">${f(s.unreal)}$</b> | محقق ${f(p.realized)}$`:'<b>لا توجد صفقة مفتوحة</b>';
document.getElementById('decbox').innerHTML=`<b>المحرك:</b> ${v.decision} — مرحلة ${v.stage} — ATR ${f(v.atr)} — Flow ${f(v.flow,1)}<br>${v.reason}`+(v.blocked?`<br>⛔ محجوب: ${v.blocked}`:'');
document.getElementById('trtbl').innerHTML='<tr><th>فتح</th><th>نوع</th><th>لوت</th><th>دخول</th><th>خروج</th><th>ربح $</th></tr>'+s.trades.map(t=>`<tr><td>${t.opened}</td><td>${t.side}</td><td>${t.lots}</td><td>${t.entry}</td><td>${t.exits}</td><td class="${cl(t.pnl)}">${t.pnl}</td></tr>`).join('');
}catch(e){}}R();setInterval(R,2000);
</script></html>"""


def serve(bot):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api/state"):
                body = json.dumps(bot.snapshot(), ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            else:
                body, ctype = PAGE.encode("utf-8"), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"اللوحة: http://localhost:{CFG['port']}")


# ----------------------------- التشغيل -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", help="ملف شموع قديم لاختبار البوت")
    ap.add_argument("--tf", default=None, help="إطار الشموع (افتراضي 1m)")
    ap.add_argument("--reset", action="store_true", help="مسح الحالة والبدء من رصيد جديد")
    ap.add_argument("--port", type=int, default=CFG["port"])
    a = ap.parse_args()
    CFG["port"] = a.port
    if a.tf:
        CFG["tf"] = a.tf
    eng.INTERVAL = CFG["tf"]          # المحرك يحسب أعمار الشموع حسب هذا الإطار
    if a.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    if a.replay:
        eng.NEWS_FILTER = False       # الأخبار مرتبطة بالوقت الحقيقي، لا معنى لها في الإعادة
        feed = ReplayFeed(a.replay, CFG["tf"])
        eng.get_klines = lambda sym, interval, limit=300: feed.klines(interval, limit)
        bot = Bot(feed, persist=False)
        eng.log = lambda *_: None
        n = 0
        while True:
            bot.step()
            n += 1
            if not feed.advance():
                break
        s = bot.snapshot()
        print(f"\nانتهت الإعادة: {s['n']} صفقة | نجاح {s['win_rate']}% | PF {s['pf']} | "
              f"ربح {s['pnl']:+.2f}$ | تراجع {s['max_dd']}% | رصيد {s['balance']}$")
        for tr in bot.st["trades"]:
            print(tr["opened"], tr["side"], tr["lots"], tr["entry"], tr["exits"], tr["pnl"])
        return

    feed = BiquoteFeed()
    eng.get_klines = lambda sym, interval, limit=300: feed.klines(interval, limit)
    eng.get_ticker_price = lambda pair: feed.tick()["mid"]
    bot = Bot(feed)
    serve(bot)
    log("بدأ البوت التجريبي (Paper). اضغط Ctrl+C للإيقاف.")
    fails = 0
    try:
        while True:
            t0 = time.time()
            try:
                bot.step()
                fails = 0
            except Exception as e:
                fails += 1
                bot.view["status"] = f"خطأ اتصال ({fails}): {e}"[:120]
                if fails in (1, 5) or fails % 30 == 0:
                    log(f"خطأ: {e}")
            time.sleep(max(0.2, CFG["tick_seconds"] - (time.time() - t0)))
    except KeyboardInterrupt:
        bot.save()
        log("تم الإيقاف وحفظ الحالة.")


if __name__ == "__main__":
    main()
