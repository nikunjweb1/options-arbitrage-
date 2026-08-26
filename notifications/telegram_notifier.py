"""
Telegram notifier for manual trade recommendations.

WHY TELEGRAM: no server, no email SMTP setup, no phone number verification
hassle -- a Telegram bot is free, takes about 3 minutes to create, and
delivers instantly to your phone. This is meant to be the "you don't have
the dashboard open right now but a good opportunity just appeared" channel,
complementing (not replacing) dashboard/frontend/index.html, which is
better for browsing/comparing many candidates at once.

SETUP (one-time, ~3 minutes, no coding required):
  1. In Telegram, message @BotFather -> /newbot -> follow the prompts.
     BotFather gives you a token that looks like
     "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" -- that's TELEGRAM_BOT_TOKEN.
  2. Message your new bot anything (e.g. "hi") so it has a chat to send to.
  3. Visit https://api.telegram.org/bot<TOKEN>/getUpdates in a browser
     (replace <TOKEN> with your real token) -- find "chat":{"id": ...} in
     the response, that number is TELEGRAM_CHAT_ID.
  4. Put both values in config/.env (see config/.env.example).

FAILS QUIETLY BY DESIGN, NOT SILENTLY: if the env vars aren't set, or the
Telegram API call fails for any reason (bad token, no internet, Telegram
outage), this logs a clear warning and returns False -- it never raises,
because a notification failure must never crash
pricing/manual_spread_finder.py's actual recommendation output. The
terminal report and the dashboard are the source of truth; this is a
best-effort convenience on top of them, not a dependency.

NOT WIRED TO ANYTHING THAT PLACES ORDERS: this module can only send a text
message. It has no code path back into any exchange adapter.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("notifications.telegram_notifier")

_API_BASE = "https://api.telegram.org"
_REQUEST_TIMEOUT_SEC = 10


def is_configured() -> bool:
    """Checks env vars without sending anything -- callers can use this to
    decide whether to bother formatting a message at all."""
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(os.environ.get("TELEGRAM_CHAT_ID"))


def send_telegram_message(text: str) -> bool:
    """
    Sends `text` to the configured Telegram chat. Returns True on confirmed
    delivery, False otherwise (not configured, or the API call failed) --
    never raises. Messages over Telegram's ~4096 character limit are
    truncated with a clear marker rather than silently rejected by the API.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning(
            "Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing from "
            "config/.env) -- skipping notification. See this module's docstring for setup."
        )
        return False

    if len(text) > 4000:
        text = text[:3900] + "\n\n...(truncated, see dashboard/terminal for full detail)"

    try:
        resp = requests.post(
            f"{_API_BASE}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("ok"):
            logger.warning("Telegram API returned ok=false: %s", payload)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram notification failed (network/API error): %s", exc)
        return False
    except ValueError as exc:  # malformed JSON response
        logger.warning("Telegram notification failed (malformed response): %s", exc)
        return False


def format_recommendation_alert(recs: list) -> str:
    """
    Builds a plain, scannable alert message from a list of
    pricing.manual_spread_finder.Recommendation objects (already filtered
    to entry_eligible / net-credit ones by the caller -- this function
    doesn't re-check that, it just formats what it's given).

    Duck-typed on purpose (reads .quote.exchange etc via getattr) rather
    than importing Recommendation directly, so this module has zero import
    dependency on pricing/manual_spread_finder.py and can be tested/reused
    independently.
    """
    if not recs:
        return ""

    lines = [f"\U0001F4B0 {len(recs)} net-credit opportunity(ies) found:\n"]
    for r in recs[:5]:  # cap at 5 even if more are passed in, keep the alert scannable
        q = r.quote
        liquidity = (
            f"~{r.max_safe_contracts} contracts safely fillable"
            if getattr(r, "max_safe_contracts", None) is not None
            else "liquidity UNKNOWN"
        )
        low_liq_flag = " \u26A0\uFE0F LOW LIQUIDITY" if (
            getattr(r, "max_safe_contracts", None) is not None and r.max_safe_contracts < 1
        ) else ""
        lines.append(
            f"<b>{q.exchange.upper()} {q.underlying} {q.option_type.value.upper()} {q.strike}</b> "
            f"(exp {q.expiry})\n"
            f"  SELL @ ~{q.bid} on {q.exchange}, BUY @ ~{r.delta_ask} on delta_india\n"
            f"  Net credit: <b>{r.net_entry_cost:+.4f}</b> per contract | {liquidity}{low_liq_flag}\n"
            f"  Est. after-tax profit: ~{r.net_profit_after_tax_estimate:.4f} "
            f"(not tax advice, see pricing/tax.py)\n"
        )
    lines.append("\u26A0\uFE0F Verify live prices on both exchanges before executing -- this may be stale.")
    return "\n".join(lines)
