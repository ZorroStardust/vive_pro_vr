"""Raw V4L2 MMAP capture for the surgical stereo endoscope.

Two UVC capture boxes (one per eye) stream YUYV/NV12 over USB3.  This module
implements the standard V4L2 MMAP pipeline (REQBUFS -> QUERYBUF -> mmap ->
QBUF -> STREAMON -> DQBUF/QBUF loop) with ctypes/select, matching the
repo's dependency-light ctypes style.

``StereoCapture`` runs one daemon thread per device.  Each thread converts
the raw frame to RGB with precomputed LUTs and publishes it to a
thread-safe ``latest`` slot; the renderer polls ``latest()`` on its own
schedule, so capture pacing and display pacing are decoupled.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import re
import select
import threading
import time

import numpy as np

# ---- ioctl plumbing -------------------------------------------------------

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, type_: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_MEMORY_MMAP = 1
V4L2_FIELD_NONE = 0

_FOURCCS = {
    "YUYV": ord("Y") | (ord("U") << 8) | (ord("Y") << 16) | (ord("V") << 24),
    "NV12": ord("N") | (ord("V") << 8) | (ord("1") << 16) | (ord("2") << 24),
}

_PIX_FIELD_OFFSETS = {
    "width": 0,
    "height": 4,
    "pixelformat": 8,
    "field": 12,
    "bytesperline": 16,
    "sizeimage": 20,
}


class _V4L2Format(ctypes.Structure):
    """v4l2_format with the union 8-byte aligned (kernel 6.8 headers)."""

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("pad", ctypes.c_uint32),
        ("pix", ctypes.c_uint8 * 200),
    ]


class _V4L2RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class _V4L2Buffer(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("tv_sec", ctypes.c_int64),
        ("tv_usec", ctypes.c_int64),
        ("tc_type", ctypes.c_uint32),
        ("tc_flags", ctypes.c_uint32),
        ("tc_frames", ctypes.c_uint8),
        ("tc_seconds", ctypes.c_uint8),
        ("tc_minutes", ctypes.c_uint8),
        ("tc_hours", ctypes.c_uint8),
        ("tc_userbits", ctypes.c_uint8 * 4),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m_offset", ctypes.c_uint64),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_uint32),
    ]


class _V4L2StreamParm(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("parm", ctypes.c_uint8 * 200),
    ]


# Ioctl numbers derive from the real ctypes struct sizes, matching the
# kernel ABI (Ubuntu 24.04 / kernel 6.8 headers: v4l2_format=208,
# v4l2_buffer=88, streamparm=204, requestbuffers=20).
VIDIOC_S_FMT = _ioc(_IOC_WRITE | _IOC_READ, "V", 5, ctypes.sizeof(_V4L2Format))
VIDIOC_G_FMT = _ioc(_IOC_WRITE | _IOC_READ, "V", 4, ctypes.sizeof(_V4L2Format))
VIDIOC_S_PARM = _ioc(_IOC_WRITE | _IOC_READ, "V", 22, ctypes.sizeof(_V4L2StreamParm))
VIDIOC_REQBUFS = _ioc(_IOC_WRITE | _IOC_READ, "V", 8, ctypes.sizeof(_V4L2RequestBuffers))
VIDIOC_QUERYBUF = _ioc(_IOC_WRITE | _IOC_READ, "V", 9, ctypes.sizeof(_V4L2Buffer))
VIDIOC_QBUF = _ioc(_IOC_WRITE | _IOC_READ, "V", 15, ctypes.sizeof(_V4L2Buffer))
VIDIOC_DQBUF = _ioc(_IOC_WRITE | _IOC_READ, "V", 17, ctypes.sizeof(_V4L2Buffer))
VIDIOC_STREAMON = _ioc(_IOC_WRITE, "V", 18, 4)
VIDIOC_STREAMOFF = _ioc(_IOC_WRITE, "V", 19, 4)


# ---- YUV -> RGB conversion ------------------------------------------------

_CR = None
_CGU = None
_CGV = None
_CB = None


def _build_luts():
    c = np.arange(256, dtype=np.int16) - 128
    return (
        np.round(1.402 * c).astype(np.int16),
        np.round(0.344136 * c).astype(np.int16),
        np.round(0.714136 * c).astype(np.int16),
        np.round(1.772 * c).astype(np.int16),
    )


def _luts():
    global _CR, _CGU, _CGV, _CB
    if _CR is None:
        _CR, _CGU, _CGV, _CB = _build_luts()
    return _CR, _CGU, _CGV, _CB


def yuyv_to_rgb(buf, width: int, height: int) -> np.ndarray:
    """Convert a packed YUYV buffer (w*h*2 bytes) to an (h, w, 3) RGB array."""
    cr, cgu, cgv, cb = _luts()
    a = np.frombuffer(buf, dtype=np.uint8, count=width * height * 2)
    yy = a[0::2].astype(np.int16).reshape(height, width)
    uv = a[1::2].reshape(height, width // 2, 2)
    u = uv[..., 0].astype(np.int16)
    v = uv[..., 1].astype(np.int16)

    r = cr[v]
    g = -cgu[u] - cgv[v]
    b = cb[u]

    out = np.empty((height, width, 3), dtype=np.uint8)
    out[..., 0][:, 0::2] = np.clip(yy[:, 0::2] + r, 0, 255)
    out[..., 0][:, 1::2] = np.clip(yy[:, 1::2] + r, 0, 255)
    out[..., 1][:, 0::2] = np.clip(yy[:, 0::2] + g, 0, 255)
    out[..., 1][:, 1::2] = np.clip(yy[:, 1::2] + g, 0, 255)
    out[..., 2][:, 0::2] = np.clip(yy[:, 0::2] + b, 0, 255)
    out[..., 2][:, 1::2] = np.clip(yy[:, 1::2] + b, 0, 255)
    return out


def nv12_to_rgb(buf, width: int, height: int) -> np.ndarray:
    """Convert a planar NV12 buffer (w*h*3/2 bytes) to an (h, w, 3) RGB array."""
    cr, cgu, cgv, cb = _luts()
    a = np.frombuffer(buf, dtype=np.uint8, count=width * height * 3 // 2)
    yy = a[: width * height].astype(np.int16).reshape(height, width)
    uv = a[width * height :].reshape(height // 2, width // 2, 2)
    u = np.repeat(uv[..., 0], 2, axis=0).astype(np.int16)
    v = np.repeat(uv[..., 1], 2, axis=0).astype(np.int16)

    r = cr[v]
    g = -cgu[u] - cgv[v]
    b = cb[u]

    out = np.empty((height, width, 3), dtype=np.uint8)
    out[..., 0][:, 0::2] = np.clip(yy[:, 0::2] + r, 0, 255)
    out[..., 0][:, 1::2] = np.clip(yy[:, 1::2] + r, 0, 255)
    out[..., 1][:, 0::2] = np.clip(yy[:, 0::2] + g, 0, 255)
    out[..., 1][:, 1::2] = np.clip(yy[:, 1::2] + g, 0, 255)
    out[..., 2][:, 0::2] = np.clip(yy[:, 0::2] + b, 0, 255)
    out[..., 2][:, 1::2] = np.clip(yy[:, 1::2] + b, 0, 255)
    return out


_CONVERTERS = {"YUYV": yuyv_to_rgb, "NV12": nv12_to_rgb}


# ---- V4L2 MMAP device -----------------------------------------------------

class V4L2Device:
    """One V4L2 MMAP streaming device with DQBUF/QBUF frame access."""

    def __init__(self, path: str, width: int, height: int,
                 fps: int = 60, fourcc: str = "YUYV"):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.fourcc = fourcc
        self.sizeimage = width * height * 2

        self._fd: int | None = None
        self._maps: list[mmap.mmap] = []
        self._queued: set[int] = set()

    # -- setup --------------------------------------------------------------

    def open(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.path, os.O_RDWR)
        try:
            fourcc = _FOURCCS[self.fourcc]
            fmt = _V4L2Format()
            fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            fmt.pix[0:4] = self.width.to_bytes(4, "little")
            fmt.pix[4:8] = self.height.to_bytes(4, "little")
            fmt.pix[8:12] = fourcc.to_bytes(4, "little")
            fmt.pix[12:16] = V4L2_FIELD_NONE.to_bytes(4, "little")
            _ioctl(fd, VIDIOC_S_FMT, fmt)

            w = int.from_bytes(fmt.pix[0:4], "little")
            h = int.from_bytes(fmt.pix[4:8], "little")
            got = int.from_bytes(fmt.pix[8:12], "little")
            if (w, h) != (self.width, self.height) or got != fourcc:
                raise RuntimeError(
                    f"{self.path}: S_FMT returned {w}x{h} fourcc=0x{got:08x} "
                    f"(wanted {self.width}x{self.height} {self.fourcc})"
                )
            self.sizeimage = int.from_bytes(fmt.pix[20:24], "little")

            if self.fps > 0:
                # v4l2_captureparm: capability(4) capturemode(4) timeperframe{num,den}(8)
                sp = _V4L2StreamParm()
                sp.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                sp.parm[8:12] = (1).to_bytes(4, "little")
                sp.parm[12:16] = self.fps.to_bytes(4, "little")
                _ioctl(fd, VIDIOC_S_PARM, sp)

            req = _V4L2RequestBuffers()
            req.count = 4
            req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            req.memory = V4L2_MEMORY_MMAP
            _ioctl(fd, VIDIOC_REQBUFS, req)

            for i in range(req.count):
                buf = _V4L2Buffer()
                buf.index = i
                buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
                buf.memory = V4L2_MEMORY_MMAP
                _ioctl(fd, VIDIOC_QUERYBUF, buf)
                m = mmap.mmap(
                    fd, int(buf.length), access=mmap.ACCESS_WRITE,
                    offset=int(buf.m_offset),
                )
                self._maps.append(m)
                _ioctl(fd, VIDIOC_QBUF, buf)
                self._queued.add(i)

            streamon = bytearray(4)
            streamon[:] = V4L2_BUF_TYPE_VIDEO_CAPTURE.to_bytes(4, "little")
            _ioctl(fd, VIDIOC_STREAMON, streamon)
            self._fd = fd
        except Exception:
            if self._maps:
                for m in self._maps:
                    try:
                        m.close()
                    except Exception:
                        pass
                self._maps = []
            os.close(fd)
            self._fd = None
            raise

    # -- streaming ----------------------------------------------------------

    def dequeue(self, timeout_s: float = 0.5):
        """Block up to *timeout_s* for a frame; return (index, bytesused) or None."""
        fd = self._fd
        if fd is None:
            return None
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return None
        buf = _V4L2Buffer()
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = V4L2_MEMORY_MMAP
        _ioctl(fd, VIDIOC_DQBUF, buf)
        self._queued.discard(buf.index)
        return buf.index, buf.bytesused

    def queue(self, index: int) -> None:
        buf = _V4L2Buffer()
        buf.index = index
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        buf.memory = V4L2_MEMORY_MMAP
        _ioctl(self._fd, VIDIOC_QBUF, buf)
        self._queued.add(index)

    def frame(self, index: int, bytesused: int) -> memoryview:
        return self._maps[index][:bytesused]

    def convert(self, frame, width: int, height: int) -> np.ndarray:
        return _CONVERTERS[self.fourcc](frame, width, height)

    # -- teardown -----------------------------------------------------------

    def close(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            streamoff = bytearray(4)
            streamoff[:] = V4L2_BUF_TYPE_VIDEO_CAPTURE.to_bytes(4, "little")
            _ioctl(fd, VIDIOC_STREAMOFF, streamoff)
        except OSError:
            pass
        for m in self._maps:
            try:
                m.close()
            except Exception:
                pass
        self._maps = []
        try:
            os.close(fd)
        except OSError:
            pass
        self._queued = set()


def _ioctl(fd: int, request: int, arg) -> None:
    import fcntl

    fcntl.ioctl(fd, request, arg)


# ---- Stereo capture -------------------------------------------------------


class StereoCapture:
    """Two-threaded capture of left/right endoscope feeds.

    Each device thread runs a DQBUF/convert/QBUF loop and publishes the
    latest RGB frame to a thread-safe slot.  Devices that fail to open (or
    drop mid-stream) are retried with backoff and reported via ``stats()``.
    """

    def __init__(self, left_path: str, right_path: str,
                 width: int = 1920, height: int = 1080,
                 fps: int = 60, fourcc: str = "YUYV"):
        self._paths = (left_path, right_path)
        self._width = width
        self._height = height
        self._fps = fps
        self._fourcc = fourcc

        self._devices: list[V4L2Device | None] = [None, None]
        self._latest: list[np.ndarray | None] = [None, None]
        self._seq: list[int] = [0, 0]
        self._t: list[float] = [0.0, 0.0]
        self._frames: list[int] = [0, 0]
        self._errors: list[int] = [0, 0]
        self._status: list[str] = ["stopped", "stopped"]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._stop.clear()
        for i in range(2):
            t = threading.Thread(
                target=self._loop,
                args=(i,),
                daemon=True,
                name=f"v4l2-{i}",
            )
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for dev in self._devices:
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads = []

    def latest(self):
        """Return (left_rgb, right_rgb, seq, wall_t) of the newest frame pair."""
        with self._lock:
            return self._latest[0], self._latest[1], self._seq[0], self._t[0]

    def seqs(self) -> tuple[int, int]:
        """Return the published-frame sequence numbers (left, right)."""
        with self._lock:
            return self._seq[0], self._seq[1]

    def stats(self) -> dict:
        with self._lock:
            return {
                "fps": [self._frames[i] for i in range(2)],
                "errors": [self._errors[i] for i in range(2)],
                "status": list(self._status),
                "seq": list(self._seq),
            }

    def _publish(self, i: int, rgb: np.ndarray) -> None:
        with self._lock:
            self._latest[i] = rgb
            self._seq[i] += 1
            self._t[i] = time.perf_counter()
            self._frames[i] += 1

    def _loop(self, i: int) -> None:
        while not self._stop.is_set():
            dev = V4L2Device(
                self._paths[i], self._width, self._height,
                fps=self._fps, fourcc=self._fourcc,
            )
            try:
                dev.open()
            except Exception as exc:
                with self._lock:
                    self._status[i] = f"error: {exc}"
                    self._errors[i] += 1
                self._devices[i] = None
                self._stop.wait(1.0)
                continue

            self._devices[i] = dev
            with self._lock:
                self._status[i] = f"streaming {dev.width}x{dev.height} {self._fourcc}"

            while not self._stop.is_set():
                try:
                    dq = dev.dequeue(timeout_s=0.2)
                    if dq is None:
                        continue
                    index, bytesused = dq
                    rgb = dev.convert(dev.frame(index, bytesused), dev.width, dev.height)
                    try:
                        dev.queue(index)
                    except OSError:
                        break
                    self._publish(i, rgb)
                except OSError as exc:
                    with self._lock:
                        self._status[i] = f"error: {exc}"
                        self._errors[i] += 1
                    break
                except Exception as exc:
                    with self._lock:
                        self._status[i] = f"fatal: {exc}"
                        self._errors[i] += 1
                    break

            dev.close()
            self._devices[i] = None
            with self._lock:
                self._status[i] = "stopped"
            if not self._stop.is_set():
                self._stop.wait(1.0)


# ---- Device auto-detection -------------------------------------------------

# Cypress EZ-USB FX3 based capture boxes used on this rig.  The VIVE Pro's
# built-in Multimedia Camera (idVendor 0bb4) is deliberately excluded.
_CAPTURE_USB_IDS = {("04b4", "00f9")}


def _sys_read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _usb_ids(sysdir: str) -> tuple[str, str] | None:
    """Walk parent dirs of a video4linux sysfs node to the USB device dir."""
    parent = os.path.dirname(os.path.realpath(sysdir))
    for _ in range(8):
        vendor = _sys_read(os.path.join(parent, "idVendor"))
        product = _sys_read(os.path.join(parent, "idProduct"))
        if vendor:
            return vendor, product
        parent = os.path.dirname(parent)
    return None


def _usb_device_dir(sysdir: str) -> str | None:
    parent = os.path.dirname(os.path.realpath(sysdir))
    for _ in range(8):
        if _sys_read(os.path.join(parent, "idVendor")):
            return parent
        parent = os.path.dirname(parent)
    return None


def _natural_bus_port_key(busport: str) -> tuple:
    return tuple(int(p) for p in re.split(r"[-.]", busport))


def _probe_capture_node(path: str) -> bool:
    """Read-only G_FMT probe; the streaming node of a UVC pair answers it."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        fmt = _V4L2Format()
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
        _ioctl(fd, VIDIOC_G_FMT, fmt)
        w = int.from_bytes(fmt.pix[0:4], "little")
        h = int.from_bytes(fmt.pix[4:8], "little")
        return w > 0 and h > 0
    except OSError:
        return False
    finally:
        os.close(fd)


