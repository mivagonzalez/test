"""Measures how long the SOW mirror takes to reach TARGET rows (initial load)."""
import os
import time

from pydeephaven import Session

t0 = time.time()
while True:
    try:
        s = Session(host="localhost", port=10000)
        break
    except Exception:
        time.sleep(1)
print("connected after", round(time.time() - t0, 1), "s", flush=True)
target = int(os.environ.get("TARGET", "1000000"))
while True:
    try:
        n = s.open_table("sow_state").size
    except Exception:
        n = -1
    print(round(time.time() - t0, 1), "rows", n, flush=True)
    if n >= target:
        break
    time.sleep(2)
os._exit(0)
