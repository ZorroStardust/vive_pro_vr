"""Live stereo endoscope video to OpenXR (VIVE Pro) or a desktop window.

Captures left/right frames from two V4L2 capture boxes
(:mod:`mujoco_vive_scripts.v4l2_capture`) and renders them as textured
quads into the OpenXR swapchain via the shared EGL/GLFW sinks.

Features:
  * fit modes (fit-width / fit-height / stretch / 1:1), cycled with ``f``
  * digital zoom (``z`` in / ``x`` out, ``1`` reset)
  * stereo scene shift (``a`` farther / ``d`` nearer), eye swap (``o``),
    mono-to-both-eyes (``p``)
  * calibration pixel offsets via ``calibration.toml`` + ``s`` to save
  * ``--screen`` desktop-window mode for debugging without Monado/HMD
  * ``--test-card`` synthetic pattern to verify the GL path without cameras
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from .xr_common import (
    RuntimeComfortState,
    OpenXRSink,
    ScreenSink,
    _StdinReader,
    _get_gl,
    _handle_input,
    add_calibration_args,
    add_comfort_args,
    add_screen_args,
    make_sink,
)
from .config_util import (
    Calibration,
    ComfortConfig,
    _default_config_path,
    load_calibration,
    load_comfort,
    save_full_config,
)
from .v4l2_capture import StereoCapture

HINT = """
Endoscope Live Video Controls
────────────────────────────────
 z / x   digital zoom in / out (x down to 0.1 shrinks the image so the
         whole frame stays visible; z up to 10.0 magnifies the centre)
 1       reset zoom to 1.0

 f       cycle fit mode: fit-width → fit-height → stretch → 1:1

 a / d   shift stereo images farther / nearer (less/more crossed)
 p       mono-to-both-eyes diagnostic mode
 o       swap left/right eyes

 s       save calibration + comfort to calibration.toml
 q / esc quit