def detect_capture_devices() -> list[str]:
    """Find USB UVC capture boxes, skipping the VIVE HMD camera.

    Returns video node paths sorted by USB bus/port.  Each box exposes two
    video nodes; the non-streaming one (which rejects G_FMT) is filtered out
    with a read-only ioctl probe.  Boxes negotiated at USB2 (480 Mbps) get a
    bandwidth warning: 1080p60 YUYV needs ~2 Gbps per stream.
    """
    boxes: dict[str, tuple[str, str, str]] = {}  # usb dev dir -> (node, name, speed)
    for n in range(64):
        sysdir = f"/sys/class/video4linux/video{n}"
        if not os.path.exists(sysdir):
            continue
        name = _sys_read(os.path.join(sysdir, "name")) or ""
        if "vive" in name.lower():
            continue
        ids = _usb_ids(sysdir)
        if ids not in _CAPTURE_USB_IDS and "capture" not in name.lower():
            continue
        if not _probe_capture_node(f"/dev/video{n}"):
            continue
        devdir = _usb_device_dir(sysdir)
        if devdir is None:
            continue
        if devdir not in boxes:
            boxes[devdir] = (f"/dev/video{n}", name, _sys_read(os.path.join(devdir, "speed")) or "?")
    ordered = sorted(boxes, key=lambda d: _natural_bus_port_key(os.path.basename(d)))
    devices = [boxes[d][0] for d in ordered]
    names = [boxes[d][1] for d in ordered]
    for d in ordered:
        node, name, speed = boxes[d]
        if speed == "480":
            print(
                f"[WARN] {node} ({name}) negotiated USB2 (480 Mbps); "
                f"1080p60 YUYV needs ~2 Gbps per stream — expect very low FPS. "
                f"Move it to a USB3 port."
            )
    # The JS3350 box is wired to the left eye on this rig.  When the two
    # boxes carry distinct product strings, prefer JS3350 for the left eye
    # so sides survive port shuffles; press 'o' to swap if re-cabled.
    if (
        len(devices) == 2
        and any("JS3350" in nm.upper() for nm in names)
        and "JS3350" not in names[0].upper()
    ):
        devices.reverse()
    return devices


