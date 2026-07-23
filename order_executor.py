"""
Places the actual order in MT5 once a human has tapped Buy/Sell.

Money management is dollar-based for POSITION SIZE (lot size), not for
take-profit. You set config.RISK_AMOUNT_USD (e.g. $5) and this figures
out the correct lot size automatically, so a full stop-loss hit costs
exactly that much regardless of symbol or stop distance.

Take-profit follows config.TP_MODE:
- "NATIVE" (default): uses Claude's own suggested take-profit (when the
  Claude plan is selected) or the ATR-based take-profit via
  config.TP_ATR_MULTIPLE (when the ATR plan is selected) -- each
  source's own target, used as-is.
- "REWARD_MULTIPLE": ignores the native target and forces take-profit
  to config.REWARD_MULTIPLE x the risk amount, regardless of source.

Claude's suggested levels (or the ATR calculation) still decide WHERE
the stop loss goes -- the technical invalidation point. This module
decides HOW BIG the trade is (always) and, depending on TP_MODE,
possibly where the profit target sits too.
"""

import MetaTrader5 as mt5

import config
import mt5_data
import position_sizer
import telegram_notifier
from position_sizer import InsufficientRiskAmountError


class StaleSignalError(Exception):
    """
    Raised when the market price has moved too far since the signal alert
    was originally generated, rendering the execution plan stale or unsafe.
    """
    pass


def _get_filling_type(symbol_info) -> int:
    """
    Dynamically select a supported execution filling mode for the given symbol/broker.
    Uses bitwise flags on symbol_info.filling_mode:
    1 = SYMBOL_FILLING_FOK, 2 = SYMBOL_FILLING_IOC.
    """
    filling_mode = getattr(symbol_info, "filling_mode", 0)
    if filling_mode & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    elif filling_mode & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    else:
        return mt5.ORDER_FILLING_RETURN



def get_open_trades_count() -> int:
    positions = mt5.positions_get()
    return len(positions) if positions else 0


def _compute_stop_loss_price(symbol: str, action: str, price: float,
                              atr_value: float = None, explicit_sl: float = None,
                              sl_atr_multiple: float = None) -> float:
    """Figures out where the stop loss goes -- either Claude's suggested
    level, or the ATR-based calculation. This is the technical call;
    it has nothing to do with position size."""
    if explicit_sl is not None:
        return explicit_sl

    sl_atr_multiple = sl_atr_multiple if sl_atr_multiple is not None else config.SL_ATR_MULTIPLE

    if atr_value:
        sl_distance = atr_value * sl_atr_multiple
    else:
        symbol_info = mt5.symbol_info(symbol)
        sl_distance = 200 * symbol_info.point  # fallback if no ATR available

    return price - sl_distance if action == "BUY" else price + sl_distance


def _compute_take_profit_price(action: str, price: float, sl: float,
                                atr_value: float = None, explicit_tp: float = None,
                                tp_atr_multiple: float = None,
                                actual_risk_usd: float = None,
                                reward_multiple: float = None) -> float:
    """
    Decides where take-profit goes, following config.TP_MODE.

    NATIVE mode: uses explicit_tp (Claude's own suggestion) if given,
    else an ATR-based target via tp_atr_multiple, else falls back to
    the reward-multiple calculation as a last resort (no better info
    available).

    REWARD_MULTIPLE mode: always overrides with risk x reward_multiple,
    regardless of what Claude or ATR suggested.
    """
    sl_distance = abs(price - sl)
    tp_atr_multiple = tp_atr_multiple if tp_atr_multiple is not None else config.TP_ATR_MULTIPLE
    reward_multiple = reward_multiple if reward_multiple is not None else config.REWARD_MULTIPLE

    if config.TP_MODE == "REWARD_MULTIPLE":
        tp_distance = sl_distance * reward_multiple
        return price + tp_distance if action == "BUY" else price - tp_distance

    # NATIVE mode
    if explicit_tp is not None:
        return explicit_tp
    if atr_value:
        tp_distance = atr_value * tp_atr_multiple
        return price + tp_distance if action == "BUY" else price - tp_distance

    # No native target available (e.g. Claude gave a stop but no TP, and
    # no ATR value was passed) -- fall back to the reward-multiple math
    # as a sane default rather than leaving take-profit unset.
    tp_distance = sl_distance * reward_multiple
    return price + tp_distance if action == "BUY" else price - tp_distance


