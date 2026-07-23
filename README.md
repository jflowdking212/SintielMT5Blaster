# Sentinel — MT5 + Claude Signal Alert Bot

Notify-first architecture: Claude analyzes market data, a deterministic
risk gate decides whether to alert you, and you tap Buy / Sell / Ignore
in Telegram. Nothing trades without your tap in this version.

## Architecture

```
MT5 (price + indicators)
        |
        v
mt5_data.py  -->  builds a compact JSON summary per symbol
        |
        v
claude_analyzer.py  -->  sends summary to Claude, gets back
                          {bias, confidence, structure_quality, reasoning}
        |
        v
risk_gate.py  -->  hard-coded rules decide if this is even worth
                    alerting on (confidence threshold, daily caps,
                    max open trades) -- Claude has no say here
        |
        v
telegram_notifier.py  -->  sends alert with Buy/Sell/Ignore buttons,
                            waits for your tap
        |
        v
order_executor.py  -->  places the order in MT5 if you tapped Buy/Sell
        |
        v
signal_tracker.py  -->  logs every signal + your decision to signals.db
        |
        v
outcome_checker.py -->  later, checks whether each signal was actually
                         right -- real trades via MT5's own trade history,
                         ignored/rejected signals via hypothetical
                         SL/TP projection using the same risk logic
        |
        v
dashboard.py  -->  prints a scorecard: overall accuracy, accuracy by
                    confidence bucket, by symbol, and aligned vs
                    counter-trend
```

Every step logs to `bot_log.jsonl` so you can review Claude's raw reads,
gate decisions, your responses, and order outcomes later. `signals.db`
is the structured version of the same story, built specifically for
the scorecard.

## Money management (no manual lot-size math)

Position size (lot size) is always dollar-based. You set:

```python
RISK_AMOUNT_USD = 5.0    # what you're willing to lose if SL is hit
```

For every trade, `position_sizer.py` works out the exact lot size so a
full stop-loss hit costs exactly `RISK_AMOUNT_USD` -- regardless of
which symbol it is or how far away the stop happens to be.

**Take-profit follows `config.TP_MODE`, which defaults to `"NATIVE"`:**

```python
TP_MODE = "NATIVE"        # default -- see below
# TP_MODE = "REWARD_MULTIPLE"

REWARD_MULTIPLE = 2.0     # only used if TP_MODE = "REWARD_MULTIPLE"
TP_ATR_MULTIPLE = 3.0     # only used if TP_MODE = "NATIVE" + ATR plan
```

- **`"NATIVE"` (default):** each plan's own take-profit is used as-is --
  Claude's own suggested target (when you pick "Claude's plan"), or the
  ATR-based target via `TP_ATR_MULTIPLE` (when you pick "ATR plan").
  `REWARD_MULTIPLE` is *not* applied in this mode.
- **`"REWARD_MULTIPLE"`:** overrides whichever target Claude/ATR
  suggested and forces take-profit to `RISK_AMOUNT_USD x REWARD_MULTIPLE`
  (e.g. $5 risk, 2.0x -> $10 target), regardless of source.

Every Telegram alert shows the calculated lot size and dollar
risk/reward for both plan options, computed using whichever `TP_MODE`
is currently set, so you always see the numbers that will actually be
used before you tap anything.

**Minimum risk check:** if `RISK_AMOUNT_USD` is too small to open even
the broker's minimum lot size at a given stop distance (this varies by
symbol and by how wide the stop is), the bot won't guess or round up --
it tells you the exact minimum you'd need instead, e.g.:

> ⚠️ EURUSD: couldn't place that trade -- $1.00 is below the minimum
> tradeable size for EURUSD at this stop distance. Minimum risk needed: $3.10

This can also show up as a preview in the alert itself, before you even
tap a button, so you know in advance if an option isn't viable at your
current risk setting.



## Choosing which stop-loss (and take-profit) source to trade with

Each alert offers two ways to take a trade, when Claude has proposed a
stop-loss level for that setup:

- **Buy/Sell (Claude's plan)** -- uses Claude's suggested stop-loss,
  and (in `TP_MODE = "NATIVE"`) Claude's own suggested take-profit too.
- **Buy/Sell (ATR plan)** -- uses the standard `SL_ATR_MULTIPLE`
  calculation, and (in `TP_MODE = "NATIVE"`) the ATR-based take-profit
  via `TP_ATR_MULTIPLE`.

Lot size is always computed the same way (see Money management above).
If `TP_MODE` is set to `"REWARD_MULTIPLE"`, both options ignore their
native take-profit and use the same risk-multiple math instead.

If Claude didn't propose a level for a setup (neutral bias or unclear
structure), only the ATR-based buttons show up.

Whichever you pick is recorded in `signals.db` (`plan_source_used`), so
over time you can compare which stop-loss/take-profit source actually
performs better via `dashboard.py`.

