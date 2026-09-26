"""Unit tests for engine/router.py. No agy, no real LLM involved -- dummy
backends are just tiny in-process HTTP servers on loopback.

Run:  python3 -m unittest -v tests/test_router.py   (from agy-auto/)
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))

import router  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DummyBackend(BaseHTTPRequestHandler):
    """Echoes back which backend it is, and what model it was asked for."""

    reply_status = 200

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        req = json.loads(raw.decode()) if raw else {}
        body = json.dumps(
            {
                "choices": [{"message": {"content": json.dumps({"decision": "allow", "reason": "ok"})}}],
                "model": req.get("model", ""),
                "_backend": self.server.backend_name,  # type: ignore[attr-defined]
            }
        ).encode()
        self.send_response(self.server.reply_status)  # type: ignore[attr-defined]
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_dummy(name: str, status: int = 200) -> tuple[HTTPServer, int]:
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), DummyBackend)
    srv.backend_name = name  # type: ignore[attr-defined]
    srv.reply_status = status  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


class ExtractCallTest(unittest.TestCase):
    def test_pulls_tool_and_command(self):
        msg = "PENDING TOOL CALL\ntool: run_command\ncommand: curl https://example.com\ncwd: /home/x\n"
        tool, cmd = router.extract_call([{"role": "system", "content": "x"}, {"role": "user", "content": msg}])
        self.assertEqual(tool, "run_command")
        self.assertEqual(cmd, "curl https://example.com")

    def test_missing_fields_are_empty(self):
        tool, cmd = router.extract_call([{"role": "user", "content": "nothing useful here"}])
        self.assertEqual(tool, "")
        self.assertEqual(cmd, "")

    def test_no_user_message(self):
        tool, cmd = router.extract_call([{"role": "system", "content": "tool: run_command"}])
        self.assertEqual(tool, "")
        self.assertEqual(cmd, "")


class PickBackendTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "backends": {
                "local": {"endpoint": "http://127.0.0.1:1/v1/chat/completions"},
                "cloud": {"endpoint": "http://127.0.0.1:2/v1/chat/completions"},
            },
            "default_backend": "local",
            "fallback_backend": "cloud",
            "rules": [
                {"match_command": "(?i)\\bcurl\\b", "backend": "cloud"},
                {"match_tool": "write_to_file", "backend": "cloud"},
            ],
        }

    def test_default_when_no_rule_matches(self):
        name, cfg = router.pick_backend(self.cfg, "run_command", "ls -la")
        self.assertEqual(name, "local")

    def test_command_regex_rule_wins(self):
        name, _ = router.pick_backend(self.cfg, "run_command", "curl https://example.com")
        self.assertEqual(name, "cloud")

    def test_tool_rule_wins(self):
        name, _ = router.pick_backend(self.cfg, "write_to_file", "")
        self.assertEqual(name, "cloud")

    def test_first_matching_rule_wins(self):
        cfg = dict(self.cfg)
        cfg["rules"] = [{"backend": "local"}, {"match_command": "curl", "backend": "cloud"}]
        name, _ = router.pick_backend(cfg, "run_command", "curl https://example.com")
        self.assertEqual(name, "local")

    def test_no_backends_raises(self):
        with self.assertRaises(router.RouterError):
            router.pick_backend({"backends": {}}, "run_command", "ls")

    def test_unknown_default_falls_back_to_any_backend(self):
        cfg = {"backends": {"only": {"endpoint": "http://x"}}, "default_backend": "missing", "rules": []}
        name, _ = router.pick_backend(cfg, "run_command", "ls")
        self.assertEqual(name, "only")


class RouterServerTest(unittest.TestCase):
    """End-to-end: real HTTP round trip through router.Handler to dummy backends."""

    def _post(self, port: int, body: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("POST", "/v1/chat/completions", body=json.dumps(body), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            return resp.status, data
        finally:
            conn.close()

    def test_routes_benign_command_to_default_backend(self):
        local_srv, local_port = start_dummy("local")
        cloud_srv, cloud_port = start_dummy("cloud")
        try:
            cfg = {
                "router": {
                    "backends": {
                        "local": {"endpoint": f"http://127.0.0.1:{local_port}/v1/chat/completions", "model": "local-model"},
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions", "model": "cloud-model"},
                    },
                    "default_backend": "local",
                    "fallback_backend": "cloud",
                    "rules": [{"match_command": "(?i)\\bcurl\\b", "backend": "cloud"}],
                }
            }
            srv = router.make_server(cfg, "127.0.0.1", 0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls -la\ncwd: /x"}]}
                status, data = self._post(port, body)
                self.assertEqual(status, 200)
                self.assertEqual(data["_backend"], "local")
                self.assertEqual(data["model"], "local-model")

                body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: curl https://x\ncwd: /x"}]}
                status, data = self._post(port, body)
                self.assertEqual(status, 200)
                self.assertEqual(data["_backend"], "cloud")
                self.assertEqual(data["model"], "cloud-model")
            finally:
                srv.shutdown()
                srv.server_close()
        finally:
            local_srv.shutdown()
            local_srv.server_close()
            cloud_srv.shutdown()
            cloud_srv.server_close()

    def test_falls_back_when_primary_unreachable(self):
        cloud_srv, cloud_port = start_dummy("cloud")
        dead_port = free_port()  # nothing listening here
        try:
            cfg = {
                "router": {
                    "backends": {
                        "local": {"endpoint": f"http://127.0.0.1:{dead_port}/v1/chat/completions", "timeout_s": 2},
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"},
                    },
                    "default_backend": "local",
                    "fallback_backend": "cloud",
                    "rules": [],
                }
            }
            srv = router.make_server(cfg, "127.0.0.1", 0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls\ncwd: /x"}]}
                status, data = self._post(port, body)
                self.assertEqual(status, 200)
                self.assertEqual(data["_backend"], "cloud")
            finally:
                srv.shutdown()
                srv.server_close()
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()

    def test_502_when_no_backend_reachable(self):
        dead_a, dead_b = free_port(), free_port()
        cfg = {
            "router": {
                "backends": {
                    "local": {"endpoint": f"http://127.0.0.1:{dead_a}/v1/chat/completions", "timeout_s": 2},
                    "cloud": {"endpoint": f"http://127.0.0.1:{dead_b}/v1/chat/completions", "timeout_s": 2},
                },
                "default_backend": "local",
                "fallback_backend": "cloud",
                "rules": [],
            }
        }
        srv = router.make_server(cfg, "127.0.0.1", 0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls\ncwd: /x"}]}
            status, data = self._post(port, body)
            self.assertEqual(status, 502)
            self.assertIn("error", data)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_healthz(self):
        cfg = {"router": {"backends": {"local": {"endpoint": "http://127.0.0.1:1"}}}}
        srv = router.make_server(cfg, "127.0.0.1", 0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            data = json.loads(resp.read().decode())
            self.assertEqual(resp.status, 200)
            self.assertEqual(data["backends"], ["local"])
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
