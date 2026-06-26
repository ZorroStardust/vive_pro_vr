from __future__ import annotations
import argparse, os, time
import glfw as _glfw
from OpenGL import GL
import xr
from xr.utils.gl import ContextObject
from xr.utils.gl.glfw_util import GLFWOffscreenContextProvider

def main() -> int:
    for _k in list(os.environ):
        if "WAYLAND" in _k:
            del os.environ[_k]
    _glfw.init_hint(_glfw.PLATFORM, _glfw.PLATFORM_X11)

    p = argparse.ArgumentParser()
    p.add_argument("--max-frames", type=int, default=600, help="0 means run forever")
    p.add_argument("--print-every", type=float, default=2.0)
    args = p.parse_args()

    names = {e.extension_name.decode() for e in xr.enumerate_instance_extension_properties()}
    if xr.KHR_OPENGL_ENABLE_EXTENSION_NAME not in names:
        raise RuntimeError("XR_KHR_opengl_enable is not available")

    frame_count = 0
    last = time.perf_counter()
    with ContextObject(
        context_provider=GLFWOffscreenContextProvider(),
        instance_create_info=xr.InstanceCreateInfo(
            enabled_extension_names=[xr.KHR_OPENGL_ENABLE_EXTENSION_NAME],
        ),
    ) as context:
        print("[INFO] pyopenxr OpenGL session started. Ctrl+C to stop.")
        for frame_index, frame_state in enumerate(context.frame_loop()):
            for view_index, view in enumerate(context.view_loop(frame_state)):
                if view_index == 0:
                    GL.glClearColor(1.0, 0.15, 0.15, 1.0)
                else:
                    GL.glClearColor(0.15, 0.25, 1.0, 1.0)
                GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
            frame_count += 1
            now = time.perf_counter()
            if now - last >= args.print_every:
                print(f"[INFO] FPS: {frame_count/(now-last):.1f}")
                frame_count, last = 0, now
            if args.max_frames > 0 and frame_index + 1 >= args.max_frames:
                break
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
