#!/bin/bash
# Start, stop and check the local OpenObserve instance used by the D-027 receipts monitor.
#
#   scripts/openobserve/o2_server.sh start|stop|status|logs
#
# start and stop also start and stop the alert destination, webhook_receiver.py on 127.0.0.1:5091, which writes every
# notification to O2_HOME/alerts/received.jsonl.
#
# O2_HOME (default /Users/amey/Projects/wtth-openobserve) holds bin/openobserve, data/, logs/ and .env.
# .env (chmod 600) holds ZO_ROOT_USER_EMAIL and ZO_ROOT_USER_PASSWORD; it is never printed or committed.
# The server listens on 127.0.0.1 only (HTTP 5080, gRPC 5081) and runs under a macOS sandbox profile that
# denies every outbound IP connection except to this machine.
set -euo pipefail

O2_HOME="${O2_HOME:-/Users/amey/Projects/wtth-openobserve}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$O2_HOME/openobserve.pid"
LOG="$O2_HOME/logs/openobserve.log"
URL="http://127.0.0.1:5080"
RPIDFILE="$O2_HOME/receiver.pid"
RLOG="$O2_HOME/logs/receiver.log"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
receiver_running() { [ -f "$RPIDFILE" ] && kill -0 "$(cat "$RPIDFILE")" 2>/dev/null; }

start_receiver() {
  if receiver_running; then echo "receiver already running (pid $(cat "$RPIDFILE"))"; return 0; fi
  mkdir -p "$O2_HOME/alerts" "$O2_HOME/logs"
  nohup python3 -I "$HERE/webhook_receiver.py" --port 5091 --out "$O2_HOME/alerts" >>"$RLOG" 2>&1 &
  echo $! >"$RPIDFILE"
  for _ in $(seq 1 20); do
    curl -sf http://127.0.0.1:5091/ >/dev/null 2>&1 && { echo "receiver started (pid $(cat "$RPIDFILE"))"; return 0; }
    sleep 0.5
  done
  echo "receiver did not start; see $RLOG" >&2
  exit 1
}

stop_receiver() {
  if receiver_running; then kill "$(cat "$RPIDFILE")"; echo "receiver stopped"; else echo "receiver not running"; fi
  rm -f "$RPIDFILE"
}

start() {
  start_receiver
  [ -f "$O2_HOME/.env" ] || { echo "missing $O2_HOME/.env" >&2; exit 1; }
  [ -x "$O2_HOME/bin/openobserve" ] || { echo "missing $O2_HOME/bin/openobserve" >&2; exit 1; }
  if running; then echo "already running (pid $(cat "$PIDFILE"))"; return 0; fi
  mkdir -p "$O2_HOME/data" "$O2_HOME/logs"
  (
    set -a
    # shellcheck disable=SC1091
    . "$O2_HOME/.env"
    set +a
    # Network: localhost only.
    export ZO_HTTP_ADDR=127.0.0.1 ZO_HTTP_PORT=5080 ZO_HTTP_IPV6_ENABLED=false
    export ZO_GRPC_ADDR=127.0.0.1 ZO_GRPC_PORT=5081
    export ZO_WEB_URL="$URL"
    # Storage: single node, local disk, everything under O2_HOME/data.
    export ZO_LOCAL_MODE=true ZO_LOCAL_MODE_STORAGE=disk ZO_NODE_ROLE=all
    export ZO_DATA_DIR="$O2_HOME/data/"
    # No phone-home: telemetry, GeoIP download, model-pricing sync and pattern download off or pointed at a dead
    # local port.
    export ZO_TELEMETRY=false ZO_USAGE_REPORTING_ENABLED=false ZO_MMDB_DISABLE_DOWNLOAD=true
    export ZO_MODEL_PRICING_ENABLED=false
    export ZO_MODEL_PRICING_SOURCE_URL=http://127.0.0.1:9/disabled
    export ZO_REGEX_PATTERNS_SOURCE_URL=http://127.0.0.1:9/disabled
    export ZO_PRINT_KEY_CONFIG=false
    # The synthetic shift date lies in the past; the default window rejects records older than 5 hours.
    export ZO_INGEST_ALLOWED_UPTO=87600
    # The alert destination is a webhook receiver on 127.0.0.1.
    export ZO_SSRF_ALLOW_LOOPBACK=true
    # Always compute query results fresh: the result cache served a stale partial result to a dashboard panel.
    export ZO_RESULT_CACHE_ENABLED=false
    cd "$O2_HOME"
    nohup sandbox-exec -f "$HERE/localhost-only.sb" "$O2_HOME/bin/openobserve" >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
  )
  for _ in $(seq 1 60); do
    if curl -sf "$URL/healthz" >/dev/null 2>&1; then
      echo "started (pid $(cat "$PIDFILE")), $URL/healthz: $(curl -s "$URL/healthz")"
      return 0
    fi
    sleep 1
  done
  echo "server did not become healthy; see $LOG" >&2
  exit 1
}

stop() {
  if running; then
    kill "$(cat "$PIDFILE")"
    for _ in $(seq 1 20); do running || break; sleep 1; done
    running && { echo "still running after 20 s" >&2; exit 1; }
    echo "stopped"
  else
    echo "not running"
  fi
  rm -f "$PIDFILE"
  stop_receiver
}

status() {
  if running; then
    echo "running (pid $(cat "$PIDFILE")); healthz: $(curl -s "$URL/healthz" || echo unreachable)"
    lsof -nP -a -p "$(cat "$PIDFILE")" -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 {print "  listening on " $9}'
  else
    echo "not running"
  fi
  if receiver_running; then echo "receiver running (pid $(cat "$RPIDFILE")) on 127.0.0.1:5091"; else echo "receiver not running"; fi
}

# Tail of the server log with the .env values redacted.
logs() {
  python3 -I - "$O2_HOME/.env" "$LOG" "${1:-40}" <<'PY'
import sys
env, log, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
secrets = []
for line in open(env):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        v = line.split("=", 1)[1].strip().strip("'\"")
        if v:
            secrets.append(v)
for line in open(log, errors="replace").read().splitlines()[-n:]:
    for s in secrets:
        line = line.replace(s, "[REDACTED]")
    print(line[:400])
PY
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  logs) logs "${2:-40}" ;;
  *) echo "usage: $0 start|stop|status|logs [n]" >&2; exit 2 ;;
esac
