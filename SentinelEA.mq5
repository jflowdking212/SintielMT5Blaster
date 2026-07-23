//+------------------------------------------------------------------+
//| SentinelEA.mq5                                                |
//|                                                                    |
//| Companion EA to the Python bridge. It does NOT call the Claude    |
//| API directly -- MT5's WebRequest() is workable but clunky for     |
//| JSON-heavy calls, so the Python script (main.py) does the heavy   |
//| lifting and writes a small signal file. This EA just watches that |
//| file, displays the analysis on-chart, and lets you click          |
//| Buy / Sell / Ignore right from the terminal instead of Telegram.  |
//|                                                                    |
//| Signal file format (written by Python, one JSON object per line   |
//| in MQL5_Files/claude_signal_<SYMBOL>.json):                       |
//| {"bias":"bearish","confidence":0.78,"structure_quality":"clear",  |
//|  "reasoning":"...", "timestamp":"2026-07-21T12:00:00"}            |
//+------------------------------------------------------------------+
#property strict

input string  SignalFilePrefix   = "claude_signal_"; // file prefix, matches Python's output
input int     CheckIntervalSecs  = 30;                // how often to check for a new signal
input double  DefaultLotSize     = 0.01;
input double  SL_ATR_Multiple    = 1.5;
input double  TP_ATR_Multiple    = 3.0;

datetime lastSignalTime = 0;
string   currentBias = "";
double   currentConfidence = 0;
string   currentReasoning = "";
bool     buttonsVisible = false;

//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(CheckIntervalSecs);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   RemoveButtons();
}

//+------------------------------------------------------------------+
//| Poll the signal file written by the Python bridge                 |
//+------------------------------------------------------------------+
void OnTimer()
{
   string filename = SignalFilePrefix + _Symbol + ".json";

   if(!FileIsExist(filename))
      return;

   int handle = FileOpen(filename, FILE_READ|FILE_TXT|FILE_ANSI);
   if(handle == INVALID_HANDLE)
      return;

   string content = "";
   while(!FileIsEnding(handle))
      content += FileReadString(handle);
   FileClose(handle);

   if(StringLen(content) == 0)
      return;

   // Minimal manual JSON field extraction (avoids needing a full JSON lib)
   string bias        = ExtractJsonValue(content, "bias");
   string confStr      = ExtractJsonValue(content, "confidence");
   string reasoning    = ExtractJsonValue(content, "reasoning");
   string timestampStr = ExtractJsonValue(content, "timestamp");

   if(bias == currentBias && confStr == DoubleToString(currentConfidence, 2))
      return; // no new signal since last check

   currentBias = bias;
   currentConfidence = StringToDouble(confStr);
   currentReasoning = reasoning;

   ShowSignalOnChart();
}

//+------------------------------------------------------------------+
//| Very small helper to pull "key":"value" or "key":number pairs     |
//+------------------------------------------------------------------+
string ExtractJsonValue(string json, string key)
{
   string search = "\"" + key + "\":";
   int pos = StringFind(json, search);
   if(pos < 0) return "";

   int start = pos + StringLen(search);
   // skip whitespace and optional opening quote
   while(start < StringLen(json) && (StringGetCharacter(json, start) == ' '))
      start++;
   bool quoted = (StringGetCharacter(json, start) == '"');
   if(quoted) start++;

   int end = start;
   while(end < StringLen(json))
     {
      ushort ch = StringGetCharacter(json, end);
      if(quoted && ch == '"') break;
      if(!quoted && (ch == ',' || ch == '}')) break;
      end++;
     }

   return StringSubstr(json, start, end - start);
}

//+------------------------------------------------------------------+
//| Display the analysis + Buy/Sell/Ignore buttons on the chart       |
//+------------------------------------------------------------------+
void ShowSignalOnChart()
{
   Comment(
      "Claude signal for ", _Symbol, "\n",
      "Bias: ", currentBias, "  Confidence: ", DoubleToString(currentConfidence * 100, 0), "%\n",
      currentReasoning
   );

   CreateButton("btnBuy",    "BUY",    10, 20, clrLimeGreen);
   CreateButton("btnSell",   "SELL",   90, 20, clrTomato);
   CreateButton("btnIgnore", "IGNORE", 170, 20, clrSilver);

   buttonsVisible = true;
}

void CreateButton(string name, string text, int x, int y, color clr)
{
   if(ObjectFind(0, name) >= 0)
      ObjectDelete(0, name);

   ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, 70);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, 25);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

void RemoveButtons()
{
   ObjectDelete(0, "btnBuy");
   ObjectDelete(0, "btnSell");
   ObjectDelete(0, "btnIgnore");
   Comment("");
}

//+------------------------------------------------------------------+
//| Handle button clicks                                              |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_CLICK) return;

   if(sparam == "btnBuy")
     {
      ExecuteTrade(ORDER_TYPE_BUY);
      RemoveButtons();
     }
   else if(sparam == "btnSell")
     {
      ExecuteTrade(ORDER_TYPE_SELL);
      RemoveButtons();
     }
   else if(sparam == "btnIgnore")
     {
      Print("User ignored Claude signal for ", _Symbol);
      RemoveButtons();
     }
}

//+------------------------------------------------------------------+
//| Place the trade. Risk management stays here in the EA, not in the |
//| signal -- Claude's output only ever informed the bias/confidence  |
//+------------------------------------------------------------------+
void ExecuteTrade(ENUM_ORDER_TYPE orderType)
{
   double atr = iATR(_Symbol, PERIOD_CURRENT, 14, 0);
   double price = (orderType == ORDER_TYPE_BUY) ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                                                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);

   double slDist = atr * SL_ATR_Multiple;
   double tpDist = atr * TP_ATR_Multiple;

   double sl = (orderType == ORDER_TYPE_BUY) ? price - slDist : price + slDist;
   double tp = (orderType == ORDER_TYPE_BUY) ? price + tpDist : price - tpDist;

   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);

   request.action    = TRADE_ACTION_DEAL;
   request.symbol    = _Symbol;
   request.volume    = DefaultLotSize;
   request.type      = orderType;
   request.price     = price;
   request.sl        = NormalizeDouble(sl, _Digits);
   request.tp        = NormalizeDouble(tp, _Digits);
   request.deviation = 10;
   request.magic     = 20260721;
   request.comment   = "Sentinel signal";

   if(!OrderSend(request, result))
      Print("OrderSend failed: ", result.retcode, " ", result.comment);
   else
      Print("Order placed: ", EnumToString(orderType), " ticket=", result.order);
}
