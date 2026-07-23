"""
Handles the MT5 connection and pulls price + indicator data.
Requires: pip install MetaTrader5 pandas numpy ta mplfinance matplotlib
"""

import base64
import io
import json
import os
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")  # headless rendering, no display needed
import mplfinance as mpf
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator
from ta.volatility import AverageTrueRange

import config

TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def connect():
    """Initialize connection to the MT5 terminal."""
    kwargs = {}
    if config.MT5_PATH:
        kwargs["path"] = config.MT5_PATH

    ok = mt5.initialize(**kwargs)
    if not ok:
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    if config.MT5_LOGIN:
        authorized = mt5.login(
            config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
        if not authorized:
            raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

    print("Connected to MT5:", mt5.terminal_info())


def disconnect():
    mt5.shutdown()


def get_ohlc(symbol: str, timeframe: str = None, bars: int = None) -> pd.DataFrame:
    """Pull OHLC candle data for a symbol into a DataFrame."""
    tf = TIMEFRAME_MAP[timeframe or config.TIMEFRAME]
    n = bars or config.BARS_LOOKBACK

    rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No data returned for {symbol}: {mt5.last_error()}")

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach standard indicators to the OHLC dataframe."""
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema_20"] = EMAIndicator(close, window=20).ema_indicator()
    df["ema_50"] = EMAIndicator(close, window=50).ema_indicator()
    df["rsi_14"] = RSIIndicator(close, window=14).rsi()

    macd = MACD(close)
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["atr_14"] = AverageTrueRange(high, low, close, window=14).average_true_range()

    return df


def get_higher_timeframe_trend(symbol: str, timeframe: str = "D1", bars: int = 60) -> dict:
    """
    Pulls a *separate*, coarser timeframe (default Daily) purely to
    establish the broader trend context -- is the bigger picture
    bullish, bearish, or range-bound while the analysis timeframe
    (H4) looks the way it does. Kept deliberately tiny: a handful of
    numbers, not another full candle series, to keep token cost low.
    """
    df = get_ohlc(symbol, timeframe=timeframe, bars=bars)
    df = compute_indicators(df)
    last = df.iloc[-1]

    if last["ema_20"] > last["ema_50"]:
        trend = "uptrend"
    elif last["ema_20"] < last["ema_50"]:
        trend = "downtrend"
    else:
        trend = "flat"

    return {
        "higher_tf": timeframe,
        "higher_tf_trend": trend,
        "higher_tf_rsi": round(float(last["rsi_14"]), 2),
        "higher_tf_price_vs_ema50": "above" if last["close"] > last["ema_50"] else "below",
    }


def summarize_for_prompt(symbol: str, df: pd.DataFrame, lookback: int = 60,
                          include_higher_tf: bool = True) -> dict:
    """
    Reduce indicator dataframe down to a compact structured summary
    that's cheap to send to Claude and easy for it to reason over.

    `lookback` widened from 30 -> 60 bars so swing high/low reflects a
    more meaningful stretch of price history (on H4, ~60 bars is
    roughly the last 10 trading days).
    """
    recent = df.tail(lookback)
    last = df.iloc[-1]

    swing_high = recent["high"].max()
    swing_low = recent["low"].min()

    summary = {
        "symbol": symbol,
        "timeframe": config.TIMEFRAME,
        "last_close": round(float(last["close"]), 5),
        "ema_20": round(float(last["ema_20"]), 5),
        "ema_50": round(float(last["ema_50"]), 5),
        "rsi_14": round(float(last["rsi_14"]), 2),
        "macd": round(float(last["macd"]), 5),
        "macd_signal": round(float(last["macd_signal"]), 5),
        "macd_hist": round(float(last["macd_hist"]), 5),
        "atr_14": round(float(last["atr_14"]), 5),
        "recent_swing_high": round(float(swing_high), 5),
        "recent_swing_low": round(float(swing_low), 5),
        "price_vs_ema20": "above" if last["close"] > last["ema_20"] else "below",
        "price_vs_ema50": "above" if last["close"] > last["ema_50"] else "below",
        "recent_closes": [round(float(c), 5) for c in recent["close"].tolist()[-10:]],
    }

    if include_higher_tf:
        summary.update(get_higher_timeframe_trend(symbol))

    return summary


def render_chart_image(symbol: str, df: pd.DataFrame, bars: int = 90) -> str:
    """
    Renders a candlestick chart (with EMA20/50 overlay) as a base64 PNG
    string, so Claude can visually inspect it for chart patterns
    (head & shoulders, double tops/bottoms, triangles, flags) and
    candlestick patterns (engulfing, pin bars, doji at key levels) --
    things that only show up as a SHAPE, not as a single indicator
    value, and so were invisible to Claude when it only saw numbers.

    Kept to ~90 bars so the chart stays visually readable (too many
    candles compresses everything into noise) and the image stays
    small enough to be cheap to send.
    """
    chart_df = df.tail(bars).copy()
    chart_df = chart_df.set_index(pd.DatetimeIndex(chart_df["time"]))
    chart_df = chart_df.rename(columns={
        "open": "Open", "high": "High", "low": "Low", "close": "Close",
        "tick_volume": "Volume",
    })

    ema20 = chart_df["ema_20"] if "ema_20" in chart_df.columns else None
    ema50 = chart_df["ema_50"] if "ema_50" in chart_df.columns else None

    add_plots = []
    if ema20 is not None:
        add_plots.append(mpf.make_addplot(ema20, color="dodgerblue", width=1.0))
    if ema50 is not None:
        add_plots.append(mpf.make_addplot(ema50, color="orange", width=1.0))

    buf = io.BytesIO()
    mpf.plot(
        chart_df,
        type="candle",
        style="yahoo",
        addplot=add_plots if add_plots else None,
        title=f"{symbol} ({config.TIMEFRAME})",
        volume=False,
        savefig=dict(fname=buf, format="png", dpi=config.CHART_IMAGE_DPI, bbox_inches="tight"),
    )
    buf.seek(0)
    encoded = base64.b64encode(buf.read()).decode("utf-8")
    buf.close()
    return encoded


# --- Chart image scheduling ---
# The image is the single biggest token cost per Claude call (roughly
# 700-1,700 tokens depending on rendered size, vs ~500-800 for
# everything else combined). Since each API call is stateless, "caching
# the rendered PNG" doesn't save any tokens by itself -- if an image is
# INCLUDED in a request, it's counted every time, whether the chart
# changed or not. The only real saving is skipping the image on most
# cycles and only including it periodically, falling back to
# indicator-only analysis in between. This tracks, per symbol, when an
# image was last actually sent to Claude.
#
# In-memory only (resets on restart) -- worst case after a restart is
# one extra image send sooner than scheduled, which is harmless.
_last_chart_sent_at = {}


def should_include_chart_image(symbol: str) -> bool:
    """
    Returns True if it's time to include a fresh chart image for this
    symbol, based on config.CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME
    (keyed by the current config.TIMEFRAME) -- e.g. with 30-min polling
    on H4 (interval 120) this returns True roughly every 4th cycle,
    while M15 (interval 30) would return True roughly every other cycle.
    """
    interval_minutes = config.CHART_IMAGE_INTERVAL_MINUTES_BY_TIMEFRAME.get(
        config.TIMEFRAME, config.CHART_IMAGE_INTERVAL_DEFAULT_MINUTES
    )

    last_sent = _last_chart_sent_at.get(symbol)
    if last_sent is None:
        return True

    elapsed_minutes = (datetime.now(timezone.utc) - last_sent).total_seconds() / 60
    return elapsed_minutes >= interval_minutes


def mark_chart_image_sent(symbol: str):
    _last_chart_sent_at[symbol] = datetime.now(timezone.utc)


def export_signal_for_ea(symbol: str, analysis: dict):
    """
    Export signal details to claude_signal_<SYMBOL>.json in MT5's MQL5/Files folder,
    allowing SentinelEA.mq5 to display live alerts directly on the MT5 chart.
    """
    if not getattr(config, "EXPORT_EA_SIGNAL_FILES", False):
        return

    payload = {
        "symbol": symbol,
        "bias": analysis.get("bias"),
        "confidence": analysis.get("confidence"),
        "structure_quality": analysis.get("structure_quality"),
        "reasoning": analysis.get("reasoning", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    target_dirs = []
    try:
        info = mt5.terminal_info()
        if info and getattr(info, "data_path", None):
            mql5_files = os.path.join(info.data_path, "MQL5", "Files")
            if os.path.exists(mql5_files):
                target_dirs.append(mql5_files)
    except Exception:
        pass

    target_dirs.append(".")  # Fallback to local working directory

    filename = f"claude_signal_{symbol}.json"
    for target_dir in target_dirs:
        try:
            filepath = os.path.join(target_dir, filename)
            with open(filepath, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            print(f"[{symbol}] Failed writing EA signal file to {target_dir}: {e}")

