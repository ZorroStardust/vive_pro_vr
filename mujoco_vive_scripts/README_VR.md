# vive_pro_vr — MuJoCo + VIVE Pro VR 渲染基础设施

## 概述

本项目提供一套完整的 **MuJoCo 物理仿真 → OpenXR 双目立体 → HTC VIVE Pro HMD** 软件栈。通过 EGL headless OpenGL 上下文 + [pyopenxr](https://github.com/pyopenxr/pyopenxr) + [Monado](https://monado.dev/) OpenXR runtime，将 MuJoCo 固定相机渲染为左右眼独立的 swapchain 图像，由 Monado compositor 扭曲后呈现到 VIVE Pro 的物理面板。

支持实时 comfort 调节（视差远近、FOV 缩放、mono/stereo 切换）、像素级标定补偿、以及配置持久化。

---

## 环境架构

```
┌─ Application ─────────────────────────────────────────────────────┐
│  Python (pixi env)                                                │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐ │
│  │ xr_*_comfort.py  │  │ xr_surgical_     │  │ xr_crosshair_   │ │
│  │ (独立渲染+调试)   │  │ robot.py         │  │ calibration.py │ │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬────────┘ │
│           │                     │                      │          │
│           └──────────┬──────────┴──────────────────────┘          │
│                      ▼                                            │
│              pyopenxr ContextObject                               │
│              (xr.utils.gl)                                        │
│              ├── EGL Offscreen Pbuffer Context                    │
│              ├── OpenXR Instance / Session                        │
│              └── Swapchain (GL texture per eye)                   │
└──────────────────────┬───────────────────────────────────────────┘
                       │ XR_KHR_opengl_enable + XR_MNDX_egl_enable
                       ▼
┌─ Monado OpenXR Runtime ───────────────────────────────────────────┐
│  monado-service                                                   │
│  ├── Compositor (Vulkan)                                          │
│  ├── VIVE Pro driver (DRM direct-mode)                            │
│  └── IPC socket: /run/user/$UID/monado_comp_ipc                   │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Vulkan display
                       ▼
                HTC VIVE Pro HMD
                2880×1600 @ 90 Hz
```

---

## 模块地图

```
mujoco_vive_scripts/mujoco_vive_scripts/
├── xr_mujoco_opengl.py            ← 核心：EGL + OpenXR + comfort 控制
├── xr_common.py                   ← 共享基础设施（EGL context / stdin / argparse 工厂）
├── xr_surgical_robot.py           ← 手术机器人桥接 + render_loop()（继承 MujocoStereoRenderer）
├── xr_crosshair_calibration.py    ← 十字线标定工具
├── xr_pink_world_check.py         ← OpenXR 连通性测试（红/蓝纯色）
├── openxr_env_check.py            ← 环境诊断（USB、扩展、GL）
├── config_util.py                 ← calibration.toml 读写
├── save_stereo_frames.py          ← 离屏渲染到 PNG
├── mujoco_sbs_glfw.py             ← GLFW side-by-side 窗口预览
├── list_monitors.py               ← GLFW 显示器列表
│
├── models/
│   └── stereo_endoscope_test.xml  ← xr-mujoco-opengl 默认测试模型
│
└── calibration.toml               ← 标定 + comfort 持久化配置

scripts/
├── run_xr_mujoco_opengl.sh
├── run_xr_surgical.sh
├── run_xr_crosshair.sh
└── run_xr_pink_world.sh
```

### 数据流

```
calibration.toml ──加载──→ config_util.load_calibration() ─→ parse_args() defaults
                        config_util.load_comfort()      ─→ RuntimeComfortState

RuntimeComfortState ──实时修改──→ s 键保存 ──→ save_full_config() ─→ calibration.toml
     │
     ├── zoom ─→ model.cam_fovy[cam_id] = original / zoom (每帧临时修改)
     ├── scene_farther_px ─→ MjrRect 偏移 (左右眼反向)
     ├── mono_to_both_eyes ─→ 相机选择逻辑
     └── swap_eyes / invert_scene_shift
```

---

## 核心模块详解

### `xr_mujoco_opengl.py` — 渲染 + Comfort 控制

核心入口模块。EGL context provider、stdin reader、comfort state、键盘处理都已迁移到 `xr_common.py` 中共享；本模块只保留 `MujocoStereoRenderer`（基础渲染器）和 `main()`。

| 组件 | 职责 |
|------|------|
| `MujocoStereoRenderer` | MuJoCo 双目渲染器：FIXED 相机模式、fovy zoom、viewport 偏移补偿 |
| `render_eye()` | `mjv_updateScene` → `glClear` → `mjr_render` |

`xr_common.py` 提供：

| 组件 | 职责 |
|------|------|
| `_NvidiaEGLContextProvider` | 原始 ctypes 调用 libEGL：枚举 GPU → 创建 pbuffer surface → 绑定 OpenGL 兼容 profile |
| `_StdinReader` | 非阻塞 tty 单键读取（termios + fcntl + select） |
| `RuntimeComfortState` | 运行时 comfort 参数容器：zoom、scene_farther、mono、swap |
| `_handle_input` | 键盘派发：`a/d/e/p/o/z/x/s/q` |
| `add_calibration_args` / `add_comfort_args` | argparse 工厂，3 个脚本共享 CLI 表面 |

**渲染流程（每帧每眼）：**
```
1. 选择相机 ID（根据 mono/swap 标志）
2. 设置 mjCAMERA_FIXED + fixedcamid
3. 临时修改 cam_fovy = original / zoom → mjv_updateScene → 恢复 cam_fovy
4. 读取当前 viewport
5. 计算 calib_x + comfort_shift → MjrRect 偏移
6. glClear → mjr_render → swapchain image
```

### `xr_surgical_robot.py` — 手术机器人桥接

核心函数 `render_loop(model, data, ...)` 是一个**纯渲染**的阻塞函数：

- **不拥有** model/data（外部仿真线程持有）
- **不修改** data（只通过 `mjv_updateScene` / `mjr_render` 读取）
- **通过 scene_lock** 与仿真线程同步
- **通过 clutch_callback** 支持 Space 离合输入
- **可选的 comfort_state**（从 calibration.toml 加载默认值）

`main()` 入口保留为 standalone 预览模式：自带仿真线程 + controller + IK。

### `config_util.py` — 配置持久化

```toml
# calibration.toml
[calibration]
left_x = 17          # 左眼水平偏移(px)
left_y = 0
right_x = -21        # 右眼水平偏移(px)
right_y = 0

[comfort]
mono_to_both_eyes = false
swap_eyes = false
scene_farther_px = 0.0
scene_shift_step_px = 2.0
max_abs_scene_shift_px = 80.0
invert_scene_shift = false
zoom = 1.0
zoom_step = 0.1
clear_r = 0.02
clear_g = 0.02
clear_b = 0.02
```

| 函数 | 功能 |
|------|------|
| `load_calibration()` | 读取 `[calibration]` |
| `load_comfort()` | 读取 `[comfort]` |
| `save_calibration(cal)` | 写 `[calibration]`，自动保留 `[comfort]` |
| `save_comfort(c)` | 写 `[comfort]`，自动保留 `[calibration]` |
| `save_full_config(cal, comfort)` | 全量写入 |

路径优先级：`MUJOCO_VIVE_CALIBRATION` 环境变量 → `<project>/calibration.toml`。

### `xr_crosshair_calibration.py` — 标定工具

向左右眼 swapchain 渲染相同的黑底白色十字线（纯 OpenGL 固定管线，无 MuJoCo）。用户通过键盘调整每眼的像素偏移，找到零视差融合点，按下 `s` 保存到 calibration.toml。

---

## 键盘控制

| 按键 | 功能 | 所属模块 |
|------|------|----------|
| `a` / `d` | 场景推远 / 拉近 | comfort |
| `e` | 重置 scene_farther 为 0 | comfort |
| `z` / `x` | FOV 缩小（看更多）/ 放大 | comfort |
| `p` | mono / stereo 双目切换 | comfort |
| `o` | 左右眼交换 | comfort |
| `s` | 保存当前全部参数到 calibration.toml | comfort |
| `Space` | 离合器 toggle（仅 surgical + VR 遥操作） | surgical |
| `q` / `Esc` | 退出 | 全部 |

---

## Pixi 任务

| 任务 | 说明 |
|------|------|
| `pixi run xr-pink-world` | OpenXR 连通性测试（左红右蓝） |
| `pixi run xr-mujoco-opengl` | 带 comfort 控制的双目渲染（默认 stereo_endoscope_test 模型） |
| `pixi run xr-crosshair` | 十字线标定工具 |
| `pixi run xr-surgical` | 手术机器人 standalone VR 预览 |
| `pixi run start-monado` | 启动 Monado OpenXR runtime |
| `pixi run openxr-check` | 环境诊断（USB、VR 扩展） |
| `pixi run save-frames` | 离屏渲染到 PNG |
| `pixi run sbs-windowed` | GLFW side-by-side 窗口预览 |
| `pixi run list-monitors` | 列出 GLFW 显示器 |

---

## 启动清单（TTY3 Direct-Mode）

```bash
# 1. 切换 TTY
sudo chvt 3

# 2. 启动 Monado（tmux session，必须）
pixi run start-monado

# 3. 运行任一 VR 脚本
pixi run xr-mujoco-opengl  # comfort 模型调试
pixi run xr-surgical     # 手术机器人预览
pixi run xr-crosshair    # 标定

# 4. 退出后 Ctrl+C Monado，切回 GNOME
sudo chvt 2
```

---

## 调试指南

### VIVE 无画面（黑屏）

| 可能原因 | 诊断方法 | 修复 |
|---------|---------|------|
| Monado 未运行 | `ls /run/user/$UID/monado_comp_ipc` | 先运行 `pixi run start-monado` |
| 模型无灯光 | 检查终端输出 `nlight=1`（仅 headlight） | 使用带 `<headlight>` 的 scene XML |
| FOV 太窄 | VR 中视野极小 | `z` 键增大 zoom |
| clear color 太暗 | `--clear-rgb 0.3 0.3 0.4` 测试 | 确认屏幕亮起则 pipeline 通 |

### 双目融合失败（重影）

1. `pixi run xr-crosshair` 重新标定
2. 按 `p` 切到 mono 模式确认单眼正常
3. 按 `a/d` 调整 scene_farther_px
4. 按 `s` 保存

### FPS 偏低

- 检查 Monado 日志是否被 Lighthouse 脉冲淹没（基站/手柄关掉）
- 减少 `max_geom`（大型模型用 50000+）
- 关闭 `--print-every 0` 减少 IO
