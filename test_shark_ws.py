import logging
import time

from exchange_adapters.shark_ws import SharkWebSocketClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

received_count = 0

def on_snapshot(snapshot):
    global received_count
    received_count += 1
    print(f"[{received_count}] {snapshot.instrument_id}  bid={snapshot.best_bid}  ask={snapshot.best_ask}  iv={snapshot.iv}")

client = SharkWebSocketClient(on_snapshot=on_snapshot)

print("Connecting...")
client.start()

if client.wait_until_connected(timeout_sec=15):
    print("Connected! Subscribing to a known symbol and listening for 20 seconds...")
    client.subscribe(["BTC-24AUG26-73000-P-USDT"])
    time.sleep(20)
    print(f"\nTotal snapshots received: {received_count}")
else:
    print("FAILED to connect within 15 seconds -- the inferred ws_url is likely wrong.")

client.stop()
