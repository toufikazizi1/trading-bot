//+------------------------------------------------------------------+
//| XAU_Signal_Executor.mq5   v1.00                                  |
//| ينفذ في MT5 إشارات البوت (xau_live_bot.py --mt5) تلقائياً.        |
//| البوت يقرر، والـEA ينفذ: دخول بالسوق + SL + 3 أهداف + نقل الستوب. |
//+------------------------------------------------------------------+
#property copyright "XAU Live Bot"
#property version   "1.00"
#property strict

#include <Trade/Trade.mqh>

input bool   EnableTrading         = false;          // false = لا يفتح صفقات (ابدأ بحساب Demo)
input string SignalFile            = "xau_signal.txt";
input double RiskPercent           = 0.5;            // نسبة المخاطرة من الرصيد لكل إشارة
input double MaxLot                = 1.0;            // سقف الحجم الإجمالي
input int    MaxSpreadPoints       = 80;             // لا دخول إذا السبريد أكبر
input int    SignalMaxAgeSec       = 30;             // إشارة أقدم من هذا تُهمل
input double DailyLossLimitPercent = 2.0;            // قفل يومي: خسارة اليوم 2% = لا صفقات جديدة
input bool   SplitTargets          = true;           // 3 صفقات (ثلث لكل هدف) إذا الحساب Hedging والحجم يكفي
input long   MagicNumber           = 26102501;
input int    SlippagePoints        = 30;

CTrade trade;
string GV_LASTID, GV_DAYKEY, GV_DAYBAL, GV_ENTRY, GV_TP1, GV_ARMED;
string g_status = "";

//+------------------------------------------------------------------+
int OnInit()
{
   string m = IntegerToString((long)MagicNumber);
   GV_LASTID = "XSE_LASTID_" + m;
   GV_DAYKEY = "XSE_DAYKEY_" + m;
   GV_DAYBAL = "XSE_DAYBAL_" + m;
   GV_ENTRY  = "XSE_ENTRY_"  + m;
   GV_TP1    = "XSE_TP1_"    + m;
   GV_ARMED  = "XSE_ARMED_"  + m;
   if(StringFind(_Symbol, "XAU") < 0)
      Print("تنبيه: الرمز ", _Symbol, " ليس ذهباً. ركّب الـEA على شارت XAUUSD.");
   EventSetTimer(1);
   g_status = EnableTrading ? "جاهز — التداول مفعّل" : "جاهز — التداول متوقف (EnableTrading=false)";
   ShowStatus();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
}

void OnTick() {}

//+------------------------------------------------------------------+
void ShowStatus()
{
   Comment("XAU Signal Executor v1.00\n", g_status, "\nالرمز: ", _Symbol,
           "\nمخاطرة: ", DoubleToString(RiskPercent, 2), "%");
}

//+------------------------------------------------------------------+
bool ReadSignal(long &id, string &side, double &entry, double &sl,
                double &tp1, double &tp2, double &tp3, long &created)
{
   int h = FileOpen(SignalFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON |
                                FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
      return(false);
   id = 0; side = ""; entry = 0; sl = 0; tp1 = 0; tp2 = 0; tp3 = 0; created = 0;
   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      int p = StringFind(line, "=");
      if(p <= 0)
         continue;
      string k = StringSubstr(line, 0, p);
      string v = StringSubstr(line, p + 1);
      StringTrimLeft(k); StringTrimRight(k);
      StringTrimLeft(v); StringTrimRight(v);
      if(k == "id")           id = StringToInteger(v);
      else if(k == "side")    side = v;
      else if(k == "entry")   entry = StringToDouble(v);
      else if(k == "sl")      sl = StringToDouble(v);
      else if(k == "tp1")     tp1 = StringToDouble(v);
      else if(k == "tp2")     tp2 = StringToDouble(v);
      else if(k == "tp3")     tp3 = StringToDouble(v);
      else if(k == "created") created = StringToInteger(v);
   }
   FileClose(h);
   return(id > 0 && (side == "BUY" || side == "SELL") && entry > 0 && sl > 0 && tp1 > 0 && tp2 > 0 && tp3 > 0);
}

//+------------------------------------------------------------------+
double NormalizeVolume(double v)
{
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double mn   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0) step = 0.01;
   v = MathFloor(v / step + 1e-9) * step;
   if(v < mn) return(0.0);
   if(v > mx) v = mx;
   int dg = (int)MathRound(-MathLog10(step));
   if(dg < 0) dg = 0;
   return(NormalizeDouble(v, dg));
}

