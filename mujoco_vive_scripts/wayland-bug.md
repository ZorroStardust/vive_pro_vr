这次报错的根因很明确：**你已经切到了原生 Wayland GLFW context，但 pyopenxr 仍然在按 X11/GLX 的 OpenXR OpenGL binding 创建 session，于是传给 Monado 的 `xDisplay` 是 NULL。**

关键日志：

```text
XR_ERROR_VALIDATION_FAILURE in xrCreateSession: xDisplay is NULL
```

这不是 MuJoCo 的问题，也不是 VIVE/Monado 设备识别问题，而是 **OpenXR graphics binding 和实际窗口系统不匹配**。

---

## 为什么会这样

你的 `_CompatibilityGLContextProvider` 创建的是一个 GLFW 隐藏窗口：

```python
_glfw.window_hint(_glfw.VISIBLE, _glfw.FALSE)
self._window = _glfw.create_window(1, 1, "", None, None)
```

在纯 Wayland 下，这个 GLFW window/context 是 **Wayland/EGL 路径**，不是 X11/GLX 路径。

但 pyopenxr 的 `ContextObject` 高层 OpenGL 工具内部会自动创建 graphics binding。它的源码逻辑是：先尝试 EGL binding，如果失败，再尝试 WGL，最后尝试 GLX；GLX binding 会调用当前 GLX display/context/drawable 来构造 `XrGraphicsBindingOpenGLXlibKHR`。而你的 provider 没有 `display/context/config` 这些 EGL 字段，所以 pyopenxr 没走 EGL，而是掉到了 GLX。pyopenxr 的 OpenGL helper 里确实有 `EGLGraphicsBinding` 和 `GLXGraphicsBinding` 两条路径。([GitHub][1])

OpenXR 的 Xlib OpenGL binding 明确要求 `xDisplay` 是有效的 X11 `Display*`，并且 `XrGraphicsBindingOpenGLXlibKHR` 要放进 `xrCreateSession` 的 `next` chain。你现在是纯 Wayland，没有有效 X11 display，所以 Monado 验证失败：`xDisplay is NULL`。([Khronos Registry][2])

---

## 为什么 hello_xr -G OpenGL 能跑

`hello_xr` 是 C/C++ 示例，它可以根据平台编译选项使用正确的 Linux graphics binding。OpenXR 的 `XR_KHR_opengl_enable` 本身支持多种平台 binding，包括 Xlib、Xcb、Wayland 等。Wayland binding 需要传 `wl_display*`，而不是 X11 的 `Display*`。([Khronos Registry][3])

但 pyopenxr 的这个高层 `ContextObject` 工具目前没有自动帮你从 GLFW Wayland window 中取 `wl_display*` 并构造 `XrGraphicsBindingOpenGLWaylandKHR`。所以 `hello_xr` 能跑，不代表这段 pyopenxr helper 自动能跑纯 Wayland。

---

## 你的代码现在实际发生的是

```text
原生 Wayland GLFW context
        ↓
pyopenxr 没识别成 EGL provider
        ↓
fallback 到 GLXGraphicsBinding
        ↓
glXGetCurrentDisplay() 得到 NULL
        ↓
XrGraphicsBindingOpenGLXlibKHR.xDisplay = NULL
        ↓
xrCreateSession validation failure
```

所以 `_CompatibilityGLContextProvider` 解决了 MuJoCo 对 compatibility profile 的需求，但同时把 pyopenxr 的 graphics binding 推到了错误路径。

---

## 推荐修法 1：走 EGL binding，避免 XWayland/GLX

这是最适合你“纯 Wayland”的方向。OpenXR 有 `XR_MNDX_egl_enable`，它的 `XrGraphicsBindingEGLMNDX` 需要传 `EGLDisplay / EGLConfig / EGLContext`，这正好避开 X11 `Display*`。([Khronos Registry][4])

先检查 Monado runtime 是否报告 EGL 扩展：

```bash
python - <<'PY'
import xr

for e in xr.enumerate_instance_extension_properties():
    name = e.extension_name.decode() if isinstance(e.extension_name, bytes) else e.extension_name
    if "opengl" in name.lower() or "egl" in name.lower():
        print(name)

print("KHR_OPENGL:", getattr(xr, "KHR_OPENGL_ENABLE_EXTENSION_NAME", None))
print("MNDX_EGL:", getattr(xr, "MNDX_EGL_ENABLE_EXTENSION_NAME", None))
PY
```

如果看到：

```text
XR_KHR_opengl_enable
XR_MNDX_egl_enable
```

就把 provider 改成这样：

