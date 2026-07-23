"""
Run this periodically (e.g. once an hour via cron/Task Scheduler, or just
call check_all() at the end of each main.py cycle) to evaluate signals
that now have enough time/price movement behind them to judge.

Two evaluation paths:
1. Real trades (you tapped Buy/Sell and an order was placed): outcome
   comes straight from MT5's own closed-trade history -- profit > 0 is
   SUCCESS, profit <= 0 is FAILURE. No guessing involved.
2. Ignored/rejected/no-order signals: there's no real P&L, so we check
   whether price *would have* hit the same TP/SL distance a real trade
   would have used (config.SL_ATR_MULTIPLE / TP_ATR_MULTIPLE), using
   current price vs the entry price recorded at signal time. This is a
   hypothetical judgment, not a real outcome -- treat it as directional
   accuracy, not P&L.
"""

from datetime import datetime, timezone

import MetaTrader5 as mt5

import config
import signal_tracker


def _hours_since(iso_timestamp: str) -> float:
    created = datetime.fromisoformat(iso_timestamp)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 3600


def _evaluate_real_trade(row: dict):
    """Look up the closed deal for this ticket in MT5 history."""
    ticket = row["order_ticket"]
    deals = mt5.history_deals_get(position=ticket)

    if not deals:
        return None  # still open, or not found yet -- check again later

    total_profit = sum(d.profit for d in deals)
    is_closed = any(d.entry == mt5.DEAL_ENTRY_OUT for d in deals)

    if not is_closed:
        return None  # still running

    if total_profit > 0:
        return "SUCCESS", f"Closed trade, profit={total_profit:.2f}"
    else:
        return "FAILURE", f"Closed trade, profit={total_profit:.2f}"


def _evaluate_hypothetical(row: dict):
    """
    For signals you ignored or rejected: did price move far enough in
    the predicted direction to have hit a hypothetical TP, or against it
    to have hit a hypothetical SL, using the same ATR multiples real
    trades use?
    """
    symbol = row["symbol"]
    entry_price = row["entry_price"]
    atr = row["atr_value"]
    bias = row["bias"]

    if entry_price is None or atr is None:
        return "EXPIRED", "Missing entry_price or atr_value at signal time"

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None  # can't fetch price right now, try again later

    current_price = (tick.bid + tick.ask) / 2

    # Same math the ATR plan would actually use: SL distance from ATR,
    # and TP following config.TP_MODE -- so "would this have worked"
    # reflects what would have actually happened had the signal been
    # taken via the ATR plan (the default/no-decision option).
    sl_distance = atr * config.SL_ATR_MULTIPLE
    if config.TP_MODE == "REWARD_MULTIPLE":
        tp_distance = sl_distance * config.REWARD_MULTIPLE
    else:
        tp_distance = atr * config.TP_ATR_MULTIPLE

    if bias == "bullish":
        moved = current_price - entry_price
        if moved >= tp_distance:
            return "SUCCESS", f"Price moved +{moved:.5f}, hit hypothetical TP (+{tp_distance:.5f})"
        if moved <= -sl_distance:
            return "FAILURE", f"Price moved {moved:.5f}, hit hypothetical SL (-{sl_distance:.5f})"
    elif bias == "bearish":
        moved = entry_price - current_price
        if moved >= tp_distance:
            return "SUCCESS", f"Price moved +{moved:.5f} in predicted direction, hit hypothetical TP"
        if moved <= -sl_distance:
            return "FAILURE", f"Price moved {moved:.5f} against prediction, hit hypothetical SL"

    # Neither TP nor SL hit yet -- check if we've run out of patience
    if _hours_since(row["created_at"]) >= config.OUTCOME_EVAL_WINDOW_HOURS:
        return "EXPIRED", f"No clear TP/SL hit within {config.OUTCOME_EVAL_WINDOW_HOURS}h window"

    return None  # still pending, check again later


def check_all():
    """Evaluate every signal that has a user action but no outcome yet."""
    pending = signal_tracker.get_unevaluated_signals()
    if not pending:
        print("No pending signals to evaluate.")
        return

    for row in pending:
        if row["order_ticket"]:
            result = _evaluate_real_trade(row)
        else:
            result = _evaluate_hypothetical(row)

        if result is None:
            continue  # not resolved yet, leave it pending

        outcome, detail = result
        signal_tracker.set_outcome(row["id"], outcome, detail)
        print(f"[{row['symbol']}] Signal #{row['id']} ({row['user_action']}) -> {outcome}: {detail}")


if __name__ == "__main__":
    mt5.initialize()
    try:
        check_all()
    finally:
        mt5.shutdown()
