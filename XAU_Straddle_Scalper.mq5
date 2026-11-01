//+------------------------------------------------------------------+
//| XAU_Straddle_Scalper.mq5  v1.00                                  |
//| مبني على الفيديو: أمران معلقان (Buy Stop فوق السعر + Sell Stop    |
//| تحته) وقت الحركة القوية، أول واحد ينفتح يلغي الثاني، وقف خسارة   |
//| صغير ثم Trailing Stop يتبع السعر. للديمو فقط.                      |
//| كل المسافات بوحدة سعر الرمز (دولار للذهب).                         |
//+------------------------------------------------------------------+
#property copyright "Tito"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input group "=== الأمان ==="
input bool   InpEnableTrading   = true;     // false = ما يفتح شي
input bool   InpAllowReal       = false;    // false = يرفض الحساب الحقيقي
input ulong  InpMagic           = 28110101;
input double InpLot             = 0.01;     // حجم الصفقة
input double InpDailyLossUSD    = 5.0;      // قفل يومي: صافي خسارة اليوم بالدولار (0 = بدون)
input int    InpMaxTradesPerDay = 100;      // سقف الصفقات المغلقة يوميا
input double InpMaxSpread       = 0.30;     // لا يدخل إذا السبريد أكبر (سعر)

input group "=== الأوامر المعلقة ==="
input double InpEntryOffset     = 0.50;     // بعد كل أمر عن السعر الحالي
input int    InpRefreshSec      = 5;        // يعيد وضع الأوامر كل كم ثانية
input int    InpRangeWindowSec  = 30;       // نافذة قياس الحركة
input double InpMinRange        = 0.80;     // أقل مدى حركة (high-low) باش يضع الأوامر

input group "=== الخروج ==="
input double InpSL              = 0.30;     // وقف الخسارة
input double InpTrailStart      = 0.30;     // يبدأ Trailing بعد هذا الربح
input double InpTrailDist       = 0.35;     // مسافة الوقف خلف السعر
input int    InpMaxHoldSec      = 120;      // أقصى مدة للصفقة
input int    InpCooldownSec     = 10;       // انتظار بعد إغلاق صفقة
input int    InpDeviation       = 50;       // انزلاق مسموح (نقاط)

CTrade  trade;

#define TBUF 600
long    g_tms[TBUF];
double  g_mid[TBUF];
int     g_n = 0;

bool    g_hadPos = false;
ulong   g_lastCloseMs = 0;
string  g_status = "جاهز";
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
      Alert("XAU_Straddle_Scalper: حساب حقيقي. الـ EA للديمو فقط.");
      return INIT_FAILED;
   }
   trade.SetExpertMagicNumber((long)InpMagic);
   trade.SetDeviationInPoints(InpDeviation);
   trade.SetTypeFillingBySymbol(_Symbol);
   EventSetMillisecondTimer(500);
   Print("XAU_Straddle_Scalper جاهز على ", _Symbol);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   DeleteMyPendings();
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

int MyPendings(long &oldestAgeSec)
{
   int c = 0;
   oldestAgeSec = 0;
   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;
      if((ulong)OrderGetInteger(ORDER_MAGIC) != InpMagic) continue;
      ENUM_ORDER_TYPE ty = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(ty != ORDER_TYPE_BUY_STOP && ty != ORDER_TYPE_SELL_STOP) continue;
      c++;
      long age = (long)(TimeCurrent() - (datetime)OrderGetInteger(ORDER_TIME_SETUP));
      if(age > oldestAgeSec) oldestAgeSec = age;
   }
   return c;
}

void DeleteMyPendings()
{
   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      ulong t = OrderGetTicket(i);
      if(t == 0) continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol) continue;
      if((ulong)OrderGetInteger(ORDER_MAGIC) != InpMagic) continue;
      ENUM_ORDER_TYPE ty = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(ty != ORDER_TYPE_BUY_STOP && ty != ORDER_TYPE_SELL_STOP) continue;
      trade.OrderDelete(t);
   }
}

//+------------------------------------------------------------------+
// إحصاءات اليوم من تاريخ الصفقات (تصمد لإعادة التشغيل)
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
void PushTick(long ms, double mid)
{
   if(g_n >= TBUF)
   {
      int half = TBUF / 2;
      for(int i = half; i < g_n; i++) { g_tms[i - half] = g_tms[i]; g_mid[i - half] = g_mid[i]; }
      g_n -= half;
   }
   g_tms[g_n] = ms;
   g_mid[g_n] = mid;
   g_n++;
}

