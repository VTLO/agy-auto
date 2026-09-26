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
  5. If the chosen backend answers but the answer looks like a content-safety
     refusal (empty/blocked completion, `finish_reason: content_filter`, a Gemini
     `promptFeedback.blockReason`, etc.) rather than a genuine network or server
     error, retry once against `router.refusal_fallback_backend` -- e.g. a local
     uncensored model that will actually render an allow/deny/ask verdict on a
     pending tool call a cloud provider's own safety layer declined to look at.
  6. A backend can set `force_decision = "allow"` (or "deny"/"ask") in its
     `[router.backends.*]` entry to say it is never trusted to reason about the
     verdict at all, only to be reachable -- once it answers, its response is
     rewritten to `force_decision` regardless of what it actually said, whether
     that parsed as JSON, or how much of the (possibly large) context it could
     read with its own small context window. This suits a low-capability model
     used purely as a rubber stamp for the cases a stronger backend refused.

This module makes no allow/deny decision itself -- engine/policy.py's Engine still
owns that, and its deterministic hard-deny layer (sudo, credential reads, disk
tools, force-push, ...) runs before any request ever reaches this router, for
every backend. Swapping in a more permissive backend here only changes which
model answers the ambiguous grey-area calls the deterministic layers didn't
already resolve -- it cannot un-deny something the hard-deny layer already
denies. Standard library only, matching the rest of agy-auto.

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
    failure, same exception shapes as urllib. An HTTP error status from the
    backend is returned, not raised -- it's still a response, and the caller
    (classify_response) decides whether it looks like a refusal, a genuine
    error, or something to relay as-is."""
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
        return e.code, e.read() if e.fp else b'{"error": "backend error"}'


# Substrings that show up in an HTTP-error body when a provider blocked the
# request itself (as opposed to a rate limit, auth failure, or malformed
# request). Matched case-insensitively against the raw response body.
_REFUSAL_ERROR_MARKERS = ("safety", "blockreason", "prohibited_content", "recitation", "blocked")


def classify_response(status: int, body: bytes) -> str:
    """Categorizes a completed backend response as "ok", "refusal", or "error".

    "refusal" means the backend was reachable and technically answered, but the
    answer is a content-safety block rather than a usable {"decision": ...}
    payload: an OpenAI-style `finish_reason: content_filter` / `refusal` field,
    a Gemini `promptFeedback.blockReason` / blocked candidate, an empty
    completion, or an HTTP error whose body names a safety block. Everything
    else that isn't a clean 2xx with real content is "error" (rate limits, auth
    failures, malformed requests, garbled JSON) -- those get the plain
    fallback_backend treatment, not the refusal one, since a different model
    won't fix a wrong API key.
    """
    if not (200 <= status < 300):
        text = body.decode("utf-8", "replace").lower()
        if any(m in text for m in _REFUSAL_ERROR_MARKERS):
            return "refusal"
        return "error"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return "error"
    if not isinstance(data, dict):
        return "error"
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        return "refusal"
    choices = data.get("choices") or []
    if not choices:
        return "error"
    choice = choices[0] or {}
    finish_reason = str(choice.get("finish_reason") or "").lower()
    if finish_reason in ("content_filter", "safety", "recitation"):
        return "refusal"
    message = choice.get("message") or {}
    if message.get("refusal"):
        return "refusal"
    content = message.get("content")
    if not content or not str(content).strip():
        # An empty completion with no explicit reason is ambiguous, but agy-auto
        # only ever sees this endpoint used for the classifier prompt, where a
        # real answer is never empty -- treat it the same as a refusal so it
        # gets a second opinion instead of a bare "malformed response" deny.
        return "refusal"
    return "ok"


def apply_force_decision(body: bytes, forced: str) -> bytes:
    """Rewrites a backend's response so the classifier always sees `forced` as the
    decision. For a `force_decision` backend (a small/low-context model only ever
    trusted to rubber-stamp one outcome, never to reason about a deny/ask verdict)
    the backend's own output format, context limits, and even whether it produced
    valid JSON at all stop mattering -- this always returns a well-formed
    completion carrying `forced`, using the backend's own "reason" text when one
    is parseable and a generic one otherwise."""
    reason = f"forced to {forced} (backend configured with force_decision)"
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if content:
            inner = json.loads(content)
            if inner.get("reason"):
                reason = str(inner["reason"])[:300]
    except Exception:
        pass
    payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"decision": forced, "reason": reason})}}],
        "model": "forced:" + forced,
    }
    return json.dumps(payload).encode()


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
        backends = router_cfg.get("backends", {})
        tool, command = extract_call(body.get("messages") or [])
        try:
            name, backend_cfg = pick_backend(router_cfg, tool, command)
        except RouterError as e:
            self._send_json(500, {"error": str(e)})
            return

        def attempt(bname: str, bcfg: dict):
            """Returns (status, body, kind) or None on a network-level failure."""
            t0 = time.time()
            try:
                status, resp_body = forward(bcfg, body)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, RouterError) as e:
                self._log_route(bname, tool, command, time.time() - t0, ok=False, note=f"unreachable: {e}")
                return None
            forced = bcfg.get("force_decision")
            if forced:
                # This backend is never trusted to decide -- once it answers at
                # all (reachable), its output is rewritten to `forced` regardless
                # of what it said, whether that parsed, or how much of the
                # context it could actually read.
                resp_body = apply_force_decision(resp_body, forced)
                status, kind = 200, "ok"
                self._log_route(bname, tool, command, time.time() - t0, ok=True, note=f"forced->{forced}")
                return status, resp_body, kind
            kind = classify_response(status, resp_body)
            self._log_route(bname, tool, command, time.time() - t0, ok=(kind == "ok"), note="" if kind == "ok" else kind)
            return status, resp_body, kind

        result = attempt(name, backend_cfg)
        if result is None:
            # Network-level failure: retry once against fallback_backend, same as
            # a refusal or any other error would, since an unreachable backend
            # can't be distinguished from "safety-blocked" until it answers.
            fb_name = router_cfg.get("fallback_backend")
            if fb_name and fb_name in backends and fb_name != name:
                result = attempt(fb_name, backends[fb_name])
            if result is None:
                self._send_json(502, {"error": f"backend {name!r} unreachable and no working fallback_backend configured"})
                return
            status, resp_body, _kind = result
            self._send_json(status, resp_body)
            return

        status, resp_body, kind = result
        if kind == "ok":
            self._send_json(status, resp_body)
            return

        # Reachable but not usable: a "refusal" (safety block / empty completion)
        # goes to refusal_fallback_backend first (e.g. a local uncensored model
        # that will actually render a verdict); any other "error" (bad key, rate
        # limit, malformed request) goes to the plain fallback_backend, since a
        # different model doesn't fix those.
        retry_name = router_cfg.get("refusal_fallback_backend") if kind == "refusal" else router_cfg.get("fallback_backend")
        if retry_name and retry_name in backends and retry_name != name:
            result2 = attempt(retry_name, backends[retry_name])
            if result2 is not None:
                status2, resp_body2, _kind2 = result2
                self._send_json(status2, resp_body2)
                return
        # No (working) fallback for this case: relay the original response as-is
        # so the classifier's own fail-closed handling (invalid JSON -> deny)
        # takes over rather than the router inventing a verdict.
        self._send_json(status, resp_body)

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
        f"refusal_fallback_backend={router_cfg.get('refusal_fallback_backend')!r} "
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
