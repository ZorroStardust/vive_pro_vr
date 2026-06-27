from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
os.environ.setdefault("MUJOCO_GL", "egl")

import xr

from .xr_common import (
    _REQUIRED_EXTENSIONS,
    _StdinReader,
    _extension_name_to_str,
    check_openxr,
)
from .config_util import Calibration, _default_config_path, save_calibration


HINT = """
Crosshair Calibration Controls
────────────────────────────────
h / l shift left-eye crosshair ← →
j / k shift right-eye crosshair ← →

y / u shift left-eye crosshair ↓ ↑
n / m shift right-eye crosshair ↓ ↑

a / d move crosshair farther / nearer
e     reset stereo depth to zero

r     reset all offsets and depth to zero
s     save current offsets to calibration.toml
q / esc quit (dumps current calibration)
"""


@dataclass
class CrosshairRenderer:
    offset_left_x: int = 0
    offset_left_y: int = 0
    offset_right_x: int = 0
    offset_right_y: int = 0

    # Stereo depth control.
    #
    # depth_disparity_px > 0:
    #   left-eye crosshair moves right,
    #   right-eye crosshair moves left,
    #   usually perceived as nearer.
    #
    # depth_disparity_px < 0:
    #   left-eye crosshair moves left,
    #   right-eye crosshair moves right,
    #   usually perceived as farther.
    depth_disparity_px: float = 0.0

    depth_step_px: float = 1.0
    max_depth_disparity_px: float = 80.0
    invert_depth_sign: bool = False

    def _viewport_w_h(self) -> tuple[int, int]:
        from .xr_common import _get_gl
        GL = _get_gl()
        vp = GL.glGetIntegerv(GL.GL_VIEWPORT)
        return int(vp[2]), int(vp[3])

    def _normalized_offset(self, px: float, ref: int) -> float:
        if ref <= 0:
            return 0.0

        # glOrtho(-1, 1, -1, 1, -1, 1)
        # Total NDC width/height is 2.
        # 1 px = 2 / viewport_size.
        return px / (ref * 0.5)

    def clamp_depth(self) -> None:
        self.depth_disparity_px = max(
            -self.max_depth_disparity_px,
            min(self.max_depth_disparity_px, self.depth_disparity_px),
        )

    def reset_all(self) -> None:
        self.offset_left_x = 0
        self.offset_left_y = 0
        self.offset_right_x = 0
        self.offset_right_y = 0
        self.depth_disparity_px = 0.0

    def reset_depth(self) -> None:
        self.depth_disparity_px = 0.0

    def move_depth_nearer(self) -> None:
        self.depth_disparity_px += self.depth_step_px
        self.clamp_depth()

    def move_depth_farther(self) -> None:
        self.depth_disparity_px -= self.depth_step_px
        self.clamp_depth()

    def _effective_offsets_px(self, view_index: int) -> tuple[float, float]:
        sign = -1.0 if self.invert_depth_sign else 1.0
        half_depth = 0.5 * self.depth_disparity_px * sign

        if view_index == 0:
            return self.offset_left_x + half_depth, self.offset_left_y

        return self.offset_right_x - half_depth, self.offset_right_y

    def render(self, view_index: int) -> None:
        from .xr_common import _get_gl
        GL = _get_gl()

        w, h = self._viewport_w_h()
        px_x, px_y = self._effective_offsets_px(view_index)

        ox = self._normalized_offset(px_x, w)
        oy = self._normalized_offset(px_y, h)

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

        # Main white crosshair.
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

        # Corner/edge reference ticks.
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

        # Dim center axes.
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


def _print_state(prefix: str, renderer: CrosshairRenderer, end: str = "\n") -> None:
    print(
        f"{prefix} "
        f"L=({renderer.offset_left_x:+d}, {renderer.offset_left_y:+d}) "
        f"R=({renderer.offset_right_x:+d}, {renderer.offset_right_y:+d}) "
        f"DEPTH={renderer.depth_disparity_px:+.1f}px",
        end=end,
        flush=True,
    )


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

    elif ch == "a":
        renderer.move_depth_farther()
        changed = True

    elif ch == "d":
        renderer.move_depth_nearer()
        changed = True

    elif ch == "e":
        renderer.reset_depth()
        changed = True

    elif ch == "r":
        renderer.reset_all()
        changed = True

    elif ch == "s":
        cal = Calibration(
            left_x=renderer.offset_left_x,
            left_y=renderer.offset_left_y,
            right_x=renderer.offset_right_x,
            right_y=renderer.offset_right_y,
        )
        cfg_path = _default_config_path()
        save_calibration(cal)
        print(f"\r[SAVED] {cfg_path} offsets only", flush=True)

    if changed:
        _print_state("\r[OFFSETS]", renderer, end="")

    return True


