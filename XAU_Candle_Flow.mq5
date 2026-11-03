//+------------------------------------------------------------------+
//| XAU_Candle_Flow.mq5  v2.00   (مطوّر من XAU_Straddle_Scalper)       |
//| - صفقة واحدة فقط في نفس الوقت                                       |
//| - الدخول فقط في أول ثواني الشمعة الجديدة (مش في وسطها)              |
//| - الاتجاه من تحليل الشمعة اللي سكّرت:                                |
//|     Order Flow  = دلتا التكّات (مشترين - بائعين)                     |
//|     Volume Profile = نقطة POC (أكثر سعر تداولو) + مكان الإغلاق منها   |
//|     قوة الشمعة = جسم + مكان الإغلاق في المدى                          |
//| - SL حسب ATR + Break-even + Trailing + أقصى مدة بالشموع               |
//| ملاحظة: Exness ما يعطيش order flow حقيقي (DOM)، فالدلتا تقديرية من    |
//| اتجاه التكّات. للديمو فقط.                                           |
//+------------------------------------------------------------------+
#property copyright "Tito"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>

input group "=== الأمان ==="
input bool   InpEnableTrading   = true;     // false = ما يفتح شي
input bool   InpAllowReal       = false;    // false = يرفض الحساب الحقيقي
input ulong  InpMagic           = 28110202;
input double InpLot             = 0.01;
input double InpDailyLossUSD    = 5.0;      // قفل يومي (0 = بدون)
input int    InpMaxTradesPerDay = 30;
input double InpMaxSpread       = 0.50;     // لا يدخل إذا السبريد أكبر (سعر)
input double InpMaxSpreadPctSL  = 30.0;     // ولا إذا السبريد > هذه % من مسافة SL

input group "=== الشمعة والدخول ==="
input ENUM_TIMEFRAMES InpTF     = PERIOD_M5; // فريم الشمعة (البوت يشتغل عليه مهما كان فريم الشارت)
input int    InpEntryWindowSec  = 5;        // يدخل فقط في أول X ثانية من الشمعة
input int    InpMinTicks        = 30;       // أقل عدد تكّات في الشمعة السابقة للتحليل
input double InpMinRangeATR     = 0.60;     // الشمعة السابقة لازم مداها >= هذا × ATR

input group "=== Order Flow / Volume Profile ==="
input double InpMinDelta        = 0.15;     // أقل دلتا (مشترين-بائعين)/المجموع
input double InpMinClosePos     = 0.60;     // الإغلاق في أعلى/أسفل % من المدى
input double InpMinBody         = 0.30;     // جسم الشمعة >= % من المدى
input double InpBinSize         = 0.10;     // حجم خانة Volume Profile (سعر)
input bool   InpNeedPOC         = true;     // شراء: الإغلاق فوق POC / بيع: تحته
input bool   InpUseTrend        = true;     // فلتر اتجاه EMA20/EMA50 على فريم أعلى
input ENUM_TIMEFRAMES InpTrendTF = PERIOD_M15;

input group "=== الخروج ==="
input int    InpATRPeriod       = 14;
input double InpSLATR           = 1.2;      // SL = هذا × ATR
input double InpTPR             = 2.0;      // TP بمضاعف المخاطرة (0 = بدون TP)
input double InpBEatR           = 1.0;      // Break-even عند ربح = هذا × المخاطرة (0 = بدون)
input double InpTrailStartR     = 1.2;      // يبدأ Trailing عند هذا الربح × المخاطرة (0 = بدون)
input double InpTrailDistR      = 0.6;      // مسافة Trailing × المخاطرة
input int    InpMaxHoldBars     = 3;        // يسكّر الصفقة بعد X شموع (0 = بدون)
input int    InpDeviation       = 50;

CTrade  trade;
int     g_hATR = INVALID_HANDLE, g_hE20 = INVALID_HANDLE, g_hE50 = INVALID_HANDLE;
datetime g_lastBar = 0;
bool    g_evalPending = false;
double  g_risk = 0.0;
string  g_status = "جاهز", g_lastSig = "-";
int     g_dTrades = 0, g_dWins = 0;
double  g_dNet = 0.0;
ulong   g_lastStatMs = 0;

