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
