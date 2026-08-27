"""Power-cycle empty USB hubs to revive wedged capture boxes.

When the capture boxes fail to enumerate (e.g. after replugging hubs), the
hub ports can stay dead until re-authorized.  This toggles the kernel
``authorized`` flag of every USB hub that currently has no downstream
devices, forcing a full re-enumeration, then re-runs capture detection.
Requires sudo for the sysfs writes.
"""

from __future__ import annotations

import subprocess
import time

from .v4l2_capture import detect_capture_devices, empty_usb_hubs, usb_topology_report


def main() -> int:
    print("[INFO] USB topology:")
    for line in usb_topology_report():
        print(f"  {line}")

    hubs = empty_usb_hubs()
    if not hubs:
        print("[INFO] No empty USB hubs found; nothing to power-cycle.")
        return 0

    print(f"[INFO] Power-cycling empty hub(s): {', '.join(hubs)} (sudo required)")
    script = "set -e\n"
    for h in hubs:
        auth = f"/sys/bus/usb/devices/{h}/authorized"
        script += f"echo 0 > {auth}\nsleep 1\necho 1 > {auth}\nsleep 2\n"
    rc = subprocess.run(["sudo", "sh", "-c", script]).returncode
    if rc != 0:
        print("[WARN] sudo failed; retry to enter your password.")
        return rc

    print("[INFO] Re-scanning capture boxes...")
    time.sleep(1.0)
    boxes = detect_capture_devices()
    print(f"[INFO] Capture boxes: {boxes if boxes else 'still none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
