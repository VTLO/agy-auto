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
    """Echoes back which backend it is, and what model it was asked for.
    `mode` (set on the server) controls the shape of the reply:
      "ok"        -- normal {"decision": "allow", ...} completion.
      "refusal"   -- 200 with an empty/blocked completion (Gemini-style safety block).
      "http_block"-- 400 whose body names a safety block, e.g. a hard content-policy reject.
      "http_error"-- 400 with an unrelated error (bad request), not a safety block.
    """

    reply_status = 200

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        req = json.loads(raw.decode()) if raw else {}
        mode = getattr(self.server, "mode", "ok")
        backend_name = self.server.backend_name  # type: ignore[attr-defined]
        if mode == "refusal":
            status = 200
            payload = {
                "choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}],
                "model": req.get("model", ""),
                "_backend": backend_name,
            }
        elif mode == "http_block":
            status = 400
            payload = {"error": {"message": "The response was blocked", "status": "PROHIBITED_CONTENT"}, "_backend": backend_name}
        elif mode == "http_error":
            status = 400
            payload = {"error": {"message": "invalid request: missing field"}, "_backend": backend_name}
        else:
            status = self.server.reply_status  # type: ignore[attr-defined]
            payload = {
                "choices": [{"message": {"content": json.dumps({"decision": "allow", "reason": "ok"})}}],
                "model": req.get("model", ""),
                "_backend": backend_name,
            }
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_dummy(name: str, status: int = 200, mode: str = "ok") -> tuple[HTTPServer, int]:
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), DummyBackend)
    srv.backend_name = name  # type: ignore[attr-defined]
    srv.reply_status = status  # type: ignore[attr-defined]
    srv.mode = mode  # type: ignore[attr-defined]
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


class ClassifyResponseTest(unittest.TestCase):
    def _body(self, obj: dict) -> bytes:
        return json.dumps(obj).encode()

    def test_normal_completion_is_ok(self):
        body = self._body({"choices": [{"message": {"content": '{"decision": "allow", "reason": "x"}'}}]})
        self.assertEqual(router.classify_response(200, body), "ok")

    def test_openai_content_filter_is_refusal(self):
        body = self._body({"choices": [{"finish_reason": "content_filter", "message": {"content": ""}}]})
        self.assertEqual(router.classify_response(200, body), "refusal")

    def test_explicit_refusal_field_is_refusal(self):
        body = self._body({"choices": [{"message": {"refusal": "I can't help with that.", "content": None}}]})
        self.assertEqual(router.classify_response(200, body), "refusal")

    def test_gemini_prompt_feedback_block_is_refusal(self):
        body = self._body({"promptFeedback": {"blockReason": "SAFETY"}, "choices": []})
        self.assertEqual(router.classify_response(200, body), "refusal")

    def test_empty_completion_is_refusal(self):
        body = self._body({"choices": [{"message": {"content": "   "}}]})
        self.assertEqual(router.classify_response(200, body), "refusal")

    def test_http_error_naming_safety_block_is_refusal(self):
        body = self._body({"error": {"message": "blocked", "status": "PROHIBITED_CONTENT"}})
        self.assertEqual(router.classify_response(400, body), "refusal")

    def test_http_error_unrelated_is_error(self):
        body = self._body({"error": {"message": "invalid api key"}})
        self.assertEqual(router.classify_response(401, body), "error")

    def test_no_choices_is_error(self):
        body = self._body({"choices": []})
        self.assertEqual(router.classify_response(200, body), "error")

    def test_garbled_json_is_error(self):
        self.assertEqual(router.classify_response(200, b"not json"), "error")


