"""
Prints a scorecard summarizing how Claude's signals have performed --
overall, by action taken (Buy/Sell/Ignore), by confidence bucket, and
by which analysis method(s) drove the call.

Run any time with: python dashboard.py
Data comes from signals.db, populated by main.py (signal_tracker.py)
and evaluated by outcome_checker.py.
"""

import json
from collections import defaultdict

import config
import signal_tracker


def confidence_bucket(confidence: float) -> str:
    if confidence >= 0.85:
        return "0.85-1.00"
    elif confidence >= 0.75:
        return "0.75-0.84"
    elif confidence >= 0.65:
        return "0.65-0.74"
    else:
        return "below 0.65"


def print_scorecard():
    rows = signal_tracker.get_all_signals()

    if not rows:
        print("No signals recorded yet.")
        return

    total = len(rows)
    evaluated = [r for r in rows if r["outcome"] in ("SUCCESS", "FAILURE")]
    pending = [r for r in rows if r["outcome"] is None and r["user_action"] not in (None, "TIMEOUT")]
    ignored_or_timeout = [r for r in rows if r["user_action"] in (None, "TIMEOUT", "IGNORE")]

    print("=" * 60)
    print("CLAUDE SIGNAL SCORECARD")
    print("=" * 60)
    print(f"Total signals recorded:      {total}")
    print(f"Evaluated (SUCCESS/FAILURE): {len(evaluated)}")
    print(f"Still pending evaluation:    {len(pending)}")
    print(f"Ignored / no response:       {len(ignored_or_timeout)}")
    print()

    if evaluated:
        successes = sum(1 for r in evaluated if r["outcome"] == "SUCCESS")
        rate = successes / len(evaluated) * 100
        print(f"Overall accuracy: {successes}/{len(evaluated)} ({rate:.1f}%)")
        print()

    # --- Breakdown: real trades you took vs signals you ignored/rejected ---
    taken = [r for r in evaluated if r["user_action"] in ("BUY", "SELL")]
    print("--- Trades you TOOK (real or hypothetical if unfilled) ---")
    if taken:
        taken_success = sum(1 for r in taken if r["outcome"] == "SUCCESS")
        print(f"  {taken_success}/{len(taken)} successful ({taken_success/len(taken)*100:.1f}%)")
    else:
        print("  No evaluated trades yet.")
    print()

    # --- Breakdown by confidence bucket ---
    print("--- Accuracy by confidence bucket ---")
    buckets = defaultdict(lambda: {"success": 0, "total": 0})
    for r in evaluated:
        b = confidence_bucket(r["confidence"])
        buckets[b]["total"] += 1
        if r["outcome"] == "SUCCESS":
            buckets[b]["success"] += 1

    for b in ["0.85-1.00", "0.75-0.84", "0.65-0.74", "below 0.65"]:
        stats = buckets.get(b)
        if stats and stats["total"] > 0:
            rate = stats["success"] / stats["total"] * 100
            print(f"  {b}: {stats['success']}/{stats['total']} ({rate:.1f}%)")
    print()

    # --- Breakdown by symbol ---
    print("--- Accuracy by symbol ---")
    by_symbol = defaultdict(lambda: {"success": 0, "total": 0})
    for r in evaluated:
        by_symbol[r["symbol"]]["total"] += 1
        if r["outcome"] == "SUCCESS":
            by_symbol[r["symbol"]]["success"] += 1

    for symbol, stats in sorted(by_symbol.items()):
        rate = stats["success"] / stats["total"] * 100
        print(f"  {symbol}: {stats['success']}/{stats['total']} ({rate:.1f}%)")
    print()

    # --- Breakdown by counter-trend vs aligned (uses higher_tf_trend logged at signal time) ---
    print("--- Aligned vs counter-trend accuracy ---")
    aligned, counter = [], []
    for r in evaluated:
        if not r["higher_tf_trend"]:
            continue
        is_counter = (
            (r["bias"] == "bullish" and r["higher_tf_trend"] == "downtrend") or
            (r["bias"] == "bearish" and r["higher_tf_trend"] == "uptrend")
        )
        (counter if is_counter else aligned).append(r)

    if aligned:
        s = sum(1 for r in aligned if r["outcome"] == "SUCCESS")
        print(f"  Aligned with higher-TF trend: {s}/{len(aligned)} ({s/len(aligned)*100:.1f}%)")
    if counter:
        s = sum(1 for r in counter if r["outcome"] == "SUCCESS")
        print(f"  Counter-trend:                {s}/{len(counter)} ({s/len(counter)*100:.1f}%)")
    print()

    # --- Breakdown by analysis method (trend/momentum/chart_pattern/etc) ---
    # Each signal can have MULTIPLE methods (e.g. trend + chart_pattern
    # together), so a signal counts toward every method it used, not just
    # one bucket. A method's win rate is only shown once it has at least
    # config.MIN_OCCURRENCES_FOR_EDGE evaluated occurrences -- below that,
    # it's noise, not edge (see config.py for why).
    print(f"--- Accuracy by method (min {config.MIN_OCCURRENCES_FOR_EDGE} occurrences to show a rate) ---")
    by_method = defaultdict(lambda: {"success": 0, "total": 0})
    for r in evaluated:
        try:
            methods = json.loads(r["method_used"]) if r["method_used"] else []
        except (json.JSONDecodeError, TypeError):
            methods = []
        for m in methods:
            by_method[m]["total"] += 1
            if r["outcome"] == "SUCCESS":
                by_method[m]["success"] += 1

    if by_method:
        for method, stats in sorted(by_method.items(), key=lambda kv: -kv[1]["total"]):
            if stats["total"] >= config.MIN_OCCURRENCES_FOR_EDGE:
                rate = stats["success"] / stats["total"] * 100
                print(f"  {method}: {stats['success']}/{stats['total']} ({rate:.1f}%)")
            else:
                print(f"  {method}: {stats['success']}/{stats['total']} "
                      f"(insufficient sample, need {config.MIN_OCCURRENCES_FOR_EDGE}+)")
    else:
        print("  No method data recorded yet.")
    print("=" * 60)


if __name__ == "__main__":
    print_scorecard()
