from __future__ import annotations

import argparse
import fcntl
import os
import select
import sys
import termios
import time
from dataclasses import dataclass, field

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
os.environ.setdefault("MUJOCO_GL", "egl")

import xr

_REQUIRED_EXTENSIONS = (
    "XR_KHR_opengl_enable",
    "XR_MNDX_egl_enable",
)

HINT = """
Crosshair Calibration Controls
────────────────────────────────
 h / l    shift left-eye  crosshair ← →
 j / k    shift right-eye crosshair ← →
 y / u    shift left-eye  crosshair ↓ ↑
 n / m    shift right-eye crosshair ↓ ↑
 r        reset all offsets to zero
 s        save current offsets to calibration.toml
 q / esc  quit (dumps current calibration)
"""


def _extension_name_to_str(name) -> str:
    if isinstance(name, bytes):
        return name.decode()
    return str(name)


@dataclass
class CrosshairRenderer:
    offset_left_x: int = 0
    offset_left_y: int = 0
    offset_right_x: int = 0
    offset_right_y: int = 0

    def _viewport_w_h(self):
        from OpenGL import GL

        vp = GL.glGetIntegerv(GL.GL_VIEWPORT)
        return int(vp[2]), int(vp[3])

    def _normalized_offset(self, px: int, ref: int) -> float:
        if ref <= 0:
            return 0.0
        return (px / (ref * 0.5)) * 2.0

    def render(self, view_index: int):
        from OpenGL import GL

        w, h = self._viewport_w_h()

        if view_index == 0:
            ox = self._normalized_offset(self.offset_left_x, w)
            oy = self._normalized_offset(self.offset_left_y, h)
        else:
            ox = self._normalized_offset(self.offset_right_x, w)
            oy = self._normalized_offset(self.offset_right_y, h)

        GL.glClearColor(0.0, 0.0, 0.0, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)

        GL.glDisable(GL.GL_DEPTH_TEST)

        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glPushMatrix()
        GL.glLoadIdentity()
        GL.glOrtho(-1, 1, -1, 1, -1, 1)
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glPushMatrix()
        GL.glLoadIdentity()

        GL.glColor3f(1.0, 1.0, 1.0)
        GL.glLineWidth(2.0)

        half = 0.04
        GL.glBegin(GL.GL_LINES)
        GL.glVertex2f(-half + ox, oy)
        GL.glVertex2f(half + ox, oy)
        GL.glVertex2f(ox, -half + oy)
        GL.glVertex2f(ox, half + oy)
        GL.glEnd()

        GL.glLineWidth(1.0)
        GL.glColor3f(0.3, 0.3, 0.3)

        gap = 0.015
        seg = 0.008
        tick_half = 0.95

        for ty in (-tick_half, 0.0, tick_half):
            for tx in (-tick_half, tick_half):
                GL.glBegin(GL.GL_LINES)
                GL.glVertex2f(tx - seg, ty)
                GL.glVertex2f(tx + seg, ty)
                GL.glEnd()

        for tx in (-tick_half, 0.0, tick_half):
            for ty in (-tick_half, tick_half):
                GL.glBegin(GL.GL_LINES)
                GL.glVertex2f(tx, ty - seg)
                GL.glVertex2f(tx, ty + seg)
                GL.glEnd()

        GL.glColor3f(0.15, 0.15, 0.15)
        GL.glBegin(GL.GL_LINES)
        GL.glVertex2f(-1, 0)
        GL.glVertex2f(-gap, 0)
        GL.glVertex2f(gap, 0)
        GL.glVertex2f(1, 0)
        GL.glVertex2f(0, -1)
        GL.glVertex2f(0, -gap)
        GL.glVertex2f(0, gap)
        GL.glVertex2f(0, 1)
        GL.glEnd()

        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glPopMatrix()
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glPopMatrix()

        GL.glEnable(GL.GL_DEPTH_TEST)


class _StdinReader:
    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        self._old_flags = fcntl.fcntl(self._fd, fcntl.F_GETFL)
        new = termios.tcgetattr(self._fd)
        new[3] = new[3] & ~(termios.ECHO | termios.ICANON)
        termios.tcsetattr(self._fd, termios.TCSANOW, new)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, self._old_flags | os.O_NONBLOCK)

    def restore(self):
        termios.tcsetattr(self._fd, termios.TCSANOW, self._old)
        fcntl.fcntl(self._fd, fcntl.F_SETFL, self._old_flags)

    def read_key(self) -> str | None:
        if select.select([sys.stdin], [], [], 0)[0]:
            try:
                data = os.read(self._fd, 16)
                return data.decode(errors="replace")
            except (OSError, BlockingIOError):
                pass
        return None


def _handle_input(reader: _StdinReader, renderer: CrosshairRenderer) -> bool:
    key = reader.read_key()
    if key is None:
        return True

    ch = key[-1] if key else ""

    changed = False
    if ch in ("\x1b", "q"):
        print("\n[INFO] Quit requested.")
        return False

    step = 1
    if ch == "h":
        renderer.offset_left_x -= step
        changed = True
    elif ch == "l":
        renderer.offset_left_x += step
        changed = True
    elif ch == "j":
        renderer.offset_right_x -= step
        changed = True
    elif ch == "k":
        renderer.offset_right_x += step
        changed = True
    elif ch == "y":
        renderer.offset_left_y += step
        changed = True
    elif ch == "u":
        renderer.offset_left_y -= step
        changed = True
    elif ch == "n":
        renderer.offset_right_y += step
        changed = True
    elif ch == "m":
        renderer.offset_right_y -= step
        changed = True
    elif ch == "r":
        renderer.offset_left_x = 0
        renderer.offset_left_y = 0
        renderer.offset_right_x = 0
        renderer.offset_right_y = 0
        changed = True
    elif ch == "s":
        from .config_util import Calibration, save_calibration, _default_config_path

        cal = Calibration(
            left_x=renderer.offset_left_x,
            left_y=renderer.offset_left_y,
            right_x=renderer.offset_right_x,
            right_y=renderer.offset_right_y,
        )
        cfg_path = _default_config_path()
        save_calibration(cal)
        print(f"\r[SAVED] {cfg_path}", flush=True)

    if changed:
        print(
            f"\r[OFFSETS] L=({renderer.offset_left_x:+d}, {renderer.offset_left_y:+d})  "
            f"R=({renderer.offset_right_x:+d}, {renderer.offset_right_y:+d})",
            end="",
            flush=True,
        )

    return True