//+------------------------------------------------------------------+
double NormVol(double v)
{
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double st = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(st <= 0) st = 0.01;
   v = MathFloor(v / st + 1e-9) * st;
   if(v < mn) v = mn;
   if(v > mx) v = mx;
   return NormalizeDouble(v, 2);
}

double MinStopDist()
{
   double pt = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   long lvl = MathMax(SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                      SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL));
   return (double)lvl * pt + 2.0 * pt;
}

//+------------------------------------------------------------------+
int OnInit()
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL && !InpAllowReal)
   {
      Alert("XAU_Candle_Flow: حساب حقيقي. الـ EA للديمو فقط.");
      return INIT_FAILED;
   }
   trade.SetExpertMagicNumber((long)InpMagic);
   trade.SetDeviationInPoints(InpDeviation);
   trade.SetTypeFillingBySymbol(_Symbol);
   g_hATR = iATR(_Symbol, InpTF, InpATRPeriod);
   g_hE20 = iMA(_Symbol, InpTrendTF, 20, 0, MODE_EMA, PRICE_CLOSE);
   g_hE50 = iMA(_Symbol, InpTrendTF, 50, 0, MODE_EMA, PRICE_CLOSE);
   if(g_hATR == INVALID_HANDLE || g_hE20 == INVALID_HANDLE || g_hE50 == INVALID_HANDLE)
   {
      Alert("XAU_Candle_Flow: فشل إنشاء المؤشرات");
      return INIT_FAILED;
   }
   g_lastBar = iTime(_Symbol, InpTF, 0);   // ما يدخل في وسط شمعة الإطلاق
   EventSetTimer(1);
   Print("XAU_Candle_Flow جاهز على ", _Symbol, " فريم ", EnumToString(InpTF));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
}

//+------------------------------------------------------------------+
int MyPositions()
{
   int c = 0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic) c++;
   }
   return c;
}

void RefreshStats()
{
   ulong now = GetTickCount64();
   if(now - g_lastStatMs < 2000) return;
   g_lastStatMs = now;
   datetime dayStart = StringToTime(TimeToString(TimeCurrent(), TIME_DATE));
   if(!HistorySelect(dayStart, TimeCurrent() + 60)) return;
   int n = 0, w = 0;
   double net = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      if((ulong)HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;
      if(HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
      if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      double p = HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP)
               + HistoryDealGetDouble(d, DEAL_COMMISSION);
      n++;
      if(p > 0) w++;
      net += p;
   }
   g_dTrades = n; g_dWins = w; g_dNet = net;
}

