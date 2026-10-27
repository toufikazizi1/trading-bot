//+------------------------------------------------------------------+
//| XAU_Tick_Scalper.mq5  v1.01                                      |
//| بوت لحظي تجريبي: يفتح صفقة صغيرة ويغلقها بعد ثواني.               |
//| للديمو فقط (يرفض الحساب الحقيقي إلا إذا فعّلت AllowReal).         |
//| يسجل كل صفقة في ملف CSV وكم دفع من السبريد.                       |
//+------------------------------------------------------------------+
#property copyright "Tito"
#property version   "1.01"
#property strict

#include <Trade\Trade.mqh>

input group "=== التشغيل ==="
input bool   EnableTrading   = true;      // true = يفتح صفقات (ديمو)
input bool   AllowReal       = false;     // لا تغيّره: false = يرفض الحساب الحقيقي
input double LotSize         = 0.01;      // حجم الصفقة
input long   MagicNumber     = 26102701;

input group "=== الإشارة (بوحدة سعر الرمز، مثلا دولار للذهب) ==="
input int    MomentumMs      = 1500;      // نافذة قياس الحركة بالميلي ثانية
input double TriggerMove     = 0.40;      // أقل حركة تفتح صفقة
input bool   FollowMomentum  = true;      // true = مع الحركة، false = عكسها

input group "=== الخروج ==="
input int    HoldSeconds     = 5;         // يغلق بعد كم ثانية
input double TakeProfitMove  = 0.60;      // هدف الربح (مسافة سعر)
input double StopLossMove    = 0.80;      // وقف الخسارة (مسافة سعر)

input group "=== الحماية ==="
input double MaxSpread       = 0.35;      // لا دخول إذا السبريد أكبر (بوحدة السعر)
input int    CooldownMs      = 2000;      // انتظار بعد كل صفقة
input int    MaxDailyTrades  = 400;       // سقف الصفقات يوميا
input double DailyLossPct    = 3.0;       // قفل يومي: خسارة % من الرصيد (0 = بدون)
input int    SlippagePoints  = 50;

CTrade   g_trade;

// تخزين التيكات
#define BUF 400
long     g_tms[BUF];
double   g_mid[BUF];
int      g_n = 0;

// حالة الصفقة
ulong    g_ticket   = 0;
ulong    g_openTick = 0;      // GetTickCount64 عند الفتح
double   g_openPx   = 0;
double   g_spreadAtOpen = 0;
string   g_side     = "";
long     g_lastCloseMs = 0;

// إحصاءات
int      g_trades = 0, g_wins = 0;
double   g_net = 0, g_spreadCost = 0;
int      g_dayTrades = 0;
int      g_dayKey = -1;
double   g_dayStartBal = 0;
bool     g_locked = false;
string   g_status = "";
string   g_csv = "tick_scalper_log.csv";

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

//+------------------------------------------------------------------+
int OnInit()
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL && !AllowReal)
   {
      Alert("XAU_Tick_Scalper: هذا حساب حقيقي. الـ EA للديمو فقط.");
      return INIT_FAILED;
   }
   g_trade.SetExpertMagicNumber(MagicNumber);
   g_trade.SetDeviationInPoints(SlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   EventSetMillisecondTimer(100);

   // ترويسة CSV
   int h = FileOpen(g_csv, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, ',');
   if(h != INVALID_HANDLE)
   {
      if(FileSize(h) == 0)
         FileWrite(h, "time", "side", "open", "close", "profit", "spread_at_open", "held_ms");
      FileClose(h);
   }
   g_status = "جاهز";
   Print("XAU_Tick_Scalper جاهز على ", _Symbol);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
}

//+------------------------------------------------------------------+
void CheckNewDay()
{
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   int key = t.year * 1000 + t.day_of_year;
   if(key != g_dayKey)
   {
      g_dayKey = key;
      g_dayStartBal = AccountInfoDouble(ACCOUNT_BALANCE);
      g_dayTrades = 0;
      g_locked = false;
   }
}