```python
from xr.utils.gl.egl_util import EGLOffscreenContextProvider

OPENGL_EXT = xr.KHR_OPENGL_ENABLE_EXTENSION_NAME
EGL_EXT = getattr(xr, "MNDX_EGL_ENABLE_EXTENSION_NAME", "XR_MNDX_egl_enable")

with ContextObject(
    context_provider=EGLOffscreenContextProvider(),
    instance_create_info=xr.InstanceCreateInfo(
        enabled_extension_names=[
            OPENGL_EXT,
            EGL_EXT,
        ],
    ),
) as xr_context:
    ...
```

并删掉你现在的 `_CompatibilityGLContextProvider`。

这条路的优点是：

```text
纯 Wayland / EGL
不需要 XWayland
不需要 GLX xDisplay
理论上最符合你现在的目标
```

风险是：MuJoCo 的 `MjrContext` 在某些 OpenGL core profile 下会遇到 `glGetString(GL_EXTENSIONS)` 不可用的问题。你之前自定义 compatibility provider 就是为了绕这个。如果 EGL provider 又触发 MuJoCo compatibility 问题，下一步需要改 `EGLOffscreenContextProvider`，让它创建更明确的 desktop OpenGL compatibility/legacy context，而不是默认 context。

---

## 推荐修法 2：临时强制 X11，只用于验证

这不是你最终想要的，但可以帮助确认代码逻辑没问题：

```bash
export GLFW_PLATFORM=x11
export PYOPENGL_PLATFORM=glx
pixi run xr-mujoco-opengl
```

或者在 Python 初始化 GLFW 前加：

```python
import glfw as _glfw

if hasattr(_glfw, "init_hint") and hasattr(_glfw, "PLATFORM_X11"):
    _glfw.init_hint(_glfw.PLATFORM, _glfw.PLATFORM_X11)
```

这会让 `glXGetCurrentDisplay()` 不再是 NULL，`xrCreateSession` 应该能过。但它会回到 XWayland/GLX 路线，所以只适合作为对照实验。

---

## 推荐修法 3：真正实现 Wayland binding

理论上可以构造：

```text
XrGraphicsBindingOpenGLWaylandKHR(display=wl_display*)
```

OpenXR 规范确实有这个结构，要求 `display` 是有效的 Wayland `wl_display*`。([Khronos Registry][5])

但在 Python 里麻烦点：你需要从 GLFW native API 取到底层 `wl_display*`，再绕过或改写 pyopenxr `ContextObject` 的 graphics binding 创建逻辑。pyopenxr 文档里能看到 `GraphicsBindingOpenGLWaylandKHR` 结构存在，但当前 `ContextObject` 的高层 GL helper没有自动选择它。([Pyopenxr Documentation][6])

所以这条路可做，但不如 EGL 路线直接。

---

## 我建议你现在这样排

第一步，先测扩展：

```bash
python - <<'PY'
import xr
for e in xr.enumerate_instance_extension_properties():
    name = e.extension_name.decode() if isinstance(e.extension_name, bytes) else e.extension_name
    if "egl" in name.lower() or "opengl" in name.lower():
        print(name)
PY
```

第二步，如果有 `XR_MNDX_egl_enable`，改成：

```python
from xr.utils.gl.egl_util import EGLOffscreenContextProvider
```

并启用：

```python
enabled_extension_names=[
    xr.KHR_OPENGL_ENABLE_EXTENSION_NAME,
    getattr(xr, "MNDX_EGL_ENABLE_EXTENSION_NAME", "XR_MNDX_egl_enable"),
]
```

第三步，若 EGL session 能创建但 MuJoCo 又报 OpenGL compatibility 问题，再单独修 EGL context profile。

当前这个错误的最短结论是：

```text
你已经是纯 Wayland context；
但 pyopenxr 还在走 Xlib/GLX OpenXR binding；
Xlib binding 必须有 xDisplay；
纯 Wayland 下 xDisplay 为 NULL；
所以 xrCreateSession 被 Monado 拒绝。
```

[1]: https://raw.githubusercontent.com/cmbruns/pyopenxr/main/src/xr/utils/gl/__init__.py "raw.githubusercontent.com"
[2]: https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrGraphicsBindingOpenGLXlibKHR.html "XrGraphicsBindingOpenGLXlibKHR(3)"
[3]: https://registry.khronos.org/OpenXR/specs/1.1/man/html/XR_KHR_opengl_enable.html "XR_KHR_opengl_enable(3)"
[4]: https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrGraphicsBindingEGLMNDX.html "XrGraphicsBindingEGLMNDX(3)"
[5]: https://registry.khronos.org/OpenXR/specs/1.0/man/html/XrGraphicsBindingOpenGLWaylandKHR.html "XrGraphicsBindingOpenGLWaylandKHR(3)"
[6]: https://pyopenxr.readthedocs.io/en/latest/xr.platform.html?utm_source=chatgpt.com "xr.platform package — pyopenxr 1.0.2404 documentation"