def place_order(symbol: str, action: str,
                 atr_value: float = None,
                 explicit_sl: float = None, explicit_tp: float = None,
                 sl_atr_multiple: float = None, tp_atr_multiple: float = None,
                 risk_amount_usd: float = None, reward_multiple: float = None,
                 signal_entry_price: float = None) -> dict:
    """
    action: "BUY" or "SELL"

    Returns a dict: {order, retcode, comment, lot, entry_price, sl, tp,
                      risk_amount_usd, actual_risk_usd, reward_amount_usd,
                      tp_mode, capped_by_volume_max}

    Raises InsufficientRiskAmountError if risk_amount_usd is too small
    to open even the broker's minimum lot at this stop-loss distance.
    Raises StaleSignalError if price has moved too far since signal creation.
    """
    risk_amount_usd = risk_amount_usd if risk_amount_usd is not None else position_sizer.get_effective_risk_amount_usd()

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise RuntimeError(f"Symbol {symbol} not found")

    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)

    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if action == "BUY" else tick.bid

    # Validate signal freshness against price drift
    if signal_entry_price is not None and atr_value is not None:
        drift = abs(price - signal_entry_price)
        max_allowed_drift = config.MAX_SLIPPAGE_ATR_MULTIPLE * atr_value
        if drift > max_allowed_drift:
            raise StaleSignalError(
                f"Price moved {drift:.5f} (max allowed: {max_allowed_drift:.5f}) "
                f"since signal was issued at {signal_entry_price:.5f}. Execution cancelled."
            )

    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

    # 1. Where does the stop loss go? (technical call -- Claude/ATR)
    sl = _compute_stop_loss_price(symbol, action, price, atr_value=atr_value,
                                   explicit_sl=explicit_sl, sl_atr_multiple=sl_atr_multiple)

    if action == "BUY" and sl >= price:
        raise ValueError(f"Invalid stop loss for BUY: sl={sl} must be below price={price}")
    if action == "SELL" and sl <= price:
        raise ValueError(f"Invalid stop loss for SELL: sl={sl} must be above price={price}")

    # 2. How big should the trade be, to risk exactly $X on that stop distance?
    sizing = position_sizer.calculate_lot_size(symbol, price, sl, risk_amount_usd)
    lot = sizing["lot"]

    # 3. Where's the take-profit?
    tp = _compute_take_profit_price(action, price, sl, atr_value=atr_value,
                                     explicit_tp=explicit_tp, tp_atr_multiple=tp_atr_multiple,
                                     reward_multiple=reward_multiple)

    if action == "BUY" and tp <= price:
        raise ValueError(f"Invalid take profit for BUY: tp={tp} must be above price={price}")
    if action == "SELL" and tp >= price:
        raise ValueError(f"Invalid take profit for SELL: tp={tp} must be below price={price}")

    sl_distance = abs(price - sl)
    tp_distance = abs(tp - price)

    filling_type = _get_filling_type(symbol_info)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": round(sl, symbol_info.digits),
        "tp": round(tp, symbol_info.digits),
        "deviation": 10,
        "magic": 20260721,
        "comment": "Sentinel signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"Order failed: {result.retcode} {result.comment}")

    # Dollar reward derived from the actual distance ratio, so it's
    # accurate whether TP came from REWARD_MULTIPLE, Claude, or ATR.
    reward_amount_usd = round(sizing["actual_risk_usd"] * (tp_distance / sl_distance), 2)

    return {
        "order": result.order,
        "retcode": result.retcode,
        "comment": result.comment,
        "lot": lot,
        "entry_price": price,
        "sl": sl,
        "tp": tp,
        "tp_mode": config.TP_MODE,
        "risk_amount_usd": risk_amount_usd,
        "actual_risk_usd": sizing["actual_risk_usd"],
        "reward_amount_usd": reward_amount_usd,
        "capped_by_volume_max": sizing["capped_by_volume_max"],
        "min_lot_override": sizing["min_lot_override"],
    }