bool HaveMyPosition()
{
   if(g_ticket == 0) return false;
   return PositionSelectByTicket(g_ticket);
}

//+------------------------------------------------------------------+
// بعد إغلاق الصفقة: نحسب الربح من التاريخ ونسجل
void FinalizeTrade()
{
   double profit = 0, closePx = 0;
   if(HistorySelectByPosition(g_ticket))
   {
      int n = HistoryDealsTotal();
      for(int i = 0; i < n; i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(d == 0) continue;
         profit += HistoryDealGetDouble(d, DEAL_PROFIT)
                 + HistoryDealGetDouble(d, DEAL_SWAP)
                 + HistoryDealGetDouble(d, DEAL_COMMISSION);
         if(HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_OUT)
            closePx = HistoryDealGetDouble(d, DEAL_PRICE);
      }
   }
   long held = (long)(GetTickCount64() - g_openTick);
   g_trades++;
   if(profit > 0) g_wins++;
   g_net += profit;
   double cs = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   g_spreadCost += g_spreadAtOpen * LotSize * cs;

   int h = FileOpen(g_csv, FILE_READ | FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_SHARE_READ, ',');
   if(h != INVALID_HANDLE)
   {
      FileSeek(h, 0, SEEK_END);
      FileWrite(h, TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS), g_side,
                DoubleToString(g_openPx, _Digits), DoubleToString(closePx, _Digits),
                DoubleToString(profit, 2), DoubleToString(g_spreadAtOpen, _Digits), (string)held);
      FileClose(h);
   }
   g_ticket = 0;
   g_lastCloseMs = (long)GetTickCount64();

   if(DailyLossPct > 0 && g_dayStartBal > 0)
   {
      double loss = (g_dayStartBal - AccountInfoDouble(ACCOUNT_EQUITY)) / g_dayStartBal * 100.0;
      if(loss >= DailyLossPct) g_locked = true;
   }
}

//+------------------------------------------------------------------+
void OnTimer()
{
   CheckNewDay();
   if(g_ticket != 0)
   {
      if(!PositionSelectByTicket(g_ticket))
      {
         FinalizeTrade();            // أُغلقت بالهدف أو الوقف
      }
      else if((long)(GetTickCount64() - g_openTick) >= (long)HoldSeconds * 1000)
      {
         if(!g_trade.PositionClose(g_ticket))
            Print("فشل الإغلاق: ", g_trade.ResultRetcodeDescription());
      }
   }
   ShowStatus();
}

//+------------------------------------------------------------------+
void PushTick(long ms, double mid)
{
   if(g_n >= BUF)
   {
      // نحذف النصف الأقدم
      int half = BUF / 2;
      for(int i = half; i < g_n; i++) { g_tms[i - half] = g_tms[i]; g_mid[i - half] = g_mid[i]; }
      g_n -= half;
   }
   g_tms[g_n] = ms;
   g_mid[g_n] = mid;
   g_n++;
}

// تغير السعر خلال النافذة. يرجع false إذا ما كفتش البيانات
bool MomentumChange(long nowMs, double nowMid, double &chg)
{
   long from = nowMs - MomentumMs;
   int idx = -1, cnt = 0;
   for(int i = 0; i < g_n; i++)
      if(g_tms[i] >= from) { if(idx < 0) idx = i; cnt++; }
   if(idx < 0 || cnt < 3) return false;
   // لازم النافذة تغطي على الأقل 60% من المدة
   if(nowMs - g_tms[idx] < (long)(MomentumMs * 0.6)) return false;
   chg = nowMid - g_mid[idx];
   return true;
}

