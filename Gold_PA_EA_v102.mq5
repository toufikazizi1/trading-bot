//+------------------------------------------------------------------+
//| Gold_PA_EA_v102.mq5  (v1.02)                                     |
//| Educational MT5 Expert Advisor: price action + liquidity sweep   |
//| Demo-first template. Not a guarantee of profitability.           |
//|                                                                  |
//| v1.02: small-account mode (InpFixedLot + risk cap), 1 position.  |
//| v1.01 changes:                                                   |
//|  - Spread limit default raised for 3-digit gold (XAUUSDm).       |
//|  - Spread log now prints the real spread and the limit.          |
//|  - New relative filter: spread as % of stop distance.            |
//|  - Trend EMA handles created once in OnInit (fixes trend filter  |
//|    that read data before the indicator was calculated).          |
//|  - Deviation and break-even offset defaults fit 3-digit gold.    |
//|  - Removed unused variables and M1 EMA handles.                  |
//|  - Chart comment shows spread, trend and open positions.         |
//+------------------------------------------------------------------+
#property strict
#property version   "1.02"
#property description "XAUUSD price-action/liquidity-sweep EA with risk controls"

#include <Trade/Trade.mqh>
CTrade trade;

// ---- General settings
input string InpTradeSymbol            = "";       // Empty = chart symbol
input ENUM_TIMEFRAMES InpSignalTF      = PERIOD_M1;
input ENUM_TIMEFRAMES InpTrendTF       = PERIOD_M5;
input ulong  InpMagic                  = 27102026;
input bool   InpAllowRealTrading       = false;    // Real accounts blocked by default
input bool   InpAllowBuy               = true;
input bool   InpAllowSell              = true;

// ---- Signal settings
input int    InpSwingLookback          = 12;       // Closed bars used to identify liquidity
input int    InpFastEMA                = 20;
input int    InpSlowEMA                = 50;
input int    InpATRPeriod              = 14;
input double InpSweepBufferATR         = 0.05;     // Minimum sweep beyond prior swing, in ATR
input double InpMinBodyToRange         = 0.20;     // Rejection candle minimum body/range
input double InpMinTickVolumeRatio     = 0.70;     // Signal tick volume / recent average

// ---- Risk and execution
input double InpFixedLot               = 0.01;     // >0 = fixed lot (small accounts). 0 = risk-based sizing
input double InpMaxRiskPctCap          = 6.0;      // Fixed-lot mode: skip trade if its stop loss risks more than this % of equity (0 = off)
input double InpRiskPercent            = 0.50;     // Risk per trade (% equity) when InpFixedLot = 0
input double InpMaxDailyLossPercent    = 10.00;     // Stop new entries after daily equity loss
input int    InpMaxOpenPositions       = 1;        // This EA + symbol + magic
input double InpMaxSpreadPoints        = 500;      // Max spread in broker points (3-digit gold: 500 = 0.50 USD)
input double InpMaxSpreadPctOfStop     = 30.0;     // Max spread as % of stop distance (0 = off)
input double InpSL_ATR_Multiplier      = 1.50;
input double InpTP_RiskReward          = 1.50;
input int    InpMaxDeviationPoints     = 300;      // Slippage tolerance in points (3-digit gold: 300 = 0.30 USD)
input int    InpCooldownBars           = 2;
input bool   InpUseBreakEven           = true;
input double InpBreakEvenAtR           = 1.00;
input double InpBreakEvenOffsetPoints  = 300;      // Should cover the spread (3-digit gold: 300 = 0.30 USD)
input bool   InpCloseOnOppositeSignal  = false;    // Conservative default: don't force close

int hATR = INVALID_HANDLE;
int hTrendFast = INVALID_HANDLE;
int hTrendSlow = INVALID_HANDLE;
datetime lastSignalBar = 0;
datetime lastEntryBar = 0;
int dayOfYearTracked = -1;
double dayStartEquity = 0.0;

string TradeSymbol()
{
   return (InpTradeSymbol == "" ? _Symbol : InpTradeSymbol);
}

