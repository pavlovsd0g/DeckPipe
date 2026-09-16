from __future__ import annotations

import importlib
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"
SENTINEL_TOKEN = "desktop-contract-token-DO-NOT-LEAK"
CHILD_ENV_ALLOWLIST = [
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATH",
]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_json(path: Path):
    return json.loads(read_text(path))


def request_json(url: str, token: str | None = None):
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body)


class FakeParentHandle:
    def __init__(self) -> None:
        self.released = threading.Event()
        self.wait_called = False
        self.closed = False

    def wait_until_exit(self) -> None:
        self.wait_called = True
        self.released.wait(2)

    def close(self) -> None:
        self.closed = True


class DesktopContractTests(unittest.TestCase):
    maxDiff = None

    def test_config_loads_bundled_ui_with_strict_csp_and_headers(self) -> None:
        config = read_json(TAURI / "tauri.conf.json")

        self.assertEqual("../ui", config["build"]["frontendDist"])
        self.assertFalse(config["app"]["withGlobalTauri"])
        self.assertEqual([], config["app"]["windows"])

        csp = config["app"]["security"]["csp"]
        for directive in [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "connect-src http://127.0.0.1:* http://[::1]:*",
            "img-src 'self' data: https:",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]:
            self.assertIn(directive, csp)
        for forbidden in ["unsafe-inline", "unsafe-eval", "localhost"]:
            self.assertNotIn(forbidden, csp)
        self.assertNotRegex(csp, r"(^|[ ;])\*(?=$|[ ;])")

        headers = config["app"]["security"]["headers"]
        self.assertEqual("same-origin", headers["Cross-Origin-Opener-Policy"])
        self.assertEqual("same-origin", headers["Cross-Origin-Resource-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertNotIn("Cross-Origin-Embedder-Policy", headers)

    def test_capability_exposes_only_main_window_custom_commands(self) -> None:
        capability = read_json(TAURI / "capabilities" / "default.json")

        self.assertEqual(["main"], capability["windows"])
        self.assertNotIn("remote", capability)
        self.assertEqual(
            ["allow-auth-broker", "allow-backend-connection", "allow-open-donation"],
            capability["permissions"],
        )

        login_permission = read_text(TAURI / "permissions" / "auth-broker.toml")
        backend_permission = read_text(TAURI / "permissions" / "backend-connection.toml")
        self.assertIn('"auth_begin", "auth_status", "auth_cancel", "auth_logout"', login_permission)
        self.assertNotIn('auth_open_setup', login_permission)
        self.assertNotIn('service_login', login_permission)
        self.assertIn('commands = { allow = ["backend_connection"] }', backend_permission)

    def test_rust_main_owns_memory_only_connection_and_sidecar_lifecycle(self) -> None:
        rust = read_text(TAURI / "src" / "main.rs")

        self.assertIn('WebviewUrl::App("index.html".into())', rust)
        self.assertNotIn('WebviewUrl::External(url.parse().expect("url"))', rust)
        self.assertNotIn('unwrap_or(7100)', rust)
        self.assertIn("OsRng.fill_bytes(&mut token_bytes)", rust)
        self.assertIn("hex::encode(token_bytes)", rust)
        self.assertIn("BackendConnection", rust)
        self.assertIn("BackendConnectionState", rust)
        self.assertIn("Mutex<Option<BackendConnection>>", rust)
        self.assertIn("ManagedSidecar", rust)
        self.assertIn("CommandChild", rust)
        self.assertIn("State<'_, BackendConnectionState>", rust)
        self.assertRegex(rust, r"\.take\(\)\s*\.ok_or_else")
        self.assertIn('window.label() != "main"', rust)
        self.assertIn("DECKPIPE_API_TOKEN", rust)
        self.assertIn("DECKPIPE_PARENT_PID", rust)
        self.assertRegex(rust, r"\.env_clear\(\)")
        self.assertRegex(rust, r"\.args\(\[\"--host\",\s*\"127\.0\.0\.1\",\s*\"--port\",\s*\"0\"\]\)")
        self.assertIn("retain_sidecar_child", rust)
        self.assertIn("kill_sidecar", rust)
        self.assertLess(
            rust.index("tauri_plugin_single_instance::init"),
            rust.index("tauri_plugin_shell::init"),
        )
        self.assertRegex(rust, r"retain_sidecar_child\(&sidecar,\s*child\)\s*\{")
        self.assertRegex(rust, r"store_backend_connection\(&connection_state,\s*connection\)\s*\{")
        self.assertIn("STARTUP_TIMEOUT", rust)
        self.assertIn("parse_port_line", rust)
        self.assertIn("tauri_plugin_single_instance::init", rust)
        self.assertNotRegex(rust, r"println!\([^)]*token|eprintln!\([^)]*token", "token must not be logged")

    def test_browser_auth_does_not_return_cookie_or_build_embedded_login_window(self) -> None:
        rust = read_text(TAURI / "src" / "main.rs")

        self.assertNotIn('w.cookies()', rust)
        self.assertNotIn('WebviewUrl::External', rust)
        self.assertNotIn('service_login', rust)
        self.assertIn('auth_cancel', rust)
        self.assertIn('DECKPIPE_AUTH_BROKER_TOKEN', rust)
        self.assertIn('PublicStatus', rust)

    def test_backend_uses_single_prebound_listener_and_same_socket_for_uvicorn(self) -> None:
        run_backend = importlib.import_module("run_backend")
        listener = run_backend.create_bound_listener("127.0.0.1", 0)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competitor:
            with self.assertRaises(OSError):
                competitor.bind(("127.0.0.1", port))

        captured = {}

        def fake_run(self, sockets=None):
            captured["sockets"] = sockets

        with patch("uvicorn.Server.run", fake_run):
            run_backend.run_server_on_listener(listener, SENTINEL_TOKEN, parent_pid=os.getpid(), install_watchdog=False)

        self.assertEqual([listener], captured["sockets"])
        self.assertEqual("127.0.0.1", os.environ["DECKPIPE_BOUND_HOST"])
        self.assertEqual(str(port), os.environ["DECKPIPE_BOUND_PORT"])

        source = read_text(ROOT / "run_backend.py")
        self.assertNotIn("free_port", source)
        self.assertNotIn("SO_REUSEADDR", source)
        self.assertIn("SO_EXCLUSIVEADDRUSE", source)
        self.assertNotIn("uvicorn.run(", source)
        self.assertIn("wintypes.HANDLE", source)
        self.assertIn("OpenProcess.argtypes", source)
        self.assertIn("WaitForSingleObject.argtypes", source)
        self.assertIn("CloseHandle.argtypes", source)
        self.assertIn("access_log=False", source)
        self.assertIn("server_header=False", source)
        self.assertIn("date_header=False", source)
        self.assertIn("proxy_headers=False", source)

    def test_parent_watchdog_fails_closed_and_closes_parent_handle(self) -> None:
        run_backend = importlib.import_module("run_backend")

        invalid_server = SimpleNamespace(should_exit=False)
        self.assertIsNone(
            run_backend.start_parent_watchdog(
                invalid_server,
                -1,
                open_parent=lambda _pid: None,
            )
        )
        self.assertTrue(invalid_server.should_exit)

        server = SimpleNamespace(should_exit=False)
        handle = FakeParentHandle()
        thread = run_backend.start_parent_watchdog(
            server,
            1234,
            open_parent=lambda pid: handle if pid == 1234 else None,
        )
        self.assertIsNotNone(thread)
        handle.released.set()
        thread.join(2)

        self.assertTrue(handle.wait_called)
        self.assertTrue(handle.closed)
        self.assertTrue(server.should_exit)

    def test_entry_process_serves_with_synthetic_token_and_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as appdata, tempfile.TemporaryDirectory() as localappdata:
            env = {
                name: value
                for name in CHILD_ENV_ALLOWLIST
                if (value := os.environ.get(name))
            }
            env.update(
                {
                    "APPDATA": appdata,
                    "LOCALAPPDATA": localappdata,
                    "DECKPIPE_DATA_DIR": str(Path(appdata) / "DeckPipeTestData"),
                    "DECKPIPE_API_TOKEN": SENTINEL_TOKEN,
                    "DECKPIPE_PARENT_PID": str(os.getpid()),
                    "PYTHONPATH": str(ROOT),
                }
            )
            process = subprocess.Popen(
                [sys.executable, "run_backend.py", "--host", "127.0.0.1", "--port", "0"],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.time() + 15
                lines: list[str] = []
                stdout_lines: queue.Queue[str | None] = queue.Queue()

                def read_stdout_line() -> None:
                    assert process.stdout is not None
                    stdout_lines.put(process.stdout.readline() or None)

                reader = threading.Thread(target=read_stdout_line, daemon=True)
                reader.start()
                while time.time() < deadline:
                    try:
                        line = stdout_lines.get(timeout=0.1)
                    except queue.Empty:
                        line = None
                    if line:
                        lines.append(line.strip())
                        break
                    if process.poll() is not None:
                        break
                stderr_output = ""
                if len(lines) != 1 and process.poll() is not None:
                    stderr_output = process.stderr.read()
                self.assertEqual(1, len(lines), stderr_output)
                self.assertRegex(lines[0], r"^DECKPIPE_PORT=([1-9][0-9]{0,4})$")
                port = int(lines[0].split("=", 1)[1])
                self.assertLessEqual(port, 65535)
                base = f"http://127.0.0.1:{port}"

                status, payload = request_json(f"{base}/api/version")
                self.assertEqual(200, status)
                self.assertEqual(
                    {"version": "0.6.0", "build_id": "0.6.0+20260827.050713.6456dba254a6"},
                    payload,
                )

                status, payload = request_json(f"{base}/api/jobs")
                self.assertEqual(401, status)
                self.assertNotIn(SENTINEL_TOKEN, json.dumps(payload))

                status, payload = request_json(f"{base}/api/jobs", token=SENTINEL_TOKEN)
                self.assertEqual(200, status)
                self.assertEqual([], payload)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
            self.assertIsNotNone(process.returncode)

    def test_dev_launcher_uses_backend_entrypoint_parent_env_and_no_token_print(self) -> None:
        dev = read_text(ROOT / "dev.js")

        self.assertIn("run_backend.py", dev)
        self.assertIn("DECKPIPE_API_TOKEN", dev)
        self.assertIn("DECKPIPE_PARENT_PID", dev)
        self.assertIn("process.pid", dev)
        self.assertIn("CHILD_ENV_ALLOWLIST", dev)
        self.assertIn("pickChildEnv", dev)
        self.assertNotIn("...process.env", dev)
        self.assertNotIn('"-m", "uvicorn"', dev)
        self.assertNotRegex(dev, r"console\.(?:log|info|warn|error)\([^)]*token", "token must not be printed")

    def test_desktop_build_regenerates_frontend_before_tauri_build(self) -> None:
        package = read_json(ROOT / "desktop" / "package.json")
        build = package["scripts"]["build"]

        self.assertRegex(build, r"npm\s+--prefix\s+\.\.\s+run\s+build:frontend")
        self.assertRegex(build, r"&&\s*tauri build")
        self.assertLess(build.index("build:frontend"), build.index("tauri build"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
