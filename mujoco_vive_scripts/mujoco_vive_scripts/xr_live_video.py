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
from .v4l2_capture import (
    StereoCapture,
    detect_capture_devices,
    link_budget_fourcc,
    usb_topology_report,
)

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

_YUV_VERTEX_SHADER = """
#version 120
varying vec2 v_texcoord;

void main() {
    gl_Position = ftransform();
    v_texcoord = gl_MultiTexCoord0.xy;
}
"""

_YUYV_FRAGMENT_SHADER = """
#version 120
uniform sampler2D u_yuyv;
uniform float u_video_width;
varying vec2 v_texcoord;

vec3 yuv_to_rgb(float y, float u, float v) {
    return clamp(vec3(
        y + 1.402000 * v,
        y - 0.344136 * u - 0.714136 * v,
        y + 1.772000 * u
    ), 0.0, 1.0);
}

void main() {
    float source_x = clamp(
        v_texcoord.x * u_video_width - 0.5, 0.0, u_video_width - 1.0
    );
    float pixel_x = clamp(floor(source_x), 0.0, u_video_width - 1.0);
    float next_x = clamp(pixel_x + 1.0, 0.0, u_video_width - 1.0);
    float pair_x = floor(pixel_x * 0.5);
    float next_pair_x = floor(next_x * 0.5);
    float pair_u = (pair_x + 0.5) / (u_video_width * 0.5);
    float next_pair_u = (next_pair_x + 0.5) / (u_video_width * 0.5);
    vec4 yuyv_sample = texture2D(u_yuyv, vec2(pair_u, v_texcoord.y));
    vec4 next_sample = texture2D(
        u_yuyv, vec2(next_pair_u, v_texcoord.y)
    );
    float y = mix(
        yuyv_sample.r, yuyv_sample.b, step(0.5, mod(pixel_x, 2.0))
    );
    float next_y = mix(
        next_sample.r, next_sample.b, step(0.5, mod(next_x, 2.0))
    );
    float fraction = clamp(fract(source_x), 0.0, 1.0);
    y = mix(y, next_y, fraction);
    float u = mix(yuyv_sample.g, next_sample.g, fraction)
              - (128.0 / 255.0);
    float v = mix(yuyv_sample.a, next_sample.a, fraction)
              - (128.0 / 255.0);
    gl_FragColor = vec4(yuv_to_rgb(y, u, v), 1.0);
}
"""