//+------------------------------------------------------------------+
// تحليل شمعة مسكّرة: دلتا + POC + قوة. يرجع الاتجاه: 1 شراء، -1 بيع، 0 لا شي
int AnalyzeBar(int shift, double atr, string &info)
{
   datetime t0 = iTime(_Symbol, InpTF, shift);
   datetime t1 = t0 + PeriodSeconds(InpTF) - 1;
   double hi = iHigh(_Symbol, InpTF, shift), lo = iLow(_Symbol, InpTF, shift);
   double op = iOpen(_Symbol, InpTF, shift), cl = iClose(_Symbol, InpTF, shift);
   double range = hi - lo;
   if(range <= 0) { info = "مدى صفر"; return 0; }
   if(atr > 0 && range < InpMinRangeATR * atr) { info = "شمعة ضعيفة (مدى " + DoubleToString(range, 2) + ")"; return 0; }

   MqlTick arr[];
   int n = CopyTicksRange(_Symbol, arr, COPY_TICKS_ALL, (ulong)t0 * 1000, (ulong)t1 * 1000 + 999);
   if(n < InpMinTicks) { info = "تكّات قليلة (" + IntegerToString(n) + ")"; return 0; }

   double bin = MathMax(InpBinSize, SymbolInfoDouble(_Symbol, SYMBOL_POINT));
   int nb = (int)MathFloor(range / bin) + 2;
   double prof[];
   ArrayResize(prof, nb);
   ArrayInitialize(prof, 0.0);

   double buy = 0, sell = 0;
   double prev = (arr[0].bid + arr[0].ask) / 2.0;
   for(int i = 0; i < n; i++)
   {
      double mid = (arr[i].bid + arr[i].ask) / 2.0;
      if(arr[i].last > 0) mid = arr[i].last;
      int idx = (int)MathFloor((mid - lo) / bin);
      if(idx < 0) idx = 0;
      if(idx >= nb) idx = nb - 1;
      prof[idx] += 1.0;                                   // Volume Profile بالتكّات

      if((arr[i].flags & TICK_FLAG_BUY) != 0)        buy  += 1.0;
      else if((arr[i].flags & TICK_FLAG_SELL) != 0)  sell += 1.0;
      else if(mid > prev + 1e-9)                     buy  += 1.0;   // تقدير من اتجاه التكّة
      else if(mid < prev - 1e-9)                     sell += 1.0;
      prev = mid;
   }
   int best = 0;
   for(int k = 1; k < nb; k++) if(prof[k] > prof[best]) best = k;
   double poc = lo + (best + 0.5) * bin;

   double tot = buy + sell;
   double delta = (tot > 0) ? (buy - sell) / tot : 0.0;
   double body = MathAbs(cl - op) / range;
   double pos = (cl - lo) / range;

   info = "دلتا " + DoubleToString(delta, 2) + " | جسم " + DoubleToString(body, 2) +
          " | إغلاق " + DoubleToString(pos, 2) + " | POC " + DoubleToString(poc, 2);

   if(body < InpMinBody) return 0;
   if(delta >= InpMinDelta && pos >= InpMinClosePos && cl > op && (!InpNeedPOC || cl > poc)) return 1;
   if(delta <= -InpMinDelta && pos <= 1.0 - InpMinClosePos && cl < op && (!InpNeedPOC || cl < poc)) return -1;
   return 0;
}

int TrendDir()
{
   double e20[1], e50[1];
   if(CopyBuffer(g_hE20, 0, 1, 1, e20) != 1) return 0;
   if(CopyBuffer(g_hE50, 0, 1, 1, e50) != 1) return 0;
   if(e20[0] > e50[0]) return 1;
   if(e20[0] < e50[0]) return -1;
   return 0;
}