bool IsNewSignalBar()
{
   datetime t = iTime(TradeSymbol(), InpSignalTF, 0);
   if(t == 0 || t == lastSignalBar) return false;
   lastSignalBar = t;
   return true;
}

void RefreshDailyBaseline()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dayOfYearTracked != dt.day_of_year)
   {
      dayOfYearTracked = dt.day_of_year;
      dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   }
}

bool DailyLossLimitHit()
{
   RefreshDailyBaseline();
   if(dayStartEquity <= 0) return false;
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double lossPct = (dayStartEquity - equity) / dayStartEquity * 100.0;
   return (lossPct >= InpMaxDailyLossPercent);
}

bool IsTradingAllowedAccount()
{
   ENUM_ACCOUNT_TRADE_MODE mode = (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_REAL && !InpAllowRealTrading)
   {
      Comment("Gold PA EA: real-account trading is disabled. Use demo first.");
      return false;
   }
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) return false;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED) || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)) return false;
   return true;
}

double GetIndicatorValue(int handle, int shift)
{
   double b[];
   ArraySetAsSeries(b, true);
   if(handle == INVALID_HANDLE || CopyBuffer(handle, 0, shift, 1, b) != 1) return 0.0;
   return b[0];
}

// +1 = uptrend (fast EMA above slow), -1 = downtrend, 0 = unknown / not ready
int TrendDirection()
{
   if(hTrendFast == INVALID_HANDLE || hTrendSlow == INVALID_HANDLE) return 0;
   if(BarsCalculated(hTrendFast) < 3 || BarsCalculated(hTrendSlow) < 3) return 0;
   double f = GetIndicatorValue(hTrendFast, 1);
   double s = GetIndicatorValue(hTrendSlow, 1);
   if(f <= 0 || s <= 0) return 0;
   if(f > s) return 1;
   if(f < s) return -1;
   return 0;
}

int CountOurPositions()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionSelectByTicket(ticket))
      {
         if(PositionGetString(POSITION_SYMBOL) == TradeSymbol() &&
            (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
            count++;
      }
   }
   return count;
}

double RecentSwingLow()
{
   double v = DBL_MAX;
   for(int i = 2; i <= InpSwingLookback + 1; ++i)
   {
      double x = iLow(TradeSymbol(), InpSignalTF, i);
      if(x > 0 && x < v) v = x;
   }
   return (v == DBL_MAX ? 0.0 : v);
}

double RecentSwingHigh()
{
   double v = 0.0;
   for(int i = 2; i <= InpSwingLookback + 1; ++i)
   {
      double x = iHigh(TradeSymbol(), InpSignalTF, i);
      if(x > v) v = x;
   }
   return v;
}

double AverageTickVolume(int bars)
{
   long sum = 0;
   int n = 0;
   for(int i = 2; i < bars + 2; ++i)
   {
      long v = (long)iVolume(TradeSymbol(), InpSignalTF, i);
      if(v > 0) { sum += v; n++; }
   }
   if(n == 0) return 0.0;
   return (double)sum / n;
}

// Current spread in broker points, or -1 if it cannot be read.
double CurrentSpreadPoints()
{
   MqlTick tick;
   if(!SymbolInfoTick(TradeSymbol(), tick)) return -1.0;
   double point = SymbolInfoDouble(TradeSymbol(), SYMBOL_POINT);
   if(point <= 0) return -1.0;
   return (tick.ask - tick.bid) / point;
}

void UpdateStatus(double spreadPoints)
{
   int trend = TrendDirection();
   string trendText = "not ready";
   if(trend > 0) trendText = "UP";
   else if(trend < 0) trendText = "DOWN";

   Comment("Gold PA EA v1.02\n",
           "Symbol: ", TradeSymbol(), "\n",
           "Spread: ", DoubleToString(spreadPoints, 0), " points (max ", DoubleToString(InpMaxSpreadPoints, 0), ")\n",
           "Trend ", EnumToString(InpTrendTF), ": ", trendText, "\n",
           "Open positions: ", CountOurPositions(), " / ", InpMaxOpenPositions);
}

