#!/bin/bash
# agy-auto router installer: runs engine/router.py as a background service,
# points [classifier].endpoint in the user policy overlay at it, and reports
# whether each configured [router.backends.*] entry is reachable.
#
# Companion to install.sh (which registers the PreToolUse hook itself and is
# unaffected by this script). Safe to run before or after install.sh, and
# safe to re-run: every step here is idempotent.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROUTER="$HERE/engine/router.py"
CFG_DIR="$HOME/.gemini/config"
AUTO_DIR="$CFG_DIR/agy-auto"
POLICY="$AUTO_DIR/policy.toml"
SERVICE_NAME="agy-auto-router"
PIDFILE="$AUTO_DIR/state/router.pid"
LOGFILE="$AUTO_DIR/router.log"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$SERVICE_NAME.service"
TERMUX_SVC_DIR="$HOME/.termux/service/$SERVICE_NAME"

MODE="auto"       # auto | systemd | termux | nohup
ACTION="install"  # install | uninstall | start | stop | restart | status
HOST=""
PORT=""
CHECK_BACKENDS=1
PATCH_POLICY=1

usage() {
  cat <<EOF
usage: $0 [action] [options]

actions (default: install):
  --start        start the router (does not touch policy.toml or the service manager)
  --stop         stop the router
  --restart      stop then start
  --status       show whether it's running, /healthz, and per-backend reachability
  --uninstall    stop and remove whatever service was registered; policy.toml is left as-is

options:
  --systemd            force a systemd --user unit (default: auto-detect)
  --termux             force a termux-services entry (default: auto-detect)
  --nohup              force the plain background-process fallback (default: auto-detect)
  --host HOST          override [router].listen_host from policy (default from policy/default.toml: 127.0.0.1)
  --port PORT          override [router].listen_port from policy (default from policy/default.toml: 8080)
  --no-policy-patch    do not edit $POLICY's [classifier].endpoint
  --no-backend-check   skip the [router.backends.*] reachability report
  -h, --help           this text

Default action installs a service that runs 'python3 engine/router.py',
detecting in order: a working 'systemctl --user' (Linux desktop/server), a
Termux install with termux-services ('sv-enable' present), otherwise a plain
'nohup ... &' background process (started now, but not survivable across a
reboot -- re-run '$0 --start' after one, or add that to your shell profile /
Termux:Boot if you don't have a service manager).
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'install-router: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --start) ACTION="start" ;;
    --stop) ACTION="stop" ;;
    --restart) ACTION="restart" ;;
    --status) ACTION="status" ;;
    --uninstall) ACTION="uninstall" ;;
    --systemd) MODE="systemd" ;;
    --termux) MODE="termux" ;;
    --nohup) MODE="nohup" ;;
    --host) HOST="${2:-}"; shift ;;
    --port) PORT="${2:-}"; shift ;;
    --no-policy-patch) PATCH_POLICY=0 ;;
    --no-backend-check) CHECK_BACKENDS=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null || die "python3 is required"
python3 -c 'import tomllib' 2>/dev/null || die "python3 >= 3.11 is required (tomllib)"
[ -f "$ROUTER" ] || die "engine/router.py not found at $ROUTER"

mkdir -p "$AUTO_DIR/state"
chmod 700 "$AUTO_DIR" "$AUTO_DIR/state" 2>/dev/null || true

# ---------------------------------------------------------------- effective host/port
# Reads the same merged policy engine/main.py would (default.toml < user overlay),
# so an existing [router] override in $POLICY is respected unless --host/--port wins.
EFFECTIVE=$(python3 - "$HERE" <<'PY'
import sys, os
here = sys.argv[1]
sys.path.insert(0, os.path.join(here, "engine"))
from main import load_policy
cfg, _, _ = load_policy([])
r = cfg.get("router", {})
print(r.get("listen_host", "127.0.0.1"))
print(r.get("listen_port", 8080))
PY
)
DEFAULT_HOST=$(printf '%s\n' "$EFFECTIVE" | sed -n 1p)
DEFAULT_PORT=$(printf '%s\n' "$EFFECTIVE" | sed -n 2p)
HOST="${HOST:-$DEFAULT_HOST}"
PORT="${PORT:-$DEFAULT_PORT}"
ENDPOINT="http://$HOST:$PORT/v1/chat/completions"
HEALTH_URL="http://$HOST:$PORT/healthz"