"""

FIT_MODES = ("fit-width", "fit-height", "stretch", "1:1")


def _make_test_card(view_index: int, width: int, height: int) -> np.ndarray:
    """Static checkerboard + border used to validate the GL path without cameras."""
    cell = 32
    ys, xs = np.mgrid[0:height, 0:width]
    check = ((xs // cell) + (ys // cell)) % 2 == 0

    img = np.zeros((height, width, 3), dtype=np.uint8)
    if view_index == 0:
        img[..., 0] = 200
        img[..., 1] = 90
        img[..., 2] = 60
    else:
        img[..., 0] = 60
        img[..., 1] = 110
        img[..., 2] = 210
    img[check] //= 3

    bw = 12
    img[:bw, :] = 255
    img[-bw:, :] = 255
    img[:, :bw] = 255
    img[:, -bw:] = 255

    cy, cx = height // 2, width // 2
    r = min(height, width) // 12
    img[cy - 2 : cy + 3, cx - r : cx + r] = 255
    img[cy - r : cy + r, cx - 2 : cx + 3] = 255
    return img


class VideoStereoRenderer:
    """Renders the latest captured left/right RGB frames as textured quads.

    One GL texture per eye, uploaded only when a new frame arrives for that
    source.  The quad geometry encodes fit mode, digital zoom, calibration
    offsets and the comfort scene shift; immediate-mode GL works because the
    shared EGL/GLFW contexts are compatibility profile.
    """

    def __init__(
        self,
        width: int,
        height: int,
        fit: str = "fit-width",
        flip_h: bool = False,
        flip_v: bool = False,
        test_card: bool = False,
        capture: StereoCapture | None = None,
    ):
        self.vid_w = width
        self.vid_h = height
        self.fit = fit
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.test_card = test_card
        self.capture = capture

        self._textures: list[int] = [0, 0]
        self._tex_seq: list[int] = [-2, -2]
        self._test_frames: list[np.ndarray | None] = [None, None]
        self._last_vp = (0, 0)

        self.upload_ms = 0.0
        self.draw_ms = 0.0

    # -- GL resource setup --------------------------------------------------

    def ensure_textures(self) -> None:
        GL = _get_gl()
        for i in range(2):
            if self._textures[i]:
                continue
            tex = int(GL.glGenTextures(1))
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glTexImage2D(
                GL.GL_TEXTURE_2D, 0, GL.GL_RGB8,
                self.vid_w, self.vid_h, 0,
                GL.GL_RGB, GL.GL_UNSIGNED_BYTE, None,
            )
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self._textures[i] = tex

    def close(self) -> None:
        GL = _get_gl()
        for i in range(2):
            if self._textures[i]:
                try:
                    GL.glDeleteTextures(1, [self._textures[i]])
                except Exception:
                    pass
                self._textures[i] = 0

    # -- per-eye frame source -----------------------------------------------

    def _frame_for_eye(self, view_index: int, state: RuntimeComfortState):
        """Return (rgb_frame, texture_index) for the given eye."""
        if self.test_card:
            if self._test_frames[view_index] is None:
                self._test_frames[view_index] = _make_test_card(
                    view_index, self.vid_w, self.vid_h
                )
            return self._test_frames[view_index], view_index

        if self.capture is None:
            return None, 0

        left, right, seq, _t = self.capture.latest()

        if state.mono_to_both_eyes:
            return (left, 0)
        if view_index == 0:
            return (right, 1) if state.swap_eyes else (left, 0)
        return (left, 0) if state.swap_eyes else (right, 1)

    def _source_seq(self, tex_idx: int, state: RuntimeComfortState) -> int:
        if self.test_card or self.capture is None:
            return -1
        if state.mono_to_both_eyes:
            return self.capture.seqs()[0]
        return self.capture.seqs()[tex_idx]

    # -- quad geometry ------------------------------------------------------

    def _quad_geometry(self, vp_w: int, vp_h: int, state: RuntimeComfortState,
                       view_index: int, calib_x: int, calib_y: int):
        """Return (x0, y0, x1, y1, u0, v0, u1, v1) in viewport pixel space."""
        vid_aspect = self.vid_w / self.vid_h
        vp_aspect = vp_w / vp_h

        if self.fit == "stretch":
            bw, bh = vp_w, vp_h
        elif self.fit == "fit-height":
            bh = vp_h
            bw = vp_h * vid_aspect
        elif self.fit == "1:1":
            bw, bh = float(self.vid_w), float(self.vid_h)
        else:  # fit-width
            bw, bh = float(vp_w), vp_w / vid_aspect

        # Digital zoom:
        #   zoom >= 1: keep the quad at fit-size and crop the texture centre
        #              (image magnified in place).
        #   zoom <  1: shrink the quad, show the full frame (image shrinks,
        #              letterbox border grows) so the whole frame stays visible.
        zoom = float(state.zoom)
        if zoom >= 1.0:
            crop = min(1.0, 1.0 / zoom)
        else:
            bw *= zoom
            bh *= zoom
            crop = 1.0

        sign = -1.0 if state.invert_scene_shift else 1.0
        half_farther = 0.5 * state.scene_farther_px * sign
        shift_x = -half_farther if view_index == 0 else half_farther

        cx = vp_w / 2 + calib_x + shift_x
        cy = vp_h / 2 + calib_y

        x0 = cx - bw / 2
        x1 = cx + bw / 2
        y0 = cy - bh / 2
        y1 = cy + bh / 2

        u0 = (1 - crop) / 2
        u1 = (1 + crop) / 2
        # OpenGL texture v=0 holds the first uploaded row (= image top), while
        # the screen quad's y0 vertex is the bottom edge. Swap v by default so
        # the image displays upright.
        v0 = (1 + crop) / 2
        v1 = (1 - crop) / 2
        if self.flip_h:
            u0, u1 = u1, u0
        if self.flip_v:
            v0, v1 = v1, v0

        return x0, y0, x1, y1, u0, v0, u1, v1

    # -- render -------------------------------------------------------------

    def render_eye(self, view_index: int, state: RuntimeComfortState,
                   calib_left_x: int, calib_left_y: int,
                   calib_right_x: int, calib_right_y: int) -> None:
        GL = _get_gl()
        t0 = time.perf_counter()

        vp = GL.glGetIntegerv(GL.GL_VIEWPORT)
        vp_x, vp_y, vp_w, vp_h = int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3])
        self._last_vp = (vp_w, vp_h)
        if vp_w <= 0 or vp_h <= 0:
            return

        calib_x = calib_left_x if view_index == 0 else calib_right_x
        calib_y = calib_left_y if view_index == 0 else calib_right_y

        rgb, tex_idx = self._frame_for_eye(view_index, state)

        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        GL.glScissor(vp_x, vp_y, vp_w, vp_h)
        GL.glClearColor(0.02, 0.02, 0.02, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        if rgb is None:
            return

        tex = self._textures[tex_idx]
        if not tex:
            return

        src_seq = self._source_seq(tex_idx, state)

        upload_ms = 0.0
        if self._tex_seq[tex_idx] != src_seq:
            t_up = time.perf_counter()
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0,
                self.vid_w, self.vid_h,
                GL.GL_RGB, GL.GL_UNSIGNED_BYTE, rgb,
            )
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            self._tex_seq[tex_idx] = src_seq
            upload_ms = (time.perf_counter() - t_up) * 1000.0

        x0, y0, x1, y1, u0, v0, u1, v1 = self._quad_geometry(
            vp_w, vp_h, state, view_index, calib_x, calib_y
        )

        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glPushMatrix()
        GL.glLoadIdentity()
        GL.glOrtho(0, vp_w, 0, vp_h, -1, 1)
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glPushMatrix()
        GL.glLoadIdentity()

        GL.glEnable(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glColor3f(1.0, 1.0, 1.0)
        GL.glBegin(GL.GL_QUADS)
        GL.glTexCoord2f(u0, v0)
        GL.glVertex2f(x0, y0)
        GL.glTexCoord2f(u1, v0)
        GL.glVertex2f(x1, y0)
        GL.glTexCoord2f(u1, v1)
        GL.glVertex2f(x1, y1)
        GL.glTexCoord2f(u0, v1)
        GL.glVertex2f(x0, y1)
        GL.glEnd()
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glDisable(GL.GL_TEXTURE_2D)

        GL.glMatrixMode(GL.GL_PROJECTION)
        GL.glPopMatrix()
        GL.glMatrixMode(GL.GL_MODELVIEW)
        GL.glPopMatrix()

        self.draw_ms = (time.perf_counter() - t0) * 1000.0 - upload_ms
        self.upload_ms = upload_ms


def _cycle_fit(renderer: VideoStereoRenderer) -> None:
    idx = FIT_MODES.index(renderer.fit)
    renderer.fit = FIT_MODES[(idx + 1) % len(FIT_MODES)]


def _print_state(renderer: VideoStereoRenderer, comfort: RuntimeComfortState,
                 capture: StereoCapture | None, capture_stats: dict) -> None:
    if capture is None:
        cam = "TEST-CARD"
    else:
        seq_l, seq_r = capture.seqs()
        cam = f"L{seq_l:5d} R{seq_r:5d}"
    print(
        f"\r[LIVE] {cam}  "
        f"FIT={renderer.fit:<10} "
        f"ZOOM={comfort.zoom:.2f}  "
        f"SF={comfort.scene_farther_px:+.1f}  "
        f"MONO={'on' if comfort.mono_to_both_eyes else 'off '}  "
        f"SWAP={'on' if comfort.swap_eyes else 'off '}  "
        f"VP={renderer._last_vp}  "
        f"UPL={renderer.upload_ms:6.2f}ms  DRAW={renderer.draw_ms:6.2f}ms   ",
        end="\r",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    calib = load_calibration()
    comfort = load_comfort()

    parser = argparse.ArgumentParser(
        description="Live stereo endoscope video to VIVE Pro (OpenXR) or a desktop window."
    )
    parser.add_argument("--left-dev", default="/dev/video0")
    parser.add_argument("--right-dev", default="/dev/video2")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--fourcc", choices=("YUYV", "NV12"), default="YUYV")
    parser.add_argument(
        "--fit", choices=FIT_MODES, default="fit-width",
        help="Aspect adaptation: fit-width letterboxes vertically (default).",
    )
    parser.add_argument("--flip-h", action="store_true", help="Mirror both eyes horizontally.")
    parser.add_argument(
        "--flip-v",
        action="store_true",
        help="Mirror both eyes vertically. Image is upright by default; use this only if the endoscope source itself is flipped.",
    )
    parser.add_argument(
        "--test-card", action="store_true",
        help="Render synthetic patterns instead of capturing (no cameras needed).",
    )
    parser.add_argument("--print-every", type=float, default=2.0)
    parser.add_argument("--max-frames", type=int, default=0)

    add_calibration_args(parser, calib)
    add_comfort_args(parser, comfort)
    add_screen_args(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    mode = "SCREEN (GLFW window)" if args.screen else "OpenXR Stereo"
    print(f"[INFO] Endoscope Live Video — {mode}")
    print(f"[INFO] Capture: {args.left_dev} + {args.right_dev} "
          f"{args.width}x{args.height}@{args.fps} {args.fourcc}")
    cfg_path = _default_config_path()
    if cfg_path.exists():
        print(f"[INFO] Loaded comfort from {cfg_path}: "
              f"zoom={args.zoom:.2f} scene_farther_px={args.scene_farther_px:+.1f} "
              f"swap={'on' if args.swap_eyes else 'off'} "
              f"mono={'on' if args.mono_to_both_eyes else 'off'}"
              f"  (press s to re-save)")

    if args.test_card:
        capture = None
        print("[INFO] TEST-CARD mode: no camera capture.")
    else:
        capture = StereoCapture(
            args.left_dev, args.right_dev,
            args.width, args.height,
            fps=args.fps, fourcc=args.fourcc,
        )
        capture.start()

    comfort = RuntimeComfortState(
        swap_eyes=args.swap_eyes,
        mono_to_both_eyes=args.mono_to_both_eyes,
        scene_farther_px=args.scene_farther_px,
        scene_shift_step_px=args.scene_shift_step_px,
        max_abs_scene_shift_px=args.max_scene_shift_px,
        invert_scene_shift=args.invert_scene_shift,
        zoom=args.zoom,
        zoom_step=args.zoom_step,
    )
    comfort.clamp()

    print(HINT)

    sink = make_sink(args)
    stdin_reader = _StdinReader()
    renderer = None
    last = time.perf_counter()
    frames = 0
    running = True

    try:
        with sink as s:
            s.make_current()

            if not args.test_card and capture is not None:
                time.sleep(1.0)
                stats = capture.stats()
                print(f"[INFO] Capture status: L={stats['status'][0]} | R={stats['status'][1]}")
                if stats["errors"][0]:
                    print(f"[WARN] Left capture device ({args.left_dev}) has errors; showing black/black frame.")
                if stats["errors"][1]:
                    print(f"[WARN] Right capture device ({args.right_dev}) has errors; showing black/black frame.")

            renderer = VideoStereoRenderer(
                args.width, args.height,
                fit=args.fit,
                flip_h=args.flip_h,
                flip_v=args.flip_v,
                test_card=args.test_card,
                capture=capture,
            )
            renderer.ensure_textures()

            for _frame_index, frame_state in enumerate(s.frame_loop()):
                if not running:
                    break

                for view_index, _view in enumerate(s.view_loop(frame_state)):
                    renderer.render_eye(
                        view_index, comfort,
                        args.calib_left_x, args.calib_left_y,
                        args.calib_right_x, args.calib_right_y,
                    )

                frames += 1
                now = time.perf_counter()
                if now - last >= args.print_every:
                    stats = capture.stats() if capture is not None else {}
                    _print_state(renderer, comfort, capture, stats)
                    frames = 0
                    last = now

                def _unhandled_cb(ch: str) -> bool:
                    if ch == "f":
                        _cycle_fit(renderer)
                        return True
                    return True

                running = _handle_input(stdin_reader, comfort, _unhandled_cb)

                if args.max_frames > 0 and _frame_index >= args.max_frames:
                    break
    finally:
        if renderer is not None:
            renderer.close()
        if capture is not None:
            capture.stop()
        stdin_reader.restore()

    print("\n[RESULT] Final state:")
    print(f"  fit={renderer.fit if renderer else '?'} zoom={comfort.zoom:.2f} "
          f"scene_farther_px={comfort.scene_farther_px:+.1f}")

    if args.save:
        cal = Calibration(
            left_x=args.calib_left_x,
            left_y=args.calib_left_y,
            right_x=args.calib_right_x,
            right_y=args.calib_right_y,
        )
        comf = ComfortConfig(
            mono_to_both_eyes=comfort.mono_to_both_eyes,
            swap_eyes=comfort.swap_eyes,
            scene_farther_px=comfort.scene_farther_px,
            scene_shift_step_px=comfort.scene_shift_step_px,
            max_abs_scene_shift_px=comfort.max_abs_scene_shift_px,
            invert_scene_shift=comfort.invert_scene_shift,
            zoom=comfort.zoom,
            zoom_step=comfort.zoom_step,
            clear_r=0.02,
            clear_g=0.02,
            clear_b=0.02,
        )
        save_full_config(cal, comf, _default_config_path())
        print(f"[INFO] Saved to {_default_config_path()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