## Outcome tracking (scorecard)

Every signal Claude produces gets logged to `signals.db`, whether or
not the gate lets it through to you. Once you respond (Buy/Sell/Ignore),
that decision is recorded too.

`outcome_checker.py` runs automatically at the end of each polling
cycle in `main.py` (you can also run it standalone: `python
outcome_checker.py`). It resolves signals two different ways:

- **If you took the trade** (a real MT5 order was placed): the outcome
  comes from MT5's own closed-trade history. Profit > 0 = SUCCESS,
  otherwise FAILURE. No guessing.
- **If you ignored/rejected it** (or the order failed to place): there's
  no real P&L, so it checks whether price *would have* moved far enough
  to hit the same SL/TP distance a real trade would have used
  (`config.SL_ATR_MULTIPLE` and `REWARD_MULTIPLE`). This is a directional
  accuracy check, not real profit -- treat it as "was Claude's call
  right," not "would you have made money."
- Signals that haven't clearly hit TP or SL within
  `config.OUTCOME_EVAL_WINDOW_HOURS` (default 72h) are marked EXPIRED
  rather than left pending forever.

Run `python dashboard.py` any time to see:
- Overall accuracy (successes / total evaluated)
- Accuracy on trades you actually took vs ones you ignored
- Accuracy broken down by confidence bucket (do 85%+ calls actually
  perform better than 65-74% ones?)
- Accuracy by symbol
- Accuracy when the signal agreed with the higher-timeframe trend vs
  fought it -- useful evidence for deciding whether to ever flip
  `BLOCK_COUNTER_TREND_SIGNALS` to `True`


## Setup

1. Install MetaTrader 5 desktop terminal (Windows, or via Wine on
   Mac/Linux) and log into your broker account there at least once.
