# Deephaven + AMPS — Real-time grid (POC) · Full context for an AI agent (GitHub Copilot)

> **For the agent:** this file replaces the official Deephaven and AMPS documentation (not reachable from this VM).
> Use ONLY the APIs that appear here. If you need something that is not here, do not invent the signature: leave a
> `TODO(verify API)` and continue. **Project constraint: everything in Python.** Do not write Java or JavaScript and
> do not add npm packages. Everything marked **[VERIFIED]** was executed end to end against Deephaven 42.6 (real
> server; the UI was exercised with a real Chrome browser clicking and typing). Items marked **[NOT VERIFIED]** are
> based on the documentation/source code but were not executed.
>
> **How to load it in Copilot:** save it as `docs/DEEPHAVEN_AMPS_GRID_CONTEXT.md` and keep a short
> `.github/copilot-instructions.md` that tells the agent to read it first (Copilot includes that file in every
> request). In chat you can also attach it with `#file:docs/DEEPHAVEN_AMPS_GRID_CONTEXT.md`.
> Research done on 2026-10-13 against `deephaven-core` 42.x, `deephaven-plugin-ui` 0.42.x and `amps-python-client` 5.3.5.13.

---

## 0. Contents

1. Verdict and "Python only" scope
2. Verified versions and environment
3. Deephaven concepts you must understand
4. Architecture (data and edits)
5. Alternatives evaluated (and why not)
6. POC repo layout
7. Full POC code (verified)
8. Grid features: how to do each one
9. Client side (no npm)
10. Performance: knobs, measurements and how to measure
11. Known issues and fixes
12. Step-by-step work plan (with acceptance criteria)
13. API cheat sheet (Deephaven Python, query language, deephaven.ui, pydeephaven, AMPS)
14. Questions to validate at work

---

## 1. Verdict and "Python only" scope

**Deephaven covers every requirement and the POC can be built 100% in Python.**

- **Server:** the app code is Python (Application Mode). Deephaven runs on the JVM, but that is just the runtime:
  it ships inside the Docker image (or you install a JDK when using `pip`). No Java is written or compiled.
- **Client:** the web grid (canvas, millions of rows) **is served by the Deephaven server itself** at
  `http://host:10000/ide` and `http://host:10000/iframe/widget/?name=<variable>`. The browser downloads it from the
  server: **no npm and no JS API package from the internal registry are needed.** The app UI (layout, filters,
  forms, menus) is built in Python with `deephaven.ui`. For tests/automation from outside, use the Python client
  `pydeephaven`.
- **Edits → AMPS without Java:** the grid only lets users edit tables that carry the `InputTable` attribute. We
  create a *native input table* (`edit_requests`, key + editable columns) and "lend" its attribute to the grid table
  with `with_attributes`. Every edit lands in `edit_requests`; a Python listener publishes it to AMPS; the value
  comes back through the subscription. AMPS remains the source of truth.

| Requirement | Supported? | How | Status |
|---|---|---|---|
| Grid with millions of cells | Yes | Canvas grid + viewport (only visible rows/columns are sent) over Barrage. | **[VERIFIED]** 1M rows × 15 cols ticking at 20k updates/s (2 vCPU); rendered in Chrome |
| Deephaven server + client | Yes | Python server (Docker or pip) + web UI served by the server + `deephaven.ui`. | **[VERIFIED]** |
| Ingest AMPS (SOW + updates) | Yes | AMPS Python client → `table_publisher` (blink) → `last_by(key)` = SOW mirror. | **[VERIFIED]** with a simulated feed; AMPS API verified by introspecting the package |
| Deletes / OOF | Yes | `IsDeleted` tombstone + `where("!IsDeleted")`. | **[VERIFIED]** |
| Conditional column formatting | Yes | `format_columns` / `format_column_where` (or `ui.TableFormat`). | **[VERIFIED]** visually in Chrome |
| Conditional row formatting | Yes | `format_row_where` / `__ROWFORMATTED`. | **[VERIFIED]** visually in Chrome |
| Inline editing | Yes | Native input table + `with_attributes({"InputTable": ...})`. Only editable columns; the key is read-only. | **[VERIFIED]** double-click + typing in Chrome, in `grid` and in `ui.table`, also with filter/sort applied |
| Edits update AMPS | Yes | Python listener → `publish` (overrides topic) or `delta_publish` (same topic). | **[VERIFIED]** with simulated AMPS; against a real AMPS **[NOT VERIFIED]** |
| Delete from the UI | Yes | Context menu (`ui.table(context_menu=...)`) → `sow_delete`. | **[VERIFIED]** right-click in Chrome |
| Filtering | Yes | Quick filters, advanced filters, search, `where`. Executed on the server. | **[VERIFIED]** quick filter in Chrome |
| Sorting | Yes | Header click, `sort`. | **[VERIFIED]** in Chrome on 200k ticking rows |

---

## 2. Verified versions and environment

| Component | Tested version | Notes |
|---|---|---|
| deephaven-server / deephaven-core (pip) | **42.6** | Docker: `ghcr.io/deephaven/server:<tag>` (Python; ships JDK 21, deephaven.ui, numpy, pandas) |
| deephaven-plugin-ui (`deephaven.ui`) | **0.42.2** | Included in the `server` Docker image. The online docs describe newer versions (see issues) |
| pydeephaven (Python client, tests only) | 42.6 | **Requires `bitstring<4.4`** |
| amps-python-client | 5.3.5.13 | `pip install amps-python-client` → `import AMPS` |
| Java (runtime only, not programmed) | JDK 21 | Shipped in the Docker image; with pip: JDK 17–25 and `JAVA_HOME` |
| Python | 3.13 tested; 3.12 in Docker | Officially 3.9+ |
| Browser | Chrome 153 (headless) | The UI is the one served by the server, no custom build |

Resources: Deephaven recommends **≥ 4 GB of heap**. With 1M rows × 15 cols the process used ~2 GB RSS.

---

## 3. Deephaven concepts you must understand

1. **Ticking tables and the Update Graph (UG).** Live tables update in *cycles* (default **1000 ms**,
   `-DPeriodicUpdateGraph.targetCycleDurationMillis`). Every operation (`where`, `sort`, `last_by`, `natural_join`,
   `update_view`...) produces a derived table that is updated **incrementally**. The table graph is a DAG.
2. **Table types:** *append-only*; *blink* (each cycle only contains that cycle's rows; produced by
   `table_publisher`); *ring* (last N rows); *input table* (append-only or keyed; editable from code and from the UI).
