from __future__ import annotations
import argparse
import time

from .xr_common import _get_gl, add_screen_args, make_sink


def clear_eye(view_index: int) -> None:
    GL = _get_gl()
    GL.glPushAttrib(GL.GL_ALL_ATTRIB_BITS)
    GL.glDisable(GL.GL_DEPTH_TEST)
    GL.glMatrixMode(GL.GL_PROJECTION)
    GL.glPushMatrix()
    GL.glLoadIdentity()
    GL.glOrtho(0, 1, 0, 1, -1, 1)
    GL.glMatrixMode(GL.GL_MODELVIEW)
    GL.glPushMatrix()
    GL.glLoadIdentity()

    if view_index == 0:
        GL.glColor3f(1.0, 0.15, 0.15)
    else:
        GL.glColor3f(0.15, 0.25, 1.0)

    GL.glRectf(0, 0, 1, 1)

    GL.glMatrixMode(GL.GL_PROJECTION)
    GL.glPopMatrix()
    GL.glMatrixMode(GL.GL_MODELVIEW)
    GL.glPopMatrix()
    GL.glPopAttrib()


def main() -> int:
    p = argparse.ArgumentParser(
        description="OpenXR pink-world check: left eye red, right eye blue.  "
        "Use --screen for desktop window mode."
    )
    p.add_argument("--max-frames", type=int, default=600, help="0 means run forever")
    p.add_argument("--print-every", type=float, default=2.0)
    add_screen_args(p)
    args = p.parse_args()

    mode = "SCREEN (GLFW window)" if args.screen else "OpenXR"
    print(f"[INFO] Pink World Check — {mode}")

    sink = make_sink(args)
    frame_count = 0
    last = time.perf_counter()

    try:
        with sink as s:
            print("[INFO] Session started. Ctrl+C to stop.")
            for frame_index, frame_state in enumerate(s.frame_loop()):
                for view_index, _view in enumerate(s.view_loop(frame_state)):
                    clear_eye(view_index)

                frame_count += 1
                now = time.perf_counter()
                if now - last >= args.print_every:
                    print(f"[INFO] FPS: {frame_count / (now - last):.1f}")
                    frame_count, last = 0, now

                if args.max_frames > 0 and frame_index + 1 >= args.max_frames:
                    break
    except KeyboardInterrupt:
        print("\n[INFO] Stopped by user.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
