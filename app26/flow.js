/* خريطة التدفق: Order Flow + Volume Profile حقيقيان من Binance (XAUUSDT)
   يستقبل نفس الرسائل الحية اللي يستعملها تبويب Gold OF، بدون اتصال إضافي. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var MAXSNAP = 300;            // 5 دقائق بمعدل لقطة كل ثانية
  var S = {
    vp: new Map(),              // مفتاح = سعر×10 (دقة 0.1) → {b,s}
    snaps: [],                  // [{t,mid,lv:[[p,q,side]]}]
    trades: [], tape: [], events: [],
    cvd: [], cum: 0, buyVol: 0, sellVol: 0, vwN: 0, vwD: 0,
    bid: 0, ask: 0, book: { b: [], a: [] }, lastDepth: 0, lastTrade: 0,
    walls: new Map(), tried: false
  };

  function fmt(x, d) { return Number.isFinite(x) ? x.toFixed(d == null ? 2 : d) : '—'; }
  function stepVal() { var e = $('flStep'); return e ? parseFloat(e.value) || 0.5 : 0.5; }
  function rangeVal() { var e = $('flRange'); return e ? parseFloat(e.value) || 20 : 20; }
  function bigThr() { var e = $('flBig'); return e ? parseFloat(e.value) || 5 : 5; }
  function binOf(p, st) { return Math.round(p / st) * st; }
  function active() { var p = $('page-flow'); return p && p.classList.contains('active'); }

  /* ---------- استقبال البيانات ---------- */
  function feedDepth(d) {
    var rb = d.b || d.bids || [], ra = d.a || d.asks || [];
    var b = rb.map(function (x) { return Array.isArray(x) ? { p: +x[0], q: +x[1] } : { p: +x.price, q: +x.qty }; }).filter(function (x) { return x.q > 0; }).sort(function (u, v) { return v.p - u.p; });
    var a = ra.map(function (x) { return Array.isArray(x) ? { p: +x[0], q: +x[1] } : { p: +x.price, q: +x.qty }; }).filter(function (x) { return x.q > 0; }).sort(function (u, v) { return u.p - v.p; });
    if (!b.length || !a.length) return;
    S.book = { b: b, a: a }; S.bid = b[0].p; S.ask = a[0].p; S.lastDepth = Date.now();
  }
  function feedTrade(d) {
    var p = +(d.p != null ? d.p : d.price), q = +(d.q != null ? d.q : d.qty), m = !!d.m;
    if (!(p > 0) || !(q > 0)) return;
    var buy = !m;                                  // m=true: المشتري صانع، البائع هو المبادر
    var k = Math.round(p * 10), c = S.vp.get(k);
    if (!c) { c = { b: 0, s: 0 }; S.vp.set(k, c); }
    if (buy) { c.b += q; S.buyVol += q; S.cum += q; } else { c.s += q; S.sellVol += q; S.cum -= q; }
    S.vwN += p * q; S.vwD += q; S.lastTrade = Date.now();
    var t = Date.now();
    S.trades.push({ t: t, p: p, q: q, buy: buy });
    if (S.trades.length > 2500) S.trades.splice(0, 500);
    S.tape.unshift({ t: t, p: p, q: q, buy: buy }); if (S.tape.length > 40) S.tape.pop();
    if (q >= bigThr()) addEvent(buy ? 'buy' : 'sell', (buy ? 'شراء ضخم منفذ: ' : 'بيع ضخم منفذ: ') + fmt(p) + ' @ ' + fmt(q));
  }
  function addEvent(kind, text) {
    S.events.unshift({ t: Date.now(), kind: kind, text: text }); if (S.events.length > 60) S.events.pop();
  }
  function hook() {
    var od = window.onDepth, ot = window.onTrade;
    if (typeof od === 'function') window.onDepth = function (d) { try { feedDepth(d); } catch (e) { } return od.apply(this, arguments); };
    if (typeof ot === 'function') window.onTrade = function (d) { try { feedTrade(d); } catch (e) { } return ot.apply(this, arguments); };
  }
  window.__flowFeed = { depth: feedDepth, trade: feedTrade, state: S };   // للاختبار

  /* ---------- لقطة كل ثانية + جدران السيولة ---------- */
  function sample() {
    if (!S.book.b.length) return;
    var lv = [], i;
    for (i = 0; i < S.book.b.length; i++) lv.push([S.book.b[i].p, S.book.b[i].q, 1]);
    for (i = 0; i < S.book.a.length; i++) lv.push([S.book.a[i].p, S.book.a[i].q, -1]);
    var mid = (S.bid + S.ask) / 2;
    S.snaps.push({ t: Date.now(), mid: mid, lv: lv });
    if (S.snaps.length > MAXSNAP) S.snaps.shift();
    S.cvd.push({ t: Date.now(), v: S.cum });
    if (S.cvd.length > MAXSNAP) S.cvd.shift();
    // جدار قوي: مستوى كميته >= 4× الوسيط ولا يقل عن الحد الأدنى
    var qs = lv.map(function (x) { return x[1]; }).sort(function (u, v) { return u - v; });
    var med = qs[Math.floor(qs.length / 2)] || 0, now = Date.now(), thr = Math.max(4 * med, bigThr() * 10);
    lv.forEach(function (x) {
      if (x[1] >= thr) {
        var key = (x[2] > 0 ? 'B' : 'A') + x[0].toFixed(2), last = S.walls.get(key) || 0;
        if (now - last > 30000) {
          S.walls.set(key, now);
          addEvent(x[2] > 0 ? 'buy' : 'sell', (x[2] > 0 ? 'جدار شراء قوي عند ' : 'جدار بيع قوي عند ') + fmt(x[0]) + ' — حجم ' + fmt(x[1], 0));
        }
      }
    });
  }

  /* ---------- Volume Profile ---------- */
  function profile(st) {
    var m = new Map(), tot = 0;
    S.vp.forEach(function (c, k) {
      var pb = binOf(k / 10, st), key = pb.toFixed(4), o = m.get(key);
      if (!o) { o = { p: pb, b: 0, s: 0 }; m.set(key, o); }
      o.b += c.b; o.s += c.s; tot += c.b + c.s;
    });
    var arr = Array.from(m.values()).sort(function (u, v) { return u.p - v.p; });
    var poc = null; arr.forEach(function (o) { if (!poc || o.b + o.s > poc.b + poc.s) poc = o; });
    var vah = null, val = null;
    if (poc) {                                   // منطقة القيمة 70%
      var idx = arr.indexOf(poc), lo = idx, hi = idx, acc = poc.b + poc.s;
      while (acc < tot * 0.7 && (lo > 0 || hi < arr.length - 1)) {
        var up = hi < arr.length - 1 ? arr[hi + 1].b + arr[hi + 1].s : -1, dn = lo > 0 ? arr[lo - 1].b + arr[lo - 1].s : -1;
        if (up >= dn) { hi++; acc += up; } else { lo--; acc += dn; }
      }
      vah = arr[hi].p; val = arr[lo].p;
    }
    return { arr: arr, map: m, poc: poc ? poc.p : null, vah: vah, val: val, tot: tot };
  }

  /* ---------- الرسم ---------- */
  function ramp(t) {
    var st = [[0, [8, 16, 32]], [.25, [11, 58, 122]], [.5, [18, 160, 181]], [.75, [242, 201, 76]], [1, [255, 255, 255]]];
    t = Math.max(0, Math.min(1, t));
    for (var i = 1; i < st.length; i++) if (t <= st[i][0]) {
      var a = st[i - 1], b = st[i], f = (t - a[0]) / (b[0] - a[0]);
      return 'rgb(' + Math.round(a[1][0] + (b[1][0] - a[1][0]) * f) + ',' + Math.round(a[1][1] + (b[1][1] - a[1][1]) * f) + ',' + Math.round(a[1][2] + (b[1][2] - a[1][2]) * f) + ')';
    }
    return '#fff';
  }
  function setup(cv) {
    var r = cv.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    var w = Math.max(200, Math.round(r.width)), h = Math.round(cv.dataset.h || 300);
    if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; cv.style.height = h + 'px'; }
    var ctx = cv.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, W: w, H: h };
  }

  function drawHeat(P) {
    var cv = $('flHeat'); if (!cv) return;
    var o = setup(cv), ctx = o.ctx, W = o.W, H = o.H, AX = 58, plotW = W - AX;
    ctx.fillStyle = '#060d1a'; ctx.fillRect(0, 0, W, H);
    if (!S.snaps.length) { ctx.fillStyle = '#8e9bb4'; ctx.font = '13px system-ui'; ctx.fillText('بانتظار بيانات Binance...', 14, 26); return; }
    var st = stepVal(), R = rangeVal(), c = (S.bid + S.ask) / 2, lo = c - R, hi = c + R;
    var Y = function (p) { return H - 4 - (p - lo) * (H - 8) / (hi - lo); };
    var rowH = Math.max(2, (st) * (H - 8) / (hi - lo));
    var colW = plotW / Math.max(120, S.snaps.length), now = Date.now(), winSec = plotW / colW;
    // سقف الألوان: المئين 95 للقيم الظاهرة
    var vals = []; S.snaps.forEach(function (s) { s.lv.forEach(function (x) { if (x[0] >= lo && x[0] <= hi) vals.push(x[1]); }); });
    vals.sort(function (u, v) { return u - v; });
    var qmax = vals.length ? vals[Math.floor(vals.length * 0.95)] || vals[vals.length - 1] : 1; if (!(qmax > 0)) qmax = 1;
    S.snaps.forEach(function (s, i) {
      var x = plotW - (S.snaps.length - i) * colW, m = new Map();
      s.lv.forEach(function (l) { if (l[0] < lo || l[0] > hi) return; var b = binOf(l[0], st).toFixed(4), e = m.get(b); if (!e) { e = { p: binOf(l[0], st), q: 0 }; m.set(b, e); } e.q += l[1]; });
      m.forEach(function (e) { ctx.fillStyle = ramp(Math.sqrt(e.q / (qmax * (st / 0.1 > 1 ? Math.min(3, st / 0.1) : 1)))); ctx.fillRect(x, Y(e.p) - rowH / 2, colW + 0.6, rowH); });
    });
    // خط السعر
    ctx.strokeStyle = 'rgba(255,255,255,.85)'; ctx.lineWidth = 1.2; ctx.beginPath();
    S.snaps.forEach(function (s, i) { var x = plotW - (S.snaps.length - i) * colW + colW / 2, y = Y(s.mid); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke();
    // فقاعات الصفقات
    S.trades.forEach(function (t) {
      var age = (now - t.t) / 1000; if (age > winSec || t.p < lo || t.p > hi) return;
      var x = plotW - age * colW, r = 2 + Math.min(13, Math.sqrt(t.q) * 1.8);
      ctx.fillStyle = t.buy ? 'rgba(53,223,134,.62)' : 'rgba(255,95,120,.62)';
      ctx.strokeStyle = t.buy ? 'rgba(53,223,134,1)' : 'rgba(255,95,120,1)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(x, Y(t.p), r, 0, 6.2832); ctx.fill(); ctx.stroke();
    });
    // POC / VWAP / VAH / VAL
    function line(p, col, dash, label) {
      if (p == null || p < lo || p > hi) return;
      ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.setLineDash(dash); ctx.beginPath(); ctx.moveTo(0, Y(p)); ctx.lineTo(plotW, Y(p)); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = col; ctx.font = '10.5px system-ui'; ctx.fillText(label + ' ' + fmt(p), 6, Y(p) - 3);
    }
    line(P.poc, '#ffc857', [6, 4], 'POC'); line(S.vwD ? S.vwN / S.vwD : null, '#b49bff', [2, 3], 'VWAP');
    line(P.vah, 'rgba(98,160,255,.8)', [3, 4], 'VAH'); line(P.val, 'rgba(98,160,255,.8)', [3, 4], 'VAL');
    // محور السعر
    ctx.fillStyle = '#0b1322'; ctx.fillRect(plotW, 0, AX, H);
    ctx.fillStyle = '#9aa9c4'; ctx.font = '11px system-ui';
    var tick = R > 30 ? 10 : R > 12 ? 5 : 2;
    for (var p = Math.ceil(lo / tick) * tick; p <= hi; p += tick) ctx.fillText(fmt(p, 1), plotW + 5, Y(p) + 4);
    var cy = Y(c); ctx.fillStyle = '#2563eb'; ctx.fillRect(plotW, cy - 9, AX, 18); ctx.fillStyle = '#fff'; ctx.font = 'bold 11px system-ui'; ctx.fillText(fmt(c), plotW + 5, cy + 4);
    // زمن
    ctx.fillStyle = '#6f7d99'; ctx.font = '10px system-ui';
    [0, 60, 120, 180, 240].forEach(function (a) { var d = new Date(now - a * 1000); ctx.fillText(d.toTimeString().slice(0, 8), Math.max(2, plotW - a * colW - 44), H - 6); });
  }

  function drawCVD() {
    var cv = $('flCvd'); if (!cv) return;
    var o = setup(cv), ctx = o.ctx, W = o.W, H = o.H; ctx.fillStyle = '#060d1a'; ctx.fillRect(0, 0, W, H);
    if (S.cvd.length < 2) return;
    var mn = 1e18, mx = -1e18; S.cvd.forEach(function (p) { mn = Math.min(mn, p.v); mx = Math.max(mx, p.v); });
    mn = Math.min(mn, 0); mx = Math.max(mx, 0); if (mx === mn) mx = mn + 1;
    var Y = function (v) { return H - 8 - (v - mn) * (H - 16) / (mx - mn); }, X = function (i) { return 6 + i * (W - 12) / (MAXSNAP - 1); };
    var off = MAXSNAP - S.cvd.length;
    ctx.strokeStyle = '#26344f'; ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(W, Y(0)); ctx.stroke();
    var up = S.cvd[S.cvd.length - 1].v >= 0, col = up ? '53,223,134' : '255,95,120';
    ctx.beginPath(); S.cvd.forEach(function (p, i) { var x = X(i + off), y = Y(p.v); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.strokeStyle = 'rgb(' + col + ')'; ctx.lineWidth = 1.6; ctx.stroke();
    ctx.lineTo(X(MAXSNAP - 1), Y(0)); ctx.lineTo(X(off), Y(0)); ctx.closePath(); ctx.fillStyle = 'rgba(' + col + ',.16)'; ctx.fill();
  }

  function renderLadder(P) {
    var host = $('flLadder'); if (!host) return;
    var st = stepVal(), c = (S.bid + S.ask) / 2; if (!(c > 0)) { host.innerHTML = '<div class="empty">بانتظار البيانات...</div>'; return; }
    var rows = Math.max(12, Math.min(26, Math.round(10 / st) )) , top = binOf(c, st) + rows * st, i;
    var cob = new Map(), last = S.book;
    last.b.forEach(function (x) { var k = binOf(x.p, st).toFixed(4); cob.set(k, { q: (cob.get(k) ? cob.get(k).q : 0) + x.q, side: 1 }); });
    last.a.forEach(function (x) { var k = binOf(x.p, st).toFixed(4); cob.set(k, { q: (cob.get(k) ? cob.get(k).q : 0) + x.q, side: -1 }); });
    var maxV = 1, maxD = 1, maxC = 1;
    var list = [];
    for (i = 0; i <= rows * 2; i++) {
      var p = top - i * st, key = p.toFixed(4), v = P.map.get(key), cb = cob.get(key);
      var tot = v ? v.b + v.s : 0, dl = v ? v.b - v.s : 0;
      maxV = Math.max(maxV, tot); maxD = Math.max(maxD, Math.abs(dl)); if (cb) maxC = Math.max(maxC, cb.q);
      list.push({ p: p, tot: tot, dl: dl, cb: cb, isP: Math.abs(p - binOf(c, st)) < st / 2 });
    }
    var h = '<div class="flRow flHead"><span>DELTA</span><span>VP</span><span>COB</span><span>السعر</span></div>';
    list.forEach(function (r) {
      var dw = Math.round(Math.abs(r.dl) / maxD * 100), vw = Math.round(r.tot / maxV * 100), cw = r.cb ? Math.round(r.cb.q / maxC * 100) : 0;
      var dcol = r.dl >= 0 ? '53,223,134' : '255,95,120', ccol = r.cb && r.cb.side > 0 ? '53,223,134' : '255,95,120';
      h += '<div class="flRow' + (r.isP ? ' flNow' : '') + '">' +
        '<span class="flC" style="background:linear-gradient(to left,rgba(' + dcol + ',.55) ' + dw + '%,transparent ' + dw + '%)">' + (r.tot ? (r.dl >= 0 ? '+' : '') + fmt(r.dl, 1) : '') + '</span>' +
        '<span class="flC" style="background:linear-gradient(to left,rgba(98,160,255,.45) ' + vw + '%,transparent ' + vw + '%)">' + (r.tot ? fmt(r.tot, 1) : '') + '</span>' +
        '<span class="flC" style="background:linear-gradient(to left,rgba(' + ccol + ',.5) ' + cw + '%,transparent ' + cw + '%)">' + (r.cb ? fmt(r.cb.q, 0) : '') + '</span>' +
        '<span class="flP">' + fmt(r.p, st < 1 ? 2 : 1) + '</span></div>';
    });
    var keep = host.scrollTop;
    host.innerHTML = h;
    var nowRow = host.querySelector('.flNow');
    if (!host.dataset.scrolled && nowRow) { host.scrollTop = Math.max(0, nowRow.offsetTop - host.clientHeight / 2); host.dataset.scrolled = '1'; } else host.scrollTop = keep;
  }

  function tm(t) { return new Date(t).toTimeString().slice(0, 8); }
  function renderLists() {
    var tp = $('flTape'); if (tp) tp.innerHTML = S.tape.slice(0, 25).map(function (x) { return '<div class="flTr ' + (x.buy ? 'g' : 'r') + '"><span>' + fmt(x.q, 3) + '</span><span>' + fmt(x.p) + '</span><span>' + tm(x.t) + '</span></div>'; }).join('') || '<div class="empty">—</div>';
    var ev = $('flEvents'); if (ev) ev.innerHTML = S.events.slice(0, 25).map(function (x) { return '<div class="flTr ' + (x.kind === 'buy' ? 'g' : 'r') + '"><span>' + x.text + '</span><span>' + tm(x.t) + '</span></div>'; }).join('') || '<div class="empty">ما فيه أحداث بعد</div>';
  }

  function renderKPI(P) {
    var set = function (id, v, cls) { var e = $(id); if (e) { e.textContent = v; if (cls) e.className = 'value ' + cls; } };
    var delta = S.buyVol - S.sellVol, imb = 0, bq = 0, aq = 0;
    S.book.b.forEach(function (x) { bq += x.q; }); S.book.a.forEach(function (x) { aq += x.q; });
    imb = bq + aq ? bq / (bq + aq) * 100 : 50;
    set('flBid', fmt(S.bid), 'green'); set('flAsk', fmt(S.ask), 'red'); set('flSpr', fmt(S.ask - S.bid, 3));
    set('flPoc', fmt(P.poc)); set('flVwap', S.vwD ? fmt(S.vwN / S.vwD) : '—');
    set('flCvdV', (S.cum >= 0 ? '+' : '') + fmt(S.cum, 1), S.cum >= 0 ? 'green' : 'red');
    set('flBuy', fmt(S.buyVol, 1), 'green'); set('flSell', fmt(S.sellVol, 1), 'red');
    set('flBook', fmt(imb, 0) + '% ' + (imb >= 50 ? 'شراء' : 'بيع'), imb >= 50 ? 'green' : 'red');
    set('flDeltaV', (delta >= 0 ? '+' : '') + fmt(delta, 1), delta >= 0 ? 'green' : 'red');
    var live = Date.now() - Math.max(S.lastDepth, S.lastTrade) < 6000, st = $('flStatus');
    if (st) { st.textContent = live ? 'مباشر • Binance Futures' : 'غير متصل'; st.style.color = live ? 'var(--green)' : 'var(--red)'; }
  }

  function renderAll() {
    if (!active()) return;
    var P = profile(stepVal());
    renderKPI(P); drawHeat(P); drawCVD(); renderLadder(P); renderLists();
    if (!S.tried && Date.now() - Math.max(S.lastDepth, S.lastTrade) > 6000) {
      S.tried = true; var b = $('ofConnect'); if (b) b.click();    // يشغّل الاتصال إذا ما كانش شغال
    }
  }

  setInterval(sample, 1000);
  setInterval(renderAll, 700);
  document.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('#tabbar button')) setTimeout(renderAll, 80); });
  ['flStep', 'flRange'].forEach(function (id) { var e = $(id); if (e) e.addEventListener('change', function () { var l = $('flLadder'); if (l) delete l.dataset.scrolled; renderAll(); }); });
  var rs = $('flReset'); if (rs) rs.addEventListener('click', function () { S.vp.clear(); S.trades = []; S.cum = 0; S.buyVol = 0; S.sellVol = 0; S.vwN = 0; S.vwD = 0; S.cvd = []; S.events = []; renderAll(); });
  window.addEventListener('resize', renderAll);
})();