3. **Aggregations on blink tables** (`last_by`, `count_by`, `agg_by`...) aggregate over **the whole observed
   stream**, not just the current cycle ⇒ `blink.last_by("Symbol")` = latest state per symbol = SOW mirror, with
   O(#keys) memory. `group_by`, `partition_by`, `slice`, `head_pct`, `tail_pct` are not allowed on blink tables.
4. **Barrage / viewport.** The client subscribes to visible rows and columns (+1 page around). The server pushes
   deltas every `barrage.minUpdateInterval` ms (default 1000). User sorts/filters run on the server.
5. **Execution context and threads.** Python code that creates/operates on tables from **its own thread** must do
   it inside `with ctx:` (with `ctx = get_exec_ctx()` captured at startup). Otherwise you get
   `ExecutionContextRegistrationException` (verified). `table_publisher` with `on_flush_callback` avoids the problem
   for ingestion. Listeners run on the UG: never block them (enqueue and process on another thread).
6. **Table attributes.** The `"InputTable"` attribute (an "updater" object that writes into an input table) is what
   makes a table editable in the grid. It is **kept** through `update_view`, `sort`, `where`, joins and `flatten`;
   `format_columns`/`format_*_where` are implemented with `update_view` ⇒ they keep it. It is **not** kept through
   aggregations ⇒ attach it after the last `last_by`/`natural_join`. The updater of one input table can be attached
   to ANOTHER table with `t.with_attributes({"InputTable": it.j_table.getAttribute("InputTable")})` (verified): the
   grid shows as editable only that input table's columns and writes into it.
7. **Application Mode.** `.app` + `.py` files in `-Ddeephaven.application.dir=...` run at startup. Global variables
   become available in the IDE, via iframe and to `pydeephaven`.
8. **Query language.** Java-like formulas in strings: backticks for strings (`` `IBM` ``), ternary `c ? a : b`,
   nulls via `isNull(x)` and constants `NULL_DOUBLE`, `NULL_LONG`.

---

## 4. Architecture

### 4.1 Data flow (AMPS → grid)

```
 Producer ──publish/delta_publish──▶ AMPS (SOW topic, key /Symbol)
                                       │ sow_and_subscribe(oof_enabled=True)   [AMPS receive thread, inside the DH server]
                                       ▼
                        json.loads → buffer (lock) + set of live keys                O(1) per message
                                       │ on_flush_callback (once per UG cycle)
                                       ▼
                 table_publisher ──▶ BLINK (rows of the cycle + IsDeleted)
                                       │ .last_by("Symbol")
                                       ▼
                        SOW mirror ──.where("!IsDeleted")──▶ market
                                       │ [overrides mode: natural_join with the overrides topic mirror]
                                       ▼ .update_view([...calculated columns...])
                      .with_attributes({"InputTable": edit_requests' updater})   ← makes it editable
                                       ▼ .format_columns(...) / .format_row_where(...)
                                     grid ──Barrage──▶ server web UI (IDE / iframe / deephaven.ui)
```

### 4.2 Edit flow (grid → AMPS → grid) — all Python

```
User: double-clicks "Note", types, presses Enter
  └─ the grid writes {Symbol, Trader, Note, TargetPx} into the native input table edit_requests
       └─ Python listener (listen) on edit_requests: takes the changed KEYS, reads the values with iter_dict(),
          enqueues → "edit-router" thread (with execution context):
            ├─ key not in the SOW → reject and remove it from edit_requests (the grid lets users append rows)
            └─ valid key → AMPS.publish(overrides topic) / AMPS.delta_publish(market topic)
                 └─ AMPS updates its SOW and re-publishes ──▶ subscription ──▶ grid shows the value
User: right-click → "Delete in AMPS" (ui.table context_menu) → AMPS.sow_delete
```
Measured: **0.3–0.6 s** round trip with a 200 ms UG cycle (simulated AMPS). With a real AMPS add its RTT.
Meanwhile the grid shows the typed value as pending and it is confirmed when it comes back from AMPS.

### 4.3 Two edit modes (`EDIT_MODE`)

| Mode | What it does | Advantage | Risk |
|---|---|---|---|
| `overrides` **(recommended)** | Edits → `publish` to a separate SOW topic (`market_data_overrides`, same key). DH subscribes to both and does a `natural_join`. | The feed never overwrites user fields. Separate permissions. | One more topic in AMPS. |
| `delta` | Edits → `delta_publish` to the market topic (AMPS merges fields). | Single topic. | If the producer publishes **full** records, it wipes user edits. |

Delete: in `overrides` it deletes the override (the market row stays, with empty user fields); in `delta` it runs a
`sow_delete` on the market record (the row disappears via OOF).

---

## 5. Alternatives evaluated (and why not)

**Ingestion**

| Option | Verdict |
|---|---|
| **A. `table_publisher` + `last_by`** | **Chosen.** Recommended API, one batch per cycle, O(keys) memory. |
| B. Keyed `input_table` + `add_async`/`delete_async` as the mirror | Valid (native upsert/delete) if sent in *batches*. Less efficient than A. |
| C. `DynamicTableWriter` | Deephaven recommends `table_publisher`; grows without bound. |
| D. AMPS Java client | Ruled out by the "Python only" constraint. |

**Editing**

| Option | Verdict |
|---|---|
| **1. Native input table `edit_requests` + `with_attributes` + Python listener** | **Chosen.** Real inline editing, no Java, no loops (edit_requests only receives human edits). |
| 2. `deephaven.ui` form (context menu → `ui.dialog`/`ui.form`) | Fallback if inline editing is not wanted. |
| 3. The grid itself as an input table mirroring AMPS + listener | Mixes feed data and user data in the same table ⇒ loops/ambiguity. Ruled out. |
| 4. Custom `InputTableUpdater` in Java | Works (it was tested), but requires Java. Ruled out. |

**Client**

| Option | Verdict |
|---|---|
| **Server web UI (`/ide`, `/iframe/widget`) + `deephaven.ui`** | **Chosen.** Zero npm. |
| React + `@deephaven/iris-grid` / JS API | Requires npm packages not available internally. Ruled out. |

---

## 6. POC repo layout

```
deephaven-amps-poc/
├── .github/copilot-instructions.md      ← short instructions pointing to docs/
├── docs/DEEPHAVEN_AMPS_GRID_CONTEXT.md  ← this file
├── Dockerfile                           ← Deephaven image + AMPS client
├── docker-compose.yml
├── server/app.d/
│   ├── amps_grid.app                    ← Application Mode
│   └── amps_grid.py                     ← AMPS ingestion, SOW mirror, editing, formatting, deephaven.ui
├── scripts/
│   ├── run_local.py                     ← server without Docker (pip)
│   └── amps_load_publisher.py           ← load generator for a development AMPS
└── tests/
    ├── run_server.py                    ← test server (anonymous auth, local only)
    ├── test_client.py                   ← editable metadata, edit, delete, ingest rate (pydeephaven)
    └── load_probe.py                    ← SOW load time
```

---

## 7. Full POC code

### 7.1 `server/app.d/amps_grid.app` — [VERIFIED]

```properties
type=script
scriptType=python
enabled=true
id=amps.grid.poc
name=AMPS Grid POC
file_0=amps_grid.py
```

### 7.2 `server/app.d/amps_grid.py` — [VERIFIED with FEED_MODE=mock; against a real AMPS: NOT VERIFIED]

```python
"""
POC: Deephaven grid fed by AMPS (SOW + subscription) with user edits published back to AMPS.
100% Python (no Java, no JS/npm): Deephaven server + the web UI served by the server + deephaven.ui.

Runs in Application Mode (data/app.d/amps_grid.app). Exported variables (IDE / iframe / pydeephaven):
  - grid          : final table (calculated + editable + formatted) -> the one users see
  - grid_ui       : the same table wrapped in deephaven.ui (if the plugin is installed)
  - market        : mirror of the market topic SOW
  - overrides     : mirror of the overrides topic SOW (only when EDIT_MODE=overrides)
  - feed_blink    : blink table with the market messages of each cycle
  - feed_stats    : total count of ingested market messages
  - edit_requests : user edit inbox (native input table)
  - dashboard     : deephaven.ui dashboard (grid + stats)

EDIT_MODE:
  - "delta"     : edits are sent with delta_publish to the SAME market topic. Simple, but if the producer publishes
                  full records (publish, not delta_publish) it overwrites the fields edited by users.
  - "overrides" : (recommended) edits go to a separate SOW topic (OVERRIDES_TOPIC, same key). Deephaven subscribes
                  to both and natural_joins them. The market feed never overwrites what users edit.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import random
import threading
import time
from typing import Any, Callable

from deephaven import dtypes as dht
from deephaven import input_table, new_table
from deephaven.column import bool_col, double_col, long_col, string_col
from deephaven.constants import NULL_DOUBLE, NULL_LONG
from deephaven.execution_context import get_exec_ctx
from deephaven.stream.table_publisher import TablePublisher, table_publisher
from deephaven.table import Table
from deephaven.table_listener import listen

log = logging.getLogger("amps_grid")
logging.basicConfig(level=logging.INFO)

# --------------------------------------------------------------------------------------
# CONFIG (environment variables)
# --------------------------------------------------------------------------------------
FEED_MODE = os.environ.get("FEED_MODE", "mock")  # "amps" | "mock"
EDIT_MODE = os.environ.get("EDIT_MODE", "overrides")  # "overrides" | "delta"
AMPS_URI = os.environ.get("AMPS_URI", "tcp://localhost:9007/amps/json")
AMPS_TOPIC = os.environ.get("AMPS_TOPIC", "market_data")
OVERRIDES_TOPIC = os.environ.get("OVERRIDES_TOPIC", "market_data_overrides")
AMPS_FILTER = os.environ.get("AMPS_FILTER") or None  # e.g. "/Region = 'US'"
AMPS_CLIENT_NAME = os.environ.get("AMPS_CLIENT_NAME", f"dh-poc-{os.getpid()}")
AMPS_BATCH_SIZE = int(os.environ.get("AMPS_BATCH_SIZE", "500"))
MOCK_ROWS = int(os.environ.get("MOCK_ROWS", "100000"))
MOCK_UPDATES_PER_SEC = int(os.environ.get("MOCK_UPDATES_PER_SEC", "20000"))

KEY_COL = "Symbol"
# AMPS JSON field -> Deephaven type. Adjust to the real message (names must be valid column names).
MARKET_SCHEMA: dict[str, dht.DType] = {
    "Symbol": dht.string,
    "Bid": dht.double,
    "Ask": dht.double,
    "Last": dht.double,
    "PrevClose": dht.double,
    "Volume": dht.int64,
}
EDIT_SCHEMA: dict[str, dht.DType] = {  # columns users can edit
    "Trader": dht.string,
    "Note": dht.string,
    "TargetPx": dht.double,
}
EDITABLE_COLS = list(EDIT_SCHEMA)
DELETED_COL = "IsDeleted"  # tombstone for OOF / sow_delete

_BUILDERS: dict[dht.DType, Callable[[str, list], Any]] = {
    dht.string: string_col,
    dht.double: double_col,
    dht.int64: long_col,
    dht.bool_: bool_col,
}


# --------------------------------------------------------------------------------------
# SOW mirror: thread-safe buffer -> table_publisher (blink) -> last_by(key) -> where(!IsDeleted)
# --------------------------------------------------------------------------------------
class SowMirror:
    def __init__(self, name: str, schema: dict[str, dht.DType], key_col: str):
        self.schema = schema
        self.key_col = key_col
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        self.keys: set = set()  # live keys (to validate edits without querying the engine)
        self.blink, self.publisher = table_publisher(
            name=name,
            col_defs={**schema, DELETED_COL: dht.bool_},
            on_flush_callback=self._on_flush,  # runs at the start of every Update Graph cycle
            on_shutdown_callback=lambda: log.warning("table_publisher %s shut down", name),
        )
        # last_by on a blink table aggregates over the WHOLE observed stream (special blink semantics)
        self.state = self.blink.last_by(key_col)
        self.live = self.state.where(f"!{DELETED_COL}").drop_columns(DELETED_COL)

    # Called from external threads (AMPS): O(1), never touch the engine.
    def upsert(self, rec: dict) -> None:
        rec[DELETED_COL] = False
        with self._lock:
            self._buf.append(rec)
            self.keys.add(rec.get(self.key_col))

    def delete(self, key: Any) -> None:
        with self._lock:
            self._buf.append({self.key_col: key, DELETED_COL: True})
            self.keys.discard(key)

    def _on_flush(self, tp: TablePublisher) -> None:
        with self._lock:
            batch, self._buf = self._buf, []
        if batch:
            tp.add(self._to_table(batch))

    def _to_table(self, batch: list[dict]) -> Table:
        cols = [_BUILDERS[t](n, [r.get(n) for r in batch]) for n, t in self.schema.items()]
        cols.append(bool_col(DELETED_COL, [r[DELETED_COL] for r in batch]))
        return new_table(cols)


# --------------------------------------------------------------------------------------
# AMPS
# --------------------------------------------------------------------------------------
class AmpsConnection:
    def __init__(self):
        import AMPS  # pip install amps-python-client

        self.AMPS = AMPS
        self.client = AMPS.HAClient(AMPS_CLIENT_NAME)
        chooser = AMPS.DefaultServerChooser()
        chooser.add(AMPS_URI)
        self.client.set_server_chooser(chooser)
        self.client.set_failed_write_handler(
            lambda msg, reason: log.error("AMPS failed write: %s %s", reason, msg.get_data() if msg else "")
        )

    def start(self) -> None:
        self.client.connect_and_logon()

    def subscribe(self, topic: str, mirror: SowMirror, filter_: str | None = None) -> None:
        t0 = time.time()

        def on_message(m) -> None:  # AMPS receive thread
            try:
                cmd = m.get_command()
                if cmd in ("sow", "p", "publish"):
                    mirror.upsert(json.loads(m.get_data()))
                elif cmd == "oof":  # record left the result set (deleted, expired, no longer matches the filter)
                    data = m.get_data()
                    key = json.loads(data).get(mirror.key_col) if data else None
                    if key is not None:
                        mirror.delete(key)
                elif cmd == "group_end":
                    log.info("SOW %s loaded in %.2fs", topic, time.time() - t0)
            except Exception:
                log.exception("error processing AMPS message on %s", topic)

        self.client.sow_and_subscribe(
            on_message, topic, filter=filter_, batch_size=AMPS_BATCH_SIZE, oof_enabled=True, timeout=0
        )
        log.info("sow_and_subscribe placed on %s", topic)

    def publish_edits(self, rows: list[dict]) -> None:
        for row in rows:
            payload = {k: v for k, v in row.items() if v is not None}
            if EDIT_MODE == "overrides":
                self.client.publish(OVERRIDES_TOPIC, json.dumps(payload))
            else:
                self.client.delta_publish(AMPS_TOPIC, json.dumps(payload))

    def delete_keys(self, keys: list) -> None:
        topic = OVERRIDES_TOPIC if EDIT_MODE == "overrides" else AMPS_TOPIC
        for k in keys:
            escaped = str(k).replace("'", "\\'")
            self.client.sow_delete(lambda _m: None, topic, f"/{KEY_COL} = '{escaped}'")


class MockAmps:
    """Simulates AMPS: initial SOW of MOCK_ROWS + random updates; edits come back the way AMPS would echo them."""

    def __init__(self):
        self.symbols = [f"SYM{i:06d}" for i in range(MOCK_ROWS)]
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.market: SowMirror | None = None
        self.overrides: SowMirror | None = None

    def start(self) -> None:
        pass

    def subscribe(self, topic: str, mirror: SowMirror, filter_: str | None = None) -> None:
        if topic == AMPS_TOPIC:
            self.market = mirror
            threading.Thread(target=self._run, name="mock-feed", daemon=True).start()
        else:
            self.overrides = mirror

    def _run(self) -> None:
        for s in self.symbols:
            px = random.uniform(10, 500)
            rec = {"Symbol": s, "Bid": px - 0.01, "Ask": px + 0.01, "Last": px,
                   "PrevClose": px * random.uniform(0.95, 1.05), "Volume": random.randint(0, 10_000_000)}
            self._state[s] = rec
            self.market.upsert(dict(rec))
        tick = 0.05
        per_tick = max(1, int(MOCK_UPDATES_PER_SEC * tick))
        while True:
            t = time.time()
            for s in random.sample(self.symbols, min(per_tick, len(self.symbols))):
                with self._lock:
                    rec = self._state[s]
                    rec["Last"] = max(0.01, rec["Last"] * random.uniform(0.999, 1.001))
                    rec["Bid"], rec["Ask"] = rec["Last"] - 0.01, rec["Last"] + 0.01
                    rec["Volume"] += random.randint(1, 1000)
                    out = dict(rec)
                self.market.upsert(out)
            time.sleep(max(0.0, tick - (time.time() - t)))

    def publish_edits(self, rows: list[dict]) -> None:
        for row in rows:
            if EDIT_MODE == "overrides":
                self.overrides.upsert({k: row.get(k) for k in [KEY_COL, *EDITABLE_COLS]})
            else:
                with self._lock:
                    rec = self._state.setdefault(row[KEY_COL], {KEY_COL: row[KEY_COL]})
                    rec.update({k: v for k, v in row.items() if v is not None})
                    out = dict(rec)
                self.market.upsert(out)

    def delete_keys(self, keys: list) -> None:
        for k in keys:
            (self.overrides if EDIT_MODE == "overrides" else self.market).delete(k)


# --------------------------------------------------------------------------------------
# 100% Python editing: grid -> native input table "edit_requests" -> listener -> AMPS
#
# 1) edit_requests = keyed input table (KEY + editable columns). It is the users' edit inbox.
# 2) The grid table gets edit_requests' "InputTable" attribute (with_attributes). For the web grid this makes it
#    editable: edit_requests' columns are editable, the rest read-only, and every edit is written into edit_requests
#    (never into the market data).
# 3) A Python listener on edit_requests publishes every added/modified row to AMPS (on a separate thread so the
#    Update Graph is never blocked). The value comes back through the AMPS subscription -> AMPS stays the source of
#    truth. No loops: edit_requests only receives human edits, never feed data.
# --------------------------------------------------------------------------------------
_NULLS = {dht.double: NULL_DOUBLE, dht.int64: NULL_LONG}


def _py(v, dtype):
    """Converts numpy values / Deephaven null sentinels to JSON-serializable Python types."""
    if v is None:
        return None
    if dtype in _NULLS and v == _NULLS[dtype]:
        return None
    if dtype == dht.double:
        return None if math.isnan(v) else float(v)
    if dtype == dht.int64:
        return int(v)
    if dtype == dht.string:
        return str(v)
    return v


class EditRouter:
    def __init__(self, amps, key_col: str, edit_schema: dict[str, dht.DType], valid_keys: set):
        self.amps = amps
        self.valid_keys = valid_keys  # only edits for keys that exist in the market SOW are accepted
        self.key_col = key_col
        self.schema = {key_col: dht.string, **edit_schema}
        self.table = input_table(col_defs=self.schema, key_cols=key_col)
        self._q: queue.Queue = queue.Queue()
        self._ctx = get_exec_ctx()  # our own threads need the execution context to operate on tables
        self._recent: dict = {}  # key -> (row, time): the grid can report the same edit twice -> dedupe for 2 s
        threading.Thread(target=self._worker, name="edit-router", daemon=True).start()
        # keep the handle: if it is garbage-collected the listener stops receiving updates
        self.handle = listen(self.table, self._on_update, on_error=lambda e: log.error("edit listener: %s", e))

    def attach_to(self, t: Table) -> Table:
        """Returns 't' editable in the grid: it shares edit_requests' InputTableUpdater."""
        updater = self.table.j_table.getAttribute("InputTable")
        return t.with_attributes({"InputTable": updater})

    def _keys(self, data: dict) -> list[str]:
        return [str(k) for k in (data or {}).get(self.key_col, [])]

    def _on_update(self, update, is_replay: bool) -> None:  # runs on the Update Graph: read fast and enqueue
        # CAREFUL: update.added()/modified() return numpy; null strings arrive as the text 'None' and null doubles as
        # NULL_DOUBLE. So only the KEYS are taken from them and the values are read with iter_dict(), which returns a
        # real None for strings (numeric nulls are cleaned with _py).
        changed = set(self._keys(update.added())) | set(self._keys(update.modified()))
        deletes = self._keys(update.removed())
        upserts = []
        if changed:
            for row in self.table.iter_dict():  # small table: only keys some user edited
                if row[self.key_col] in changed:
                    upserts.append({c: _py(row[c], t) for c, t in self.schema.items()})
        if upserts or deletes:
            self._q.put((upserts, deletes))

    def _dup(self, row: dict) -> bool:
        now = time.time()
        prev = self._recent.get(row[self.key_col])
        self._recent[row[self.key_col]] = (row, now)
        return prev is not None and prev[0] == row and now - prev[1] < 2.0

    def _worker(self) -> None:
        while True:
            upserts, deletes = self._q.get()
            try:
                with self._ctx:
                    # The grid lets users append new rows to an input table: reject keys that do not exist in AMPS
                    unknown = [r[self.key_col] for r in upserts if r[self.key_col] not in self.valid_keys]
                    if unknown:
                        log.warning("edit rejected, key not in the SOW: %s", unknown)
                        self.table.delete_async(new_table([string_col(self.key_col, unknown)]))
                    upserts = [r for r in upserts if r[self.key_col] in self.valid_keys and not self._dup(r)]
                    if upserts:
                        log.info("user edit -> AMPS: %s", upserts)
                        self.amps.publish_edits(upserts)
                    deletes = [k for k in deletes if k in self.valid_keys]
                    if deletes:
                        log.info("user delete -> AMPS: %s", deletes)
                        self.amps.delete_keys(deletes)
            except Exception:
                log.exception("error publishing edits to AMPS")


# --------------------------------------------------------------------------------------
# Build the table graph
# --------------------------------------------------------------------------------------
amps = AmpsConnection() if FEED_MODE == "amps" else MockAmps()

if EDIT_MODE == "overrides":
    _market_mirror = SowMirror("amps_market", MARKET_SCHEMA, KEY_COL)
    _overrides_mirror = SowMirror("amps_overrides", {KEY_COL: dht.string, **EDIT_SCHEMA}, KEY_COL)
    market = _market_mirror.live
    overrides = _overrides_mirror.live
    _joined = market.natural_join(overrides, on=KEY_COL, joins=EDITABLE_COLS)
else:
    _market_mirror = SowMirror("amps_market", {**MARKET_SCHEMA, **EDIT_SCHEMA}, KEY_COL)
    _overrides_mirror = None
    market = _market_mirror.live
    _joined = market

feed_blink = _market_mirror.blink
feed_stats = feed_blink.view([KEY_COL]).count_by("MessagesIngested")

_calc = _joined.update_view([
    "Spread = Ask - Bid",
    "Chg = Last - PrevClose",
    "ChgPct = PrevClose == 0 ? NULL_DOUBLE : (Last - PrevClose) / PrevClose",
])

# 1) make it editable (edit_requests' InputTable attribute)  2) format.
#    format_* is implemented with update_view, which keeps the InputTable attribute.
_router = EditRouter(amps, KEY_COL, EDIT_SCHEMA, _market_mirror.keys)
edit_requests = _router.table

grid = (
    _router.attach_to(_calc)
    .format_columns([
        # every branch of the ternary must have the same type (long): bgfga(...) / bg(NO_FORMATTING)
        "Chg = Chg > 0 ? bgfga(colorRGB(0,110,0)) : Chg < 0 ? bgfga(colorRGB(150,0,0)) : bg(NO_FORMATTING)",
        "ChgPct = Decimal(`0.00%`)",
        "Last = Decimal(`#,##0.00`)",
        "Volume = heatmap(Volume, 0, 10000000, colorRGB(255,255,255), colorRGB(255,140,0))",
        "TargetPx = !isNull(TargetPx) && Last >= TargetPx ? bgfga(colorRGB(0,90,200)) : bg(NO_FORMATTING)",
    ])
    .format_row_where("!isNull(Note) && Note.toUpperCase().contains(`WATCH`)", "bgfga(colorRGB(255,230,120))")
)


