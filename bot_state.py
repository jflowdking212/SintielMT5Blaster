"""
Tiny persistent on/off switch, controlled via Telegram commands
(/pause, /resume, /status) -- see telegram_notifier.py's
check_and_handle_commands(). Independent of every other rule (daily
alert caps, confidence thresholds, etc.) so it works as a genuine
"stop everything right now" regardless of what else is configured.

Persisted to a JSON file so a restart doesn't silently un-pause the bot.
"""

import json
import os

import config


def _read_state() -> dict:
    if not os.path.exists(config.BOT_STATE_FILE):
        return {"paused": False}
    try:
        with open(config.BOT_STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"paused": False}


def _write_state(state: dict):
    with open(config.BOT_STATE_FILE, "w") as f:
        json.dump(state, f)


def is_paused() -> bool:
    return _read_state().get("paused", False)


def pause():
    _write_state({"paused": True})


def resume():
    _write_state({"paused": False})
