#!/usr/bin/env bash
# Netemango launcher: creates/uses .venv, installs deps, runs the GUI.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# 1. System prerequisites (Linux + tc + ping; iw is optional, only used on Wi-Fi).
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Netemango only runs on Linux (uses tc/netem)." >&2
  exit 2
fi
for bin in tc ping; do
  if ! command -v "$bin" >/dev/null 2>&1 \
     && [[ ! -x "/sbin/$bin" ]] && [[ ! -x "/usr/sbin/$bin" ]]; then
    echo "Missing required tool: $bin (install iproute2 / iputils-ping)." >&2
    exit 2
  fi
done

# 2. Virtualenv
if [[ ! -d .venv ]]; then
  echo "Creating virtualenv in .venv ..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 3. Deps (only when requirements.txt changed since last install)
STAMP=".venv/.requirements.stamp"
if [[ ! -f "$STAMP" ]] || [[ requirements.txt -nt "$STAMP" ]]; then
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
  touch "$STAMP"
fi

# 4. Run
exec python src/network_controller_pyqt.py "$@"