_NV12_FRAGMENT_SHADER = """
#version 120
uniform sampler2D u_y;
uniform sampler2D u_uv;
varying vec2 v_texcoord;

void main() {
    float y = texture2D(u_y, v_texcoord).r;
    vec2 chroma = texture2D(u_uv, v_texcoord).rg
                  - vec2(128.0 / 255.0);
    vec3 rgb = vec3(
        y + 1.402000 * chroma.y,
        y - 0.344136 * chroma.x - 0.714136 * chroma.y,
        y + 1.772000 * chroma.x
    );
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), 1.0);
}
"""


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
    """Render raw YUYV/NV12 capture frames with GPU color conversion.

    Capture threads publish immutable YUV bytes; this renderer uploads only
    a newly published frame and converts it in a fragment shader.  Test-card
    mode intentionally keeps the original RGB texture path.
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
        fourcc: str | None = None,
    ):
        self.vid_w = width
        self.vid_h = height
        self.fit = fit
        self.flip_h = flip_h
        self.flip_v = flip_v
        self.test_card = test_card
        self.capture = capture
        self.pixel_format = "RGB" if test_card else (
            fourcc or (capture.fourcc if capture is not None else "YUYV")
        )
        if self.pixel_format not in ("RGB", "YUYV", "NV12"):
            raise ValueError(f"Unsupported renderer pixel format: {self.pixel_format}")
        if self.pixel_format != "RGB" and (width % 2 or height % 2):
            raise ValueError(
                f"{self.pixel_format} rendering requires even dimensions, "
                f"got {width}x{height}"
            )

        self._textures: list[list[int]] = [[], []]
        self._tex_seq: list[int] = [-2, -2]
        self._test_frames: list[np.ndarray | None] = [None, None]
        self._program = 0
        self._uniforms: dict[str, int] = {}
        self._last_vp = (0, 0)

        self.upload_ms = 0.0
        self.draw_ms = 0.0

    # -- GL resource setup --------------------------------------------------

    @staticmethod
    def _allocate_texture(width: int, height: int, internal_format: int,
                          external_format: int, linear: bool) -> int:
        GL = _get_gl()
        tex = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D, 0, internal_format,
            width, height, 0, external_format, GL.GL_UNSIGNED_BYTE, None,
        )
        filtering = GL.GL_LINEAR if linear else GL.GL_NEAREST
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, filtering)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, filtering)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        return tex

    def _compile_yuv_shader(self) -> None:
        if self.pixel_format == "RGB" or self._program:
            return
        GL = _get_gl()
        from OpenGL.GL.shaders import compileProgram, compileShader

        fragment = (
            _YUYV_FRAGMENT_SHADER
            if self.pixel_format == "YUYV"
            else _NV12_FRAGMENT_SHADER
        )
        self._program = int(compileProgram(
            compileShader(_YUV_VERTEX_SHADER, GL.GL_VERTEX_SHADER),
            compileShader(fragment, GL.GL_FRAGMENT_SHADER),
        ))
        names = (
            ("u_yuyv", "u_video_width")
            if self.pixel_format == "YUYV"
            else ("u_y", "u_uv")
        )
        self._uniforms = {
            name: int(GL.glGetUniformLocation(self._program, name))
            for name in names
        }

    def ensure_textures(self) -> None:
        GL = _get_gl()
        self._compile_yuv_shader()
        for i in range(2):
            if self._textures[i]:
                continue
            if self.pixel_format == "RGB":
                self._textures[i] = [self._allocate_texture(
                    self.vid_w, self.vid_h, GL.GL_RGB8, GL.GL_RGB, True
                )]
            elif self.pixel_format == "YUYV":
                self._textures[i] = [self._allocate_texture(
                    self.vid_w // 2, self.vid_h,
                    GL.GL_RGBA8, GL.GL_RGBA, True,
                )]
            else:
                self._textures[i] = [
                    self._allocate_texture(
                        self.vid_w, self.vid_h, GL.GL_R8, GL.GL_RED, True
                    ),
                    self._allocate_texture(
                        self.vid_w // 2, self.vid_h // 2,
                        GL.GL_RG8, GL.GL_RG, True,
                    ),
                ]

    def close(self) -> None:
        GL = _get_gl()
        for i in range(2):
            for tex in self._textures[i]:
                try:
                    GL.glDeleteTextures(1, [tex])
                except Exception:
                    pass
            self._textures[i] = []
        if self._program:
            try:
                GL.glDeleteProgram(self._program)
            except Exception:
                pass
            self._program = 0

    # -- per-eye frame source -----------------------------------------------

    def _frame_for_eye(self, view_index: int, state: RuntimeComfortState):
        """Return ``(frame, texture_index, source_sequence)`` for one eye."""
        if self.test_card:
            if self._test_frames[view_index] is None:
                self._test_frames[view_index] = _make_test_card(
                    view_index, self.vid_w, self.vid_h
                )
            return self._test_frames[view_index], view_index, -1

        if self.capture is None:
            return None, 0, -1

        left, right, seq_left, seq_right = self.capture.latest()

        if state.mono_to_both_eyes:
            return left, 0, seq_left
        if view_index == 0:
            return (right, 1, seq_right) if state.swap_eyes else (
                left, 0, seq_left
            )
        return (left, 0, seq_left) if state.swap_eyes else (
            right, 1, seq_right
        )

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

    def _upload_frame(self, frame, tex_idx: int) -> None:
        """Upload RGB or raw YUV bytes without CPU color conversion."""
        GL = _get_gl()
        textures = self._textures[tex_idx]
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)

        if self.pixel_format == "RGB":
            GL.glBindTexture(GL.GL_TEXTURE_2D, textures[0])
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0,
                self.vid_w, self.vid_h,
                GL.GL_RGB, GL.GL_UNSIGNED_BYTE, frame,
            )
        elif self.pixel_format == "YUYV":
            expected = self.vid_w * self.vid_h * 2
            if len(frame) < expected:
                raise ValueError(
                    f"Short YUYV frame: {len(frame)} < {expected} bytes"
                )
            packed = np.frombuffer(frame, dtype=np.uint8, count=expected)
            GL.glBindTexture(GL.GL_TEXTURE_2D, textures[0])
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0,
                self.vid_w // 2, self.vid_h,
                GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, packed,
            )
        else:
            y_size = self.vid_w * self.vid_h
            uv_size = y_size // 2
            expected = y_size + uv_size
            if len(frame) < expected:
                raise ValueError(
                    f"Short NV12 frame: {len(frame)} < {expected} bytes"
                )
            planes = np.frombuffer(frame, dtype=np.uint8, count=expected)
            GL.glBindTexture(GL.GL_TEXTURE_2D, textures[0])
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0,
                self.vid_w, self.vid_h,
                GL.GL_RED, GL.GL_UNSIGNED_BYTE, planes[:y_size],
            )
            GL.glBindTexture(GL.GL_TEXTURE_2D, textures[1])
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D, 0, 0, 0,
                self.vid_w // 2, self.vid_h // 2,
                GL.GL_RG, GL.GL_UNSIGNED_BYTE, planes[y_size:],
            )

        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)

    def _bind_for_draw(self, tex_idx: int) -> None:
        GL = _get_gl()
        textures = self._textures[tex_idx]
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, textures[0])

        if self.pixel_format == "RGB":
            GL.glEnable(GL.GL_TEXTURE_2D)
            return

        GL.glUseProgram(self._program)
        if self.pixel_format == "YUYV":
            GL.glUniform1i(self._uniforms["u_yuyv"], 0)
            GL.glUniform1f(self._uniforms["u_video_width"], float(self.vid_w))
        else:
            GL.glUniform1i(self._uniforms["u_y"], 0)
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, textures[1])
            GL.glUniform1i(self._uniforms["u_uv"], 1)
            GL.glActiveTexture(GL.GL_TEXTURE0)

    def _unbind_after_draw(self) -> None:
        GL = _get_gl()
        if self.pixel_format == "RGB":
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            GL.glDisable(GL.GL_TEXTURE_2D)
            return

        if self.pixel_format == "NV12":
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glUseProgram(0)

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

        frame, tex_idx, src_seq = self._frame_for_eye(view_index, state)

        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_SCISSOR_TEST)
        GL.glScissor(vp_x, vp_y, vp_w, vp_h)
        GL.glClearColor(0.02, 0.02, 0.02, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)

        if frame is None:
            return

        if not self._textures[tex_idx]:
            return

        upload_ms = 0.0
        if self._tex_seq[tex_idx] != src_seq:
            t_up = time.perf_counter()
            self._upload_frame(frame, tex_idx)
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

        self._bind_for_draw(tex_idx)
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
        self._unbind_after_draw()

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
                 capture: StereoCapture | None, capture_stats: dict,
                 wall_dt: float) -> None:
    if capture is None:
        cam = "TEST-CARD"
    else:
        seq_l, seq_r = capture.seqs()
        cam = f"L{seq_l:5d} R{seq_r:5d}"
    timeouts = capture_stats.get("timeouts", [0, 0])
    drops = capture_stats.get("driver_drops", [0, 0])
    copy_ms = capture_stats.get("copy_ms", [0.0, 0.0])
    print(
        f"\r[LIVE] {cam}  DT={wall_dt:4.2f}s  TO={timeouts[0]}/{timeouts[1]}  "
        f"DROP={drops[0]}/{drops[1]}  "
        f"COPY={copy_ms[0]:4.1f}/{copy_ms[1]:4.1f}ms  "
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


def _resolve_capture_devices(left: str | None, right: str | None) -> tuple[str, str]:
    """Fill in missing device nodes by auto-detection, else old defaults."""
    if left is not None and right is not None:
        return left, right
    boxes = detect_capture_devices()
    if boxes:
        print(f"[INFO] Auto-detected capture box(es): {', '.join(boxes)}")
    if len(boxes) < 2:
        print("[WARN] Fewer than 2 capture boxes detected. USB topology:")
        for line in usb_topology_report():
            print(f"  {line}")
    if left is None:
        left = next((b for b in boxes if b != right), boxes[0] if boxes else None)
    if right is None:
        right = next((b for b in boxes if b != left), boxes[0] if boxes else None)
    if left is None:
        left = "/dev/video0"
        print("[WARN] No capture box auto-detected; falling back to /dev/video0 for left.")
    if right is None:
        right = "/dev/video2"
        print("[WARN] No capture box auto-detected; falling back to /dev/video2 for right.")
    return left, right


def parse_args() -> argparse.Namespace:
    calib = load_calibration()
    comfort = load_comfort()

    parser = argparse.ArgumentParser(
        description="Live stereo endoscope video to VIVE Pro (OpenXR) or a desktop window."
    )
    parser.add_argument(
        "--left-dev", default=None,
        help="Left capture device node (default: auto-detect capture boxes, skipping the VIVE HMD camera).",
    )
    parser.add_argument(
        "--right-dev", default=None,
        help="Right capture device node (default: auto-detect).",
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument(
        "--fourcc", choices=("auto", "YUYV", "NV12"), default="auto",
        help="Pixel format: 'auto' picks YUYV/NV12 from the shared USB link budget.",
    )
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
    args = parser.parse_args()
    if not args.test_card:
        args.left_dev, args.right_dev = _resolve_capture_devices(
            args.left_dev, args.right_dev
        )
        if args.fourcc == "auto":
            args.fourcc, reason = link_budget_fourcc(
                args.left_dev, args.right_dev,
                args.width, args.height, args.fps,
            )
            print(f"[INFO] {reason}")
    return args


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
                fourcc=args.fourcc,
            )
            renderer.ensure_textures()
            print(
                f"[INFO] Video conversion path: {renderer.pixel_format} "
                f"GPU fragment shader"
                if renderer.pixel_format != "RGB"
                else "[INFO] Video conversion path: RGB test-card texture"
            )

            last_frame_ts = time.perf_counter()
            for _frame_index, frame_state in enumerate(s.frame_loop()):
                if not running:
                    break

                iter_ts = time.perf_counter()
                iter_gap = iter_ts - last_frame_ts
                last_frame_ts = iter_ts
                if iter_gap > 0.030:
                    print(
                        f"\n[XR GAP] frame loop stalled {iter_gap * 1000:.0f}ms "
                        f"@t={iter_ts:.1f} (vsync 11ms; >30ms = compositor/GPU stall)"
                    )

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
                    _print_state(renderer, comfort, capture, stats, now - last)
                    frames = 0
                    last = now
                    if capture is not None:
                        for ts, eye, gap, to in capture.drain_gap_events():
                            print(
                                f"\n[CAP GAP] eye={'L' if eye == 0 else 'R'} "
                                f"{gap * 1000:.0f}ms @t={ts:.1f} "
                                f"select_timeouts_in_gap={to} "
                                f"(0=thread starved, >0=device silent)"
                            )

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