double CalcLots(bool buy, double price, double slDist)
{
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskMoney = bal * RiskPercent / 100.0;
   double slPrice = buy ? price - slDist : price + slDist;
   double profit = 0.0;
   if(!OrderCalcProfit(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, price, slPrice, profit))
      return(0.0);
   double lossPerLot = MathAbs(profit);
   if(lossPerLot <= 0.0)
      return(0.0);
   double lots = riskMoney / lossPerLot;
   double margin = 0.0;
   if(OrderCalcMargin(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, price, margin) && margin > 0.0)
   {
      double maxByMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.5 / margin;
      lots = MathMin(lots, maxByMargin);
   }
   lots = MathMin(lots, MaxLot);
   return(NormalizeVolume(lots));
}

//+------------------------------------------------------------------+
int CountMyPositions()
{
   int c = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         c++;
   }
   return(c);
}

bool DailyLossLocked()
{
   MqlDateTime d;
   TimeToStruct(TimeCurrent(), d);
   int key = d.year * 10000 + d.mon * 100 + d.day;
   if((int)GlobalVariableGet(GV_DAYKEY) != key)
   {
      GlobalVariableSet(GV_DAYKEY, key);
      GlobalVariableSet(GV_DAYBAL, AccountInfoDouble(ACCOUNT_EQUITY));
   }
   double start = GlobalVariableGet(GV_DAYBAL);
   return(start > 0.0 && AccountInfoDouble(ACCOUNT_EQUITY) < start * (1.0 - DailyLossLimitPercent / 100.0));
}