def _delete_action(data: dict) -> None:
    """'Delete in AMPS' context menu: uses the selection if any, otherwise the clicked row."""
    # 'selected_rows' exists in newer deephaven.ui versions; 0.42.x does not send it -> use the clicked row
    sel = data.get("selected_rows")
    keys = [r[KEY_COL] for r in sel.iter_dict(cols=[KEY_COL])] if sel is not None else []
    if not keys:
        v = (data.get("always_fetch_columns") or {}).get(KEY_COL)
        if isinstance(v, dict):  # deephaven.ui 0.42.x: {'value': ..., 'text': ..., 'type': ...}
            v = v.get("value")
        keys = [v] if v is not None else []
    if not keys:
        return
    log.info("context-menu delete -> AMPS: %s", keys)
    amps.delete_keys(keys)


try:
    from deephaven import ui

    grid_ui = ui.table(
        grid,
        frozen_columns=[KEY_COL],
        show_quick_filters=True,
        always_fetch_columns=[KEY_COL],
        column_groups=[
            {"name": "Market", "children": ["Bid", "Ask", "Last", "Spread"]},
            {"name": "UserInput", "children": EDITABLE_COLS, "color": "#3b6bda"},
        ],
        context_menu={"title": "Delete in AMPS", "action": _delete_action},
    )
    dashboard = ui.dashboard(
        ui.column(
            ui.panel(grid_ui, title="Blotter"),
            ui.row(ui.panel(feed_stats, title="Ingestion"), ui.panel(edit_requests, title="Edits")),
        )
    )