2. `pip install -r requirements.txt`
3. **Secrets go in `.env`, not `config.py`:**
   ```
   cp .env.example .env
   ```
   Then edit `.env` and fill in:
   - `ANTHROPIC_API_KEY` -- from console.anthropic.com
   - `TELEGRAM_BOT_TOKEN` -- create a bot via @BotFather in Telegram
   - `TELEGRAM_CHAT_ID` -- message your new bot once, then visit
     `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and copy
     the `chat.id` value from the response
   - `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` -- your broker account
     credentials (or leave blank if MT5 terminal is already logged in)

   `.env` is already listed in `.gitignore` and should never be
   committed, shared, or uploaded anywhere. `config.py` only imports
   these values via environment variables -- it no longer contains any
   real secrets itself, so it's safe to share or back up.
4. Tune everything else (non-secret) directly in `config.py`:
   `SYMBOLS`, `TIMEFRAME`, risk thresholds, chart image settings, etc.
5. Run: `python main.py`
   (fails immediately with a clear error if any required `.env` value
   is missing, rather than failing confusingly later inside an API call)

## Important notes

- **This is notify-only.** Nothing executes until you tap a button.
  Get comfortable with the alert quality before considering automation.
- **Risk management is separate from Claude on purpose.** Confidence
  thresholds, daily alert caps, and max open trades live in
  `risk_gate.py` as plain code you can read and test -- not something
  the model decides.
- **Fail-safe on errors.** If the Claude API call fails or returns
  something unparseable, that symbol is skipped for the cycle. It
  never falls back to trading blind.
- **Paper test first.** Point `MT5_LOGIN` at a demo account and let
  this run for at least a few weeks before touching a live account.
  Use `bot_log.jsonl` to review how often Claude's reads lined up with
  what actually happened.
- **Telegram polling vs webhooks.** `wait_for_response()` uses simple
  long-polling, which is fine for personal use. If you run this
  24/7 on a server, consider switching to a webhook-based Telegram
  handler instead.

## Enhancements adopted from a broader trading-bot plan

A few ideas worth borrowing from a more elaborate (binary-options-style)
implementation plan, adapted to this forex/MT5 setup:

### 1. Chart image analysis (multi-pattern, not just indicators)

`config.USE_CHART_IMAGE = True` (default). Each analysis cycle can
render an actual candlestick chart (with EMA20/EMA50 overlay) via
`mt5_data.render_chart_image()` and send it to Claude alongside the
indicator numbers. This lets Claude visually identify things that are
invisible in indicator values alone:

- Chart patterns: head & shoulders, double top/bottom, triangles, flags
- Candlestick patterns: engulfing bars, pin bars, doji at key levels

Claude is instructed to scan across multiple analysis families each
cycle (trend, momentum, chart patterns, candlestick patterns,
support/resistance) rather than anchoring on one, and reports which
method(s) actually drove the call via `method_used`, plus
`pattern_detail` (e.g. "bearish engulfing at resistance") when a
specific pattern was identified.

**Chart images are the single biggest token cost per call** (roughly
700-1,700 tokens depending on rendered size, vs ~500-800 for everything
else combined). Since each API call is stateless, including an image
costs the same tokens every time regardless of whether the chart
actually changed -- so `config.CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME`
decouples image frequency from polling frequency, **set per analysis
timeframe** since a sensible interval depends heavily on how fast that
timeframe's own candles move:

```python
CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME = {
    "M15": 30,
    "M30": 60,
    "H1": 60,
    "H4": 120,
    "D1": 240,
}
CHART_IMAGE_INTERVAL_DEFAULT_MINUTES = 60   # fallback if TIMEFRAME isn't listed
```

Verified behavior (30-min polling unless noted):
- `M15` (interval 30, 15-min polling) -> image on every other cycle
- `M30`/`H1` (interval 60) -> image on every other cycle
- `H4` (interval 120) -> image on roughly every 4th cycle
- `D1` (interval 240) -> image on roughly every 8th cycle

Cycles that skip the image still get full indicator-based analysis --
they just don't get the visual pattern read that cycle. This is
tracked per-symbol in memory (`mt5_data.should_include_chart_image()`);
it resets on restart, which just means one extra image gets sent
slightly earlier than scheduled -- harmless.

Turn images off entirely with `USE_CHART_IMAGE = False` to revert to
indicator-only analysis on every cycle (the old cost/behavior).

**All the knobs are in `config.py`, adjustable any time without touching code:**

```python
CHART_IMAGE_DPI = 110              # lower = smaller image = fewer tokens
CHART_IMAGE_BARS = 90              # fewer candles = smaller image
CHART_IMAGE_SYMBOLS = None         # None = all SYMBOLS get images; or e.g. ["EURUSD"]
CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME = {"M15": 30, "M30": 60, "H1": 60, "H4": 120, "D1": 240}
```

`CHART_IMAGE_SYMBOLS` lets you restrict visual pattern analysis to just
the pairs you actually care about seeing charts for -- the rest still
get full indicator-based analysis every cycle, just without the image.

**Rough cost, with defaults (3 symbols, 30-min polling, 60-min image interval):**
roughly half your calls include an image, half don't -- landing around
**$10-20/month**, versus indicator-only analysis at every cycle, which
would run closer to $15-30/month. Actual numbers depend on how large
the rendered chart turns out to be; check console.anthropic.com →
Usage after a day or two of running for your real per-call token count.

### 2. Method/pattern performance tracking

Every signal's `method_used` and `pattern_detail` are now logged to
`signals.db`. `dashboard.py` breaks down accuracy by method (a signal
can count toward multiple methods, e.g. `trend` + `chart_pattern`
together). Following the "don't trust small samples" principle:

- A method's win rate is only shown once it has at least
  `config.MIN_OCCURRENCES_FOR_EDGE` (default 100) evaluated occurrences.
- Below that threshold, the dashboard labels it "insufficient sample"
  instead of showing a win rate that looks meaningful but isn't --
  this is how you'll eventually discover whether your real edge is in
  chart patterns, momentum, or something else entirely, rather than
  assuming it's whichever method you started with.

### 3. Kill switch (Telegram commands)

Independent of every other rule in the bot -- confidence thresholds,
daily caps, everything -- you can send these commands to the bot's
Telegram chat at any time:

- `/pause` -- stops all new signal alerts immediately (checked between
  every symbol within a cycle, not just once per cycle). Open trades
  are untouched.
- `/resume` -- resumes normal operation.
- `/status` -- reports whether the bot is currently active or paused.

State persists in `config.BOT_STATE_FILE` (`bot_state.json`), so a
restart doesn't silently un-pause the bot.

### 4. Percentage-based position sizing (in addition to fixed-$)

`config.POSITION_SIZE_MODE`:
- `"FIXED"` (default) -- always risk `RISK_AMOUNT_USD`, as before.
- `"PERCENT"` -- risk `RISK_PERCENT`% of your CURRENT account balance,
  recalculated fresh via `mt5.account_info()` on every trade, so stake
  naturally scales up as the account grows and down during drawdown.

`config.AUTO_ADJUST_TO_MIN_LOT` (default `False`, matching how the bot
already behaves): if `True`, instead of blocking a trade that's too
small for the broker's minimum lot size, the bot bumps it up to the
minimum automatically and sends a Telegram notification flagging that
an override happened -- so it's visible later (in `signals.db` /
`bot_log.jsonl`) that this trade wasn't sized normally and shouldn't be
compared 1:1 against your other trades.



- Add a lightweight "auto-execute" path for very high confidence
  reads (e.g. >= 0.85) with a small position size cap, while keeping
  everything else notify-only.
- Add the MT5 Expert Advisor (MQL5) that mirrors these alerts directly
  on-chart, for when you're at your desk and prefer working from the
  MT5 terminal instead of Telegram.