# ---- USB topology diagnostics ---------------------------------------------

_USB_DEV_CHILD_RE = re.compile(r"^\d+-\d+(\.\d+)*$")


def _usb_device_sysdirs() -> list[str]:
    """Real sysfs dirs of every enumerated USB device (excluding interfaces)."""
    base = "/sys/bus/usb/devices"
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in sorted(names):
        if ":" in name:
            continue
        d = os.path.realpath(os.path.join(base, name))
        if os.path.exists(os.path.join(d, "idVendor")):
            out.append(d)
    return out


def _hub_children(sysdir: str) -> list[str]:
    return [e for e in os.listdir(sysdir) if _USB_DEV_CHILD_RE.match(e)]


def _is_usb_hub(sysdir: str) -> bool:
    cls = _sys_read(os.path.join(sysdir, "bDeviceClass"))
    try:
        return int(cls, 16) == 9
    except (TypeError, ValueError):
        return False


def empty_usb_hubs() -> list[str]:
    """USB device names of hubs that currently have no downstream devices.

    HTC (0bb4) hubs are skipped: resetting the VIVE link-box chain is risky.
    """
    hubs = []
    for d in _usb_device_sysdirs():
        if not _is_usb_hub(d):
            continue
        if _sys_read(os.path.join(d, "idVendor")) == "0bb4":
            continue
        if not _hub_children(d):
            hubs.append(os.path.basename(d))
    return hubs