# ---------------------------------------------------------------- policy.toml patch
patch_policy_endpoint() {
  local ts
  ts=$(date +%Y%m%d-%H%M%S)
  if [ ! -f "$POLICY" ]; then
    mkdir -p "$AUTO_DIR"
    printf '# agy-auto user policy overlay. Merged over %s/policy/default.toml (lists are unioned).\n' "$HERE" > "$POLICY"
  fi
  cp "$POLICY" "$POLICY.bak-$ts"
  python3 - "$POLICY" "$ENDPOINT" <<'PY'
import re, sys, os
path, endpoint = sys.argv[1:3]
text = ""
if os.path.exists(path):
    with open(path) as fh:
        text = fh.read()
lines = text.splitlines(keepends=True)
out = []
in_classifier = False
found_section = False
found_endpoint = False
for line in lines:
    stripped = line.strip()
    if re.match(r'^\[[^.\[]', stripped):
        in_classifier = stripped == "[classifier]"
        found_section = found_section or in_classifier
        out.append(line)
        continue
    if in_classifier and re.match(r'^\s*#?\s*endpoint\s*=', line):
        out.append('endpoint = "%s"\n' % endpoint)
        found_endpoint = True
        continue
    out.append(line)
if found_section and not found_endpoint:
    for idx, line in enumerate(out):
        if line.strip() == "[classifier]":
            out.insert(idx + 1, 'endpoint = "%s"\n' % endpoint)
            found_endpoint = True
            break
if not found_section:
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    out.append("\n[classifier]\n")
    out.append('endpoint = "%s"\n' % endpoint)
with open(path, "w") as fh:
    fh.writelines(out)
print("policy.toml: [classifier].endpoint -> %s" % endpoint)
PY
  # a no-op edit (endpoint already correct) still leaves a timestamped backup;
  # drop it if nothing actually changed so re-runs don't pile up junk.
  cmp -s "$POLICY" "$POLICY.bak-$ts" && rm -f "$POLICY.bak-$ts"
  return 0
}

# ---------------------------------------------------------------- service backends
detect_mode() {
  if [ "$MODE" != "auto" ]; then
    printf '%s\n' "$MODE"
    return
  fi
  if command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then
    printf 'systemd\n'
    return
  fi
  if [ -n "${PREFIX:-}" ] && command -v sv-enable >/dev/null 2>&1; then
    printf 'termux\n'
    return
  fi
  printf 'nohup\n'
}

install_systemd() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=agy-auto classifier router
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) $ROUTER --host $HOST --port $PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now "$SERVICE_NAME"
  say "systemd --user service '$SERVICE_NAME' enabled and started ($UNIT)"
}

stop_systemd() {
  systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
}

uninstall_systemd() {
  systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload 2>/dev/null || true
}

install_termux() {
  mkdir -p "$TERMUX_SVC_DIR/log"
  cat > "$TERMUX_SVC_DIR/run" <<EOF
#!$PREFIX/bin/sh
exec $(command -v python3) $ROUTER --host $HOST --port $PORT 2>&1
EOF
  chmod 700 "$TERMUX_SVC_DIR/run"
  cat > "$TERMUX_SVC_DIR/log/run" <<EOF
#!$PREFIX/bin/sh
exec svlogd -tt "$AUTO_DIR/router-log"
EOF
  chmod 700 "$TERMUX_SVC_DIR/log/run"
  mkdir -p "$AUTO_DIR/router-log"
  sv-enable "$SERVICE_NAME" 2>/dev/null || true
  sv up "$SERVICE_NAME" >/dev/null 2>&1 || true
  say "termux-services entry '$SERVICE_NAME' created under $TERMUX_SVC_DIR"
  say "(runit starts it automatically; 'sv down $SERVICE_NAME' / 'sv up $SERVICE_NAME' to stop/start by hand)"
}

stop_termux() {
  sv down "$SERVICE_NAME" >/dev/null 2>&1 || true
}

uninstall_termux() {
  sv down "$SERVICE_NAME" >/dev/null 2>&1 || true
  sv-disable "$SERVICE_NAME" 2>/dev/null || true
  rm -rf "$TERMUX_SVC_DIR"
}

pidfile_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null
}