def update_trailing_stops():
    """
    Scans open MT5 positions and automatically updates the Stop Loss (SL)
    as trades move into profit, according to config.ENABLE_TRAILING_STOP parameters.
    """
    if not getattr(config, "ENABLE_TRAILING_STOP", False):
        return

    positions = mt5.positions_get()
    if not positions:
        return

    for position in positions:
        symbol = position.symbol
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info:
            continue

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue

        mode = getattr(config, "TRAILING_STOP_MODE", "ATR")
        digits = symbol_info.digits
        point = symbol_info.point

        if mode == "ATR":
            df = mt5_data.get_ohlc(symbol)
            if df.empty:
                continue
            df = mt5_data.compute_indicators(df)
            atr = df["atr_14"].iloc[-1] if ("atr_14" in df and not df["atr_14"].isna().all()) else (200 * point)
            activation_dist = atr * getattr(config, "TRAILING_STOP_ACTIVATION_ATR_MULTIPLE", 1.0)
            trailing_dist = atr * getattr(config, "TRAILING_STOP_DISTANCE_ATR_MULTIPLE", 1.5)
            step_dist = atr * getattr(config, "TRAILING_STOP_STEP_ATR_MULTIPLE", 0.2)
        else:
            activation_dist = getattr(config, "TRAILING_STOP_ACTIVATION_POINTS", 150) * point
            trailing_dist = getattr(config, "TRAILING_STOP_DISTANCE_POINTS", 200) * point
            step_dist = getattr(config, "TRAILING_STOP_STEP_POINTS", 50) * point

        pos_type = position.type
        open_price = position.price_open
        current_sl = position.sl

        if pos_type == mt5.POSITION_TYPE_BUY:
            current_price = tick.bid
            profit_dist = current_price - open_price

            if profit_dist >= activation_dist:
                target_sl = round(current_price - trailing_dist, digits)
                is_first_sl = (current_sl == 0)
                if is_first_sl and target_sl > open_price:
                    _modify_position_sl(position, target_sl)
                elif not is_first_sl and target_sl >= current_sl + step_dist:
                    _modify_position_sl(position, target_sl)

        elif pos_type == mt5.POSITION_TYPE_SELL:
            current_price = tick.ask
            profit_dist = open_price - current_price

            if profit_dist >= activation_dist:
                target_sl = round(current_price + trailing_dist, digits)
                is_first_sl = (current_sl == 0)
                if is_first_sl and target_sl < open_price:
                    _modify_position_sl(position, target_sl)
                elif not is_first_sl and target_sl <= current_sl - step_dist:
                    _modify_position_sl(position, target_sl)


def _modify_position_sl(position, new_sl: float):
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": position.ticket,
        "symbol": position.symbol,
        "sl": new_sl,
        "tp": position.tp,
    }
    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        profit = position.profit
        msg = f"⚙️ *Trailing Stop Updated* for {position.symbol} (`#{position.ticket}`)\n• New SL: `{new_sl:.5f}`\n• Current Profit: `${profit:+.2f}`"
        print(f"\n  ⚙️ [Trailing Stop Updated] {position.symbol} (#{position.ticket}): New SL={new_sl:.5f} | Profit=${profit:+.2f}\n")
        try:
            telegram_notifier.send_plain_message(msg)
        except Exception:
            pass