//+------------------------------------------------------------------+
void Execute(string side, double sigEntry, double sl, double tp1, double tp2, double tp3)
{
   bool buy = (side == "BUY");
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double price = buy ? ask : bid;
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   // المسافات من سعر الإشارة، وتُطبَّق على سعر دخولنا الفعلي عند الوسيط
   double slD = MathAbs(sigEntry - sl);
   double d1  = MathAbs(tp1 - sigEntry);
   double d2  = MathAbs(tp2 - sigEntry);
   double d3  = MathAbs(tp3 - sigEntry);
   if(slD <= 0.0 || d1 <= 0.0 || d2 <= 0.0 || d3 <= 0.0)
   {
      Print("إشارة مرفوضة: مسافات غير صالحة");
      return;
   }

   double total = CalcLots(buy, price, slD);
   if(total <= 0.0)
   {
      Print("الحجم المحسوب = 0 (الرصيد/الهامش لا يكفي) — لا دخول");
      return;
   }

   double slP = NormalizeDouble(buy ? price - slD : price + slD, digits);
   bool hedging = ((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
   double each = NormalizeVolume(total / 3.0);
   bool split = (SplitTargets && hedging && each > 0.0);

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(SlippagePoints);

   GlobalVariableSet(GV_ARMED, 0);

   if(split)
   {
      double v3 = NormalizeVolume(total - 2.0 * each);
      if(v3 <= 0.0) v3 = each;
      double vols[3];
      double dists[3];
      vols[0] = each; vols[1] = each; vols[2] = v3;
      dists[0] = d1;  dists[1] = d2;  dists[2] = d3;
      int opened = 0;
      for(int i = 0; i < 3; i++)
      {
         double tpP = NormalizeDouble(buy ? price + dists[i] : price - dists[i], digits);
         string cm = "XAUL" + IntegerToString(i + 1);
         bool ok = buy ? trade.Buy(vols[i], _Symbol, 0.0, slP, tpP, cm)
                       : trade.Sell(vols[i], _Symbol, 0.0, slP, tpP, cm);
         if(ok) opened++;
         else Print("فشل فتح الصفقة ", i + 1, ": ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
      }
      if(opened == 3)
      {
         GlobalVariableSet(GV_ENTRY, price);
         GlobalVariableSet(GV_TP1, NormalizeDouble(buy ? price + d1 : price - d1, digits));
         GlobalVariableSet(GV_ARMED, 1);
      }
      g_status = "فُتحت " + IntegerToString(opened) + "/3 صفقات " + side + " (" + DoubleToString(total, 2) + " لوت)";
   }
   else
   {
      // حجم صغير أو حساب Netting: صفقة واحدة بهدف TP2
      double tpP = NormalizeDouble(buy ? price + d2 : price - d2, digits);
      bool ok = buy ? trade.Buy(total, _Symbol, 0.0, slP, tpP, "XAUL1")
                    : trade.Sell(total, _Symbol, 0.0, slP, tpP, "XAUL1");
      if(!ok)
         Print("فشل فتح الصفقة: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
      g_status = ok ? "فُتحت صفقة واحدة " + side + " (" + DoubleToString(total, 2) + " لوت)" : "فشل فتح الصفقة";
   }
   Print(g_status, " | SL ", DoubleToString(slP, digits));
}

//+------------------------------------------------------------------+
// بعد إغلاق الهدف الأول: الستوب لنقطة الدخول. بعد الثاني: الستوب عند الهدف الأول.
void MoveStopIfBetter(ulong ticket, bool buy, double newSL)
{
   if(!PositionSelectByTicket(ticket))
      return;
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   newSL = NormalizeDouble(newSL, digits);
   double curSL = PositionGetDouble(POSITION_SL);
   double curTP = PositionGetDouble(POSITION_TP);
   bool better = buy ? (curSL < newSL) : (curSL > newSL || curSL == 0.0);
   if(!better)
      return;
   trade.SetExpertMagicNumber(MagicNumber);
   if(!trade.PositionModify(ticket, newSL, curTP))
      Print("تعذر نقل الستوب: ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
}

void ManageLegs()
{
   ulong t1 = 0, t2 = 0, t3 = 0;
   bool buy = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol || PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;
      string cm = PositionGetString(POSITION_COMMENT);
      buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      if(StringFind(cm, "XAUL1") >= 0)      t1 = t;
      else if(StringFind(cm, "XAUL2") >= 0) t2 = t;
      else if(StringFind(cm, "XAUL3") >= 0) t3 = t;
   }
   if(t1 == 0 && t2 == 0 && t3 == 0)
   {
      GlobalVariableSet(GV_ARMED, 0);
      return;
   }
   if(GlobalVariableGet(GV_ARMED) < 0.5)
      return;
   double entry = GlobalVariableGet(GV_ENTRY);
   double tp1p  = GlobalVariableGet(GV_TP1);
   if(t1 == 0)
   {
      if(t2 != 0) MoveStopIfBetter(t2, buy, entry);
      if(t3 != 0) MoveStopIfBetter(t3, buy, entry);
   }
   if(t1 == 0 && t2 == 0 && t3 != 0)
      MoveStopIfBetter(t3, buy, tp1p);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   ManageLegs();

   long id, created;
   string side;
   double entry, sl, tp1, tp2, tp3;
   if(!ReadSignal(id, side, entry, sl, tp1, tp2, tp3, created))
   {
      ShowStatus();
      return;
   }
   long last = (long)GlobalVariableGet(GV_LASTID);
   if(id <= last)
   {
      ShowStatus();
      return;
   }
   GlobalVariableSet(GV_LASTID, (double)id);   // نسجّلها كمستهلكة حتى لا تتكرر

   if(!EnableTrading)
   {
      g_status = "إشارة " + side + " وصلت — تجاهل (EnableTrading=false)";
      Print(g_status);
      ShowStatus();
      return;
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      g_status = "التداول الآلي غير مفعّل في MT5 (زر Algo Trading)";
      Print(g_status);
      ShowStatus();
      return;
   }
   long age = (long)TimeGMT() - created;
   if(age > SignalMaxAgeSec || age < -10)
   {
      g_status = "إشارة " + side + " قديمة (" + IntegerToString((int)age) + "ث) — تجاهل";
      Print(g_status);
      ShowStatus();
      return;
   }
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > MaxSpreadPoints)
   {
      g_status = "السبريد عالي (" + IntegerToString((int)spread) + ") — تجاهل";
      Print(g_status);
      ShowStatus();
      return;
   }
   if(CountMyPositions() > 0)
   {
      g_status = "توجد صفقة مفتوحة — تجاهل الإشارة";
      Print(g_status);
      ShowStatus();
      return;
   }
   if(DailyLossLocked())
   {
      g_status = "قفل الخسارة اليومية — لا صفقات جديدة";
      Print(g_status);
      ShowStatus();
      return;
   }
   Execute(side, entry, sl, tp1, tp2, tp3);
   ShowStatus();
}
//+------------------------------------------------------------------+
