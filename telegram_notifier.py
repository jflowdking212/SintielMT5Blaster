"""
Sends the alert to Telegram with inline Buy / Sell / Ignore buttons,
polls for the user's tap, and also handles standalone kill-switch
commands (/pause, /resume, /status) that can arrive at any time,
independent of whatever alert is currently in flight.

Requires: pip install requests
Create a bot via @BotFather on Telegram to get TELEGRAM_BOT_TOKEN,
then message it once so you can find your chat_id (see get_chat_id.py note below).

Polling design note: Telegram's getUpdates uses an incrementing offset
to mark messages as "seen". Both the alert-response wait and the
kill-switch command check share ONE offset (the module-level
_last_update_id below) so a /pause sent while waiting for a button tap
is still caught, and vice versa -- two independent pollers would each
think the other's updates were unread.
"""

import json
import os
import requests

import config
import position_sizer
import bot_state

from datetime import datetime, timezone

API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
PENDING_ALERTS_FILE = "pending_alerts.json"

_last_update_id = None

# Registry for active alerts waiting for a user button tap: symbol -> alert_dict
_pending_alerts = {}

def _save_pending_alerts():
    try:
        with open(PENDING_ALERTS_FILE, "w") as f:
            json.dump(_pending_alerts, f)
    except Exception as e:
        print(f"Failed to save pending alerts: {e}")

def _load_pending_alerts():
    global _pending_alerts
    if os.path.exists(PENDING_ALERTS_FILE):
        try:
            with open(PENDING_ALERTS_FILE, "r") as f:
                _pending_alerts = json.load(f)
        except Exception as e:
            print(f"Failed to load pending alerts: {e}")
            _pending_alerts = {}

_load_pending_alerts()


def register_pending_alert(signal_id: int, symbol: str, analysis: dict, summary: dict):
    _pending_alerts[symbol] = {
        "signal_id": signal_id,
        "symbol": symbol,
        "analysis": analysis,
        "summary": summary,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_pending_alerts()


def get_pending_alert(symbol: str) -> dict:
    return _pending_alerts.get(symbol)


def clear_pending_alert(symbol: str):
    if _pending_alerts.pop(symbol, None) is not None:
        _save_pending_alerts()


def has_pending_alert(symbol: str) -> bool:
    return symbol in _pending_alerts


def poll_all_updates() -> list[tuple[str, str, str]]:
    """
    Non-blocking poll for Telegram updates (timeout=0).
    Dispatches text commands (/pause, /resume, /status) immediately.
    Returns a list of callback query button taps [(action, source, symbol), ...]
    """
    global _last_update_id
    params = {"timeout": 0}
    if _last_update_id is not None:
        params["offset"] = _last_update_id + 1

    try:
        resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=5)
        resp.raise_for_status()
        updates = resp.json().get("result", [])
    except Exception as e:
        # Non-critical network jitter shouldn't crash loop
        return []

    callbacks = []
    for update in updates:
        _last_update_id = update["update_id"]

        callback = update.get("callback_query")
        if callback:
            try:
                requests.post(f"{API_BASE}/answerCallbackQuery", json={
                    "callback_query_id": callback["id"]
                })
            except Exception:
                pass
            data = callback.get("data", "")
            if "|" in data:
                parts = data.split("|")
                if len(parts) == 3:
                    callbacks.append((parts[0], parts[1], parts[2]))
            continue

        message = update.get("message")
        if message:
            text = (message.get("text") or "").strip()
            if text.startswith("/"):
                _handle_command(text)

    return callbacks


def check_expired_pending_alerts(on_timeout_fn=None) -> list[str]:
    """
    Identifies pending alerts older than config.RESPONSE_TIMEOUT_SECONDS,
    triggers on_timeout_fn(signal_id, symbol) if provided, and removes them.
    Returns a list of expired symbol names.
    """
    now = datetime.now(timezone.utc)
    expired_symbols = []
    timeout_sec = getattr(config, "RESPONSE_TIMEOUT_SECONDS", 900)
    for symbol, alert in list(_pending_alerts.items()):
        created_at_str = alert.get("created_at")
        if isinstance(created_at_str, str):
            created_at = datetime.fromisoformat(created_at_str)
        else:
            created_at = created_at_str
            
        elapsed = (now - created_at).total_seconds()
        if elapsed >= timeout_sec:
            expired_symbols.append(symbol)
            if on_timeout_fn:
                on_timeout_fn(alert["signal_id"], symbol)
            del _pending_alerts[symbol]
            
    if expired_symbols:
        _save_pending_alerts()
        
    return expired_symbols


