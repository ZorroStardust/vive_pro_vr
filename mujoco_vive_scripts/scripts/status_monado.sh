#!/usr/bin/env bash
# Show monado-service status: PID, session, DISPLAY, IPC socket, log tail.

set -euo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/monado"
PID_FILE="$STATE_DIR/monado-detached.pid"
LOG_FILE="$STATE_DIR/monado-detached.log"

echo "=== monado-service ==="
if [ -f "$PID_FILE" ]; then
    MPID=$(cat "$PID_FILE")
    if kill -0 "$MPID" 2>/dev/null; then
        ps -o pid,ppid,pgid,sid,etime,cmd -p "$MPID" 2>/dev/null | tail -n +2
        echo "  DISPLAY        : $(tr '\0' '\n' < /proc/$MPID/environ 2>/dev/null | grep '^DISPLAY=' || echo '?')"
        echo "  WAYLAND_DISPLAY: $(tr '\0' '\n' < /proc/$MPID/environ 2>/dev/null | grep '^WAYLAND_DISPLAY=' || echo '(unset)')"
        echo "  IPC socket     : $( [ -S "${XDG_RUNTIME_DIR:-/tmp}/monado_comp_ipc" ] && echo present || echo 'MISSING' )"
        [ -f "$LOG_FILE" ] && { echo "  --- last 5 log lines:"; tail -5 "$LOG_FILE" | sed 's/^/    /'; }
    else
        echo "  PID file stale (pid $MPID not alive)."
        [ -f "$LOG_FILE" ] && { echo "  --- last 5 log lines:"; tail -5 "$LOG_FILE" | sed 's/^/    /'; }
    fi
else
    echo "  not running (no PID file)."
fi