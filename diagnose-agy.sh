#!/bin/bash
# agy-auto diagnostic: reveals the REAL state of agy + agy-auto on this machine --
# what's actually installed, where, what's actually registered, and what's
# actually running -- as opposed to what any one config file claims. Read-only:
# this script never writes or deletes anything. Use it before install-router.sh
# or reinstall-agy-auto.sh so you know what you're starting from, or any time
# something isn't behaving like you expect and you want to see the whole picture
# instead of guessing which of several installs is actually active.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CFG_DIR="$HOME/.gemini/config"
HOOKS_JSON="$CFG_DIR/hooks.json"
SETTINGS="$HOME/.gemini/antigravity-cli/settings.json"
AUTO_DIR="$CFG_DIR/agy-auto"
POLICY="$AUTO_DIR/policy.toml"

hr() { printf -- '-------------------------------------------------------------------\n'; }
section() { printf '\n=== %s ===\n' "$1"; }

section "agy binary"
if command -v agy >/dev/null 2>&1; then
  say_path=$(command -v agy)
  printf 'path:    %s\n' "$say_path"
  printf 'version: %s\n' "$(agy --version 2>/dev/null | head -1)"
else
  printf 'agy: not found in PATH\n'
fi

section "agy's own view (agy -p /hooks, agy -p /config)"
if command -v agy >/dev/null 2>&1; then
  agy -p "/hooks" 2>&1 || printf '(agy -p /hooks failed)\n'
  hr
  agy -p "/config" 2>&1 || printf '(agy -p /config failed)\n'
else
  printf '(skipped, agy not on PATH)\n'
fi

section "settings.json ($SETTINGS)"
if [ -f "$SETTINGS" ]; then
  cat "$SETTINGS"
else
  printf '(does not exist -- toolPermission is at its default, request-review)\n'
fi

section "global hooks.json ($HOOKS_JSON)"
if [ -f "$HOOKS_JSON" ]; then
  cat "$HOOKS_JSON"
  echo
  n=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$HOOKS_JSON" 2>/dev/null || echo "?")
  printf '(%s top-level hook entr(y/ies) registered)\n' "$n"
else
  printf '(does not exist -- no global hooks registered)\n'
fi

section "workspace hooks.json under \$PWD ($PWD/.agents/hooks.json)"
if [ -f "$PWD/.agents/hooks.json" ]; then
  cat "$PWD/.agents/hooks.json"
else
  printf '(none here)\n'
fi

section "every agy-auto install found on disk"
# Known conventional locations from README (Option A global hook clone, Option B
# native plugin, per-project plugin) plus a bounded find in case there are others.
CANDIDATES=(
  "$HOME/.local/share/agy-auto"
  "$CFG_DIR/plugins/agy-auto"
  "$PWD/.agents/plugins/agy-auto"
  "$HERE"
)
FOUND=()
for d in "${CANDIDATES[@]}"; do
  [ -d "$d" ] || continue
  rp=$(cd "$d" && pwd)
  dup=0
  for f in "${FOUND[@]}"; do [ "$f" = "$rp" ] && dup=1; done
  [ "$dup" = 1 ] && continue
  FOUND+=("$rp")
done
# Bounded extra search (depth 6, skip huge/irrelevant trees) in case of a stray
# clone somewhere else under $HOME -- best-effort, capped so it can't hang.
while IFS= read -r d; do
  rp=$(cd "$d" && pwd) 2>/dev/null || continue
  dup=0
  for f in "${FOUND[@]}"; do [ "$f" = "$rp" ] && dup=1; done
  [ "$dup" = 1 ] || FOUND+=("$rp")
done < <(timeout 10 find "$HOME" -maxdepth 6 -type d -iname 'agy-auto' \
            -not -path '*/node_modules/*' -not -path '*/.git/*' 2>/dev/null)

if [ "${#FOUND[@]}" -eq 0 ]; then
  printf '(none found under the usual locations or %s, depth 6)\n' "$HOME"