def usb_topology_report() -> list[str]:
    """Human-readable summary of USB hubs and capture boxes for debugging."""
    hubs, cypress = [], []
    for d in _usb_device_sysdirs():
        vendor = _sys_read(os.path.join(d, "idVendor")) or ""
        prod = _sys_read(os.path.join(d, "product")) or ""
        speed = _sys_read(os.path.join(d, "speed")) or "?"
        name = os.path.basename(d)
        if vendor == "04b4":
            cypress.append(f"{name} ({prod}) speed={speed}M")
        if _is_usb_hub(d):
            hubs.append(
                f"{name} ({prod}) upstream={speed}M children={len(_hub_children(d))}"
            )
    lines = [
        "USB hubs: " + ("; ".join(hubs) if hubs else "none found"),
        "Cypress capture boxes: " + ("; ".join(cypress) if cypress else "NONE on any USB bus"),
    ]
    if not cypress:
        lines.append(
            "No capture boxes enumerated — check the hub's power adapter and "
            "re-seat the cards, or run `pixi run usb-recover` to power-cycle "
            "empty hubs."
        )
    return lines


# ---- Link-budget based fourcc selection ------------------------------------

# Practical isochronous capacity per shared-link speed (MB/s).  5 Gbps is
# conservatively 420 MB/s: two 1080p60 YUYV streams (~513 MB/s) don't fit,
# two NV12 streams (~384 MB/s) do.
_USB_LINK_CAPACITY_MBPS = {480: 40.0, 5000: 420.0, 10000: 900.0, 20000: 1800.0}
_BPP = {"YUYV": 2.0, "NV12": 1.5}
_ISOC_OVERHEAD = 1.03


