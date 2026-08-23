"""
Shark Exchange WebSocket -- DIAGNOSTIC CAPTURE MODE, not a finished adapter.

WHY THIS FILE EXISTS IN THIS FORM: unlike Delta (exchange_adapters/delta_ws.py,
built against Delta's own published channel-name list, then corrected
against a live connection), Shark Exchange has ZERO public API
documentation for options (confirmed docs/architecture.md Section M.6:
"No self-serve public API docs found for options specifically... only
browser-based trading confirmed to exist"). We have no evidence at all for:
  - whether connecting alone is enough to receive data, or an explicit
    subscribe/join message is required after connecting
  - what that subscribe message (if needed) looks like
  - what the incoming ticker/orderbook message shape looks like

Per this project's own established rule (see ev_engine.py's Bug #2, backtest
engine.py's Bug fix, delta_ws.py's channel-name correction -- every one of
those was found by looking at REAL data, never by guessing), writing a real
parser here without evidence would just be a guess wearing code's clothing.
So this file's only job is: connect, log EVERYTHING received verbatim, and
report a clean summary of what event types actually showed up. A real
parser gets written after someone (a person running this, or the person
inspecting Shark's own options page in their browser's Network tab -> WS
frames -> Messages sub-tab) has looked at real captured output.

Investigated as a possible shortcut (2026-08-23): Mirror Pip (mirrorpip.com),
a third-party copy-trading app that connects user exchange accounts. Ruled
out as a source of protocol evidence -- Mirrorpip almost certainly talks to
each exchange from ITS OWN backend using each exchange's sanctioned API
(user-supplied API keys), not from its frontend, so inspecting Mirrorpip's
browser traffic would only reveal Mirrorpip's own API to its own backend,
never Shark's actual WebSocket protocol. No shortcut around real capture.

SCOPE BOUNDARY, ENFORCED IN CODE NOT JUST IN CONVERSATION: Shark's
"-uds-"-prefixed subdomains (fawss-uds, fawss-uds-options) are, per the
naming convention exchanges commonly use ("User Data Stream"), almost
certainly account-authenticated channels (positions/orders/balances) tied
to a logged-in session -- not public market data. Connecting to those
programmatically would mean replicating account session auth outside
Shark's sanctioned channel, which is a materially different (and
meaningfully riskier -- likely a Terms of Service concern) thing than
listening to a public broadcast. This script refuses to connect to any
host containing "uds" and requires an explicit --i-understand-the-risk flag
even for the public hosts, so running this is always a deliberate choice,
never an accident.

Protocol notes (these ARE independently verifiable -- Engine.IO/Socket.IO
v4 is a public, documented open protocol, unlike Shark's own application-
level message shapes on top of it): the URLs originally captured
(wss://fawss-options.sharkexchange.in/socket.io/?EIO=4&transport=websocket&sid=...)
are Engine.IO v4 WebSocket URLs. The `sid` is NOT reusable -- it's issued
per-connection during the Engine.IO handshake. `python-socketio`'s Client
handles that handshake correctly on its own; we do not hardcode or reuse
any previously-captured `sid`.

Verified offline before push: syntax compiles, the uds-refusal logic
correctly blocks "fawss-uds-options.sharkexchange.in" and allows
"fawss-options.sharkexchange.in", the --i-understand-the-risk gate correctly
refuses to run without it, and the missing-python-socketio-dependency path
fails with a clear message rather than a bare traceback (tested with
python-socketio genuinely not installed). The actual WebSocket connection
itself is NOT yet verified against a live Shark endpoint -- this sandbox has
no network access; running this against the real host and sharing the
output/capture file is the next real step.

Usage:
    pip install "python-socketio[client]" --break-system-packages
    python -m exchange_adapters.shark_ws_capture --host fawss-options.sharkexchange.in --duration-sec 120 --i-understand-the-risk
    python -m exchange_adapters.shark_ws_capture --host fawss.sharkexchange.in --duration-sec 120 --i-understand-the-risk

Output: prints every event as it arrives, and on exit (duration elapsed or
Ctrl+C) prints a summary -- distinct event names seen, count of each, and
one full example payload per event name -- plus writes the complete raw
capture to a timestamped JSON file for later inspection.
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


def _refuse_if_uds(host: str) -> None:
    if "uds" in host.lower():
        logger.error(
            "Refusing to connect to %s -- 'uds' hosts are almost certainly "
            "account-authenticated (User Data Stream) channels tied to a "
            "logged-in session, not public market data. This script only "
            "connects to public feeds (e.g. fawss.sharkexchange.in, "
            "fawss-options.sharkexchange.in). See this file's module "
            "docstring for why.",
            host,
        )
        raise SystemExit(2)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Diagnostic capture: log every raw message from a Shark Exchange public WS feed."
    )
    parser.add_argument("--host", required=True, help="e.g. fawss-options.sharkexchange.in")
    parser.add_argument("--duration-sec", type=int, default=120, help="How long to listen before disconnecting.")
    parser.add_argument("--subscribe-event", default=None,
                         help="Optional: event name to emit after connecting, e.g. 'subscribe'. "
                              "No default -- we have no evidence one is needed or what it should say. "
                              "Try running with no subscribe first; if nothing arrives, that itself is "
                              "useful information (probably means a subscribe step IS required).")
    parser.add_argument("--subscribe-payload", default=None,
                         help='Optional JSON payload to send with --subscribe-event, e.g. \'{"symbol":"BTCUSDT"}\'. '
                              "Only meaningful together with --subscribe-event.")
    parser.add_argument("--i-understand-the-risk", action="store_true",
                         help="Required. Confirms you're connecting to an undocumented, reverse-engineered "
                              "public endpoint, not something Shark has published/sanctioned.")
    parser.add_argument("--output-dir", default="reports/shark_ws_capture")
    args = parser.parse_args()

    if not args.i_understand_the_risk:
        logger.error(
            "Refusing to run without --i-understand-the-risk. This connects to an "
            "undocumented Shark Exchange endpoint -- see this file's module docstring."
        )
        return 2

    _refuse_if_uds(args.host)

    try:
        import socketio
    except ImportError:
        logger.error(
            "python-socketio is not installed. Run: "
            'pip install "python-socketio[client]" --break-system-packages'
        )
        return 1

    subscribe_payload = None
    if args.subscribe_payload:
        try:
            subscribe_payload = json.loads(args.subscribe_payload)
        except json.JSONDecodeError as exc:
            logger.error("--subscribe-payload is not valid JSON: %s", exc)
            return 2

    sio = socketio.Client(logger=False, engineio_logger=False)
    captured: list[dict] = []
    event_counts: dict[str, int] = {}
    event_examples: dict[str, dict] = {}

    @sio.event
    def connect():
        logger.info("Connected to wss://%s/socket.io/ (real sid negotiated by the client, not reused).", args.host)
        if args.subscribe_event:
            logger.info("Emitting %r with payload=%r", args.subscribe_event, subscribe_payload)
            if subscribe_payload is not None:
                sio.emit(args.subscribe_event, subscribe_payload)
            else:
                sio.emit(args.subscribe_event)
        else:
            logger.info(
                "No --subscribe-event given -- just listening for anything the server sends "
                "unprompted after connecting."
            )

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
        logger.info("EVENT %r: %s", event, json.dumps(data)[:300] if data is not None else "(no payload)")

    url = f"https://{args.host}"
    logger.info("Connecting to %s (default Socket.IO path /socket.io/) ...", url)
    try:
        sio.connect(url, wait_timeout=15)
    except Exception as exc:  # noqa: BLE001 -- report whatever the real failure was, don't swallow it
        logger.error("Failed to connect: %s", exc)
        return 1

    logger.info("Listening for %ds ... (Ctrl+C to stop early)", args.duration_sec)
    try:
        time.sleep(args.duration_sec)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    finally:
        sio.disconnect()

    # -- summary ---------------------------------------------------------------
    logger.info("=== Capture summary for %s ===", args.host)
    if not captured:
        logger.warning(
            "Zero events received in %ds. This is real, useful information: either (a) a "
            "subscribe message IS required and we didn't send one -- try --subscribe-event, "
            "or (b) this host doesn't push data without account auth after all, or (c) the "
            "connection itself didn't fully establish (check the connect log line above). "
            "Next step either way: open Shark's real options page in a browser, DevTools -> "
            "Network -> WS -> click the connection -> Messages tab, and share what's actually "
            "sent/received there -- that's real evidence, this empty capture on its own is not "
            "enough to build a parser from.",
            args.duration_sec,
        )
    else:
        logger.info("%d total message(s), %d distinct event type(s):", len(captured), len(event_counts))
        for event, count in sorted(event_counts.items(), key=lambda kv: -kv[1]):
            logger.info("  %r: %d message(s)", event, count)
            logger.info("    example: %s", json.dumps(event_examples[event])[:500])

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_file = output_dir / f"{args.host}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_file.write_text(json.dumps(captured, indent=2, default=str))
    logger.info("Wrote full raw capture (%d messages) to %s -- share this file's contents to build a real parser.",
                len(captured), out_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
