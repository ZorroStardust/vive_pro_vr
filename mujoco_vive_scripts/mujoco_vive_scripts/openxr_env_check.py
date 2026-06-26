import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd):
    print(f"$ {' '.join(cmd)}")
    try:
        cp = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(cp.stdout.rstrip())
        return cp.returncode
    except FileNotFoundError:
        print(f"[ERROR] Command not found: {cmd[0]}")
        return 127


def main() -> int:
    print("=== OpenXR / Monado environment check ===")
    print(f"XDG_SESSION_TYPE={os.environ.get('XDG_SESSION_TYPE')}")
    print(f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}")
    print(f"DISPLAY={os.environ.get('DISPLAY')}")
    print(f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR')}")
    print(f"XR_RUNTIME_JSON={os.environ.get('XR_RUNTIME_JSON')}")

    xr_json = os.environ.get("XR_RUNTIME_JSON", "/usr/share/openxr/1/openxr_monado.json")
    print(f"Runtime JSON exists: {Path(xr_json).exists()} -> {xr_json}")

    for exe in ["monado-service", "monado-cli", "hello_xr", "vulkaninfo"]:
        path = shutil.which(exe)
        print(f"{exe}: {path or '<not found>'}")

    print("\n=== USB devices ===")
    run(["bash", "-lc", "lsusb | grep -iE '0bb4|28de|vive|htc|valve' || true"])

    print("\n=== User groups ===")
    run(["bash", "-lc", "groups"])

    print("\n=== Optional pyopenxr check ===")
    try:
        import xr  # type: ignore
        print("pyopenxr import: OK")
        props = xr.enumerate_instance_extension_properties()
        names = [p.extension_name.decode() for p in props]
        for name in names:
            if "opengl" in name.lower() or "vulkan" in name.lower():
                print(f"  extension: {name}")
        if getattr(xr, "KHR_OPENGL_ENABLE_EXTENSION_NAME", None) in names:
            print("XR_KHR_opengl_enable: available")
        else:
            print("XR_KHR_opengl_enable: not found in pyopenxr enumeration")
    except Exception as exc:
        print(f"pyopenxr import/check skipped: {exc}")
        print("Install optional deps with: pip install -r requirements-openxr-python.txt")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
