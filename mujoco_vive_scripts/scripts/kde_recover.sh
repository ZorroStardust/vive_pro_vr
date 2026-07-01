#!/usr/bin/env bash
# Try to restart KDE's :1 Xwayland after a crash.
#
# Background: when monado triggered an Xwayland crash earlier, KWin's
# XwaylandCrashPolicy=Restart eventually did restart :1, but in some
# sessions KWin doesn't auto-recover.  This script:
#
#   1. Asks KWin to reconfigure (DBus signal) — sometimes triggers
#      Xwayland restart.
#   2. If :1 didn't come back, prints manual recovery options:
#        - Log out and log back in (cleanest)
#        - Switch to TTY2 and run `kwin_wayland --replace &`
#        - Reboot

set -euo pipefail

echo "[INFO] Asking KWin to reconfigure (may restart Xwayland :1)..."

if gdbus call --session --dest org.kde.KWin --object-path /KWin \
        --method org.kde.KWin.reconfigure > /dev/null 2>&1; then
    echo "[INFO] DBus reconfigure signal sent."
else
    echo "[WARN] Could not reach KWin via DBus.  Try from a TTY or after login."
fi

# Give KWin a moment to act, then check whether :1 is alive.
sleep 3

if pgrep -f 'Xwayland :1' > /dev/null 2>&1; then
    NEW_PID=$(pgrep -f 'Xwayland :1' | head -1)
    echo "[OK] Xwayland :1 is back, PID $NEW_PID."
    [ -S /tmp/.X11-unix/X1 ] && echo "     socket /tmp/.X11-unix/X1: present."
    exit 0
fi

cat <<'EOF'

[FAIL] Xwayland :1 did NOT come back.  Manual recovery options:

  Option A (cleanest):
    1. Log out from KDE (Ctrl+Alt+Del or plasmashell logout menu)
    2. Log back in; :1 will be recreated fresh.

  Option B (no logout):
    1. Switch to TTY2: Ctrl+Alt+F2
    2. Log in
    3. Run:  kwin_wayland --replace &
    4. Switch back: Ctrl+Alt+F1 (or F7)

  Option C (last resort):
    Reboot.

EOF
exit 1