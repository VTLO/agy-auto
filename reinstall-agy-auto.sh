#!/bin/bash
# agy-auto clean reinstall: shows what's actually there (diagnose-agy.sh),
# removes the current hook + router registration, then reinstalls both fresh
# (install.sh + install-router.sh) and verifies the result.
#
# Never deletes a stray/duplicate agy-auto clone or plugin directory it finds --
# diagnose-agy.sh lists those so you can remove the ones you don't want by hand;
# this script only ever touches the hook registration, the router service, and
# (with --purge-logs, and only after confirming) this install's own audit/state
# logs and policy.toml backups.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CFG_DIR="$HOME/.gemini/config"
AUTO_DIR="$CFG_DIR/agy-auto"

YES=0
PURGE_LOGS=0
SKIP_UNINSTALL=0

usage() {
  cat <<EOF
usage: $0 [--yes] [--purge-logs] [--skip-uninstall]

  --yes             don't ask before uninstalling the current hook/router (or
                     purging logs with --purge-logs); needed for a non-interactive
                     run, since without a tty this script defaults to "no".
  --purge-logs       also delete this install's audit/state logs and
                     policy.toml.bak-* backups under $AUTO_DIR (asks first, same
                     as everything else, unless --yes).
  --skip-uninstall   go straight to a fresh install without uninstalling first
                     (use when nothing is currently installed).
  -h, --help         this text

Steps: diagnose current state -> uninstall hook + router -> reinstall both ->
diagnose again so you can see the before/after.
EOF
}

for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --purge-logs) PURGE_LOGS=1 ;;
    --skip-uninstall) SKIP_UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option $a" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'reinstall-agy-auto: WARNING: %s\n' "$*" >&2; }

confirm() {
  [ "$YES" = 1 ] && return 0
  if [ -r /dev/tty ]; then
    printf '%s [y/N] ' "$1" > /dev/tty
    read -r ans < /dev/tty
    case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
  fi
  say "(no tty to confirm '$1'; skipping -- pass --yes to do this non-interactively)"
  return 1
}

chmod +x "$HERE/hook.sh" "$HERE/install.sh" "$HERE/install-router.sh" "$HERE/diagnose-agy.sh" 2>/dev/null || true

say "################################################################"
say "# 1/4: current state"
say "################################################################"
"$HERE/diagnose-agy.sh" || warn "diagnose-agy.sh reported a problem (see above); continuing anyway"

say ""
say "################################################################"
say "# 2/4: clean slate"
say "################################################################"
if [ "$SKIP_UNINSTALL" = 1 ]; then
  say "skipped (--skip-uninstall)"
elif confirm "Uninstall the current hook + router now?"; then
  "$HERE/install.sh" --uninstall || warn "install.sh --uninstall failed or found nothing to remove"
  "$HERE/install-router.sh" --uninstall || warn "install-router.sh --uninstall failed or found nothing to remove"
else
  say "skipped -- nothing was removed. Re-running install.sh/install-router.sh next will just"
  say "re-register over whatever is already there (both are idempotent), it just won't start"
  say "from a verified-clean state."
fi

if [ "$PURGE_LOGS" = 1 ] && [ -d "$AUTO_DIR" ]; then
  if confirm "Also delete audit/state logs and policy.toml backups under $AUTO_DIR?"; then
    rm -rf "$AUTO_DIR/state" "$AUTO_DIR/audit"
    rm -f "$AUTO_DIR"/policy.toml.bak-*
    mkdir -p "$AUTO_DIR/state" "$AUTO_DIR/audit"
    say "logs and backups purged"
  fi
fi

say ""
say "################################################################"
say "# 3/4: fresh install"
say "################################################################"
HOOK_OK=1
ROUTER_OK=1
"$HERE/install.sh" || { HOOK_OK=0; warn "install.sh failed (see above) -- is 'agy' on PATH?"; }
"$HERE/install-router.sh" || { ROUTER_OK=0; warn "install-router.sh failed (see above)"; }

say ""
say "################################################################"
say "# 4/4: verify"
say "################################################################"
"$HERE/install-router.sh" --status || true
say ""
"$HERE/diagnose-agy.sh" || true

say ""
if [ "$HOOK_OK" = 1 ] && [ "$ROUTER_OK" = 1 ]; then
  say "reinstall complete."
else
  say "reinstall finished with warnings above -- hook_ok=$HOOK_OK router_ok=$ROUTER_OK"
fi
say "If 'every agy-auto install found on disk' above lists more than one, remove the"
say "ones you don't want by hand -- this script never deletes a clone/plugin directory."