class _V4L2FmtDesc(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("desc", ctypes.c_ubyte * 32),
        ("pixelformat", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


VIDIOC_ENUM_FMT = _ioc(_IOC_WRITE | _IOC_READ, "V", 2, ctypes.sizeof(_V4L2FmtDesc))


def _node_supports_fourcc(path: str, fourcc: str) -> bool:
    """Read-only ENUM_FMT probe: does this video node advertise *fourcc*?"""
    wanted = _FOURCCS.get(fourcc)
    if wanted is None:
        return False
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        for i in range(32):
            m = _V4L2FmtDesc()
            m.index = i
            m.type = V4L2_BUF_TYPE_VIDEO_CAPTURE
            try:
                _ioctl(fd, VIDIOC_ENUM_FMT, m)
            except OSError:
                break
            if m.pixelformat == wanted:
                return True
        return False
    finally:
        os.close(fd)


def _usb_ancestor_speeds(sysdir: str) -> list[tuple[str, int]]:
    """Collect (dir, speed_mbps) for every USB device on the path to the root."""
    chain = []
    parent = os.path.dirname(os.path.realpath(sysdir))
    for _ in range(10):
        speed = _sys_read(os.path.join(parent, "speed"))
        if speed is not None:
            try:
                chain.append((parent, int(speed)))
            except ValueError:
                pass
        grand = os.path.dirname(parent)
        if grand == parent:
            break
        parent = grand
    return chain


def link_budget_fourcc(left_dev: str, right_dev: str,
                       width: int, height: int, fps: int) -> tuple[str, str]:
    """Choose YUYV or NV12 from the shared upstream USB link speed.

    Returns ``(fourcc, reason)``.  The deepest common USB ancestor of the
    two device nodes is the shared link; its speed caps the aggregate
    isochronous payload of both streams.
    """
    chains = []
    for dev in (left_dev, right_dev):
        sysdir = f"/sys/class/video4linux/{os.path.basename(dev)}"
        if not os.path.exists(sysdir):
            return "YUYV", f"fourcc=YUYV: no sysfs entry for {dev}"
        chains.append({path: speed for path, speed in _usb_ancestor_speeds(sysdir)})

    common = None
    shared_mbps = None
    for path, speed in chains[0].items():
        if path in chains[1]:
            common = path
            shared_mbps = speed
            break
    if common is None or shared_mbps is None:
        return "YUYV", "fourcc=YUYV: no shared USB ancestor found"

    capacity = next(
        (c for s, c in sorted(_USB_LINK_CAPACITY_MBPS.items()) if s >= shared_mbps),
        _USB_LINK_CAPACITY_MBPS[20000],
    )
    yuyv_bw = width * height * _BPP["YUYV"] * fps * 2 * _ISOC_OVERHEAD / 1e6
    nv12_bw = width * height * _BPP["NV12"] * fps * 2 * _ISOC_OVERHEAD / 1e6
    hub = f"shared {shared_mbps} Mbps upstream ({os.path.basename(common)})"

    if yuyv_bw <= capacity:
        return "YUYV", (
            f"fourcc=YUYV: {hub} carries {yuyv_bw:.0f} MB/s "
            f"(2 streams) under {capacity:.0f} MB/s"
        )
    if nv12_bw > capacity:
        return "NV12", (
            f"fourcc=NV12: {hub} cannot carry even NV12 ({nv12_bw:.0f} MB/s > "
            f"{capacity:.0f} MB/s); expect frame loss — lower --fps"
        )
    if (_node_supports_fourcc(left_dev, "NV12")
            and _node_supports_fourcc(right_dev, "NV12")):
        return "NV12", (
            f"fourcc=NV12: {hub} cannot carry {yuyv_bw:.0f} MB/s YUYV; "
            f"NV12 ({nv12_bw:.0f} MB/s, 4:2:0) fits"
        )
    return "YUYV", (
        f"fourcc=YUYV: {hub} needs {yuyv_bw:.0f} MB/s but the boxes lack NV12; "
        f"expect frame loss"
    )
