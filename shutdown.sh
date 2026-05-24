#!/usr/bin/env bash
# Netemango shutdown: stop any running GUI instance and strip leftover
# netem qdiscs from every non-loopback interface.
#
# Usage:
#   ./shutdown.sh            # graceful: SIGTERM, then clean qdiscs
#   ./shutdown.sh --force    # SIGKILL instead of SIGTERM
#   ./shutdown.sh --dry-run  # show what would happen, change nothing
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

FORCE=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    -f|--force)   FORCE=1 ;;
    -n|--dry-run) DRY=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

run() {
  if (( DRY )); then
    echo "DRY: $*"
  else
    "$@"
  fi
}

# --- 1. Stop the GUI process ------------------------------------------------
SIG="TERM"; (( FORCE )) && SIG="KILL"
# Match the script we actually launch (avoid killing this shutdown script).
PIDS="$(pgrep -f 'python.*network_controller_pyqt\.py' || true)"
if [[ -n "$PIDS" ]]; then
  echo "Stopping Netemango (SIG${SIG}): $PIDS"
  # shellcheck disable=SC2086
  run kill -s "$SIG" $PIDS || true
  # Give it a moment to clean up its own qdisc on closeEvent.
  if (( ! FORCE )); then
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      pgrep -f 'python.*network_controller_pyqt\.py' >/dev/null || break
      sleep 0.2
    done
    # Escalate if still alive.
    LEFT="$(pgrep -f 'python.*network_controller_pyqt\.py' || true)"
    if [[ -n "$LEFT" ]]; then
      echo "Still running, escalating to SIGKILL: $LEFT"
      # shellcheck disable=SC2086
      run kill -9 $LEFT || true
    fi
  fi
else
  echo "No Netemango process found."
fi

# --- 2. Kill any orphan ping spawned by the Verify tab ----------------------
PING_PIDS="$(pgrep -f '^/[^ ]*ping .* -O ' || true)"
if [[ -n "$PING_PIDS" ]]; then
  echo "Killing orphan verify-ping: $PING_PIDS"
  # shellcheck disable=SC2086
  run kill $PING_PIDS 2>/dev/null || true
fi

# --- 3. Strip leftover netem qdiscs ----------------------------------------
TC="$(command -v tc || echo /sbin/tc)"
if [[ ! -x "$TC" ]]; then
  echo "tc not found; skipping qdisc cleanup." >&2
  exit 0
fi

# Prime sudo once so per-iface deletes don't each prompt.
if (( ! DRY )); then
  sudo -v || { echo "sudo auth failed; cannot clean qdiscs." >&2; exit 1; }
fi

for iface_path in /sys/class/net/*; do
  iface="$(basename "$iface_path")"
  [[ "$iface" == "lo" ]] && continue
  if "$TC" qdisc show dev "$iface" 2>/dev/null | grep -q netem; then
    echo "Removing netem qdisc on $iface"
    run sudo "$TC" qdisc del dev "$iface" root || true
  fi
done

echo "Shutdown complete."