except ImportError:
    log.warning("deephaven.ui not installed; only 'grid' is exported")

amps.start()
amps.subscribe(AMPS_TOPIC, _market_mirror, AMPS_FILTER)
if _overrides_mirror is not None:
    amps.subscribe(OVERRIDES_TOPIC, _overrides_mirror)
log.info("amps_grid started (feed=%s, edit=%s)", FEED_MODE, EDIT_MODE)
```

Key points:
- The AMPS handler runs on the AMPS *receive thread*: it only parses JSON and enqueues. It never touches tables.
- `_on_flush` runs once per cycle and builds **one** typed table per batch. Keep it light: it blocks the cycle.
- `EditRouter._on_update` runs on the UG: it only reads keys/values and enqueues; publishing to AMPS happens on
  another thread inside `with self._ctx:` (execution context).
- Edit values are read with `iter_dict()` (not with `update.added()`, which turns null strings into the text
  `'None'`). Numeric nulls (`NULL_DOUBLE`, `NULL_LONG`) are cleaned by `_py`.
- `SowMirror.keys` lets us validate edits without querying the engine.
- AMPS field names that are not valid column names (spaces, starting with a digit) → map them in `upsert`.

### 7.3 `Dockerfile` and `docker-compose.yml` — [NOT VERIFIED: no Docker in the test environment]

```dockerfile
# POC image: Deephaven server (Python; includes the web UI and deephaven.ui) + AMPS client.
# 100% Python: no Java compilation, no npm. Pin the version (tested with 42.6).
# If the corporate registry mirrors ghcr.io, change the prefix.
ARG DH_IMAGE=ghcr.io/deephaven/server:latest
FROM ${DH_IMAGE}
RUN pip install --no-cache-dir amps-python-client==5.3.5.13
```

```yaml
services:
  deephaven:
    build: .
    ports:
      - "${DEEPHAVEN_PORT:-10000}:10000"
    volumes:
      - ./server:/data          # ./server/app.d -> /data/app.d
    environment:
      # JVM: heap, app mode, auth and latency (update graph cycle + push interval to clients)
      - START_OPTS=-Xmx8g -Ddeephaven.application.dir=/data/app.d -Dauthentication.psk=${DH_PSK:-change-me} -DPeriodicUpdateGraph.targetCycleDurationMillis=100 -Dbarrage.minUpdateInterval=100
      # POC
      - FEED_MODE=${FEED_MODE:-mock}            # mock | amps
      - EDIT_MODE=${EDIT_MODE:-overrides}       # overrides | delta
      - AMPS_URI=${AMPS_URI:-tcp://amps-host:9007/amps/json}
      - AMPS_TOPIC=${AMPS_TOPIC:-market_data}
      - OVERRIDES_TOPIC=${OVERRIDES_TOPIC:-market_data_overrides}
      - AMPS_FILTER=${AMPS_FILTER:-}
      - MOCK_ROWS=${MOCK_ROWS:-200000}
      - MOCK_UPDATES_PER_SEC=${MOCK_UPDATES_PER_SEC:-20000}
```

Start: `DH_PSK=my-key docker compose up --build` → `http://localhost:10000/ide` (asks for the PSK).

### 7.4 Without Docker (pip) — [VERIFIED]

```python
"""Starts Deephaven without Docker (pip). Requires JDK 17+ and JAVA_HOME (the server runs on the JVM,
but no Java is written or compiled).

    pip install deephaven-server deephaven-plugin-ui amps-python-client
    FEED_MODE=mock DH_PSK=my-key python scripts/run_local.py      # then open http://localhost:10000/ide
"""
import os
import time

from deephaven_server import Server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PSK = os.environ.get("DH_PSK", "change-me")

server = Server(
    port=int(os.environ.get("DEEPHAVEN_PORT", "10000")),
    jvm_args=[
        f"-Xmx{os.environ.get('DH_HEAP', '6g')}",
        f"-Dauthentication.psk={PSK}",
        f"-Ddeephaven.application.dir={ROOT}/server/app.d",
        "-DPeriodicUpdateGraph.targetCycleDurationMillis=100",
        "-Dbarrage.minUpdateInterval=100",
    ],
)
server.start()
print(f"Deephaven en http://localhost:{server.port}/ide  (PSK={PSK})", flush=True)
while True:
    time.sleep(3600)
```

### 7.5 Tests — [VERIFIED]

`tests/run_server.py` (test server with anonymous auth; never use it outside your local machine):
```python
import os, sys, time
from deephaven_server import Server
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
s = Server(port=10000, jvm_args=[
    "-Xmx3g",
    "-DAuthHandlers=io.deephaven.auth.AnonymousAuthenticationHandler",
    f"-Ddeephaven.application.dir={root}/server/app.d",
    "-DPeriodicUpdateGraph.targetCycleDurationMillis=200",
    "-Dbarrage.minUpdateInterval=200",
])
s.start()
print("SERVER STARTED", flush=True)
while True:
    time.sleep(3600)
```

`tests/test_client.py` (edits through the same RPC the web grid uses):
```python
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
```
Real output (200k rows, EDIT_MODE=overrides):
```
grid size: 200000 static: False
  Symbol   input-table metadata: {'deephaven:inputtable.isKey': 'true'}
  Trader   input-table metadata: {'deephaven:inputtable.isValue': 'true'}
  Note     input-table metadata: {'deephaven:inputtable.isValue': 'true'}
  TargetPx input-table metadata: {'deephaven:inputtable.isValue': 'true'}
  Last     input-table metadata: {}
before: {'Symbol': 'SYM000077', 'Last': 136.65, 'Note': None, 'Trader': None, 'TargetPx': None}
after edit (0.39s): {'Symbol': 'SYM000077', 'Last': 136.65, 'Note': 'watch this', 'Trader': 'mauricio', 'TargetPx': 123.45}
after delete: [{'Symbol': 'SYM000077', 'Note': None, 'Trader': None}]
ingest rate ~ 20000 msgs/s
```

`tests/load_probe.py`:
```python
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
```

### 7.6 `scripts/amps_load_publisher.py` — load for a development AMPS [NOT VERIFIED]

```python
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
```

---

## 8. Grid features

Two layers:
- **Server side (table):** `format_columns`, `format_row_where`, `layout_hints`, `where`, `sort`. Every view sees
  it (IDE, iframe, `ui.table`). Keeps editability. **[VERIFIED]**
- **`deephaven.ui` (`ui.table`):** UI state (quick filters, frozen columns, column groups, context menu,
  `format_`). **[VERIFIED]** that `ui.table` on top of the editable grid keeps inline editing.

### 8.1 Conditional column formatting — [VERIFIED]
```python
t = t.format_columns([
    "Chg = Chg > 0 ? bgfga(colorRGB(0,110,0)) : Chg < 0 ? bgfga(colorRGB(150,0,0)) : bg(NO_FORMATTING)",
    "ChgPct = Decimal(`0.00%`)",                     # number format
    "Volume = heatmap(Volume, 0, 10000000, colorRGB(255,255,255), colorRGB(255,140,0))",
    "Name = `#87CEFA`",                              # hex in backticks
])
t = t.format_column_where("TestGrade", "GPA < 3.0", "DEEP_GREEN")
```
- Colors: UPPERCASE names (`RED`, `GREEN`, `VIVID_YELLOW`, `NO_FORMATTING`...), `` `#RRGGBB` ``, `colorRGB(r,g,b[,a])`.
- `bg(c)` background · `fg(c)` text · `bgfg(bg, fg)` · **`bgfga(c)`** background + auto-contrast text ·
  `bgo/fgo` override selection highlight · `heatmap(col, min, max, cMin, cMax)` · `heatmapFg(...)`.
