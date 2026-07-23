"""
Converts a plain-dollar risk amount (e.g. "I'll risk $5") into the
correct MT5 lot size for a given stop-loss distance -- so the user
never has to do pip/lot/tick-value math themselves.

Core idea: for any symbol, MT5 exposes trade_tick_value (how much 1
tick is worth, in account currency, for 1.0 lot) and trade_tick_size
(the price size of 1 tick). Together they let us work out exactly how
big a position needs to be so that hitting the stop loss costs exactly
the dollar amount the user chose -- no manual pip-value lookups needed.
"""

import math
import MetaTrader5 as mt5

import config


class InsufficientRiskAmountError(Exception):
    """
    Raised when the user's chosen risk amount is too small to open even
    the broker's minimum lot size at the given stop-loss distance.
    Carries the minimum risk amount that WOULD work, so the user can be
    told exactly what to increase it to.
    """
    def __init__(self, message: str, min_risk_usd: float):
        super().__init__(message)
        self.min_risk_usd = min_risk_usd


def get_effective_risk_amount_usd() -> float:
    """
    Resolves config.POSITION_SIZE_MODE into an actual dollar amount:
    - "FIXED": just config.RISK_AMOUNT_USD, unchanged.
    - "PERCENT": config.RISK_PERCENT % of the CURRENT account balance,
      fetched fresh from MT5 every time this is called -- so stake
      automatically scales as the account grows or shrinks.
    """
    if config.POSITION_SIZE_MODE == "PERCENT":
        account_info = mt5.account_info()
        if account_info is None:
            raise RuntimeError("Could not read account balance from MT5")
        balance = account_info.balance
        return round(balance * (config.RISK_PERCENT / 100.0), 2)

    return config.RISK_AMOUNT_USD


def calculate_lot_size(symbol: str, entry_price: float, stop_loss_price: float,
                        risk_amount_usd: float) -> dict:
    """
    Returns {"lot": float, "actual_risk_usd": float, "capped_by_volume_max": bool,
              "min_lot_override": bool}

    Raises InsufficientRiskAmountError if risk_amount_usd is too small to
    reach the broker's minimum tradeable lot size for this symbol/stop
    distance -- UNLESS config.AUTO_ADJUST_TO_MIN_LOT is True, in which
    case it silently bumps up to the minimum lot instead and flags that
    with min_lot_override=True so callers can log/notify about it.
    """
    info = mt5.symbol_info(symbol)
    if info is None or not getattr(info, "visible", False):
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    if info is None:
        raise RuntimeError(f"Symbol {symbol} not found")

    price_distance = abs(entry_price - stop_loss_price)
    if price_distance <= 0:
        raise ValueError("Stop loss distance must be greater than zero")

    tick_size = info.trade_tick_size
    tick_value = info.trade_tick_value  # already in account currency, per 1.0 lot

    if not tick_size or not tick_value:
        raise RuntimeError(f"Could not read tick size/value for {symbol}")

    ticks = price_distance / tick_size
    value_per_lot = ticks * tick_value  # $ risk if trading 1.0 lot at this stop distance

    if value_per_lot <= 0:
        raise RuntimeError(f"Computed zero/negative tick value for {symbol}")

    raw_lot = risk_amount_usd / value_per_lot

    # Round DOWN to the nearest allowed volume step -- we never want to
    # round up and accidentally risk more than the user asked for.
    volume_step = info.volume_step
    steps = math.floor(raw_lot / volume_step)
    lot = round(steps * volume_step, 8)

    min_lot_override = False

    if lot < info.volume_min:
        min_risk_usd = math.ceil(info.volume_min * value_per_lot * 100) / 100  # round up to cents

        if config.AUTO_ADJUST_TO_MIN_LOT:
            lot = info.volume_min
            min_lot_override = True
        else:
            raise InsufficientRiskAmountError(
                f"${risk_amount_usd:.2f} is below the minimum tradeable size for {symbol} "
                f"at this stop distance. Minimum risk needed: ${min_risk_usd:.2f}",
                min_risk_usd=min_risk_usd,
            )

    capped = False
    if lot > info.volume_max:
        lot = info.volume_max
        capped = True  # actual risk will now exceed what the user asked for

    actual_risk_usd = round(lot * value_per_lot, 2)

    return {
        "lot": lot,
        "actual_risk_usd": actual_risk_usd,
        "capped_by_volume_max": capped,
        "min_lot_override": min_lot_override,
    }
