"""Real daemon wire boundary with a recording resize recipient, without a model."""

import json
import signal
import sys
from pathlib import Path

from rodex import daemon
from rodex.daemon_client import daemon_socket_path
from rodex.implementation_identity import RODEX_IMPLEMENTATION_ID


class RecordingRuntimeManager:
    def __init__(self, root):
        self.events = root / f"{RODEX_IMPLEMENTATION_ID}.events"

    def wake_runtime(self, runtime_id, cause):
        with self.events.open("a") as events:
            events.write(json.dumps([runtime_id, cause]) + "\n")

    def close(self):
        pass


if __name__ == "__main__":
    root = Path(sys.argv[1])
    daemon.DaemonRuntimeManager = RecordingRuntimeManager
    server = daemon.RodexDaemonServer(root)
    signal.signal(signal.SIGTERM, lambda *_: server.stop())
    print(json.dumps({"socket": str(daemon_socket_path(root)), "identity": RODEX_IMPLEMENTATION_ID}), flush=True)
    server.run()