def flush_pending_updates():
    """
    Call once at bot startup to discard any stale updates (old button
    taps, old commands) that arrived before this run started, so the
    bot doesn't act on them the moment it connects.
    """
    global _last_update_id
    resp = requests.get(f"{API_BASE}/getUpdates", params={"timeout": 0}, timeout=10)
    resp.raise_for_status()
    updates = resp.json().get("result", [])
    if updates:
        _last_update_id = updates[-1]["update_id"]


def _handle_command(text: str):
    cmd = text.strip().lower().split()[0]
    if cmd == "/pause":
        bot_state.pause()
        send_plain_message("⏸ Sentinel paused. No new signal alerts until you send /resume.")
    elif cmd == "/resume":
        bot_state.resume()
        send_plain_message("▶️ Sentinel resumed. Signal alerts are active again.")
    elif cmd == "/status":
        state_text = "⏸ PAUSED" if bot_state.is_paused() else "▶️ ACTIVE"
        send_plain_message(f"Sentinel status: {state_text}")
    # unrecognized commands are silently ignored


def _poll(timeout: int):
    """
    Synchronous helper returning first callback or None (maintained for backwards compatibility).
    """
    callbacks = poll_all_updates()
    return callbacks[0] if callbacks else None


def check_and_handle_commands():
    """
    Quick, short poll for any pending kill-switch commands.
    """
    poll_all_updates()


def wait_for_response(timeout_seconds: int = 900, poll_interval: int = 3):
    """
    Synchronous fallback for waiting on a single response.
    """
    elapsed = 0
    while elapsed < timeout_seconds:
        callbacks = poll_all_updates()
        if callbacks:
            return callbacks[0]
        time.sleep(poll_interval)
        elapsed += poll_interval

    return None, None, None



def _preview(symbol: str, entry_price: float, sl_price: float,
             native_tp_price: float = None, native_tp_atr_ratio: float = None) -> str:
    """
    Computes the lot size and dollar risk/reward that WOULD result if the
    user tapped this option, honoring config.POSITION_SIZE_MODE and
    config.TP_MODE, so the preview matches exactly what order_executor.py
    will actually do.
    """
    try:
        risk_amount_usd = position_sizer.get_effective_risk_amount_usd()
        sizing = position_sizer.calculate_lot_size(symbol, entry_price, sl_price, risk_amount_usd)
    except position_sizer.InsufficientRiskAmountError as e:
        return f"⚠️ not tradeable at current risk setting -- {e}"
    except Exception as e:
        return f"⚠️ could not calculate lot size ({e})"

    sl_distance = abs(entry_price - sl_price)

    if config.TP_MODE == "REWARD_MULTIPLE":
        reward_ratio = config.REWARD_MULTIPLE
    elif native_tp_price is not None:
        reward_ratio = abs(native_tp_price - entry_price) / sl_distance
    elif native_tp_atr_ratio is not None:
        reward_ratio = native_tp_atr_ratio
    else:
        reward_ratio = config.REWARD_MULTIPLE  # same fallback order_executor uses

    reward_usd = round(sizing["actual_risk_usd"] * reward_ratio, 2)
    note = " (capped by broker max lot)" if sizing["capped_by_volume_max"] else ""
    return f"Lot: {sizing['lot']}  Risk: ${sizing['actual_risk_usd']:.2f}  Reward: ${reward_usd:.2f}{note}"