start_nohup() {
  if pidfile_running; then
    say "router already running (pid $(cat "$PIDFILE"))"
    return
  fi
  nohup python3 "$ROUTER" --host "$HOST" --port "$PORT" >>"$LOGFILE" 2>&1 &
  disown
  echo $! > "$PIDFILE"
  sleep 0.3
  pidfile_running || die "router failed to start; see $LOGFILE"
  say "router started in background (pid $(cat "$PIDFILE")), logging to $LOGFILE"
  say "no service manager detected (no systemd --user, no termux-services): it will NOT survive a reboot."
  say "start it again with '$0 --start', or add that to your shell profile / Termux:Boot."
}

stop_nohup() {
  if pidfile_running; then
    kill "$(cat "$PIDFILE")"
    rm -f "$PIDFILE"
    say "router stopped"
  else
    say "router not running (no valid pidfile at $PIDFILE)"
    rm -f "$PIDFILE"
  fi
}

# ---------------------------------------------------------------- health / backends
healthcheck() {
  python3 - "$HEALTH_URL" <<'PY'
import json, sys, urllib.error, urllib.request
try:
    data = json.loads(urllib.request.urlopen(sys.argv[1], timeout=3).read())
    print("router: reachable at", sys.argv[1], "-- backends:", ", ".join(data.get("backends", [])))
except Exception as e:
    print("router: NOT reachable at", sys.argv[1], "--", e)
    sys.exit(1)
PY
}

check_backends() {
  python3 - "$HERE" <<'PY'
import sys, os, urllib.error, urllib.request
here = sys.argv[1]
sys.path.insert(0, os.path.join(here, "engine"))
from main import load_policy
cfg, _, _ = load_policy([])
backends = cfg.get("router", {}).get("backends", {})
if not backends:
    print("  (no [router.backends] configured)")
    raise SystemExit
for name, b in backends.items():
    ep = b.get("endpoint") or ""
    base = ep.rsplit("/v1/", 1)[0] if "/v1/" in ep else ep
    forced = " (force_decision=%r)" % b["force_decision"] if b.get("force_decision") else ""
    try:
        urllib.request.urlopen(base + "/v1/models", timeout=3).read()
        print(f"  {name}: reachable  ({ep}){forced}")
    except Exception as e:
        print(f"  {name}: NOT reachable  ({ep}){forced} -- {e}")
PY
}

# ---------------------------------------------------------------- dispatch
do_start() {
  case "$(detect_mode)" in
    systemd) install_systemd ;;
    termux) install_termux ;;
    nohup) start_nohup ;;
  esac
}

do_stop() {
  # best-effort across every mechanism, since we may not know which one is live
  stop_systemd 2>/dev/null || true
  stop_termux 2>/dev/null || true
  stop_nohup
}

case "$ACTION" in
  start)
    do_start
    sleep 0.5
    healthcheck || true
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_stop
    sleep 0.3
    do_start
    sleep 0.5
    healthcheck || true
    ;;
  status)
    say "mode: $(detect_mode)"
    case "$(detect_mode)" in
      systemd) systemctl --user status "$SERVICE_NAME" --no-pager 2>&1 | sed -n '1,5p' || true ;;
      termux) sv status "$SERVICE_NAME" 2>&1 || say "$SERVICE_NAME not registered under termux-services" ;;
      nohup) pidfile_running && say "running (pid $(cat "$PIDFILE"))" || say "not running" ;;
    esac
    healthcheck || true
    if [ "$CHECK_BACKENDS" = 1 ]; then
      say "backends:"
      check_backends
    fi
    ;;
  uninstall)
    uninstall_systemd 2>/dev/null || true
    uninstall_termux 2>/dev/null || true
    stop_nohup
    say "router service removed. $POLICY was left as-is (classifier.endpoint still points at $ENDPOINT)."
    say "backups from every install/patch are at $POLICY.bak-*; restore one by hand if you want the old endpoint back."
    ;;
  install)
    if [ "$PATCH_POLICY" = 1 ]; then
      patch_policy_endpoint
    fi
    do_start
    sleep 0.5
    healthcheck || say "WARNING: router did not come up cleanly; check $LOGFILE (nohup mode) or the service manager's logs."
    if [ "$CHECK_BACKENDS" = 1 ]; then
      say ""
      say "backend reachability (informational -- an unreachable backend only affects the calls routed to it; see policy/default.toml [[router.rules]]):"
      check_backends
    fi
    say ""
    say "installed. classifier endpoint: $ENDPOINT"
    say "status:    $0 --status"
    say "uninstall: $0 --uninstall"
    ;;
esac