- Numbers: `Decimal(`pattern`)` (Java DecimalFormat: `$#,##0.00`, `0.00%`).
- Creates hidden columns `Col__TABLE_STYLE_FORMAT` / `Col__TABLE_NUMBER_FORMAT`: apply formatting **last**.

### 8.2 Conditional row formatting — [VERIFIED]
```python
t = t.format_row_where("!isNull(Note) && Note.toUpperCase().contains(`WATCH`)", "bgfga(colorRGB(255,230,120))")
t = t.format_columns("__ROWFORMATTED = GPA >= 3.5 ? bg(LIGHT_GREEN) : GPA >= 3 ? bg(LIGHT_BLUE) : bg(BRIGHT_YELLOW)")
```
Where they overlap, column formatting wins.

### 8.3 Formatting with `deephaven.ui` — [Docs; validate against 0.42.2]
```python
ui.table(grid, format_=[
    ui.TableFormat(background_color="blue", color="white"),                     # whole row
    ui.TableFormat(cols="Sym", if_="Sym = `DOG`", background_color="negative"),
    ui.TableFormat(cols=["Price"], value="$#,##0.00"),
    ui.TableFormat(cols="Price", mode=ui.TableDatabar(color=["negative", "positive"], axis="middle")),
    ui.TableFormat(cols="Size", background_color=ui.TableHeatmap(gradient="viridis")),
])
```
The last matching rule wins. `if_` is a boolean engine formula. No `cols` = row formatting.