//+------------------------------------------------------------------+
void OnTick()
{
   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk)) return;
   double mid = (tk.bid + tk.ask) / 2.0;
   PushTick(tk.time_msc, mid);

   CheckNewDay();
   if(!EnableTrading) { g_status = "متوقف (EnableTrading=false)"; return; }
   if(g_locked)       { g_status = "مقفول: وصلنا حد الخسارة اليومي"; return; }
   if(g_ticket != 0)  { g_status = "صفقة مفتوحة"; return; }
   if(g_dayTrades >= MaxDailyTrades) { g_status = "وصلنا سقف الصفقات اليومي"; return; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   { g_status = "Algo Trading مقفول"; return; }

   if((long)GetTickCount64() - g_lastCloseMs < CooldownMs) { g_status = "انتظار بين الصفقات"; return; }

   double spread = tk.ask - tk.bid;
   if(spread > MaxSpread) { g_status = "السبريد عالي: " + DoubleToString(spread, _Digits); return; }

   double chg;
   if(!MomentumChange(tk.time_msc, mid, chg)) { g_status = "جمع بيانات"; return; }
   if(MathAbs(chg) < TriggerMove) { g_status = "ينتظر حركة"; return; }

   bool up = (chg > 0);
   bool buy = FollowMomentum ? up : !up;

   // الهدف والوقف: الوسيط يحسب الوقف من سعر الإغلاق (Bid للشراء، Ask للبيع)
   double pt   = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double lvl  = (double)MathMax(SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                                 SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL)) * pt;
   double minD = lvl + 2 * pt;

   double vol = NormVol(LotSize);
   double slPx, tpPx;
   bool ok;
   if(buy)
   {
      slPx = tk.bid - MathMax(StopLossMove, minD);
      tpPx = tk.ask + TakeProfitMove;
      if(tpPx - tk.bid < minD) tpPx = tk.bid + minD;
      ok = g_trade.Buy(vol, _Symbol, 0, NormalizeDouble(slPx, _Digits),
                       NormalizeDouble(tpPx, _Digits), "XTS");
   }
   else
   {
      slPx = tk.ask + MathMax(StopLossMove, minD);
      tpPx = tk.bid - TakeProfitMove;
      if(tk.ask - tpPx < minD) tpPx = tk.ask - minD;
      ok = g_trade.Sell(vol, _Symbol, 0, NormalizeDouble(slPx, _Digits),
                        NormalizeDouble(tpPx, _Digits), "XTS");
   }

   if(ok && g_trade.ResultRetcode() == TRADE_RETCODE_DONE)
   {
      // نجيب رقم الـposition
      ulong deal = g_trade.ResultDeal();
      ulong posId = 0;
      if(deal > 0 && HistoryDealSelect(deal))
         posId = (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
      if(posId == 0) posId = g_trade.ResultOrder();
      g_ticket = posId;
      g_openTick = GetTickCount64();
      g_openPx = g_trade.ResultPrice();
      g_spreadAtOpen = spread;
      g_side = buy ? "BUY" : "SELL";
      g_dayTrades++;
      g_status = "فتح " + g_side;
   }
   else
   {
      g_status = "فشل الفتح: " + g_trade.ResultRetcodeDescription();
      g_lastCloseMs = (long)GetTickCount64();
   }
}

//+------------------------------------------------------------------+
void ShowStatus()
{
   double wr = (g_trades > 0) ? 100.0 * g_wins / g_trades : 0;
   string s = "XAU Tick Scalper v1.01 (ديمو)\n";
   s += "الحالة: " + g_status + "\n";
   s += "الرمز: " + _Symbol + "  حجم: " + DoubleToString(NormVol(LotSize), 2) + "\n";
   s += "الصفقات: " + IntegerToString(g_trades) + "  نجاح: " + DoubleToString(wr, 1) + "%\n";
   s += "صافي الربح: " + DoubleToString(g_net, 2) + "\n";
   s += "السبريد المدفوع تقريبا: " + DoubleToString(g_spreadCost, 2) + "\n";
   Comment(s);
}
//+------------------------------------------------------------------+
