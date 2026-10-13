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