### 8.4 Editing — [VERIFIED in Chrome]
- Double-click an editable cell (`Trader`, `Note`, `TargetPx`), type, press Enter. Non-editable columns and the key
  are read-only. Works with quick filters and sorting applied.
- Pasting ranges: several rows reach the listener.
- **The grid shows an area to append new rows** (it is an input table): if someone enters a key that does not exist,
  the router rejects it (log `edit rejected`) and removes it from `edit_requests`. The UI shows its own validations
  (e.g. "Key can't be empty") and Commit/Discard buttons for new rows.
- **Deleting:** use the "Delete in AMPS" context menu item. The built-in "Delete Selected Rows" item only deletes
  from `edit_requests` and only reaches AMPS if that key was edited during this server session.
- Business validation: do it in `EditRouter._worker` (reject and log / do not publish). Type errors are rejected by
  the grid when it converts the typed text to the column type.
- Alternative without inline editing: `context_menu` → `ui.dialog` with `ui.form`/`ui.text_field`/`ui.number_field`.

### 8.5 Filtering — [VERIFIED quick filter in Chrome]
- Server (fixed): `t.where("Last > 100")`, `` t.where(["Region = `US`", "Volume > 0"]) ``, `t.where_in(other, "Symbol")`.
- UI (per user, executed on the server): **Quick Filters** (Ctrl+F; `>=9000000`, `!=x`, `a,b`; on strings it is an
  exact match — `SYM00000` does not match `SYM000001`), **Advanced Filters** (funnel icon), **Search bar**,
  "Filter by value".
- `deephaven.ui`: `ui.table(t, show_quick_filters=True, quick_filters={"Sym": "CAT", "Price": ">=100"}, show_search=True)`.

### 8.6 Sorting — [VERIFIED in Chrome]
- Server: `t.sort("Symbol")`, `t.sort_descending("Volume")`, `t.sort(["A", "B"])`, `t.reverse()`.
- UI: header click (click again to reverse; Shift+click for multi-sort), column menu.
- `ui.table(sorts=["Category", ui.TableSort(column="Name", direction="DESC")], reverse=True)`.
- `last_by` after `reverse()` does not tick: sort *after* the `last_by`.

### 8.7 Layout and extras
- `t.layout_hints(front=[...], back=[...], freeze=[...], hide=[...], column_groups=[{"name": "Mkt", "children": [...], "color": "#3b6bda"}])`.
- `ui.table(frozen_columns=..., front_columns=..., back_columns=..., hidden_columns=..., column_groups=..., column_display_names={...}, density="compact")` — **[VERIFIED]** column groups and quick filters.
- Aggregation row: `ui.table(aggregations=[ui.TableAgg(agg="sum", cols=["Volume"])])` or Table Options.
- Rollups: `t.rollup(aggs=[agg.sum_("Volume")], by=["Sector"])` or Table Options → Rollup Rows.
- From the UI (Table Options, ☰ icon): Organize Columns, no-code Conditional Formatting, Custom Columns,
  Select Distinct, Download CSV.
- `ui.table` events: `on_row_press`, `on_cell_press`, `on_row_double_press`, `on_selection_change`, `context_menu`.
  **In 0.42.2** the `context_menu` data contains `value`, `text`, `column_name`, `is_column_header`,
  `is_row_header` and `always_fetch_columns` (each value is a dict `{'value', 'text', 'type', ...}`); it does **not**
  contain `selected_rows` (that appears in the online docs for newer versions).

---

## 9. Client side (no npm)

| Option | Effort | Use |
|---|---|---|
| **A. Web IDE** `http://host:10000/ide` → Panels → `grid`, `grid_ui`, `dashboard` | Zero | Validate and measure. |
| **B. `deephaven.ui` dashboard** (Python) | Low | The "app": layout, filters, forms, menus. **Recommended.** |
| **C. iframe** `http://host:10000/iframe/widget/?name=dashboard` | Low | Embed in an existing page (plain HTML, no npm). **[VERIFIED]** `grid` and `grid_ui` |
| D. `pydeephaven` (Python) | — | Tests, automation, measurements from outside (not a UI). |

### 9.B `deephaven.ui`
```python
from deephaven import ui

@ui.component
def blotter():
    sym, set_sym = ui.use_state("")
    t = grid.where(f"Symbol.startsWith(`{sym}`)") if sym else grid   # where keeps editability
    return ui.flex(
        ui.text_field(label="Symbol prefix", value=sym, on_change=set_sym),
        ui.table(t, frozen_columns=["Symbol"], show_quick_filters=True, always_fetch_columns=["Symbol"]),
        direction="column", flex_grow=1,
    )

app = ui.dashboard(ui.column(ui.panel(blotter(), title="Blotter"), ui.row(ui.panel(feed_stats, title="Ingestion"))))
```
Dashboard rules: a single child; `row`/`column`/`stack`/`panel`; sizes add up to 100. The `blotter` component is
**[NOT VERIFIED]** (the `dashboard` in the 7.2 code is exported without errors).

### 9.C iframe (plain HTML)
```html
<iframe src="http://host:10000/iframe/widget/?name=dashboard&authProvider=parent" width="100%" height="800"></iframe>
<script>
window.addEventListener("message", (e) => {
  const { id, message } = e.data ?? {};
  if (message === "io.deephaven.message.LoginOptions.request") {
    e.source.postMessage({ id, payload: {
      type: "io.deephaven.authentication.psk.PskAuthenticationHandler", token: "MY-PSK" } }, e.origin);
  }
});
</script>
```
Without `authProvider=parent` the iframe asks for the PSK with its own login screen. Configurable security headers:
`-Dhttp.add.header.X-Frame-Options.enabled`, `...Content-Security-Policy...`, `...Cross-Origin-Resource-Policy...`.

### 9.D `pydeephaven` (tests)
```python
from pydeephaven import Session            # pip install pydeephaven "bitstring<4.4"
s = Session(host="localhost", port=10000,
            auth_type="io.deephaven.authentication.psk.PskAuthenticationHandler", auth_token="MY-PSK")
t = s.open_table("grid"); t.size; t.where("Symbol = `IBM`").to_arrow().to_pylist()
s.run_script("print('hello from the server')")
```

---

## 10. Performance

### 10.1 Knobs

| Property (`START_OPTS`) | Default | POC | Effect |
|---|---|---|---|
| `PeriodicUpdateGraph.targetCycleDurationMillis` | 1000 | **100–200** | Engine latency |
| `barrage.minUpdateInterval` | 1000 | **100–200** | How often deltas are pushed to clients |
| `-Xmx` | — | 4–8g+ | Heap (recommended minimum 4 GB) |
| `PeriodicUpdateGraph.updateThreads` | -1 | — | Parallelism across tables (Python code is limited by the GIL) |
| `grpc.maxInboundMessageSize` | 100 MiB | — | Large messages |

Reducing load: `t.snapshot_when(time_table("PT1S"))`, fewer visible columns, `update_view` (lazy) instead of
`update`, avoid Python functions inside formulas (they cross Python↔Java per row).

### 10.2 Measured results (2 vCPU, 7 GB RAM, 200 ms cycle, simulated feed in Python)

| Scenario | Result |
|---|---|
| Initial SOW load, 1,000,000 rows | ~8 s |
| Sustained updates | 20,000 msg/s, process CPU 10–30 % |
| Memory | ~2 GB RSS (1M rows × 15 columns) |
| Edit → visible | 0.3–0.6 s |
| UI | rendering, scrolling, filtering and sorting smooth with 200k ticking rows (headless Chrome) |

