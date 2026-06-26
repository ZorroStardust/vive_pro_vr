import sys

def main() -> int:
    try:
        from mujoco.glfw import glfw
    except Exception as exc:
        print(f"[ERROR] Failed to import mujoco.glfw: {exc}", file=sys.stderr)
        return 2

    if not glfw.init():
        print("[ERROR] Failed to initialize GLFW.", file=sys.stderr)
        return 1
    try:
        version = glfw.get_version_string()
        if isinstance(version, bytes):
            version = version.decode(errors="replace")
        print(f"GLFW version: {version}")
        monitors = glfw.get_monitors()
        print(f"Found {len(monitors)} monitor(s)")
        for i, mon in enumerate(monitors):
            name = glfw.get_monitor_name(mon)
            mode = glfw.get_video_mode(mon)
            print(f"[{i}] {name}: {mode.size.width}x{mode.size.height} @ {mode.refresh_rate} Hz")
    finally:
        glfw.terminate()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