//+------------------------------------------------------------------+
void TryEnter(const MqlTick &tk)
{
   if(!InpEnableTrading) { g_status = "متوقف (InpEnableTrading=false)"; return; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   { g_status = "Algo Trading مقفول"; return; }
   if(MyPositions() > 0) { g_status = "صفقة مفتوحة، ما نفتحوش ثانية"; return; }
   RefreshStats();
   if(InpDailyLossUSD > 0 && g_dNet <= -InpDailyLossUSD) { g_status = "مقفول: خسارة اليوم وصلت الحد"; return; }
   if(g_dTrades >= InpMaxTradesPerDay) { g_status = "مقفول: سقف الصفقات اليومي"; return; }

   double a[1];
   if(CopyBuffer(g_hATR, 0, 1, 1, a) != 1 || a[0] <= 0) { g_status = "ATR غير جاهز"; return; }
   double atr = a[0];

   int sig = AnalyzeBar(1, atr, g_lastSig);
   if(sig == 0) { g_status = "ما فيش إشارة"; return; }
   if(InpUseTrend)
   {
      int tr = TrendDir();
      if(tr != sig) { g_status = "الإشارة عكس اتجاه " + EnumToString(InpTrendTF); return; }
   }

   double spread = tk.ask - tk.bid;
   double minD = MinStopDist();
   double risk = MathMax(InpSLATR * atr, minD + 2.0 * SymbolInfoDouble(_Symbol, SYMBOL_POINT));
   if(spread > InpMaxSpread) { g_status = "السبريد عالي " + DoubleToString(spread, _Digits); return; }
   if(spread > risk * InpMaxSpreadPctSL / 100.0) { g_status = "السبريد كبير مقارنة بالـSL"; return; }

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double vol = NormVol(InpLot);
   bool ok;
   if(sig > 0)
   {
      double sl = NormalizeDouble(tk.bid - risk, digits);
      double tp = (InpTPR > 0) ? NormalizeDouble(tk.ask + InpTPR * risk, digits) : 0.0;
      ok = trade.Buy(vol, _Symbol, 0.0, sl, tp, "CF");
   }
   else
   {
      double sl = NormalizeDouble(tk.ask + risk, digits);
      double tp = (InpTPR > 0) ? NormalizeDouble(tk.bid - InpTPR * risk, digits) : 0.0;
      ok = trade.Sell(vol, _Symbol, 0.0, sl, tp, "CF");
   }
   if(ok) { g_risk = risk; g_status = (sig > 0 ? "دخل شراء" : "دخل بيع"); }
   else   g_status = "فشل الدخول: " + trade.ResultRetcodeDescription();
}

//+------------------------------------------------------------------+
void ManagePositions(const MqlTick &tk)
{
   double minD = MinStopDist();
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0 || !PositionSelectByTicket(t)) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;

      ENUM_POSITION_TYPE ty = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);

      if(g_risk <= 0)
      {
         if(sl > 0) g_risk = MathAbs(open - sl);
         else continue;
      }

      if(InpMaxHoldBars > 0)
      {
         long held = (long)(TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME));
         if(held >= (long)InpMaxHoldBars * PeriodSeconds(InpTF)) { trade.PositionClose(t); continue; }
      }

      double profit = (ty == POSITION_TYPE_BUY) ? (tk.bid - open) : (open - tk.ask);
      double want = 0.0;     // SL المطلوب
      bool   have = false;

      if(InpBEatR > 0 && profit >= InpBEatR * g_risk)
      {
         want = (ty == POSITION_TYPE_BUY) ? open + (tk.ask - tk.bid) : open - (tk.ask - tk.bid);
         have = true;
      }
      if(InpTrailStartR > 0 && profit >= InpTrailStartR * g_risk)
      {
         double tsl = (ty == POSITION_TYPE_BUY) ? tk.bid - InpTrailDistR * g_risk
                                                : tk.ask + InpTrailDistR * g_risk;
         if(!have || (ty == POSITION_TYPE_BUY ? tsl > want : tsl < want)) want = tsl;
         have = true;
      }
      if(!have) continue;

      want = NormalizeDouble(want, digits);
      if(ty == POSITION_TYPE_BUY)
      {
         if((sl <= 0 || want > sl) && want < tk.bid - minD) trade.PositionModify(t, want, tp);
      }
      else
      {
         if((sl <= 0 || want < sl) && want > tk.ask + minD) trade.PositionModify(t, want, tp);
      }
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;

   datetime bt = iTime(_Symbol, InpTF, 0);
   if(bt != g_lastBar)
   {
      g_lastBar = bt;
      g_evalPending = true;           // شمعة جديدة: تقييم واحد فقط
   }
   if(g_evalPending)
   {
      g_evalPending = false;
      if(TimeCurrent() - bt <= InpEntryWindowSec) TryEnter(tk);
      else g_status = "فاتت نافذة الدخول (أول " + IntegerToString(InpEntryWindowSec) + "ث)";
   }

   if(MyPositions() > 0) ManagePositions(tk);
}

void OnTimer()
{
   RefreshStats();
   ShowStatus();
}

//+------------------------------------------------------------------+
void ShowStatus()
{
   double wr = (g_dTrades > 0) ? 100.0 * g_dWins / g_dTrades : 0.0;
   string s = "XAU Candle Flow v2.00 (ديمو)  فريم " + EnumToString(InpTF) + "\n";
   s += "الحالة: " + g_status + "\n";
   s += "آخر تحليل: " + g_lastSig + "\n";
   s += "سبريد: " + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID), _Digits) +
        "  صفقات مفتوحة: " + IntegerToString(MyPositions()) + " / 1\n";
   s += "صفقات اليوم: " + IntegerToString(g_dTrades) + "  نجاح: " + DoubleToString(wr, 1) + "%\n";
   s += "صافي اليوم: " + DoubleToString(g_dNet, 2) + "\n";
   Comment(s);
}
//+------------------------------------------------------------------+
