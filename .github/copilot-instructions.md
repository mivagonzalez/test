# Copilot instructions — Deephaven + AMPS POC

- Before writing any code, read `docs/DEEPHAVEN_AMPS_GRID_CONTEXT.md`: it is the only source of truth for the
  Deephaven and AMPS APIs (the official documentation is not reachable from this VM).
- **Everything in Python.** Do not write Java or JavaScript; do not add npm dependencies. The web UI is served by the
  Deephaven server and the app is built with `deephaven.ui` (Python).
- Do not invent API signatures. If something is not in that document, leave `TODO(verify API)` and explain what is
  missing.
- Follow the architecture in section 4 (table_publisher -> last_by -> with_attributes(InputTable) -> format) and the
  plan in section 12. Before marking a step as done, run `tests/test_client.py`.
- On any error, check the known-issues table (section 11) before improvising a workaround.
