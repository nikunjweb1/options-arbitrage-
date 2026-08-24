import logging
import time

from exchange_adapters.shark_ws import SharkWebSocketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

received_count = 0

def on_snapshot(snapshot):
    global received_count
    received_count += 1
    print(f"[{received_count}] {snapshot.instrument_id}  bid={snapshot.best_bid}  ask={snapshot.best_ask}  iv={snapshot.iv}")


if __name__ == "__main__":
    print(
        "NOTE: this script's original constructor call "
        "(SharkWebSocketClient(on_snapshot=on_snapshot)) is from before shark_ws.py "
        "was rewritten against real captured Socket.IO frames -- SharkWebSocketClient "
        "now requires an explicit host= argument (e.g. 'fawss-options.sharkexchange.in', "
        "the confirmed real host, not the earlier guessed URL). Update the call below "
        "before running this again, e.g.:\n\n"
        "    client = SharkWebSocketClient(host='fawss-options.sharkexchange.in', on_snapshot=on_snapshot)\n"
    )
