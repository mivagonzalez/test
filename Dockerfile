# POC image: Deephaven server (Python; includes the web UI and deephaven.ui) + AMPS client.
# 100% Python: no Java compilation, no npm. Pin the version (tested with 42.6).
# If the corporate registry mirrors ghcr.io, change the prefix.
ARG DH_IMAGE=ghcr.io/deephaven/server:latest
FROM ${DH_IMAGE}
RUN pip install --no-cache-dir amps-python-client==5.3.5.13
