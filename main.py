"""
Main loop: pulls MT5 data -> asks Claude for a read -> runs the
deterministic risk gate -> sends a Telegram alert -> waits for the
user's tap -> executes (or logs an ignore).

Run this with: python main.py
Stop with Ctrl+C.
"""

import json
import time
import traceback
from datetime import datetime, timezone

import config
import mt5_data
import claude_analyzer
import risk_gate
import telegram_notifier
import order_executor
import signal_tracker
import outcome_checker
import bot_state


def log_event(event: dict):
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(config.LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def handle_user_tap(action: str, source: str, symbol: str):
    """Process a user button tap from Telegram non-blockingly."""
    alert = telegram_notifier.get_pending_alert(symbol)
    if not alert:
        print(f"[{symbol}] Received tap {action} but no active pending alert found.")
        return

    signal_id = alert["signal_id"]
    analysis = alert["analysis"]
    summary = alert["summary"]

    log_event({"symbol": symbol, "stage": "user_response", "action": action, "source": source})

    if action == "IGNORE":
        signal_tracker.record_user_action(signal_id, "IGNORE")
        print(f"[{symbol}] User chose to ignore signal #{signal_id}.")
        telegram_notifier.clear_pending_alert(symbol)
        return

    plan = analysis.get("suggested_trade_plan") or {}
    use_claude_plan = (source == "CLAUDE" and plan.get("stop_loss") is not None)

    try:
        if use_claude_plan:
            result = order_executor.place_order(
                symbol=symbol,
                action=action,
                explicit_sl=plan["stop_loss"],
                explicit_tp=plan.get("take_profit"),
                atr_value=summary.get("atr_14"),
                signal_entry_price=summary.get("last_close"),
            )
            print(f"[{symbol}] Order placed using Claude's stop-loss (TP mode: {config.TP_MODE}).")
        else:
            result = order_executor.place_order(
                symbol=symbol,
                action=action,
                atr_value=summary.get("atr_14"),
                signal_entry_price=summary.get("last_close"),
            )
            print(f"[{symbol}] Order placed using standard ATR-based stop-loss (TP mode: {config.TP_MODE}).")

        signal_tracker.record_user_action(signal_id, action, order_ticket=result["order"], plan_source=source)
        log_event({"symbol": symbol, "stage": "order_placed", "action": action, "source": source,
                   "retcode": result["retcode"], "order_id": result["order"],
                   "lot": result["lot"], "actual_risk_usd": result["actual_risk_usd"],
                   "reward_amount_usd": result["reward_amount_usd"]})
        # Always send an explicit Order Placed confirmation to Telegram!
        confirm_text = (
            f"✅ *{action} order placed for {symbol}!*\n"
            f"• Order Ticket: `#{result['order']}`\n"
            f"• Volume / Lot: `{result['lot']}`\n"
            f"• Actual Risk: `${result['actual_risk_usd']:.2f}`\n"
            f"• Target Reward: `${result['reward_amount_usd']:.2f}`"
        )
        telegram_notifier.send_plain_message(confirm_text)

        if result["capped_by_volume_max"]:
            telegram_notifier.send_plain_message(
                f"⚠️ *Note for {symbol}*: position was capped at the broker's maximum lot size -- "
                f"actual risk (${result['actual_risk_usd']:.2f}) is higher "
                f"than your configured risk (${result['risk_amount_usd']:.2f})."
            )

        if result["min_lot_override"]:
            telegram_notifier.send_plain_message(
                f"ℹ️ *Note for {symbol}*: your configured risk (${result['risk_amount_usd']:.2f}) was "
                f"below the broker's minimum lot size, so this trade was auto-adjusted up to "
                f"the minimum ({result['lot']} lot, actual risk: ${result['actual_risk_usd']:.2f})."
            )
            log_event({"symbol": symbol, "stage": "min_lot_override",
                       "configured_risk": result["risk_amount_usd"],
                       "actual_risk": result["actual_risk_usd"]})

        telegram_notifier.clear_pending_alert(symbol)

    except order_executor.StaleSignalError as e:
        signal_tracker.record_user_action(signal_id, action, order_ticket=None, plan_source=source)
        log_event({"symbol": symbol, "stage": "order_rejected_stale", "error": str(e)})
        print(f"[{symbol}] Stale signal execution blocked: {e}")
        telegram_notifier.send_plain_message(f"⚠️ {symbol}: {e}")
        telegram_notifier.clear_pending_alert(symbol)

    except order_executor.InsufficientRiskAmountError as e:
        signal_tracker.record_user_action(signal_id, action, order_ticket=None, plan_source=source)
        log_event({"symbol": symbol, "stage": "order_rejected_min_risk", "error": str(e)})
        print(f"[{symbol}] {e}")
        telegram_notifier.send_plain_message(f"⚠️ {symbol}: couldn't place that trade -- {e}")

    except Exception as e:
        signal_tracker.record_user_action(signal_id, action, order_ticket=None, plan_source=source)
        log_event({"symbol": symbol, "stage": "order_failed", "error": str(e)})
        print(f"[{symbol}] Order failed: {e}")
        telegram_notifier.send_plain_message(f"⚠️ {symbol}: order failed -- {e}")


def handle_timeout(signal_id: int, symbol: str):
    log_event({"symbol": symbol, "stage": "user_response", "action": "TIMEOUT"})
    signal_tracker.record_user_action(signal_id, "TIMEOUT")
    print(f"[{symbol}] Signal #{signal_id} timed out unanswered.")


def process_symbol(symbol: str):
    df = mt5_data.get_ohlc(symbol)
    df = mt5_data.compute_indicators(df)
    summary = mt5_data.summarize_for_prompt(symbol, df)

    symbol_wants_image = (config.CHART_IMAGE_SYMBOLS is None
                           or symbol in config.CHART_IMAGE_SYMBOLS)

    chart_image_b64 = None
    if config.USE_CHART_IMAGE and symbol_wants_image and mt5_data.should_include_chart_image(symbol):
        try:
            chart_image_b64 = mt5_data.render_chart_image(symbol, df, bars=config.CHART_IMAGE_BARS)
            mt5_data.mark_chart_image_sent(symbol)
            print(f"[{symbol}] Including fresh chart image this cycle.")
        except Exception as e:
            # Chart rendering failing shouldn't block analysis entirely --
            # fall back to indicator-only analysis for this cycle.
            print(f"[{symbol}] Chart rendering failed, falling back to indicators only: {e}")
            log_event({"symbol": symbol, "stage": "chart_render_failed", "error": str(e)})

    try:
        analysis = claude_analyzer.analyze(summary, chart_image_b64=chart_image_b64)
    except Exception as e:
        log_event({"symbol": symbol, "stage": "claude_analysis", "error": str(e)})
        print(f"[{symbol}] Claude analysis failed, skipping this cycle: {e}")
        return  # fail-safe: do nothing on error, never trade blind

    # Export signal file for SentinelEA.mq5 on chart display
    mt5_data.export_signal_for_ea(symbol, analysis)

    open_trades = order_executor.get_open_trades_count()
    allowed, reason = risk_gate.should_alert(
        symbol, analysis, open_trades,
        higher_tf_trend=summary.get("higher_tf_trend"),
    )

    # Record every signal Claude produces, even ones the gate blocks
    signal_id = signal_tracker.record_signal(symbol, analysis, summary)

    log_event({
        "symbol": symbol,
        "stage": "analysis_complete",
        "chart_image_included": chart_image_b64 is not None,
        "summary": summary,
        "analysis": analysis,
        "gate_allowed": allowed,
        "gate_reason": reason,
    })

    if not allowed:
        print(f"[{symbol}] Gated out: {reason}")
        return

    print(f"[{symbol}] Alerting user -- bias={analysis['bias']} confidence={analysis['confidence']:.2f}")
    telegram_notifier.send_alert(symbol, analysis, summary)

    # Register alert in pending queue (NON-BLOCKING)
    telegram_notifier.register_pending_alert(signal_id, symbol, analysis, summary)


def main_loop():
    mt5_data.connect()
    telegram_notifier.flush_pending_updates()  # discard stale updates from before this run
    print(f"Sentinel started. Send /pause, /resume, or /status in Telegram at any time.")

    last_analysis_at = 0
    last_outcome_check_at = 0
    TICK_INTERVAL = 5  # ticker loop runs every 5 seconds for responsive Telegram polling

    try:
        while True:
            # 1. Non-blocking Telegram updates & command handling
            callbacks = telegram_notifier.poll_all_updates()
            for action, source, symbol in callbacks:
                handle_user_tap(action, source, symbol)

            # 2. Check for expired pending alerts
            telegram_notifier.check_expired_pending_alerts(on_timeout_fn=handle_timeout)

            now = time.time()

            # 3. Periodically run symbol analysis cycles when interval elapses
            if not bot_state.is_paused() and (now - last_analysis_at >= config.POLL_INTERVAL_SECONDS or last_analysis_at == 0):
                last_analysis_at = now
                print(f"\n--- Running market analysis cycle ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}) ---")
                for symbol in config.SYMBOLS:
                    if bot_state.is_paused():
                        print("Paused mid-cycle -- skipping remaining symbols.")
                        break
                    try:
                        process_symbol(symbol)
                    except Exception as e:
                        print(f"[{symbol}] Unexpected error: {e}")
                        traceback.print_exc()

            # 4. Periodically run outcome evaluations (every 10 minutes)
            if not bot_state.is_paused() and (now - last_outcome_check_at >= 600 or last_outcome_check_at == 0):
                last_outcome_check_at = now
                try:
                    outcome_checker.check_all()
                except Exception as e:
                    print(f"Outcome check failed this cycle: {e}")

            time.sleep(TICK_INTERVAL)

    except KeyboardInterrupt:
        print("Stopped by user.")
    finally:
        mt5_data.disconnect()



if __name__ == "__main__":
    main_loop()