else
  for d in "${FOUND[@]}"; do
    printf '%s\n' "$d"
    if [ -d "$d/.git" ]; then
      remote=$(git -C "$d" remote get-url origin 2>/dev/null || echo "?")
      branch=$(git -C "$d" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
      commit=$(git -C "$d" rev-parse --short HEAD 2>/dev/null || echo "?")
      dirty=$(git -C "$d" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
      printf '  git: %s @ %s (%s), %s uncommitted change(s)\n' "$remote" "$branch" "$commit" "$dirty"
    else
      printf '  (not a git repo)\n'
    fi
    [ -f "$d/hook.sh" ] && printf '  hook.sh: present\n'
    [ -f "$d/engine/router.py" ] && printf '  engine/router.py: present\n'
  done
  if [ "${#FOUND[@]}" -gt 1 ]; then
    printf '\nWARNING: more than one agy-auto install found. The README says not to\n'
    printf 'combine the global-hook installer (Option A) with the native-plugin layout\n'
    printf '(Option B) in the same folder, and only one hooks.json entry actually runs --\n'
    printf 'reinstall-agy-auto.sh will list these again before touching anything.\n'
  fi
fi

section "policy layers (merge order: default.toml < user overlay < workspace overlay)"
printf 'repo default: %s/policy/default.toml (%s)\n' "$HERE" "$([ -f "$HERE/policy/default.toml" ] && echo present || echo MISSING)"
printf 'user overlay: %s (%s)\n' "$POLICY" "$([ -f "$POLICY" ] && echo present || echo "not created yet")"
printf 'workspace overlay checked at: %s/.agents/agy-auto.toml (%s)\n' "$PWD" "$([ -f "$PWD/.agents/agy-auto.toml" ] && echo present || echo none)"

section "effective merged config (classifier + router)"
if [ -f "$HERE/engine/main.py" ]; then
  python3 - "$HERE" <<'PY'
import json, os, sys
here = sys.argv[1]
sys.path.insert(0, os.path.join(here, "engine"))
from main import load_policy
cfg, version, sources = load_policy([])
print("policy version:", version)
print("sources:", ", ".join(sources))
print("mode:", cfg.get("mode"))
print()
print("[classifier]")
for k, v in (cfg.get("classifier") or {}).items():
    print(f"  {k} = {v!r}")
r = cfg.get("router") or {}
if r:
    print()
    print("[router]")
    for k in ("listen_host", "listen_port", "default_backend", "fallback_backend", "refusal_fallback_backend"):
        if k in r:
            print(f"  {k} = {r[k]!r}")
    for name, b in (r.get("backends") or {}).items():
        forced = f", force_decision={b['force_decision']!r}" if b.get("force_decision") else ""
        print(f"  backend {name!r}: endpoint={b.get('endpoint')!r} model={b.get('model')!r}{forced}")
    print(f"  {len(r.get('rules', []))} routing rule(s)")
else:
    print()
    print("[router] not configured")
PY
else
  printf '(engine/main.py not found under %s -- is this really the repo root?)\n' "$HERE"
fi

section "what's actually listening"
for port in 8080 8081 8082 11434; do
  if python3 - "$port" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.3)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)
except Exception:
    sys.exit(1)
PY
  then
    label=""
    case "$port" in
      8080) label=" (default agy-auto router port)" ;;
      11434) label=" (default Ollama port)" ;;
    esac
    printf '127.0.0.1:%s -- something is listening%s\n' "$port" "$label"
  else
    printf '127.0.0.1:%s -- nothing listening\n' "$port"
  fi
done
printf '\nrouter/model processes:\n'
pgrep -af 'engine/router\.py|ollama serve|llama-server|llama\.cpp' 2>/dev/null | grep -v 'diagnose-agy.sh\|pgrep -af' || printf '  (none found)\n'

section "audit / state"
if [ -d "$AUTO_DIR" ]; then
  printf 'audit log:  %s (%s file(s))\n' "$AUTO_DIR/audit" "$(find "$AUTO_DIR/audit" -type f 2>/dev/null | wc -l | tr -d ' ')"
  printf 'cache/state: %s (%s file(s))\n' "$AUTO_DIR/state" "$(find "$AUTO_DIR/state" -type f 2>/dev/null | wc -l | tr -d ' ')"
  printf 'policy.toml backups: %s\n' "$(find "$AUTO_DIR" -maxdepth 1 -name 'policy.toml.bak-*' 2>/dev/null | wc -l | tr -d ' ')"
else
  printf '(%s does not exist yet -- nothing installed)\n' "$AUTO_DIR"
fi

printf '\ndone.\n'