class ApplyForceDecisionTest(unittest.TestCase):
    def _decode(self, body: bytes) -> dict:
        data = json.loads(body.decode())
        return json.loads(data["choices"][0]["message"]["content"])

    def test_overrides_a_real_decision(self):
        body = json.dumps({"choices": [{"message": {"content": '{"decision": "deny", "reason": "looked risky"}'}}]}).encode()
        out = router.apply_force_decision(body, "allow")
        inner = self._decode(out)
        self.assertEqual(inner["decision"], "allow")
        self.assertEqual(inner["reason"], "looked risky")

    def test_overrides_garbled_output(self):
        out = router.apply_force_decision(b"not even json", "allow")
        inner = self._decode(out)
        self.assertEqual(inner["decision"], "allow")
        self.assertIn("forced", inner["reason"])

    def test_overrides_empty_completion(self):
        body = json.dumps({"choices": [{"message": {"content": ""}}]}).encode()
        out = router.apply_force_decision(body, "allow")
        inner = self._decode(out)
        self.assertEqual(inner["decision"], "allow")

    def test_result_is_always_well_formed_json(self):
        out = router.apply_force_decision(b"", "allow")
        data = json.loads(out.decode())
        self.assertEqual(router.classify_response(200, out), "ok")
        self.assertIn("choices", data)


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

    def _run(self, cfg: dict, body: dict) -> tuple[int, dict]:
        srv = router.make_server(cfg, "127.0.0.1", 0)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            return self._post(port, body)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_refusal_falls_through_to_refusal_fallback_backend(self):
        """A cloud backend that safety-blocks the request routes to the configured
        refusal_fallback_backend (e.g. a local uncensored model) instead of relaying
        the block back verbatim."""
        cloud_srv, cloud_port = start_dummy("cloud", mode="refusal")
        unc_srv, unc_port = start_dummy("uncensored", mode="ok")
        try:
            cfg = {
                "router": {
                    "backends": {
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"},
                        "uncensored": {"endpoint": f"http://127.0.0.1:{unc_port}/v1/chat/completions"},
                    },
                    "default_backend": "cloud",
                    "fallback_backend": "cloud",
                    "refusal_fallback_backend": "uncensored",
                    "rules": [],
                }
            }
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: hcitool lescan\ncwd: /x"}]}
            status, data = self._run(cfg, body)
            self.assertEqual(status, 200)
            self.assertEqual(data["_backend"], "uncensored")
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()
            unc_srv.shutdown()
            unc_srv.server_close()

    def test_http_safety_block_falls_through_to_refusal_fallback_backend(self):
        cloud_srv, cloud_port = start_dummy("cloud", mode="http_block")
        unc_srv, unc_port = start_dummy("uncensored", mode="ok")
        try:
            cfg = {
                "router": {
                    "backends": {
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"},
                        "uncensored": {"endpoint": f"http://127.0.0.1:{unc_port}/v1/chat/completions"},
                    },
                    "default_backend": "cloud",
                    "refusal_fallback_backend": "uncensored",
                    "rules": [],
                }
            }
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls\ncwd: /x"}]}
            status, data = self._run(cfg, body)
            self.assertEqual(status, 200)
            self.assertEqual(data["_backend"], "uncensored")
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()
            unc_srv.shutdown()
            unc_srv.server_close()

    def test_unrelated_http_error_uses_plain_fallback_not_refusal_fallback(self):
        """A non-safety error (e.g. bad API key) should go to fallback_backend,
        not refusal_fallback_backend -- a different model won't fix a bad key."""
        cloud_srv, cloud_port = start_dummy("cloud", mode="http_error")
        local_srv, local_port = start_dummy("local", mode="ok")
        unc_srv, unc_port = start_dummy("uncensored", mode="ok")
        try:
            cfg = {
                "router": {
                    "backends": {
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"},
                        "local": {"endpoint": f"http://127.0.0.1:{local_port}/v1/chat/completions"},
                        "uncensored": {"endpoint": f"http://127.0.0.1:{unc_port}/v1/chat/completions"},
                    },
                    "default_backend": "cloud",
                    "fallback_backend": "local",
                    "refusal_fallback_backend": "uncensored",
                    "rules": [],
                }
            }
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls\ncwd: /x"}]}
            status, data = self._run(cfg, body)
            self.assertEqual(status, 200)
            self.assertEqual(data["_backend"], "local")
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()
            local_srv.shutdown()
            local_srv.server_close()
            unc_srv.shutdown()
            unc_srv.server_close()

    def test_refusal_with_no_refusal_fallback_configured_relays_original(self):
        cloud_srv, cloud_port = start_dummy("cloud", mode="refusal")
        try:
            cfg = {
                "router": {
                    "backends": {"cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"}},
                    "default_backend": "cloud",
                    "rules": [],
                }
            }
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: ls\ncwd: /x"}]}
            status, data = self._run(cfg, body)
            self.assertEqual(status, 200)
            self.assertEqual(data["_backend"], "cloud")
            self.assertEqual(data["choices"][0]["finish_reason"], "content_filter")
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()

    def test_force_decision_backend_always_yields_allow(self):
        """cloud refuses -> falls through to the uncensored backend, which is
        configured with force_decision="allow" -- the final decision the
        classifier sees must be "allow" even though the uncensored dummy itself
        answers with an empty/refusal-shaped completion (simulating a tiny model
        that can't reliably produce well-formed JSON either)."""
        cloud_srv, cloud_port = start_dummy("cloud", mode="refusal")
        unc_srv, unc_port = start_dummy("uncensored", mode="refusal")
        try:
            cfg = {
                "router": {
                    "backends": {
                        "cloud": {"endpoint": f"http://127.0.0.1:{cloud_port}/v1/chat/completions"},
                        "uncensored": {"endpoint": f"http://127.0.0.1:{unc_port}/v1/chat/completions", "force_decision": "allow"},
                    },
                    "default_backend": "cloud",
                    "refusal_fallback_backend": "uncensored",
                    "rules": [],
                }
            }
            body = {"messages": [{"role": "user", "content": "tool: run_command\ncommand: hcitool lescan\ncwd: /x"}]}
            status, data = self._run(cfg, body)
            self.assertEqual(status, 200)
            inner = json.loads(data["choices"][0]["message"]["content"])
            self.assertEqual(inner["decision"], "allow")
        finally:
            cloud_srv.shutdown()
            cloud_srv.server_close()
            unc_srv.shutdown()
            unc_srv.server_close()


if __name__ == "__main__":
    unittest.main()