double NormalizeVolume(double volume)
{
   double minVol = SymbolInfoDouble(TradeSymbol(), SYMBOL_VOLUME_MIN);
   double maxVol = SymbolInfoDouble(TradeSymbol(), SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(TradeSymbol(), SYMBOL_VOLUME_STEP);
   if(step <= 0) return 0.0;
   volume = MathMax(minVol, MathMin(maxVol, volume));
   volume = MathFloor(volume / step) * step;
   int digits = 2;
   if(step >= 1.0) digits = 0;
   else if(step >= 0.1) digits = 1;
   else if(step >= 0.01) digits = 2;
   else if(step >= 0.001) digits = 3;
   return NormalizeDouble(volume, digits);
}

double CalculateVolume(ENUM_ORDER_TYPE type, double entry, double sl)
{
   if(InpFixedLot > 0)
   {
      double lot = NormalizeVolume(InpFixedLot);
      double minV = SymbolInfoDouble(TradeSymbol(), SYMBOL_VOLUME_MIN);
      if(lot < minV) return 0.0;
      double eq = AccountInfoDouble(ACCOUNT_EQUITY);
      double lossFixed = 0.0;
      if(!OrderCalcProfit(type, TradeSymbol(), lot, entry, sl, lossFixed)) return 0.0;
      lossFixed = MathAbs(lossFixed);
      if(InpMaxRiskPctCap > 0 && eq > 0 && lossFixed > eq * InpMaxRiskPctCap / 100.0)
      {
         Print("No trade: stop risks ", DoubleToString(lossFixed, 2), " USD (",
               DoubleToString(lossFixed / eq * 100.0, 1), "% of equity) > cap ",
               DoubleToString(InpMaxRiskPctCap, 1), "%.");
         return 0.0;
      }
      double margin = 0.0;
      if(OrderCalcMargin(type, TradeSymbol(), lot, entry, margin) &&
         margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE) * 0.8)
      {
         Print("No trade: not enough free margin for ", DoubleToString(lot, 2), " lot.");
         return 0.0;
      }
      return lot;
   }
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskMoney = equity * InpRiskPercent / 100.0;
   if(riskMoney <= 0) return 0.0;

   double profitAtSL = 0.0;
   if(!OrderCalcProfit(type, TradeSymbol(), 1.0, entry, sl, profitAtSL))
      return 0.0;
   double lossForOneLot = MathAbs(profitAtSL);
   if(lossForOneLot <= 0) return 0.0;

   double volume = NormalizeVolume(riskMoney / lossForOneLot);
   // Do not force the broker's minimum lot if it exceeds the intended risk.
   double minVol = SymbolInfoDouble(TradeSymbol(), SYMBOL_VOLUME_MIN);
   if(volume < minVol) return 0.0;

   double estimatedLoss = 0.0;
   if(!OrderCalcProfit(type, TradeSymbol(), volume, entry, sl, estimatedLoss))
      return 0.0;
   if(MathAbs(estimatedLoss) > riskMoney * 1.05) return 0.0;
   return volume;
}

bool CooldownPassed()
{
   if(lastEntryBar == 0) return true;
   int shift = iBarShift(TradeSymbol(), InpSignalTF, lastEntryBar, false);
   return (shift < 0 || shift >= InpCooldownBars);
}

