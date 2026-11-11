#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PID=""
FRONTEND_PID=""

say() { printf '\n[Dividend] %s\n' "$*"; }
fail() { say "ERROR: $*" >&2; exit 1; }

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  for pid in "$FRONTEND_PID" "$BACKEND_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$FRONTEND_PID" "$BACKEND_PID"; do
    [[ -z "$pid" ]] || wait "$pid" 2>/dev/null || true
  done
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ "$(uname -s)" == Linux ]] || fail "This launcher supports Debian and Ubuntu Linux only."
[[ -r /etc/os-release ]] || fail "Cannot identify this Linux distribution."
source /etc/os-release
case "${ID:-} ${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) fail "Unsupported distribution: ${PRETTY_NAME:-unknown}. Use Debian or Ubuntu." ;; esac

missing=()
command -v python3 >/dev/null || missing+=("sudo apt update && sudo apt install -y python3 python3-venv python3-pip")
command -v pip3 >/dev/null || missing+=("sudo apt update && sudo apt install -y python3-pip")
command -v node >/dev/null || missing+=("sudo apt update && sudo apt install -y nodejs npm (Node.js 20.19+ or 22.12+)")
command -v npm >/dev/null || missing+=("sudo apt update && sudo apt install -y npm")
command -v dpkg-query >/dev/null || missing+=("sudo apt update && sudo apt install -y dpkg")
((${#missing[@]} == 0)) || { say "Missing prerequisites. Install with:"; printf '  %s\n' "${missing[@]}"; exit 1; }
python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || fail "Python 3.11 or newer is required; found $python_version. Install with: sudo apt install python3.11 python3.11-venv python3-pip"
node_version="$(node -p 'process.versions.node')"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit((a===20&&b>=19 || a>=22&&(a>22 || b>=12))?0:1)' || fail "Node.js 20.19+ or 22.12+ is required; found $node_version. Install a supported Node.js release and npm from NodeSource or your approved package source."

for file in pyproject.toml dividend/__main__.py dividend/cli.py dividend/main.py frontend/package.json frontend/package-lock.json; do
  [[ -f "$ROOT/$file" ]] || fail "Required project file is missing: $file"
done
[[ -f "$ROOT/.env" ]] || fail "Missing $ROOT/.env. Create it with 'cp .env.example .env', then configure the required backend settings. The launcher will not create or overwrite credentials."
git -C "$ROOT" check-ignore -q .env || fail ".env is not ignored by Git; refusing to start with secrets at risk."

say "Installing backend dependencies in the repository virtual environment..."
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then python3 -m venv "$ROOT/.venv" || fail "Could not create .venv. Install with: sudo apt install python3-venv"; fi
"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"

say "Installing frontend dependencies..."
if [[ -f "$ROOT/frontend/package-lock.json" ]]; then (cd "$ROOT/frontend" && npm ci); else (cd "$ROOT/frontend" && npm install); fi

mkdir -p "$ROOT/.local/scans"
scan_file="$ROOT/.local/scans/host-packages-$(date -u +%Y%m%dT%H%M%SZ).json"
say "Scanning installed host Debian packages (dpkg-query; no Docker or sudo)..."
(cd "$ROOT" && "$ROOT/.venv/bin/python" -m dividend scan --output "$scan_file") || fail "Host package scan failed."
[[ -s "$scan_file" ]] || fail "Scanner produced an empty report."
read -r source_count binary_count < <("$ROOT/.venv/bin/python" -c 'import json,sys; r=json.load(open(sys.argv[1])); assert r["origin"]=="dpkg" and r["source_count"]==len(r["sources"]) and r["binary_count"]==sum(map(len,r["sources"].values())); print(r["source_count"],r["binary_count"])' "$scan_file") || fail "Scanner report failed JSON validation."
say "Scanned $source_count source packages and $binary_count binary packages."
say "Report: $scan_file"

cd "$ROOT"
setsid "$ROOT/.venv/bin/dotenv" -f "$ROOT/.env" run -- "$ROOT/.venv/bin/uvicorn" dividend.main:create_app --factory --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!
setsid npm --prefix "$ROOT/frontend" run dev -- --host 127.0.0.1 --port 5173 &
FRONTEND_PID=$!

say "Backend:  http://127.0.0.1:8000"
say "Frontend: http://127.0.0.1:5173"
say "Import the host scan from $scan_file using the existing upload workflow."
say "Press Ctrl+C to stop both services."

if wait -n -p exited "$BACKEND_PID" "$FRONTEND_PID"; then status=0; else status=$?; fi
if [[ "$exited" == "$BACKEND_PID" ]]; then fail "Backend exited unexpectedly (status $status)."; fi
fail "Frontend exited unexpectedly (status $status)."
