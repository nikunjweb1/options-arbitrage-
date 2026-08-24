"""
Diagnostic run for SharkWebSocketClient -- deliberately does NOT call
subscribe() with any guessed symbol.

WHY: shark_ws.py's _parse_ticker silently drops any ticker event whose
`symbol` field isn't in the locally-tracked subscribed set (see that file's
`if tracked and symbol not in tracked: return None`). If the previous test
run subscribed to a guessed symbol string that didn't exactly match what
the server sends, every real incoming frame would have been filtered out
before reaching on_snapshot -- producing exactly "Total snapshots
received: 0" even if data was flowing fine. Leaving the tracked-symbol set
empty disables that filter entirely (tracked is falsy -> condition never
fires), so this run either confirms data really is flowing (and shows the
real symbol strings, settling the "what does a real symbol look like"
question) or confirms it genuinely isn't (a different problem).

Also logs every event at DEBUG, not just the three confirmed handlers, so
nothing is silently missed.

Usage:
    python test_shark_ws_nofilter.py
"""

from __future__ import annotations

import logging
import time

from exchange_adapters.shark_ws import SharkWebSocketClient

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("socketio").setLevel(logging.WARNING)  # keep the library's own noise down
logging.getLogger("engineio").setLevel(logging.WARNING)

received_count = 0
seen_instrument_ids: set[str] = set()


def on_snapshot(snapshot) -> None:
    global received_count
    received_count += 1
    seen_instrument_ids.add(snapshot.instrument_id)
    print(
        f"[{received_count}] id={snapshot.instrument_id!r}  "
        f"bid={snapshot.best_bid}  ask={snapshot.best_ask}  "
        f"iv={snapshot.iv}  index={snapshot.index_price}"
    )


def main() -> None:
    client = SharkWebSocketClient(host="fawss-options.sharkexchange.in", on_snapshot=on_snapshot)

    print("Connecting (no subscribe -- listening for ANY unsolicited data)...")
    client.start()

    if not client.wait_until_connected(timeout_sec=15):
        print("FAILED to connect within 15s.")
        return

    print("Connected. Listening for 30s with no symbol filter applied...")
    time.sleep(30)

    client.stop()

    print(f"\n=== Result ===")
    print(f"Total snapshots received: {received_count}")
    print(f"Distinct instrument_ids seen: {sorted(seen_instrument_ids)}")
    if received_count == 0:
        print(
            "\nZero snapshots even with NO symbol filter applied. This is real "
            "negative evidence -- either the connection genuinely receives nothing "
            "unsolicited (contradicting the earlier browser capture), or something "
            "about *how* the browser session established that earlier connection "
            "matters (e.g. Referer/Origin header, cookies, or another concurrent "
            "browser tab). Worth re-capturing a fresh, full (untruncated) frame "
            "directly from DevTools right now to confirm the server still behaves "
            "the same way, rather than assuming this script's connection method "
            "should be identical to a browser's."
        )
    else:
        print(
            "\nReal data received with no filter. Compare the instrument_id/symbol "
            "strings printed above against whatever subscribe() was called with in "
            "the earlier test -- a mismatch there is the likely root cause of the "
            "earlier zero-result run."
        )


if __name__ == "__main__":
    main()