void ManageBreakEven()
{
   if(!InpUseBreakEven) return;
   MqlTick tick;
   if(!SymbolInfoTick(TradeSymbol(), tick)) return;
   double point = SymbolInfoDouble(TradeSymbol(), SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(TradeSymbol(), SYMBOL_DIGITS);

   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != TradeSymbol() ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;

      ENUM_POSITION_TYPE ptype = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      if(sl <= 0) continue;
      double initialRisk = MathAbs(open - sl);
      if(initialRisk <= 0) continue;

      if(ptype == POSITION_TYPE_BUY)
      {
         if(tick.bid - open >= InpBreakEvenAtR * initialRisk)
         {
            double newSL = NormalizeDouble(open + InpBreakEvenOffsetPoints * point, digits);
            if(newSL > sl && newSL < tick.bid)
               trade.PositionModify(ticket, newSL, tp);
         }
      }
      else if(ptype == POSITION_TYPE_SELL)
      {
         if(open - tick.ask >= InpBreakEvenAtR * initialRisk)
         {
            double newSL = NormalizeDouble(open - InpBreakEvenOffsetPoints * point, digits);
            if((sl == 0 || newSL < sl) && newSL > tick.ask)
               trade.PositionModify(ticket, newSL, tp);
         }
      }
   }
}

bool BullishSweepSignal()
{
   // Last fully closed candle (shift 1) sweeps the prior swing low then closes back above it.
   double swingLow = RecentSwingLow();
   double atr = GetIndicatorValue(hATR, 1);
   if(swingLow <= 0 || atr <= 0) return false;

   double o = iOpen(TradeSymbol(), InpSignalTF, 1);
   double c = iClose(TradeSymbol(), InpSignalTF, 1);
   double l = iLow(TradeSymbol(), InpSignalTF, 1);
   double h = iHigh(TradeSymbol(), InpSignalTF, 1);
   double range = h - l;
   if(range <= 0) return false;

   double bodyRatio = MathAbs(c - o) / range;
   double avgVol = AverageTickVolume(10);
   double vol = (double)iVolume(TradeSymbol(), InpSignalTF, 1);
   bool volumeOk = (avgVol <= 0 || vol >= avgVol * InpMinTickVolumeRatio);

   return (l < swingLow - InpSweepBufferATR * atr && c > swingLow &&
           c > o && bodyRatio >= InpMinBodyToRange && volumeOk &&
           TrendDirection() > 0);
}

bool BearishSweepSignal()
{
   double swingHigh = RecentSwingHigh();
   double atr = GetIndicatorValue(hATR, 1);
   if(swingHigh <= 0 || atr <= 0) return false;

   double o = iOpen(TradeSymbol(), InpSignalTF, 1);
   double c = iClose(TradeSymbol(), InpSignalTF, 1);
   double l = iLow(TradeSymbol(), InpSignalTF, 1);
   double h = iHigh(TradeSymbol(), InpSignalTF, 1);
   double range = h - l;
   if(range <= 0) return false;

   double bodyRatio = MathAbs(c - o) / range;
   double avgVol = AverageTickVolume(10);
   double vol = (double)iVolume(TradeSymbol(), InpSignalTF, 1);
   bool volumeOk = (avgVol <= 0 || vol >= avgVol * InpMinTickVolumeRatio);

   return (h > swingHigh + InpSweepBufferATR * atr && c < swingHigh &&
           c < o && bodyRatio >= InpMinBodyToRange && volumeOk &&
           TrendDirection() < 0);
}