// مدى الحركة (high-low) خلال النافذة، -1 إذا ما كفتش البيانات
double RecentRange(long nowMs)
{
   long from = nowMs - (long)InpRangeWindowSec * 1000;
   double hi = -1e18, lo = 1e18;
   int cnt = 0;
   long first = 0;
   for(int i = 0; i < g_n; i++)
   {
      if(g_tms[i] < from) continue;
      if(cnt == 0) first = g_tms[i];
      cnt++;
      if(g_mid[i] > hi) hi = g_mid[i];
      if(g_mid[i] < lo) lo = g_mid[i];
   }
   if(cnt < 5) return -1.0;
   if(nowMs - first < (long)InpRangeWindowSec * 600) return -1.0;   // يغطي 60% من النافذة
   return hi - lo;
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

      // أقصى مدة
      long held = (long)(TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME));
      if(InpMaxHoldSec > 0 && held >= InpMaxHoldSec)
      {
         trade.PositionClose(t);
         continue;
      }

      if(ty == POSITION_TYPE_BUY)
      {
         double profit = tk.bid - open;
         if(profit >= InpTrailStart)
         {
            double nsl = NormalizeDouble(tk.bid - InpTrailDist, digits);
            if((sl <= 0 || nsl > sl) && nsl < tk.bid - minD)
               trade.PositionModify(t, nsl, tp);
         }
      }
      else
      {
         double profit = open - tk.ask;
         if(profit >= InpTrailStart)
         {
            double nsl = NormalizeDouble(tk.ask + InpTrailDist, digits);
            if((sl <= 0 || nsl < sl) && nsl > tk.ask + minD)
               trade.PositionModify(t, nsl, tp);
         }
      }
   }
}

//+------------------------------------------------------------------+
void PlaceStraddle(const MqlTick &tk)
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double pt = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double minD = MinStopDist();
   double off  = MathMax(InpEntryOffset, minD + 2.0 * pt);
   double slD  = MathMax(InpSL, minD + 2.0 * pt);
   double vol  = NormVol(InpLot);

   double buyPx  = NormalizeDouble(tk.ask + off, digits);
   double sellPx = NormalizeDouble(tk.bid - off, digits);
   double buySL  = NormalizeDouble(buyPx - slD, digits);
   double sellSL = NormalizeDouble(sellPx + slD, digits);

   bool okB = trade.BuyStop(vol, buyPx, _Symbol, buySL, 0.0, ORDER_TIME_GTC, 0, "STR");
   if(!okB)
   {
      g_status = "فشل Buy Stop: " + trade.ResultRetcodeDescription();
      return;
   }
   bool okS = trade.SellStop(vol, sellPx, _Symbol, sellSL, 0.0, ORDER_TIME_GTC, 0, "STR");
   if(!okS)
   {
      g_status = "فشل Sell Stop: " + trade.ResultRetcodeDescription();
      DeleteMyPendings();
      return;
   }
   g_status = "أوامر معلقة موضوعة";
}

//+------------------------------------------------------------------+
void OnTick()
{
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   PushTick(tk.time_msc, (tk.bid + tk.ask) / 2.0);

   if(MyPositions() > 0)
   {
      DeleteMyPendings();       // أول أمر انفتح يلغي الثاني
      ManagePositions(tk);
   }
}

void OnTimer()
{
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   RefreshStats();

   if(!InpEnableTrading)
   {
      DeleteMyPendings();
      g_status = "متوقف (InpEnableTrading=false)";
      ShowStatus();
      return;
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      g_status = "Algo Trading مقفول";
      ShowStatus();
      return;
   }

   if(MyPositions() > 0)
   {
      g_hadPos = true;
      DeleteMyPendings();
      g_status = "صفقة مفتوحة";
      ShowStatus();
      return;
   }
   if(g_hadPos)
   {
      g_hadPos = false;
      g_lastCloseMs = GetTickCount64();
   }

   if(InpDailyLossUSD > 0 && g_dNet <= -InpDailyLossUSD)
   {
      DeleteMyPendings();
      g_status = "مقفول: خسارة اليوم وصلت الحد";
      ShowStatus();
      return;
   }
   if(g_dTrades >= InpMaxTradesPerDay)
   {
      DeleteMyPendings();
      g_status = "مقفول: سقف الصفقات اليومي";
      ShowStatus();
      return;
   }
   if(GetTickCount64() - g_lastCloseMs < (ulong)InpCooldownSec * 1000)
   {
      g_status = "انتظار بعد الإغلاق";
      ShowStatus();
      return;
   }

   double spread = tk.ask - tk.bid;
   if(spread > InpMaxSpread)
   {
      DeleteMyPendings();
      g_status = "السبريد عالي: " + DoubleToString(spread, _Digits);
      ShowStatus();
      return;
   }

   double rng = RecentRange(tk.time_msc);
   if(rng < 0)
   {
      g_status = "جمع بيانات";
      ShowStatus();
      return;
   }
   if(rng < InpMinRange)
   {
      DeleteMyPendings();
      g_status = "السوق هادئ (مدى " + DoubleToString(rng, 2) + ")";
      ShowStatus();
      return;
   }

   long age;
   int pend = MyPendings(age);
   if(pend > 0)
   {
      if(age < InpRefreshSec)
      {
         g_status = "أوامر معلقة تنتظر";
         ShowStatus();
         return;
      }
      DeleteMyPendings();
   }
   PlaceStraddle(tk);
   ShowStatus();
}

//+------------------------------------------------------------------+
void ShowStatus()
{
   double wr = (g_dTrades > 0) ? 100.0 * g_dWins / g_dTrades : 0.0;
   string s = "XAU Straddle Scalper v1.00 (ديمو)\n";
   s += "الحالة: " + g_status + "\n";
   s += "حجم: " + DoubleToString(NormVol(InpLot), 2) + "  سبريد: " + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID), _Digits) + "\n";
   s += "صفقات اليوم: " + IntegerToString(g_dTrades) + "  نجاح: " + DoubleToString(wr, 1) + "%\n";
   s += "صافي اليوم: " + DoubleToString(g_dNet, 2) + "\n";
   Comment(s);
}
//+------------------------------------------------------------------+
