#!/usr/bin/env bash
# Start the intake server, load the demo corpus through the local sender, and
# leave the server running so the pages can be inspected.
#
#   scripts/run_demo.sh            # http://127.0.0.1:8000
#   PORT=9000 scripts/run_demo.sh
#
# Everything is standard library Python; nothing is installed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
DB="${APP_DB:-$ROOT/data/demo.sqlite3}"

if [ "${RESET:-0}" = "1" ]; then
  rm -f "$DB" "$DB"-wal "$DB"-shm
fi

mkdir -p "$(dirname "$DB")"

APP_DB="$DB" python3 -m app.web --host "$HOST" --port "$PORT" &
SERVER_PID=$!

cleanup() {
  echo
  echo "stopping server (pid $SERVER_PID)"
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait for readiness rather than sleeping a fixed amount.
python3 - "$HOST" "$PORT" <<'PY'
import sys, time, urllib.request
host, port = sys.argv[1], int(sys.argv[2])
deadline = time.time() + 15
while time.time() < deadline:
    try:
        urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1).read()
        sys.exit(0)
    except Exception:
        time.sleep(0.1)
print("server did not become ready", file=sys.stderr)
sys.exit(1)
PY

python3 scripts/sender.py --url "http://$HOST:$PORT" \
  --manifest scripts/demo_manifest.json --actor demo-sender

cat <<EOF

Server is running:
  UI        http://$HOST:$PORT/
  Receipts  http://$HOST:$PORT/failures
  Mappings  http://$HOST:$PORT/mappings
  Database  $DB

Press Ctrl-C to stop.
EOF

wait "$SERVER_PID"