bool OpenTrade(bool buy)
{
   MqlTick tick;
   if(!SymbolInfoTick(TradeSymbol(), tick)) return false;
   double atr = GetIndicatorValue(hATR, 1);
   if(atr <= 0) return false;

   int digits = (int)SymbolInfoInteger(TradeSymbol(), SYMBOL_DIGITS);
   double point = SymbolInfoDouble(TradeSymbol(), SYMBOL_POINT);
   double entry = buy ? tick.ask : tick.bid;
   double stopDistance = MathMax(atr * InpSL_ATR_Multiplier,
                                 2.0 * (double)SymbolInfoInteger(TradeSymbol(), SYMBOL_TRADE_STOPS_LEVEL) * point);

   // Relative spread filter: skip when the spread eats too much of the risk.
   if(InpMaxSpreadPctOfStop > 0)
   {
      double spreadPrice = tick.ask - tick.bid;
      if(spreadPrice > stopDistance * InpMaxSpreadPctOfStop / 100.0)
      {
         Print("No trade: spread ", DoubleToString(spreadPrice / point, 0),
               " points is above ", DoubleToString(InpMaxSpreadPctOfStop, 0),
               "% of the stop distance (", DoubleToString(stopDistance / point, 0), " points).");
         return false;
      }
   }

   double sl = buy ? entry - stopDistance : entry + stopDistance;
   double tp = buy ? entry + stopDistance * InpTP_RiskReward : entry - stopDistance * InpTP_RiskReward;
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   ENUM_ORDER_TYPE orderType = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double volume = CalculateVolume(orderType, entry, sl);
   if(volume <= 0)
   {
      Print("No trade: calculated volume is below broker minimum or risk calculation failed.");
      return false;
   }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpMaxDeviationPoints);
   trade.SetTypeFillingBySymbol(TradeSymbol());

   bool ok = buy
      ? trade.Buy(volume, TradeSymbol(), 0.0, sl, tp, "PA liquidity sweep BUY")
      : trade.Sell(volume, TradeSymbol(), 0.0, sl, tp, "PA liquidity sweep SELL");

   if(ok)
   {
      lastEntryBar = iTime(TradeSymbol(), InpSignalTF, 0);
      Print("Opened ", (buy ? "BUY" : "SELL"), " volume=", volume, " SL=", sl, " TP=", tp);
   }
   else
   {
      Print("Order failed. Retcode=", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
   }
   return ok;
}

int OnInit()
{
   string sym = TradeSymbol();
   if(!SymbolSelect(sym, true))
   {
      Print("Could not select symbol: ", sym);
      return INIT_FAILED;
   }

   hATR = iATR(sym, InpSignalTF, InpATRPeriod);
   hTrendFast = iMA(sym, InpTrendTF, InpFastEMA, 0, MODE_EMA, PRICE_CLOSE);
   hTrendSlow = iMA(sym, InpTrendTF, InpSlowEMA, 0, MODE_EMA, PRICE_CLOSE);
   if(hATR == INVALID_HANDLE || hTrendFast == INVALID_HANDLE || hTrendSlow == INVALID_HANDLE)
   {
      Print("Failed to create indicator handles.");
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpMaxDeviationPoints);
   RefreshDailyBaseline();
   Print("Gold PA Liquidity EA v1.02 initialized on ", sym, ". Real trading allowed: ", InpAllowRealTrading);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(hATR != INVALID_HANDLE) IndicatorRelease(hATR);
   if(hTrendFast != INVALID_HANDLE) IndicatorRelease(hTrendFast);
   if(hTrendSlow != INVALID_HANDLE) IndicatorRelease(hTrendSlow);
   Comment("");
}

void OnTick()
{
   ManageBreakEven();
   RefreshDailyBaseline();

   if(!IsTradingAllowedAccount()) return;
   if(DailyLossLimitHit())
   {
      Comment("Gold PA EA: daily loss limit reached; no new trades today.");
      return;
   }
   if(!IsNewSignalBar()) return;

   double spreadPoints = CurrentSpreadPoints();
   UpdateStatus(spreadPoints);
   if(spreadPoints < 0 || spreadPoints > InpMaxSpreadPoints)
   {
      Print("Skipped: spread ", DoubleToString(spreadPoints, 0),
            " points is above limit ", DoubleToString(InpMaxSpreadPoints, 0), ".");
      return;
   }
   if(CountOurPositions() >= InpMaxOpenPositions) return;
   if(!CooldownPassed()) return;

   bool buySignal = InpAllowBuy && BullishSweepSignal();
   bool sellSignal = InpAllowSell && BearishSweepSignal();

   if(InpCloseOnOppositeSignal)
   {
      // Deliberately not implemented as a blind close; exits remain SL/TP/break-even managed.
      // This option is reserved to avoid accidental closing on noisy opposite signals.
   }

   if(buySignal && !sellSignal) OpenTrade(true);
   else if(sellSignal && !buySignal) OpenTrade(false);
}
