import logging
import time

from exchange_adapters.shark_ws import SharkWebSocketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

received_count = 0
index_price_count = 0

def on_snapshot(snapshot):
    global received_count
    received_count += 1
    print(f"[{received_count}] {snapshot.instrument_id}  bid={snapshot.best_bid}  ask={snapshot.best_ask}  iv={snapshot.iv}")

def on_index_price(base_coin, quote_coin, price):
    global index_price_count
    index_price_count += 1
    if index_price_count % 5 == 1:  # don't flood the console, this one updates fast
        print(f"  (indexPrice #{index_price_count}) {base_coin}/{quote_coin} = {price}")

client = SharkWebSocketClient(
    host="fawss-options.sharkexchange.in",
    on_snapshot=on_snapshot,
    on_index_price=on_index_price,
)

print("Connecting...")
client.start()

if client.wait_until_connected(timeout_sec=15):
    print("Connected! Listening for 20 seconds (no subscribe call needed -- this feed streams unprompted)...")
    time.sleep(20)
    print(f"\nTotal ticker snapshots received: {received_count}")
    print(f"Total indexPrice updates received: {index_price_count}")
    print(f"Full event breakdown: {client.event_counts}")
else:
    print("FAILED to connect within 15 seconds.")

client.stop()