def parse_args():
    from .config_util import load_calibration

    calib = load_calibration()

    parser = argparse.ArgumentParser(
        description="OpenXR crosshair calibration tool — same crosshair to both eyes."
    )
    parser.add_argument(
        "--print-every",
        type=float,
        default=2.0,
        help="FPS print interval in seconds.",
    )
    parser.add_argument(
        "--offset-left-x",
        type=int,
        default=calib.left_x if calib else 0,
        help="Initial left-eye horizontal offset in pixels.",
    )
    parser.add_argument(
        "--offset-left-y",
        type=int,
        default=calib.left_y if calib else 0,
        help="Initial left-eye vertical offset in pixels.",
    )
    parser.add_argument(
        "--offset-right-x",
        type=int,
        default=calib.right_x if calib else 0,
        help="Initial right-eye horizontal offset in pixels.",
    )
    parser.add_argument(
        "--offset-right-y",
        type=int,
        default=calib.right_y if calib else 0,
        help="Initial right-eye vertical offset in pixels.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save calibration to config file on exit.",
    )
    parser.add_argument(
        "--save-on-quit",
        dest="save",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def check_openxr():
    names = {
        _extension_name_to_str(ext.extension_name)
        for ext in xr.enumerate_instance_extension_properties()
    }
    for ext in _REQUIRED_EXTENSIONS:
        if ext not in names:
            raise RuntimeError(f"{ext} is not available")
    print("[INFO] Required OpenXR extensions available:")
    for ext in _REQUIRED_EXTENSIONS:
        print(f"       {ext}")


def main() -> int:
    args = parse_args()

    from .xr_mujoco_opengl import _NvidiaEGLContextProvider

    print("[INFO] Crosshair Calibration — OpenXR")
    print("[INFO] Both eyes receive the same crosshair pattern.")
    print("[INFO] Use keyboard to align left/right crosshairs.")

    if any((args.offset_left_x, args.offset_left_y, args.offset_right_x, args.offset_right_y)):
        from .config_util import _default_config_path
        src = _default_config_path()
        label = "config" if src.exists() else "CLI"
        print(f"[INFO] Loaded calibration ({label}): "
              f"L=({args.offset_left_x:+d}, {args.offset_left_y:+d})  "
              f"R=({args.offset_right_x:+d}, {args.offset_right_y:+d})")

    print(HINT)

    provider = _NvidiaEGLContextProvider()
    check_openxr()

    from xr.utils.gl import ContextObject

    renderer = CrosshairRenderer(
        offset_left_x=args.offset_left_x,
        offset_left_y=args.offset_left_y,
        offset_right_x=args.offset_right_x,
        offset_right_y=args.offset_right_y,
    )

    stdin_reader = _StdinReader()
    start = time.perf_counter()
    last = start
    frames = 0
    running = True

    try:
        with ContextObject(
            context_provider=provider,
            instance_create_info=xr.InstanceCreateInfo(
                enabled_extension_names=list(_REQUIRED_EXTENSIONS),
            ),
        ) as xr_context:
            print("[INFO] Session started. Adjust offsets until crosshairs fuse.")
            print(f"[OFFSETS] L=({renderer.offset_left_x:+d}, {renderer.offset_left_y:+d})  "
                  f"R=({renderer.offset_right_x:+d}, {renderer.offset_right_y:+d})")

            for _frame_index, frame_state in enumerate(xr_context.frame_loop()):
                if not running:
                    break

                for view_index, _view in enumerate(xr_context.view_loop(frame_state)):
                    renderer.render(view_index)

                frames += 1
                now = time.perf_counter()
                if now - last >= args.print_every:
                    print(
                        f"\r[INFO] FPS: {frames / (now - last):.1f}  "
                        f"L=({renderer.offset_left_x:+d}, {renderer.offset_left_y:+d})  "
                        f"R=({renderer.offset_right_x:+d}, {renderer.offset_right_y:+d})",
                    )
                    frames = 0
                    last = now

                running = _handle_input(stdin_reader, renderer)

    finally:
        provider.destroy()
        stdin_reader.restore()
        print("\n[RESULT] Calibration offsets:")
        print(f"  Left  eye: dx={renderer.offset_left_x:+d}, dy={renderer.offset_left_y:+d}")
        print(f"  Right eye: dx={renderer.offset_right_x:+d}, dy={renderer.offset_right_y:+d}")

        if args.save:
            from .config_util import Calibration, save_calibration, _default_config_path

            cal = Calibration(
                left_x=renderer.offset_left_x,
                left_y=renderer.offset_left_y,
                right_x=renderer.offset_right_x,
                right_y=renderer.offset_right_y,
            )
            cfg_path = _default_config_path()
            save_calibration(cal)
            print(f"[INFO] Saved calibration to {cfg_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