def _format_trade_plan(analysis: dict, summary: dict) -> str:
    """
    Builds the text block showing Claude's suggested entry/SL/TP next to
    what the ATR plan would use, plus the method(s) that drove the call,
    and what the lot size and dollar risk/reward would actually be.
    """
    symbol = summary.get("symbol")
    plan = analysis.get("suggested_trade_plan") or {}
    entry = plan.get("entry")
    sl = plan.get("stop_loss")
    tp = plan.get("take_profit")
    rationale = _clean_md(plan.get("rationale"))

    lines = []

    methods = analysis.get("method_used") or []
    pattern_detail = analysis.get("pattern_detail")
    if methods:
        method_line = "🔍 *Methods:* " + ", ".join(methods)
        if pattern_detail:
            method_line += f" ({pattern_detail})"
        lines.append(method_line)
        lines.append("")

    if entry is not None and sl is not None:
        lines.append("💡 *Claude's suggested plan:*")
        tp_display = f"  TP: `{tp}`" if (tp is not None and config.TP_MODE == "NATIVE") else ""
        lines.append(f"   Entry: `{entry}`  SL: `{sl}`{tp_display}")
        if rationale:
            lines.append(f"   _{rationale}_")
        if symbol:
            lines.append(f"   {_preview(symbol, entry, sl, native_tp_price=tp)}")
    else:
        lines.append("💡 Claude did not propose specific levels for this setup.")

    # ATR-based plan preview
    last_close = summary.get("last_close")
    atr = summary.get("atr_14")
    if last_close is not None and atr is not None:
        sl_dist = atr * config.SL_ATR_MULTIPLE
        actual_sl = last_close - sl_dist if analysis["bias"] == "bullish" else last_close + sl_dist

        lines.append("")
        lines.append("⚙️ *Standard ATR-based plan:*")
        tp_atr_ratio = config.TP_ATR_MULTIPLE / config.SL_ATR_MULTIPLE
        if config.TP_MODE == "NATIVE":
            tp_dist = atr * config.TP_ATR_MULTIPLE
            actual_tp = last_close + tp_dist if analysis["bias"] == "bullish" else last_close - tp_dist
            lines.append(f"   Entry: `~{last_close}`  SL: `{actual_sl:.5f}`  TP: `{actual_tp:.5f}`")
        else:
            lines.append(f"   Entry: `~{last_close}`  SL: `{actual_sl:.5f}`")
        if symbol:
            lines.append(f"   {_preview(symbol, last_close, actual_sl, native_tp_atr_ratio=tp_atr_ratio)}")

    lines.append("")
    mode_note = ("Using each plan's own take-profit target." if config.TP_MODE == "NATIVE"
                 else f"Take-profit forced to {config.REWARD_MULTIPLE}x risk (TP_MODE=REWARD_MULTIPLE).")
    lines.append(f"_{mode_note}_")

    return "\n".join(lines)


def _clean_md(s: str) -> str:
    """Strip markdown formatting symbols from dynamic text generated by AI."""
    if not s:
        return ""
    return str(s).replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")


def send_alert(symbol: str, analysis: dict, summary: dict) -> int:
    """Send the alert message with buttons. Returns the sent message_id."""
    clean_reasoning = _clean_md(analysis.get("reasoning", ""))
    text = (
        f"📊 *{symbol}* signal\n\n"
        f"Bias: *{analysis['bias'].upper()}*\n"
        f"Confidence: {analysis['confidence']:.0%}\n"
        f"Structure: {analysis['structure_quality']}\n\n"
        f"_{clean_reasoning}_\n\n"
        f"{_format_trade_plan(analysis, summary)}"
    )

    has_claude_plan = all(
        (analysis.get("suggested_trade_plan") or {}).get(k) is not None
        for k in ("entry", "stop_loss")
    )

    keyboard_rows = []

    if has_claude_plan:
        keyboard_rows.append([
            {"text": "✅ Buy (Claude's plan)", "callback_data": f"BUY|CLAUDE|{symbol}"},
            {"text": "✅ Buy (ATR plan)", "callback_data": f"BUY|ATR|{symbol}"},
        ])
        keyboard_rows.append([
            {"text": "🔻 Sell (Claude's plan)", "callback_data": f"SELL|CLAUDE|{symbol}"},
            {"text": "🔻 Sell (ATR plan)", "callback_data": f"SELL|ATR|{symbol}"},
        ])
    else:
        keyboard_rows.append([
            {"text": "✅ Buy", "callback_data": f"BUY|ATR|{symbol}"},
            {"text": "🔻 Sell", "callback_data": f"SELL|ATR|{symbol}"},
        ])

    keyboard_rows.append([
        {"text": "⏭ Ignore", "callback_data": f"IGNORE|NONE|{symbol}"},
    ])

    keyboard = {"inline_keyboard": keyboard_rows}

    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        }, timeout=15)
        resp.raise_for_status()
    except Exception:
        # Fallback if Telegram rejects unescaped markdown characters in reasoning
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text.replace("*", "").replace("_", ""),
            "reply_markup": keyboard,
        }, timeout=15)
        resp.raise_for_status()

    return resp.json()["result"]["message_id"]


def send_plain_message(text: str):
    """Simple one-off notification, no buttons -- used for warnings,
    kill-switch confirmations, etc."""
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=15)
        resp.raise_for_status()
    except Exception:
        resp = requests.post(f"{API_BASE}/sendMessage", json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text.replace("*", "").replace("_", ""),
        }, timeout=15)
        resp.raise_for_status()
    return resp.json()["result"]["message_id"]
