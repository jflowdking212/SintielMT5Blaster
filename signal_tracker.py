"""
Records every signal Claude produces, what you did with it (Buy/Sell/
Ignore/Timeout), and later -- once outcome_checker.py has evaluated it --
whether it turned out to be a good call or not.

This is the data source for dashboard.py's success/failure scorecard.
Uses SQLite so it's easy to query without extra dependencies.
"""

import json
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bias TEXT NOT NULL,
    confidence REAL NOT NULL,
    structure_quality TEXT,
    higher_tf_trend TEXT,
    method_used TEXT,           -- JSON array, e.g. '["trend","chart_pattern"]'
    pattern_detail TEXT,        -- e.g. "bearish engulfing at resistance", or NULL
    reasoning TEXT,
    entry_price REAL,
    atr_value REAL,

    suggested_entry REAL,
    suggested_sl REAL,
    suggested_tp REAL,

    user_action TEXT,          -- 'BUY', 'SELL', 'IGNORE', 'TIMEOUT'
    plan_source_used TEXT,      -- 'CLAUDE' or 'ATR', only set when a trade was taken
    order_ticket INTEGER,       -- set only if a real trade was placed

    outcome TEXT,               -- 'SUCCESS', 'FAILURE', 'PENDING', 'EXPIRED', NULL until evaluated
    outcome_detail TEXT,        -- human-readable note on why
    evaluated_at TEXT
);
"""


def _connect():
    conn = sqlite3.connect(config.SIGNALS_DB)
    conn.execute(SCHEMA)
    return conn


def record_signal(symbol: str, analysis: dict, summary: dict) -> int:
    """Call this right after Claude's analysis, regardless of whether the
    gate lets it through -- gives you a full record later of what Claude
    saw and said, even for signals that never reached you."""
    plan = analysis.get("suggested_trade_plan") or {}
    method_used_json = json.dumps(analysis.get("method_used") or [])
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO signals
           (symbol, created_at, bias, confidence, structure_quality,
            higher_tf_trend, method_used, pattern_detail, reasoning,
            entry_price, atr_value, suggested_entry, suggested_sl, suggested_tp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            symbol,
            datetime.now(timezone.utc).isoformat(),
            analysis["bias"],
            analysis["confidence"],
            analysis.get("structure_quality"),
            summary.get("higher_tf_trend"),
            method_used_json,
            analysis.get("pattern_detail"),
            analysis.get("reasoning"),
            summary.get("last_close"),
            summary.get("atr_14"),
            plan.get("entry"),
            plan.get("stop_loss"),
            plan.get("take_profit"),
        ),
    )
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id


def record_user_action(signal_id: int, action: str, order_ticket: int = None,
                        plan_source: str = None):
    """action: 'BUY', 'SELL', 'IGNORE', or 'TIMEOUT'
    plan_source: 'CLAUDE' or 'ATR', only meaningful when action is BUY/SELL"""
    conn = _connect()
    conn.execute(
        "UPDATE signals SET user_action = ?, order_ticket = ?, plan_source_used = ? WHERE id = ?",
        (action, order_ticket, plan_source, signal_id),
    )
    conn.commit()
    conn.close()


def get_unevaluated_signals():
    """Returns signals that have a user_action but no outcome yet --
    the ones outcome_checker.py needs to go check on."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM signals
           WHERE user_action IS NOT NULL
             AND user_action != 'TIMEOUT'
             AND outcome IS NULL"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_outcome(signal_id: int, outcome: str, detail: str = ""):
    conn = _connect()
    conn.execute(
        """UPDATE signals SET outcome = ?, outcome_detail = ?, evaluated_at = ?
           WHERE id = ?""",
        (outcome, detail, datetime.now(timezone.utc).isoformat(), signal_id),
    )
    conn.commit()
    conn.close()


def get_all_signals():
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM signals ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]
