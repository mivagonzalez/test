"""Verifies the POC from an external client (pydeephaven): size, ticking, editable metadata, edit round trip and delete."""
import time

import pyarrow as pa
from pydeephaven import Session
from pydeephaven.table import InputTable

import os
_psk = os.environ.get("DH_PSK")
s = (Session(host="localhost", port=10000, auth_type="io.deephaven.authentication.psk.PskAuthenticationHandler", auth_token=_psk)
     if _psk else Session(host="localhost", port=10000))
print("tables:", sorted(s.tables))

grid = s.open_table("grid")
print("grid size:", grid.size, "static:", grid.is_static)
md = {f.name: dict(f.metadata or {}) for f in grid.schema}
for col in ["Symbol", "Trader", "Note", "TargetPx", "Last", "Spread"]:
    flags = {k.decode(): v.decode() for k, v in md[col].items() if b"input" in k.lower()}
    print(f"  {col:8s} input-table metadata: {flags}")


def row(sym):
    return s.open_table("grid").where(f"Symbol = `{sym}`").to_arrow().to_pylist()


sym = "SYM000077"
before = row(sym)[0]
print("before:", {k: before[k] for k in ["Symbol", "Last", "Note", "Trader", "TargetPx"]})

# Edit the same way the web grid does: a table with KEY + editable columns
edit = s.import_table(pa.table({
    "Symbol": pa.array([sym], pa.string()),
    "Trader": pa.array(["mauricio"], pa.string()),
    "Note": pa.array(["watch this"], pa.string()),
    "TargetPx": pa.array([123.45], pa.float64()),
}))
it = InputTable(s, ticket=grid.ticket, schema=grid.schema)
it.key_cols = ["Symbol"]
t0 = time.time()
it.add(edit)
for _ in range(50):
    r = row(sym)[0]
    if r["Note"] == "watch this":
        break
    time.sleep(0.1)
print(f"after edit ({time.time()-t0:.2f}s):", {k: r[k] for k in ["Symbol", "Last", "Note", "Trader", "TargetPx"]})

# Delete
it.delete(s.import_table(pa.table({"Symbol": pa.array([sym], pa.string())})))
for _ in range(50):
    rs = row(sym)
    if not rs or rs[0]["Note"] is None:  # delta: the row disappears / overrides: the edited fields are cleared
        break
    time.sleep(0.1)
print("after delete:", [{k: x[k] for k in ["Symbol", "Note", "Trader"]} for x in row(sym)])

# Ticking: feed_stats at two points in time
c1 = s.open_table("feed_stats").to_arrow().to_pylist()[0]["MessagesIngested"]
time.sleep(5)
c2 = s.open_table("feed_stats").to_arrow().to_pylist()[0]["MessagesIngested"]
print(f"ingest rate ~ {(c2-c1)/5:.0f} msgs/s (total {c2})")
s.close()
import os; os._exit(0)
