# vive_pro_vr

MuJoCo 物理仿真 → OpenXR 双目立体 → HTC VIVE Pro HMD 的完整软件栈。

通过 EGL headless OpenGL + [pyopenxr](https://github.com/pyopenxr/pyopenxr) + [Monado](https://monado.dev/) OpenXR runtime，将 MuJoCo 固定相机渲染为左右眼独立的图像，由 Monado compositor 扭曲后呈现到 VIVE Pro 的 2880×1600@90Hz OLED 面板。

---

## 环境

| 组件 | 版本/型号 |
|------|----------|
| OS | Ubuntu 24.04, KDE/GNOME Wayland |
| GPU | NVIDIA GeForce RTX 5060 Ti, driver 580.x |
| HMD | HTC VIVE Pro / P110, Link Box 连接 |
| 依赖管理 | [pixi](https://pixi.sh) (conda + PyPI) |
| OpenXR Runtime | [Monado](https://monado.dev/) |
| MuJoCo | ≥3.9.0 |
| Python | 3.13.x |

HMD 显示为 DRM `card1-DP-3` (non-desktop=1), 2880×1600@90Hz。VkDisplay direct-mode 需要 TTY（GNOME Wayland DRM lease 不可用），通过 `sudo chvt 3` 切换到 TTY3 运行。

---

## 快速开始

```bash
# 1. 安装依赖
pixi install
pixi run install-system-deps   # Monado, GLFW, Vulkan 等系统包

# 2. 环境检查
pixi run openxr-check

# 3. 启动 Monado（TTY3 direct-mode 需要先切 TTY）
sudo chvt 3
pixi run start-monado          # tmux session 中运行

# 4. 基础渲染测试（左右眼红/蓝纯色）
pixi run xr-pink-world

# 5. 双目立体渲染（comfort 控制）
pixi run xr-mujoco-opengl

# 6. 退出后切回桌面
sudo chvt 2
```

---

## Pixi 任务一览

| 任务 | 说明 |
|------|------|
| `pixi run xr-pink-world` | OpenXR 连通性测试（左眼红色、右眼蓝色） |
| `pixi run xr-mujoco-opengl` | 带实时 comfort 控制的双目 MuJoCo 渲染（推荐） |
| `pixi run xr-surgical` | 手术 continuum robot standalone VR 预览 |
| `pixi run xr-crosshair` | 十字线标定工具（解决零视差偏移） |
| `pixi run start-monado` | 启动 Monado OpenXR runtime |
| `pixi run test-openxr` | 运行 hello_xr 验证 Monado |
| `pixi run openxr-check` | 环境诊断（USB 设备、Vulkan 扩展、用户组） |
| `pixi run save-frames` | MuJoCo 离屏渲染到 PNG (`out/left.png right.png sbs.png`) |
| `pixi run sbs-windowed` | GLFW side-by-side 窗口预览 |
| `pixi run sbs-fullscreen` | GLFW 全屏双目（`MONITOR=N` 指定显示器） |
| `pixi run list-monitors` | 列出 GLFW 可用的显示器 |

---

## 模块结构

```
mujoco_vive_scripts/                    ← Python 包（活跃开发）
├── mujoco_vive_scripts/
│   ├── xr_mujoco_opengl.py             ← 核心：EGL + OpenXR + comfort 控制
│   ├── xr_common.py                    ← 共享基础设施（EGL context / stdin / argparse 工厂）
│   ├── xr_surgical_robot.py            ← 手术机器人桥接，对外暴露 render_loop()
│   ├── xr_crosshair_calibration.py     ← 十字线标定工具（纯 GL，无 MuJoCo）
│   ├── xr_pink_world_check.py          ← OpenXR 连通性测试
│   ├── config_util.py                  ← calibration.toml 读写
│   ├── save_stereo_frames.py           ← 离屏渲染到 PNG
│   ├── mujoco_sbs_glfw.py              ← GLFW side-by-side 预览
│   ├── openxr_env_check.py             ← 环境诊断
│   └── list_monitors.py                ← GLFW 显示器列表
├── models/
│   └── stereo_endoscope_test.xml       ← 简单双目测试模型
├── calibration.toml                    ← 标定 + comfort 持久化配置
├── scripts/                            ← shell wrapper 脚本
└── pyproject.toml

script_test/                            ← Monado/VIVE TTY 测试脚本
```

---

## 键盘控制（VR 运行时）

| 按键 | 功能 |
|------|------|
| `a` / `d` | 场景推远 / 拉近 |
| `e` | 重置远近偏移为 0 |
| `z` / `x` | FOV 缩小（看更多）/ 放大 |
| `p` | mono / stereo 双目模式切换 |
| `o` | 左右眼交换 |
| `s` | 保存当前参数到 `calibration.toml` |
| `q` / `Esc` | 退出 |

---

## 与其他项目集成

本项目的 `render_loop()` 函数可作为**可插拔的 VR viewer** 被外部项目调用：

```python
from mujoco_vive_scripts.xr_surgical_robot import render_loop

# 调用方拥有 model/data 和仿真线程，VR 渲染器只读
render_loop(
    model=scene.model,
    data=scene.data,
    stop_event=stop_event,
    scene_lock=scene_lock,
    clutch_callback=my_clutch_handler,   # 可选
)
```

详见：[surgical_continuum_robot/README_VR.md](../surgical_continuum_robot/README_VR.md)

---

## 详细文档

- [VR 渲染管线和工具链文档](mujoco_vive_scripts/README_VR.md) — 架构、模块详解、调试指南
- [AGENTS.md](AGENTS.md) — 开发者备忘（环境变量、进程管理、常见坑）
- [stage1.md](stage1.md) — 硬件/软件调试日志
