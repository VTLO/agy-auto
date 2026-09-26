"""agy-auto backend router.

A tiny local OpenAI-compatible endpoint (`POST /v1/chat/completions`) that sits in
front of one or more real classifier backends. Point `[classifier].endpoint` in
policy.toml at it (e.g. "http://127.0.0.1:8080/v1/chat/completions") and it will:

  1. Look at the pending tool call embedded in the request that engine/classifier.py
     sends (see classifier.build_user_message: "tool: ...", "command: ...").
  2. Match it against `[[router.rules]]` in the merged policy to pick a named
     backend from `[router.backends.*]` -- e.g. send anything that touches
     network/cloud/deploy tools to a stronger cloud model, everything else to a
     fast local model.
  3. Forward the *same* chat-completions request body to that backend (only the
     "model" field is overridden, from the backend's own config) and relay its
     response back verbatim.
  4. If the chosen backend is unreachable or times out, retry once against
     `router.fallback_backend` before failing.

This module makes no allow/deny decision itself -- engine/policy.py's Engine still
owns that. It only decides which model answers the classifier's question this
time. Standard library only, matching the rest of agy-auto.

Run standalone:  python3 engine/router.py [--host H] [--port P] [--policy FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(ENGINE_DIR)
sys.path.insert(0, ENGINE_DIR)

from main import load_policy  # noqa: E402

TOOL_RE = re.compile(r"^tool:\s*(\S+)", re.M)
COMMAND_RE = re.compile(r"^command:\s*(.*)$", re.M)


class RouterError(Exception):
    pass


def extract_call(messages: list[dict]) -> tuple[str, str]:
    """Best-effort pull of the tool name and command line out of the classifier's
    own user message (built by classifier.build_user_message). Never raises --
    an unparseable message just yields ("", "") and falls through to the default
    backend."""
    text = ""
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            text = m.get("content") or ""
            break
    tool_m = TOOL_RE.search(text)
    cmd_m = COMMAND_RE.search(text)
    return (tool_m.group(1) if tool_m else ""), (cmd_m.group(1).strip() if cmd_m else "")


def pick_backend(router_cfg: dict, tool: str, command: str) -> tuple[str, dict]:
    """First matching [[router.rules]] entry wins; each rule may set match_tool
    (exact tool name) and/or match_command (regex over "tool command"). A rule
    with neither field always matches. Falls back to default_backend, then to
    whatever backend happens to be configured."""
    backends = router_cfg.get("backends", {})
    if not backends:
        raise RouterError("no [router.backends] configured")
    haystack = f"{tool} {command}"
    for rule in router_cfg.get("rules", []):
        mt = rule.get("match_tool")
        if mt and mt != tool:
            continue
        mc = rule.get("match_command")
        if mc and not re.search(mc, haystack, re.I):
            continue
        name = rule.get("backend")
        if name in backends:
            return name, backends[name]
    default = router_cfg.get("default_backend")
    if default in backends:
        return default, backends[default]
    name = next(iter(backends))
    return name, backends[name]


def forward(backend_cfg: dict, body: dict) -> tuple[int, bytes]:
    """Forwards `body` (an OpenAI chat-completions request) to `backend_cfg`,
    overriding only the model / auth / any chat_template_kwargs the backend
    declares. Returns (http_status, raw_response_body). Raises on network
    failure, same exception shapes as urllib."""
    endpoint = backend_cfg.get("endpoint") or ""
    if not endpoint:
        raise RouterError("backend has no endpoint configured")
    out = dict(body)
    if backend_cfg.get("model"):
        out["model"] = backend_cfg["model"]
    if "chat_template_kwargs" in backend_cfg:
        out["chat_template_kwargs"] = backend_cfg["chat_template_kwargs"]
    headers = {"Content-Type": "application/json"}
    api_key = backend_cfg.get("api_key")
    if not api_key:
        key_env = backend_cfg.get("api_key_env")
        if key_env and os.environ.get(key_env):
            api_key = os.environ[key_env]
    if api_key:
        headers["Authorization"] = "Bearer " + str(api_key).strip()
    timeout = float(backend_cfg.get("timeout_s", 20))
    req = urllib.request.Request(endpoint, data=json.dumps(out).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # An HTTP error from the backend is still a response; relay it rather
        # than treating it as unreachable (no fallback for e.g. a 400).
        return e.code, e.read() if e.fp else b'{"error": "backend error"}'


class Handler(BaseHTTPRequestHandler):
    server_version = "agy-auto-router/1"

    def log_message(self, fmt, *args):  # route the default access log through stderr
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status: int, payload) -> None:
        body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", "/health", ""):
            router_cfg = self.server.cfg.get("router", {})  # type: ignore[attr-defined]
            self._send_json(200, {"status": "ok", "backends": list(router_cfg.get("backends", {}))})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        router_cfg = self.server.cfg.get("router", {})  # type: ignore[attr-defined]
        tool, command = extract_call(body.get("messages") or [])
        try:
            name, backend_cfg = pick_backend(router_cfg, tool, command)
        except RouterError as e:
            self._send_json(500, {"error": str(e)})
            return
        t0 = time.time()
        try:
            status, resp_body = forward(backend_cfg, body)
            self._send_json(status, resp_body)
            self._log_route(name, tool, command, time.time() - t0, ok=True)
            return
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, RouterError) as e:
            fallback_name = router_cfg.get("fallback_backend")
            backends = router_cfg.get("backends", {})
            if fallback_name and fallback_name in backends and fallback_name != name:
                self._log_route(name, tool, command, time.time() - t0, ok=False, note=f"{e}; trying fallback {fallback_name}")
                t1 = time.time()
                try:
                    status, resp_body = forward(backends[fallback_name], body)
                    self._send_json(status, resp_body)
                    self._log_route(fallback_name, tool, command, time.time() - t1, ok=True, note=f"fallback from {name}")
                    return
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, RouterError) as e2:
                    self._log_route(fallback_name, tool, command, time.time() - t1, ok=False, note=str(e2))
                    self._send_json(502, {"error": f"all backends unreachable: {name}: {e}; {fallback_name}: {e2}"})
                    return
            self._log_route(name, tool, command, time.time() - t0, ok=False, note=str(e))
            self._send_json(502, {"error": f"backend {name} unreachable: {e}"})

    def _log_route(self, backend: str, tool: str, command: str, dt: float, ok: bool, note: str = "") -> None:
        sys.stderr.write(
            f"agy-auto-router: {'OK' if ok else 'FAIL'} backend={backend} tool={tool!r} "
            f"cmd={command[:80]!r} {dt * 1000:.0f}ms {note}\n"
        )


def make_server(cfg: dict, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.cfg = cfg  # type: ignore[attr-defined]
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="overrides [router].listen_host (default 127.0.0.1)")
    ap.add_argument("--port", type=int, help="overrides [router].listen_port (default 8080)")
    ap.add_argument("--policy", help="extra policy.toml overlay (same effect as AGY_AUTO_POLICY)")
    ns = ap.parse_args(argv)
    if ns.policy:
        os.environ["AGY_AUTO_POLICY"] = ns.policy
    cfg, version, sources = load_policy([])
    router_cfg = cfg.get("router", {})
    if not router_cfg.get("backends"):
        sys.stderr.write(
            "agy-auto-router: no [router.backends] configured in policy (see policy/default.toml [router]); "
            "nothing to route to.\n"
        )
        return 1
    host = ns.host or router_cfg.get("listen_host", "127.0.0.1")
    port = ns.port or int(router_cfg.get("listen_port", 8080))
    httpd = make_server(cfg, host, port)
    sys.stderr.write(
        f"agy-auto-router: listening on http://{host}:{port}/v1/chat/completions "
        f"(policy {version}, sources: {', '.join(sources)})\n"
    )
    sys.stderr.write("agy-auto-router: backends: " + ", ".join(router_cfg["backends"].keys()) + "\n")
    sys.stderr.write(
        f"agy-auto-router: default_backend={router_cfg.get('default_backend')!r} "
        f"fallback_backend={router_cfg.get('fallback_backend')!r} "
        f"rules={len(router_cfg.get('rules', []))}\n"
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