def parse_args() -> argparse.Namespace:
    from .config_util import load_calibration
    calib = load_calibration()

    parser = argparse.ArgumentParser(
        description="OpenXR crosshair calibration tool with adjustable stereo depth."
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
        "--depth-px",
        type=float,
        default=0.0,
        help="Initial stereo depth disparity in pixels. Positive usually means nearer.",
    )
    parser.add_argument(
        "--depth-step-px",
        type=float,
        default=1.0,
        help="Stereo depth adjustment step in pixels.",
    )
    parser.add_argument(
        "--max-depth-px",
        type=float,
        default=80.0,
        help="Maximum absolute stereo depth disparity in pixels.",
    )
    parser.add_argument(
        "--invert-depth-sign",
        action="store_true",
        help="Invert stereo depth sign if near/far direction feels reversed.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save calibration offsets to config file on exit.",
    )
    parser.add_argument(
        "--save-on-quit",
        dest="save",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from .xr_common import _NvidiaEGLContextProvider

    print("[INFO] Crosshair Calibration — OpenXR")
    print("[INFO] Both eyes receive a 2D crosshair pattern.")
    print("[INFO] Calibration offsets and stereo depth are controlled separately.")

    if any(
        (
            args.offset_left_x,
            args.offset_left_y,
            args.offset_right_x,
            args.offset_right_y,
        )
    ):
        src = _default_config_path()
        label = "config" if src.exists() else "CLI"

        print(
            f"[INFO] Loaded calibration ({label}): "
            f"L=({args.offset_left_x:+d}, {args.offset_left_y:+d}) "
            f"R=({args.offset_right_x:+d}, {args.offset_right_y:+d})"
        )

    print(HINT)

    provider = _NvidiaEGLContextProvider()
    check_openxr()

    from xr.utils.gl import ContextObject

    renderer = CrosshairRenderer(
        offset_left_x=args.offset_left_x,
        offset_left_y=args.offset_left_y,
        offset_right_x=args.offset_right_x,
        offset_right_y=args.offset_right_y,
        depth_disparity_px=args.depth_px,
        depth_step_px=args.depth_step_px,
        max_depth_disparity_px=args.max_depth_px,
        invert_depth_sign=args.invert_depth_sign,
    )

    renderer.clamp_depth()

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
            print("[INFO] Session started.")
            print("[INFO] First calibrate offsets at DEPTH=+0.0px, then adjust depth.")
            _print_state("[OFFSETS]", renderer)

            for _frame_index, frame_state in enumerate(xr_context.frame_loop()):
                if not running:
                    break

                for view_index, _view in enumerate(xr_context.view_loop(frame_state)):
                    renderer.render(view_index)

                frames += 1

                now = time.perf_counter()

                if now - last >= args.print_every:
                    fps = frames / (now - last)

                    print(
                        f"\r[INFO] FPS: {fps:.1f} "
                        f"L=({renderer.offset_left_x:+d}, {renderer.offset_left_y:+d}) "
                        f"R=({renderer.offset_right_x:+d}, {renderer.offset_right_y:+d}) "
                        f"DEPTH={renderer.depth_disparity_px:+.1f}px",
                    )

                    frames = 0
                    last = now

                running = _handle_input(stdin_reader, renderer)

    finally:
        provider.destroy()
        stdin_reader.restore()

    print("\n[RESULT] Calibration offsets:")
    print(f"  Left eye:  dx={renderer.offset_left_x:+d}, dy={renderer.offset_left_y:+d}")
    print(f"  Right eye: dx={renderer.offset_right_x:+d}, dy={renderer.offset_right_y:+d}")
    print(f"  Stereo depth disparity: {renderer.depth_disparity_px:+.1f}px")

    if args.save:
        cal = Calibration(
            left_x=renderer.offset_left_x,
            left_y=renderer.offset_left_y,
            right_x=renderer.offset_right_x,
            right_y=renderer.offset_right_y,
        )
        cfg_path = _default_config_path()
        save_calibration(cal)
        print(f"[INFO] Saved calibration offsets to {cfg_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
