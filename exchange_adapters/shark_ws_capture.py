"""
Shark Exchange WebSocket -- capture/diagnostic tool for the PUBLIC feed only.

UPDATED (2026-08-24) with the real, documented subscribe protocol, found in
Shark's own official API reference (docs.sharkexchange.in, "Public Web
Sockets" section) -- not guessed:

    Public WS host (documented):  https://fawss.sharkexchange.in/
    Subscribe:                    socket.emit('subscribe', {params: [topics]})
    Topic format:                 {contract_pair}@{stream_type}
                                   e.g. 'btcinr@ticker', 'btcinr@depth_0.1'
    Listen events:                depthUpdate, kline, markPriceUpdate,
                                   aggTrade, 24hrTicker, marketInfo,
                                   markPriceArr, tickerArr,
                                   allContractDetails, marginRate

IMPORTANT GAP THIS SCRIPT EXISTS TO INVESTIGATE: every documented example
uses lowercase FUTURES pairs (btcinr, ethinr, grtinr) -- contractType is
PERPETUAL in every single REST response example across the whole doc, and
the documented public WS host is fawss.sharkexchange.in, NOT
fawss-options.sharkexchange.in. The two "-options" hosts originally
captured from the browser do not appear anywhere in the official docs.
That means: Options support is real (confirmed via the product's "Options
Beta" nav tab and these separate subdomains existing), but it is NOT part
of the documented API surface -- it's a separate, still-undocumented
extension. This script can now do two useful things:

  1. Prove the documented protocol actually works, against a KNOWN-GOOD
     futures topic on the documented host (fawss.sharkexchange.in). If this
     doesn't work, something more fundamental is wrong (network, library
     version) and that's worth knowing before testing anything undocumented.
  2. Test the SAME subscribe protocol against the undocumented
     fawss-options.sharkexchange.in host, with a guessed options-style
     topic. This is explicitly a guess (we have zero confirmed evidence of
     Shark's options symbol format) -- if it works, we've learned the
     contract_pair naming convention for options by trial; if it doesn't,
     that's useful negative evidence too, logged plainly as a guess that
     failed, not silently discarded.

SCOPE BOUNDARY, ENFORCED IN CODE: "-uds-" hosts (fawss-uds,
fawss-uds-options) remain off-limits -- confirmed by the official docs
themselves to be the authenticated stream (requires a listenKey from a
real account). See _refuse_if_uds().

Verified offline before push: syntax compiles, uds-host refusal still
blocks both "-uds-" hosts, the (previously mis-flagged as risky)
"fawss-options" host is correctly allowed through since it is NOT a uds
host, missing --i-understand-the-risk still refuses to run, and the
documented event-name set is correctly loaded. The actual live connection
and subscribe round-trip are NOT yet verified -- no network access in this
sandbox; running this against the real hosts and sharing the output is the
next step.

Usage:
    pip install "python-socketio[client]" --break-system-packages

    # Step 1: prove the documented protocol works at all (known-good topic)
    python -m exchange_adapters.shark_ws_capture --host fawss.sharkexchange.in \\
        --topics btcinr@ticker btcinr@markPrice --duration-sec 60 --i-understand-the-risk

    # Step 2: probe the undocumented options host with a guessed topic
    python -m exchange_adapters.shark_ws_capture --host fawss-options.sharkexchange.in \\
        --topics "C-BTC-65000-210826@ticker" --duration-sec 60 --i-understand-the-risk --is-guess

Output: prints every event as it arrives, and on exit prints a summary --
distinct event names seen, count of each, one example payload per event --
plus writes the complete raw capture to a timestamped JSON file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("shark_ws_capture")

# Confirmed from Shark's own docs -- the full set of documented listen
# events for the public futures feed. Anything OUTSIDE this set that shows
# up during a capture (especially against the -options host) is genuinely
# new information, not something we already knew to expect.
_KNOWN_DOCUMENTED_EVENTS = {
    "depthUpdate", "kline", "markPriceUpdate", "aggTrade", "24hrTicker",
    "marketInfo", "markPriceArr", "tickerArr", "allContractDetails", "marginRate",
}


def _refuse_if_uds(host: str) -> None:
    if "uds" in host.lower():
        logger.error(
            "Refusing to connect to %s -- confirmed by Shark's own docs to be the "
            "authenticated stream (requires a listenKey tied to a logged-in account), "
            "not public market data. This script only connects to public feeds.",
            host,
        )
        raise SystemExit(2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Capture real messages from a Shark Exchange public WS feed using the documented subscribe protocol."
    )
    parser.add_argument("--host", required=True, help="e.g. fawss.sharkexchange.in or fawss-options.sharkexchange.in")
    parser.add_argument("--topics", nargs="+", required=True,
                         help="Topics to subscribe to, e.g. btcinr@ticker btcinr@depth_0.1. "
                              "Documented format: {contract_pair}@{stream_type}.")
    parser.add_argument("--duration-sec", type=int, default=60)
    parser.add_argument("--is-guess", action="store_true",
                         help="Mark this run as testing a GUESSED topic format (e.g. an options symbol "
                              "we have no confirmed evidence for). Purely for clearer logging -- makes "
                              "the summary explicitly say whether this was a known-good or speculative test.")
    parser.add_argument("--i-understand-the-risk", action="store_true",
                         help="Required. The -options host specifically is undocumented -- see module docstring.")
    parser.add_argument("--output-dir", default="reports/shark_ws_capture")
    args = parser.parse_args()

    if not args.i_understand_the_risk:
        logger.error("Refusing to run without --i-understand-the-risk. See this file's module docstring.")
        return 2

    _refuse_if_uds(args.host)

    try:
        import socketio
    except ImportError:
        logger.error('python-socketio is not installed. Run: pip install "python-socketio[client]" --break-system-packages')
        return 1

    sio = socketio.Client(logger=False, engineio_logger=False)
    captured: list[dict] = []
    event_counts: dict[str, int] = {}
    event_examples: dict[str, dict] = {}

    guess_label = " [GUESSED TOPIC FORMAT]" if args.is_guess else " [documented, known-good format]"

    @sio.event
    def connect():
        logger.info("Connected to %s.%s", args.host, guess_label)
        logger.info("Subscribing to: %s", args.topics)
        sio.emit("subscribe", {"params": args.topics})

    @sio.event
    def connect_error(data):
        logger.error("Connection failed: %s", data)

    @sio.event
    def disconnect():
        logger.info("Disconnected.")

    @sio.on("*")
    def catch_all(event, data=None):
        ts = datetime.now(timezone.utc).isoformat()
        record = {"ts": ts, "event": event, "data": data}
        captured.append(record)
        event_counts[event] = event_counts.get(event, 0) + 1
        if event not in event_examples:
            event_examples[event] = record
        is_new = event not in _KNOWN_DOCUMENTED_EVENTS
        tag = " <-- NOT in documented event list, genuinely new info" if is_new else ""
        logger.info("EVENT %r%s: %s", event, tag, json.dumps(data)[:300] if data is not None else "(no payload)")

    url = f"https://{args.host}"
    logger.info("Connecting to %s ...", url)
    try:
        sio.connect(url, wait_timeout=15)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to connect: %s", exc)
        return 1

    logger.info("Listening for %ds ... (Ctrl+C to stop early)", args.duration_sec)
    try:
        time.sleep(args.duration_sec)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        sio.disconnect()

    logger.info("=== Capture summary for %s%s ===", args.host, guess_label)
    if not captured:
        logger.warning(
            "Zero events received in %ds. If this was the documented known-good test "
            "(--host fawss.sharkexchange.in, --topics btcinr@ticker or similar), that's "
            "unexpected and worth investigating -- something more basic may be wrong. "
            "If this was a --is-guess run against the options host, this is a real "
            "negative result: this specific guessed topic format doesn't work, not proof "
            "options data is unavailable there entirely -- other formats may still work.",
            args.duration_sec,
        )
    else:
        logger.info("%d total message(s), %d distinct event type(s):", len(captured), len(event_counts))
        for event, count in sorted(event_counts.items(), key=lambda kv: -kv[1]):
            new_tag = " (NOT in documented list)" if event not in _KNOWN_DOCUMENTED_EVENTS else ""
            logger.info("  %r%s: %d message(s)", event, new_tag, count)
            logger.info("    example: %s", json.dumps(event_examples[event])[:500])

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{args.host}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_file.write_text(json.dumps(captured, indent=2, default=str))
    logger.info("Wrote full raw capture (%d messages) to %s", len(captured), out_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
