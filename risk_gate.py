"""
Hard, deterministic gating rules. This is intentionally NOT part of the
Claude prompt -- confidence scores and market reads come from the model,
but whether an alert is actually allowed through is decided here, in
plain code you can audit and unit test.
"""

from collections import defaultdict
from datetime import date

import config

# in-memory counters; swap for a real DB/file store if you need persistence
_alerts_today = defaultdict(int)
_last_reset_day = date.today()


def _reset_if_new_day():
    global _last_reset_day
    today = date.today()
    if today != _last_reset_day:
        _alerts_today.clear()
        _last_reset_day = today


def should_alert(symbol: str, analysis: dict, open_trades_count: int,
                  higher_tf_trend: str = None) -> tuple[bool, str]:
    """
    Returns (allowed, reason). Even a high-confidence bullish read from
    Claude can be blocked here for reasons Claude has no visibility into
    (daily alert caps, open trade limits, etc).

    `higher_tf_trend` comes from the market data summary (mt5_data.py),
    not from Claude's response -- it's only used here if
    config.BLOCK_COUNTER_TREND_SIGNALS is turned on.
    """
    _reset_if_new_day()

    if analysis["confidence"] < config.MIN_CONFIDENCE_TO_ALERT:
        return False, f"confidence {analysis['confidence']:.2f} below threshold"

    if analysis["structure_quality"] == "unclear":
        return False, "structure_quality is unclear"

    if analysis["bias"] == "neutral":
        return False, "bias is neutral, nothing actionable"

    if config.BLOCK_COUNTER_TREND_SIGNALS:
        is_counter_trend = (
            (analysis["bias"] == "bullish" and higher_tf_trend == "downtrend") or
            (analysis["bias"] == "bearish" and higher_tf_trend == "uptrend")
        )
        if is_counter_trend:
            return False, f"counter-trend blocked (H4 {analysis['bias']} vs Daily {higher_tf_trend})"

    if open_trades_count >= config.MAX_OPEN_TRADES:
        return False, f"max open trades ({config.MAX_OPEN_TRADES}) reached"

    # Enforce max same-direction open trades per symbol
    max_same_dir = getattr(config, "MAX_SAME_DIRECTION_TRADES_PER_SYMBOL", 2)
    try:
        import MetaTrader5 as mt5
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            target_type = mt5.POSITION_TYPE_BUY if analysis["bias"] == "bullish" else mt5.POSITION_TYPE_SELL
            same_dir_count = sum(1 for p in positions if p.type == target_type)
            if same_dir_count >= max_same_dir:
                return False, f"max open {analysis['bias'].upper()} trades for {symbol} ({max_same_dir}) reached"
    except Exception:
        pass

    if _alerts_today[symbol] >= config.MAX_DAILY_ALERTS_PER_SYMBOL:
        return False, f"daily alert cap reached for {symbol}"

    _alerts_today[symbol] += 1
    return True, "passed all gates"
