"""Exercise the supervisor with real child processes and isolated fake services.

The real buyer HTTP workflow has separate integration tests. These deliberately
small stdlib services make crashes, readiness failures and signals reproducible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.run_app import ROOT, parse_args, require_free_port


FAKE_SERVICE = r'''
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

kind = sys.argv[1]
state = Path(os.environ["FAKE_STATE"])
def stop(signum, frame):
    (state / (kind + ".stopped")).write_text(str(signum))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
record = {
    "pid": os.getpid(), "cwd": os.getcwd(), "args": sys.argv[2:],
    "ui_mode": os.environ.get("BUYER_UI_MODE"),
    "api_url": os.environ.get("BUYER_API_URL"),
    "data_dir": os.environ.get("EKT_DATA_DIR"),
    "api_was_ready": (state / "api.ready").exists(),
}
(state / (kind + ".json")).write_text(json.dumps(record))
if os.environ.get("FAIL_AT_START") == kind:
    raise SystemExit(23)
if kind == "api" and os.environ.get("SPAWN_DESCENDANT"):
    child_code = """
import os, signal, time
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
def stop(signum, frame):
    (state / 'descendant.stopped').write_text(str(signum))
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(state / 'descendant.ready').write_text('ready')
while True: time.sleep(.1)
"""
    subprocess.Popen([sys.executable, "-c", child_code])
flag = "--port" if kind == "api" else "--server.port"
port = int(sys.argv[sys.argv.index(flag) + 1])
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if kind == "api" and os.environ.get("API_UNHEALTHY"):
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        body = b'{"status":"ok"}' if kind == "api" else b'ok'
        (state / (kind + ".ready")).write_text("ready")
        self.wfile.write(body)
    def log_message(self, *args):
        pass
with HTTPServer(("127.0.0.1", port), Handler) as server:
    server.serve_forever()
'''


class LauncherArgumentsTests(unittest.TestCase):
    def test_cli_then_ui_port_then_platform_port(self):
        with patch.dict(os.environ, {"PORT": "9000", "UI_PORT": "9001", "API_PORT": "9002"}):
            args = parse_args([])
            self.assertEqual((args.port, args.api_port), (9001, 9002))
            self.assertEqual(parse_args(["--port", "9003"]).port, 9003)
        with patch.dict(os.environ, {"PORT": "9000", "UI_PORT": ""}):
            self.assertEqual(parse_args([]).port, 9000)

    @unittest.skipUnless(os.name == "posix", "POSIX socket restart behavior")
    def test_recently_closed_connections_do_not_prevent_restart(self):
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            port = server.getsockname()[1]
            server.listen()
            with socket.create_connection(("127.0.0.1", port)):
                connection, _ = server.accept()
                connection.close()
        require_free_port("127.0.0.1", port)


@unittest.skipUnless(os.name == "posix", "POSIX process-group and signal lifecycle checks")
class LauncherProcessesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ekt-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "fake_service.py").write_text(FAKE_SERVICE)
        for module, kind in (("uvicorn", "api"), ("streamlit", "ui")):
            (self.directory / f"{module}.py").write_text(
                f"import sys\nsys.argv.insert(1, {kind!r})\nimport fake_service\n"
            )
        with socket.socket() as first, socket.socket() as second:
            first.bind(("127.0.0.1", 0))
            second.bind(("127.0.0.1", 0))
            self.api_port = first.getsockname()[1]
            self.ui_port = second.getsockname()[1]
        self.process = None
        self.log_path = self.directory / "launcher.log"
        self.log = self.log_path.open("w")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop_launcher)

    def start(self, **environment):
        env = dict(os.environ)
        env.update(
            PYTHONPATH=str(self.directory), FAKE_STATE=str(self.directory),
            EKT_DATA_DIR=str(self.directory / "custom-state"),
            BUYER_UI_MODE="mock", BUYER_API_URL="http://wrong-api.invalid",
        )
        env.update(environment)
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/run_app.py"), "--port", str(self.ui_port),
             "--api-port", str(self.api_port), "--startup-timeout", "1.5"],
            cwd=self.directory, env=env, stdout=self.log, stderr=subprocess.STDOUT,
        )

    def stop_launcher(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
                self.fail("Launcher did not cleanly stop")

    def wait_for(self, condition):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail("Condition timed out:\n" + self.log_path.read_text())

    def wait_ready(self):
        self.wait_for(lambda: "Application ready:" in self.log_path.read_text())

    def service(self, name):
        return json.loads((self.directory / f"{name}.json").read_text())

    def assert_child_gone(self, name):
        pid = self.service(name)["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_ready_uses_live_api_and_sigterm_reaps_both_process_groups(self):
        self.start(SPAWN_DESCENDANT="1")
        self.wait_ready()
        self.wait_for(lambda: (self.directory / "descendant.ready").exists())
        ui = self.service("ui")
        self.assertEqual(ui["ui_mode"], "http")
        self.assertEqual(ui["api_url"], f"http://127.0.0.1:{self.api_port}")
        self.assertEqual(ui["data_dir"], str(self.directory / "custom-state"))
        self.assertEqual(ui["cwd"], str(ROOT))
        self.assertTrue(ui["api_was_ready"])
        self.process.terminate()
        self.assertEqual(self.process.wait(timeout=12), 143)
        self.assert_child_gone("api")
        self.assert_child_gone("ui")
        self.assertTrue((self.directory / "descendant.stopped").exists())

    def test_api_failure_never_starts_ui(self):
        self.start(FAIL_AT_START="api")
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assertFalse((self.directory / "ui.json").exists())
        self.assertIn("API exited unexpectedly (code 23)", self.log_path.read_text())
        self.assert_child_gone("api")

    def test_unhealthy_api_times_out_and_is_reaped(self):
        self.start(API_UNHEALTHY="1")
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assertFalse((self.directory / "ui.json").exists())
        self.assertIn("did not become healthy", self.log_path.read_text())
        self.assert_child_gone("api")

    def test_ui_startup_failure_stops_api(self):
        self.start(FAIL_AT_START="ui")
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assert_child_gone("api")
        self.assert_child_gone("ui")

    def test_api_exit_after_ready_stops_ui(self):
        self.start()
        self.wait_ready()
        os.kill(self.service("api")["pid"], signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assert_child_gone("api")
        self.assert_child_gone("ui")

    def test_ui_exit_after_ready_stops_api(self):
        self.start()
        self.wait_ready()
        os.kill(self.service("ui")["pid"], signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=8), 1)
        self.assert_child_gone("api")
        self.assert_child_gone("ui")

    def test_sigint_during_api_startup_cleans_up_without_starting_ui(self):
        self.start(API_UNHEALTHY="1")
        self.wait_for(lambda: (self.directory / "api.json").exists())
        self.process.send_signal(signal.SIGINT)
        self.assertEqual(self.process.wait(timeout=8), 130)
        self.assertFalse((self.directory / "ui.json").exists())
        self.assert_child_gone("api")

    def test_occupied_api_port_fails_without_launching_children(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.api_port))
            occupied.listen()
            self.start()
            self.assertEqual(self.process.wait(timeout=8), 1)
        self.assertIn("Cannot bind", self.log_path.read_text())
        self.assertFalse((self.directory / "api.json").exists())


if __name__ == "__main__":
    unittest.main()
