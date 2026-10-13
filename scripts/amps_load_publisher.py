"""Load generator for a development AMPS: fills the SOW and publishes ticks.  (NOT verified against a real AMPS)

    AMPS_URI=tcp://amps-dev:9007/amps/json AMPS_TOPIC=market_data ROWS=100000 RATE=20000 python scripts/amps_load_publisher.py

Requires the topic to be a SOW topic keyed by /Symbol in the AMPS server config.
"""
import json
import os
import random
import time

import AMPS

URI = os.environ.get("AMPS_URI", "tcp://localhost:9007/amps/json")
TOPIC = os.environ.get("AMPS_TOPIC", "market_data")
ROWS = int(os.environ.get("ROWS", "100000"))
RATE = int(os.environ.get("RATE", "20000"))  # messages/sec

client = AMPS.Client(f"dh-poc-loadgen-{os.getpid()}")
client.connect(URI)
client.logon()

syms = [f"SYM{i:06d}" for i in range(ROWS)]
state = {}
t0 = time.time()
for s in syms:
    px = random.uniform(10, 500)
    state[s] = {"Symbol": s, "Bid": px - 0.01, "Ask": px + 0.01, "Last": px,
                "PrevClose": px * random.uniform(0.95, 1.05), "Volume": random.randint(0, 10_000_000)}
    client.publish(TOPIC, json.dumps(state[s]))
client.publish_flush()
print(f"SOW cargado: {ROWS} filas en {time.time() - t0:.1f}s")

tick, sent, last = 0.05, 0, time.time()
while True:
    t = time.time()
    for s in random.sample(syms, min(len(syms), int(RATE * tick))):
        r = state[s]
        r["Last"] = max(0.01, r["Last"] * random.uniform(0.999, 1.001))
        r["Bid"], r["Ask"] = r["Last"] - 0.01, r["Last"] + 0.01
        r["Volume"] += random.randint(1, 1000)
        # delta_publish: market fields only -> does not overwrite user-edited fields on the same record
        client.delta_publish(TOPIC, json.dumps({k: r[k] for k in ("Symbol", "Bid", "Ask", "Last", "Volume")}))
        sent += 1
    if time.time() - last >= 5:
        print(f"{sent / (time.time() - last):.0f} msg/s")
        sent, last = 0, time.time()
    time.sleep(max(0.0, tick - (time.time() - t)))