### 10.3 How to measure
- Throughput: `feed_stats.MessagesIngested` vs the publisher's rate.
- Latency: put a producer timestamp in the message (`SendNanos`) and in DH
  `update_view("LatencyMs = (currentTimeNanos() - SendNanos) / 1e6")` [verify the function name in the IDE].
- Performance tables (`deephaven.perfmon`) and Barrage metrics (`SnapshotNanos`, `WriteNanos`, `PropagateNanos`).
- Browser: scrolling smoothness with 1M rows, 1/5/10 users.
- If Python cannot sustain the rate (GIL): `orjson` instead of `json`, less work per message, several
  `table_publisher`s + `merge`, a larger `batch_size` for the initial SOW, conflation on the AMPS side (ask the AMPS
  team whether subscriptions support conflation) [NOT VERIFIED].

---

## 11. Known issues and fixes

| # | Symptom | Cause | Fix | Source |
|---|---|---|---|---|
| 1 | Listener: null strings arrive as `'None'` (text) and null doubles as `-1.79e308` | `update.added()/modified()` convert to numpy | Use the update only for the keys; read values with `iter_dict()` and clean `NULL_DOUBLE`/`NULL_LONG` | **Verified** |
| 2 | `ExecutionContextRegistrationException` when using tables from your own thread | Missing execution context | `ctx = get_exec_ctx()` at startup and `with ctx:` in the thread | **Verified** |
| 3 | `KeyError: 'selected_rows'` in `context_menu` | deephaven.ui 0.42.2 does not send it (newer versions do) | `data.get("selected_rows")`; fall back to `always_fetch_columns[KEY]["value"]` | **Verified** |
| 4 | `TypeError: unhashable type: 'dict'` in `context_menu` | In 0.42.2 `always_fetch_columns[col]` is a dict | Use `["value"]` | **Verified** |
| 5 | Users append rows with made-up keys | The grid lets users append rows to input tables | Validate against live keys (`SowMirror.keys`) and remove from `edit_requests` | **Verified** |
| 6 | "Delete Selected Rows" does not delete in AMPS | It only deletes from `edit_requests` | "Delete in AMPS" context menu item | **Verified** |
| 7 | `pydeephaven.Session(...)` → `TypeError ... BitArray(1024)` | `bitstring` 5.x | `pip install "bitstring<4.4"` | **Verified** |
| 8 | `format_columns` → `Incompatible types in condition operation ... long ... Color` | Ternary mixes `bgfga(...)` (long) with `NO_FORMATTING` (Color) | Same type in every branch: `bg(NO_FORMATTING)` | **Verified** |
| 9 | Two `format_column_where` on the same column: only the last one applies | They share the hidden format column | One `format_columns` with a nested ternary | **Verified** |
| 10 | pydeephaven `InputTable.delete` → `AttributeError: key_cols` | Not filled in when created from a ticket | `it.key_cols = ["Symbol"]` | **Verified** |
| 11 | Grid is not editable | Missing `InputTable` attribute, or it was lost by a later aggregation | Attach with `with_attributes` after `last_by`/`natural_join` | **Verified** |
| 12 | Text quick filter finds nothing | On strings it is an exact match | Use the full value, `Advanced Filters` (contains/starts with) or the search bar | **Verified** |
| 13 | Blink table looks "empty" | By design it only holds the current cycle | `last_by`, `ring_table` or `blink_to_append_only` | Docs |
| 14 | `last_by` does not tick | Applied after `reverse()` | Do not reverse before; or use `first_by` | Docs (FAQ) |
| 15 | Rows deleted in AMPS stay in the grid | `last_by` does not remove keys | `IsDeleted` tombstone + `where` (OOF) | **Verified** |
| 16 | Null fields with `sow_and_delta_subscribe` | Partial delta messages + `last_by` replaces the whole row | `sow_and_subscribe`, or merge into a dict per key before enqueuing | Design |
| 17 | The feed overwrites edits | The producer publishes full records | `EDIT_MODE=overrides`, or have the producer use `delta_publish` | Design |
| 18 | Slow cycles / laggy UI | Heavy work in `on_flush_callback` or in listeners | Parse on the AMPS thread; listeners only enqueue | Docs |
| 19 | The listener stops firing | The `listen()` handle was garbage-collected | Keep the handle (attribute/global variable) | Docs |
| 20 | Tables stop ticking | Objects without a live reference | Keep them in global variables of the app mode script | Docs |
| 21 | `UpdateGraphConflictException` | Combining tables from different update graphs | Use the default UG | Docs (FAQ) |
| 22 | Ticking freezes on WSL2 | Hyper-V vs NTP clock fight | `sudo timedatectl set-ntp false` in WSL or tune `w32time` | Docs (FAQ) |
| 23 | `Received message larger than max` | 100 MB gRPC limit | Smaller messages or raise the limit | Docs (FAQ) |
| 24 | Cannot log into the IDE | Random PSK by default | `-Dauthentication.psk=...` (or look for "access through pre-shared key" in the log) | **Verified** |
| 25 | pip install on Windows | The docs require WSL2 | WSL2 or Docker | Docs |
| 26 | No access to ghcr.io / PyPI from the VM | Corporate network | Mirror in Artifactory; versions from section 2 | Environment |
| 27 | Dates as strings in JSON | JSON has no Instant | Epoch nanos `long` + `epochNanosToInstant(x)` or `parseInstant(str)` [verify] | Docs |
| 28 | The same UI edit is sometimes published twice | The grid can report one edit in two notifications | Dedupe identical (key, values) within 2 s (`EditRouter._dup`); publishes are idempotent anyway | **Verified** |

---

## 12. Work plan for the agent

1. **Scaffold** (section 6) by copying the code from section 7. *Acceptance:* the server starts and logs
   `amps_grid started (feed=mock, edit=overrides)`.
2. **Validate in mock mode:** open `/ide` → `grid_ui` and `dashboard`; run `tests/test_client.py`.
   *Acceptance:* isKey/isValue metadata, edit < 1 s, delete OK, `feed_stats` growing.
3. **Editing from the UI:** edit `Note` with the mouse, paste several cells, "Delete in AMPS", try a non-existent
   key. *Acceptance:* logs `user edit`, `context-menu delete`, `edit rejected`.
4. **Development AMPS** (`FEED_MODE=amps`, `AMPS_URI`, `AMPS_TOPIC`): adjust `MARKET_SCHEMA`, `EDIT_SCHEMA`,
   `KEY_COL`. *Acceptance:* `market.size` == number of SOW records; ticks visible; OOF removes rows.
5. **Edits → real AMPS:** overrides topic (or delta). *Acceptance:* round trip measured.
6. **Formatting** per business rules.
7. **Performance** with `amps_load_publisher.py`: rows 100k → 1M+, rate 5k → 20k → 50k msg/s, 1/5/10 users,
   100 vs 1000 ms cycle. *Acceptance:* results table and breaking point.
8. **Final UI** with `deephaven.ui` (dashboard) and, if needed, embedded via iframe.
9. **Report:** section 1 vs results, risks (section 11), next steps.

---

## 13. API cheat sheet (Python)

