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
