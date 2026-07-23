"""
Sends a market data summary AND a rendered chart image to Claude, and
parses back a structured, machine-readable analysis. Claude is used
for read/reasoning only -- it never places or sizes trades directly.

The chart image lets Claude see actual SHAPES (chart patterns like
head & shoulders, double tops, triangles; candlestick patterns like
engulfing bars, pin bars, doji at key levels) that are invisible when
it only sees indicator numbers. Claude is asked to scan across several
analysis families each cycle -- trend, momentum, chart patterns,
candlestick patterns, support/resistance structure -- rather than
anchoring on one, and to report which one(s) actually drove the call
via `method_used`.
"""

import json
import requests

import config

SYSTEM_PROMPT = """You are a market structure and technical analysis assistant.
You will be given:
1. An image of the recent candlestick chart (with EMA20/EMA50 overlay) for
   a single instrument, on its primary analysis timeframe.
2. Indicator values and recent price data for that same timeframe.
3. A small higher-timeframe (usually Daily) trend snapshot for broader context.

Your job is to describe the CURRENT technical picture as objectively as
possible, using BOTH the chart image and the indicator numbers -- not
indicators alone. Scan across multiple analysis methods each time rather
than anchoring on just one:

- Trend-following: EMA/SMA relationship, overall directional structure
- Momentum/oscillators: RSI, MACD (overbought/oversold, divergence)
- Chart patterns (visible in the image): head & shoulders, double top/bottom,
  triangles, flags, wedges, channels
- Candlestick patterns (visible in the image): engulfing bars, pin bars/hammers,
  doji, especially when they occur at a key support/resistance level
- Support/resistance and price structure: breakouts, retests, range boundaries

Only report methods that actually gave a clear read for THIS chart -- do not
force multiple methods to agree if the picture is genuinely mixed, and do not
invent a chart pattern that isn't actually visible in the image.

Explicitly weigh whether the primary timeframe's signal agrees or conflicts
with the higher-timeframe trend. A primary-timeframe signal that aligns with
the higher-timeframe trend deserves higher confidence than one that fights it.

You are not giving financial advice and you are not deciding whether to trade.
A human will read your analysis and decide what to do with it.

Respond with ONLY a JSON object, no other text, no markdown fences, in this exact shape:

{
  "bias": "bullish" | "bearish" | "neutral",
  "confidence": <float 0.0 to 1.0>,
  "structure_quality": "clear" | "mixed" | "unclear",
  "method_used": [<array of strings from: "trend", "momentum", "chart_pattern",
                   "candlestick_pattern", "support_resistance">],
  "pattern_detail": "<name of the specific chart/candlestick pattern seen, e.g.
                      'bearish engulfing at resistance' or 'ascending triangle',
                      or null if no chart/candlestick pattern was identified>",
  "key_levels": {"support": <number or null>, "resistance": <number or null>},
  "suggested_trade_plan": {
    "entry": <number or null>,
    "stop_loss": <number or null>,
    "take_profit": <number or null>,
    "rationale": "<1 sentence on why these specific levels, referencing swing highs/lows, ATR, or key_levels>"
  },
  "reasoning": "<2-4 sentence plain-English explanation citing specific indicator
                 values AND, if applicable, what you saw in the chart image>"
}

Notes on method_used:
- List every method that meaningfully contributed to this call. A confluence
  setup might list ["trend", "momentum", "chart_pattern"] together.
- If nothing clear shows up in the chart image (no discernible pattern), it's
  fine to only report "trend"/"momentum"/"support_resistance" and leave
  pattern_detail null -- don't force a pattern that isn't there.

Notes on suggested_trade_plan:
- This is ADVISORY ONLY, for the human to compare against a separate
  ATR-based calculation -- it is not automatically executed.
- If bias is "neutral" or structure_quality is "unclear", set all three
  values to null rather than forcing a plan onto a bad setup.
- Base entry/stop_loss/take_profit on the actual swing highs/lows, key_levels,
  and any chart pattern's own technical invalidation point (e.g. below a
  head & shoulders neckline), not arbitrary round numbers.

Guidelines for confidence:
- 0.8-1.0: multiple methods clearly agree (e.g. trend + momentum + a clean
  chart pattern all pointing the same way) AND the primary-timeframe bias
  agrees with the higher-timeframe trend
- 0.5-0.79: some agreement but mixed signals, unclear structure, or the primary
  timeframe disagrees with the higher-timeframe trend (counter-trend setup)
- below 0.5: methods conflict or price action is choppy/range-bound

Be conservative. Most real market conditions are mixed, not clean setups.
A clean-looking H4 setup that fights the Daily trend should usually be flagged
as lower confidence, not ignored -- mention the conflict explicitly in reasoning."""


def analyze(market_summary: dict, chart_image_b64: str = None) -> dict:
    """
    Call Claude with a market summary and (if provided) a base64-encoded
    chart image, return parsed structured analysis.
    """
    content = []

    if chart_image_b64:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": chart_image_b64,
            },
        })

    content.append({
        "type": "text",
        "text": (
            "Analyze this market data and chart (if provided) and respond with "
            "the JSON format specified:\n\n" + json.dumps(market_summary, indent=2)
        ),
    })

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": config.ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": config.CLAUDE_MODEL,
            "max_tokens": 800,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
        },
        timeout=45,
    )
    response.raise_for_status()
    data = response.json()

    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    ).strip()

    # Defensive parsing -- strip accidental code fences if the model adds them
    cleaned = text.replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"Could not parse Claude response as JSON: {e}\nRaw response: {text}")

    _validate(parsed)
    return parsed


def _validate(parsed: dict):
    """Fail loudly rather than silently trading on a malformed response."""
    required_keys = {"bias", "confidence", "structure_quality", "method_used",
                      "pattern_detail", "key_levels", "suggested_trade_plan", "reasoning"}
    missing = required_keys - parsed.keys()
    if missing:
        raise ValueError(f"Claude response missing required keys: {missing}")

    if parsed["bias"] not in ("bullish", "bearish", "neutral"):
        raise ValueError(f"Invalid bias value: {parsed['bias']}")

    if not (0.0 <= float(parsed["confidence"]) <= 1.0):
        raise ValueError(f"Confidence out of range: {parsed['confidence']}")

    if not isinstance(parsed["method_used"], list):
        raise ValueError(f"method_used must be a list, got: {type(parsed['method_used'])}")