### 13.1 Deephaven server
```python
from deephaven import new_table, empty_table, time_table, input_table, ring_table, merge, agg
from deephaven import dtypes as dht          # string, double, int32, int64, bool_, Instant, JObject
from deephaven.column import string_col, double_col, int_col, long_col, bool_col, datetime_col
from deephaven.constants import NULL_DOUBLE, NULL_LONG, NULL_INT
from deephaven.stream.table_publisher import table_publisher, TablePublisher
from deephaven.stream import blink_to_append_only
from deephaven.table_listener import listen, merged_listen, TableListener
from deephaven.execution_context import get_exec_ctx
from deephaven import update_graph            # shared_lock(t), exclusive_lock(t)
from deephaven.table import Table

# table_publisher(name, col_defs, on_flush_callback=None, on_shutdown_callback=None, update_graph=None, chunk_size=2048)
blink, pub = table_publisher("feed", {"Sym": dht.string, "Px": dht.double}, on_flush_callback=lambda tp: ...)
pub.add(table); pub.is_alive(); pub.publish_failure(RuntimeError("..."))

t = new_table([string_col("Sym", ["A"]), double_col("Px", [1.0])])    # None -> null
it = input_table(col_defs={"K": dht.string, "V": dht.double}, key_cols="K")   # keyed (upsert by key)
it.add(t); it.add_async(t, on_success=None, on_error=None); it.delete(keys_t); it.delete_async(keys_t)
it.key_names; it.value_names

updater = it.j_table.getAttribute("InputTable")          # "lend" editability to another table
editable = other.with_attributes({"InputTable": updater})

t.where("Px > 10"); t.where_in(other, "Sym"); t.update_view(["C = A + B"]); t.update([...]); t.view([...])
t.drop_columns("X"); t.rename_columns("New = Old"); t.move_columns_up("Sym")
t.sort("Sym"); t.sort_descending("Px"); t.reverse(); t.head(10); t.tail(10); t.flatten()
t.last_by("Sym"); t.first_by("Sym"); t.count_by("N", by=["Sym"]); t.sum_by("Sym")
t.agg_by([agg.sum_("Vol"), agg.last("Px"), agg.count_("N")], by=["Sym"])
t.natural_join(right, on="Sym", joins=["A", "B"]); t.join(right, on="Sym", joins=[...])
t.snapshot_when(time_table("PT1S")); t.rollup(aggs=[agg.sum_("Vol")], by=["Sector"])
t.with_attributes({...}); t.with_keys("Sym"); t.remove_blink(); t.is_refreshing; t.size
t.format_columns([...]); t.format_column_where(col, cond, formula); t.format_row_where(cond, formula)
t.layout_hints(front=[...], back=[...], freeze=[...], hide=[...], column_groups=[...])
for row in t.iter_dict(): ...     # null strings -> None; numeric nulls -> NULL_* (sentinel)
h = listen(t, lambda update, is_replay: ..., on_error=lambda e: ...)   # keep h
# update.added() / modified() / removed() / modified_prev() -> dict col -> numpy (null strings = 'None'!)
ctx = get_exec_ctx()      # in the script; then in your own threads: with ctx: ...
```

### 13.2 Query language
- Strings in backticks (`` "Sym = `IBM`" ``); `in`: `` "Sym in `A`, `B`" ``; ternary; `&& || !`.
- Nulls: `isNull(x)`, `NULL_DOUBLE`, `NULL_LONG`; NaN ≠ null.
- Java string methods: `Sym.startsWith(`A`)`, `Note.toUpperCase().contains(`X`)`.
- Dates: `now()`, `parseInstant`, `epochNanosToInstant`.
- Python variables/functions from the script can be used in formulas (slower).

### 13.3 Server configuration
- Docker: `START_OPTS` (appends), `JAVA_OPTS` (replaces), volumes `/data` and `/cache`, port 10000,
  config `/opt/deephaven/config/deephaven.prop`; Python packages via `RUN pip install ...` in the Dockerfile.
- pip: `Server(port=10000, jvm_args=[...]).start()` (needs a JDK and `JAVA_HOME`).
- App mode: `-Ddeephaven.application.dir=/data/app.d`; `.app`: `type=script`, `scriptType=python`, `enabled=true`,
  `id`, `name`, `file_0=...`.
- Auth: PSK (default) `-Dauthentication.psk=...`; anonymous (local only)
  `-DAuthHandlers=io.deephaven.auth.AnonymousAuthenticationHandler`; also mTLS, username/password, Keycloak.
  Disable the console: `-Ddeephaven.console.disable=true`.

### 13.4 deephaven.ui
```python
from deephaven import ui
@ui.component
def comp():
    v, set_v = ui.use_state("x")                 # also use_memo, use_effect, use_callback...
    return ui.flex(ui.text_field(label="x", value=v, on_change=set_v), ui.table(table), direction="column")
ui.table(t, format_=[ui.TableFormat(...)], quick_filters={...}, show_quick_filters=True, sorts=[...],
         frozen_columns=[...], hidden_columns=[...], column_groups=[...], aggregations=[ui.TableAgg(...)],
         on_row_press=fn, on_cell_press=fn, always_fetch_columns=["Symbol"],
         context_menu={"title": "Action", "action": fn})   # fn(data): see 8.7 for the 0.42.2 format
ui.dashboard(ui.column(ui.panel(a, title="A"), ui.row(ui.panel(b, title="B"), ui.panel(c, title="C"))))
# also: ui.button(on_press=...), ui.dialog, ui.form, ui.number_field, ui.picker, ui.toast, ui.tabs
```

### 13.5 pydeephaven (tests)
```python
from pydeephaven import Session
from pydeephaven.table import InputTable
s = Session(host, port, auth_type="io.deephaven.authentication.psk.PskAuthenticationHandler", auth_token=psk)
s.tables; t = s.open_table("grid"); t.schema; t.size; t.to_arrow()
it = InputTable(s, ticket=t.ticket, schema=t.schema); it.key_cols = ["Symbol"]
it.add(s.import_table(pyarrow_table)); it.delete(s.import_table(pyarrow_keys))
s.run_script("python code to run on the server"); s.close()
```

### 13.6 AMPS Python client (`import AMPS`) — verified by introspecting 5.3.5.13
```python
c = AMPS.HAClient("unique-name")                     # or AMPS.Client(name); c.connect(uri); c.logon()
ch = AMPS.DefaultServerChooser(); ch.add("tcp://user:pass@host:9007/amps/json"); c.set_server_chooser(ch)
c.connect_and_logon()
# sow_and_subscribe(on_message, topic, filter=None, batch_size=1, oof_enabled=False, timeout=0, top_n=None, order_by=None, bookmark=None, options=None)
c.sow_and_subscribe(handler, "topic", filter="/Region = 'US'", batch_size=500, oof_enabled=True)
# handler(m): m.get_command() -> 'group_begin' | 'sow' | 'group_end' | 'p' (publish) | 'oof'
#             m.get_data(), m.get_sow_key(), m.get_reason(), m.get_timestamp(), m.get_topic()
c.sow_and_delta_subscribe(handler, topic, filter=None, batch_size=1, oof_enabled=False, send_empties=False, ...)
c.publish(topic, data_str, expiration=None, correlation_id=None, sow_key=None)    # non-blocking
c.delta_publish(topic, data_str)                                                  # field merge in the SOW
c.sow_delete(handler_or_none, topic, filter)       # with handler: async; without: returns the ack
c.sow_delete_by_keys(topic, "k1,k2"); c.sow_delete_by_data(topic, data)
c.set_failed_write_handler(lambda msg, reason: ...); c.add_connection_state_listener(lambda connected: ...)
c.publish_flush(); c.close()
cmd = AMPS.Command("sow_and_subscribe").set_topic(t).set_filter(f).set_options("oof").set_batch_size(500)
c.execute_async(cmd, handler)
```
Handlers run on the AMPS **receive thread**: never block. OOF = the record left the result set (deleted, expired,
or no longer matches the filter).

---

## 14. Questions to validate at work

1. AMPS message format (JSON? FIX/NVFIX/BFlat?) and field names/types → `MARKET_SCHEMA`.
2. SOW key (`/Symbol`, `/OrderId`, composite) and whether the topic supports `delta_publish`.
3. Does the producer publish full records or deltas? (determines `EDIT_MODE`).
4. Can we create an overrides SOW topic? Entitlements to publish from the Deephaven server.
5. AMPS auth (user/password in the URI, Kerberos, TLS → `tcps` transport).
6. Volume: rows in the SOW, peak msgs/s, concurrent users.
7. Docker on the VM? If not: pip (Linux or WSL2) with JDK 17+. Access to ghcr.io and PyPI (or mirrors).
8. Where will the final UI live? (Deephaven IDE, `deephaven.ui` dashboard, or iframe inside another app).
9. License: Deephaven Community License — confirm with legal before going beyond the POC.

Sources: code and docs from `github.com/deephaven/deephaven-core` (docs/python, engine, server, py/server),
`github.com/deephaven/deephaven-plugins` (deephaven.ui), `github.com/deephaven/deephaven-server-docker`,
deephaven.io/docs, PyPI `amps-python-client` 5.3.5.13 (compiled module docstrings).
