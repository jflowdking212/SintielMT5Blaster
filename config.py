"""
Central configuration for the MT5 + Claude signal-alert bot.

Secrets (API keys, tokens, passwords) are loaded from a separate `.env`
file via python-dotenv, NOT stored here -- this file only holds
tunable settings that are safe to share, back up, or commit to a repo.
See `.env.example` for the secrets template: copy it to `.env` and
fill in your real values there. `.env` should never be committed or
shared -- it's already listed in `.gitignore`.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)  # reads .env in this directory into environment variables


def _require_env(key: str) -> str:
    """Fail loudly and early if a required secret is missing, rather than
    silently running with a placeholder string and failing confusingly
    later deep inside an API call."""
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Copy .env.example to .env and fill in your real values."
        )
    return value.strip()


# --- Anthropic API ---
ANTHROPIC_API_KEY = _require_env("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# --- MT5 ---
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")
MT5_PATH = os.getenv("MT5_PATH") or None  # e.g. r"C:\Program Files\MetaTrader 5\terminal64.exe"

# --- Symbols to watch ---
SYMBOLS = ["EURUSD", "XAUUSD"]

# --- Timeframe for analysis (MT5 constant name, resolved in mt5_data.py) ---
TIMEFRAME = "H1"   # e.g. "M15", "H1", "H4", "D1"
BARS_LOOKBACK = 200  # how many bars of history to pull for indicator calc

# --- Polling ---
POLL_INTERVAL_SECONDS = 60 * 30  # check every 30 minutes; align with your timeframe

# --- Risk / gating rules (deterministic, NOT decided by Claude) ---
MIN_CONFIDENCE_TO_ALERT = 0.65   # below this, don't even notify
MAX_OPEN_TRADES = 3
MAX_DAILY_ALERTS_PER_SYMBOL = 4
MAX_SAME_DIRECTION_TRADES_PER_SYMBOL = 2  # max allowed open trades in the same direction on a single symbol

# How long an alert remains interactive in Telegram before timing out (seconds)
RESPONSE_TIMEOUT_SECONDS = 900   # 15 minutes

# Max allowed price drift (in ATR multiples) between signal alert creation and button tap.
# If current price has moved further than this against the signal, trade execution is blocked.
MAX_SLIPPAGE_ATR_MULTIPLE = 0.5

# Whether to export claude_signal_<SYMBOL>.json to MT5's MQL5/Files folder for SentinelEA.mq5
EXPORT_EA_SIGNAL_FILES = True


# --- Chart image analysis ---
# When True, renders an actual candlestick chart image and sends it to
# Claude alongside the indicator numbers, so it can visually identify
# chart patterns (head & shoulders, double tops, triangles) and
# candlestick patterns (engulfing, pin bars, doji) -- shapes that are
# invisible when Claude only sees indicator values. Adds a small amount
# of image-token cost per call (see README for the estimate).
USE_CHART_IMAGE = True
CHART_IMAGE_BARS = 90   # how many candles the chart image shows

# Image resolution -- lower = smaller image = fewer tokens, at some cost
# to visual clarity. 110 is a reasonable middle ground; try dropping to
# 80-90 if you want to trim image-token cost further after checking
# real usage on console.anthropic.com. Chart patterns (head & shoulders,
# engulfing candles, etc.) are usually still clearly visible well below 110.
CHART_IMAGE_DPI = 110

# Which symbols actually get a chart image, when USE_CHART_IMAGE is True.
# None (default) = every symbol in SYMBOLS gets one (subject to
# CHART_IMAGE_MIN_INTERVAL_MINUTES below). Set this to a subset list
# (e.g. ["EURUSD"]) to only render/send images for the symbols you
# actually want visual pattern analysis on, and skip it (indicator-only)
# for the rest -- useful if you're watching several pairs but only
# care about chart patterns on one or two of them.
CHART_IMAGE_SYMBOLS = ["XAUUSD"]

# How often (in minutes) to actually INCLUDE a chart image in the
# analysis call, independent of how often the bot polls. Since each
# Claude call is stateless, sending an image every single poll costs
# tokens every time regardless of whether the chart changed -- most
# polls don't need a fresh image because the underlying candles haven't
# changed enough to matter yet. Cycles that skip the image still get
# full indicator-based analysis -- they just don't get the visual read.
#
# Set per TIMEFRAME (the analysis timeframe, config.TIMEFRAME above),
# since a sensible image interval depends heavily on how fast that
# timeframe's candles actually move -- a 15-min chart barely changes in
# 15 minutes, but a Daily chart barely changes even in a few hours.
# If the current config.TIMEFRAME isn't listed here, falls back to
# CHART_IMAGE_INTERVAL_DEFAULT_MINUTES below.
#
# Examples of what these produce, combined with POLL_INTERVAL_SECONDS:
#   TIMEFRAME=M15, interval=30  + 15-min polling -> image ~every other cycle
#   TIMEFRAME=M30, interval=60  + 30-min polling -> image ~every other cycle
#   TIMEFRAME=H1,  interval=60  + 30-min polling -> image ~every other cycle
#   TIMEFRAME=H4,  interval=120 + 30-min polling -> image ~every 4th cycle
CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME = {
    "M15": 30,
    "M30": 60,
    "H1": 60,
    "H4": 120,
    "D1": 240,
}
CHART_IMAGE_INTERVAL_DEFAULT_MINUTES = 60   # used if TIMEFRAME isn't in the dict above

# --- Position sizing (dollar-risk based) ---
# "FIXED": always risk RISK_AMOUNT_USD, regardless of account balance.
# "PERCENT": risk RISK_PERCENT % of your CURRENT account balance, recalculated
#   fresh each trade -- so stake naturally scales with account growth/drawdown.
POSITION_SIZE_MODE = "FIXED"   # "FIXED" or "PERCENT"

RISK_AMOUNT_USD = 5.0   # used when POSITION_SIZE_MODE = "FIXED"
RISK_PERCENT = 1.0      # used when POSITION_SIZE_MODE = "PERCENT" (e.g. 1.0 = 1%)

# If the calculated stake is below the broker's minimum lot size:
# False (default) -- block the trade and tell the user the exact minimum
#   risk amount needed (matches how the bot already behaves).
# True -- automatically bump the trade up to the broker's minimum lot size
#   instead of blocking, and log that an override happened (so it's
#   visible later that this trade wasn't sized normally).
AUTO_ADJUST_TO_MIN_LOT = True

# --- Take-profit mode ---
# "NATIVE" (default): use Claude's own suggested take-profit (when you pick
#   Claude's plan) or the standard ATR-based take-profit (when you pick the
#   ATR plan, using TP_ATR_MULTIPLE below). Each source's own target is used
#   as-is -- REWARD_MULTIPLE is NOT applied in this mode.
# "REWARD_MULTIPLE": ignore Claude's/ATR's native target entirely and force
#   the take-profit to REWARD_MULTIPLE x your risk (e.g. $5 risk, 2.0x ->
#   $10 target), regardless of what Claude or ATR originally suggested.
# Only trades placed in REWARD_MULTIPLE mode use REWARD_MULTIPLE; NATIVE
# mode (the default) always uses the selected plan's own take-profit.
TP_MODE = "NATIVE"   # "NATIVE" or "REWARD_MULTIPLE"

# Only used when TP_MODE = "REWARD_MULTIPLE"
REWARD_MULTIPLE = 2.0

# Only used when TP_MODE = "NATIVE" and you pick the ATR plan -- sets the
# ATR plan's own take-profit distance (separate from the SL_ATR_MULTIPLE
# used for its stop-loss).
TP_ATR_MULTIPLE = 3.0

# Option A (default, False): alert on everything above MIN_CONFIDENCE_TO_ALERT,
#   including counter-trend setups (H4 bias disagrees with Daily trend) --
#   Claude just scores these lower and explains the conflict in its reasoning.
# Option B (True): hard-block any alert where the H4 bias disagrees with the
#   Daily trend, no matter how high the confidence score is. Fewer alerts,
#   only "with the trend" setups.
BLOCK_COUNTER_TREND_SIGNALS = False

# --- Logging ---
LOG_FILE = "bot_log.jsonl"

# --- Outcome tracking (scorecard) ---
SIGNALS_DB = "signals.db"

# SL distance in ATR multiples, used for the "standard ATR plan" option
# (order_executor.py) and for judging hypothetical outcomes on ignored
# signals (outcome_checker.py), so both use identical stop-loss logic.
SL_ATR_MULTIPLE = 1.5

# How long to wait before giving up on a signal that never clearly hit its
# hypothetical TP or SL (applies to ignored/rejected signals only -- real
# trades are evaluated from MT5's own closed-trade history instead).
OUTCOME_EVAL_WINDOW_HOURS = 72

# --- Kill switch ---
# Independent of every other rule above -- send /pause to the bot's
# Telegram chat at any time to immediately stop new signal alerts
# (open trades are untouched). Send /resume to continue, /status to
# check current state. State persists in BOT_STATE_FILE across restarts.
BOT_STATE_FILE = "bot_state.json"

# --- Statistical rigor for method/pattern performance ---
# Don't treat a method_used tag (e.g. "chart_pattern") or pattern_detail
# (e.g. "ascending triangle") as a proven edge until it has at least this
# many evaluated occurrences in signals.db -- below this, dashboard.py
# will label it "insufficient sample" rather than showing a win rate that
# looks meaningful but isn't.
MIN_OCCURRENCES_FOR_EDGE = 100
