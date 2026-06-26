# mujoco_vive_scripts

一个面向 **Ubuntu 24.04 + KDE Wayland + RTX 5060 Ti + VIVE Pro/P110 + Monado** 的最小 MuJoCo 双目测试包。

当前包的目标是先把 **MuJoCo 双相机 / 双目内镜画面** 跑通，并保留你当前已经验证成功的 Monado/OpenXR 启动脚本。

> 重要说明：  
> 这个包是 **Python-first** 的最小测试包。  
> 其中 `mujoco_sbs_glfw.py` 是稳定的 MuJoCo 双目 SBS 测试程序，直接用 MuJoCo + GLFW 渲染到普通窗口或普通显示器。  
> 如果 VIVE 正由 Monado NVIDIA Direct-Mode 接管，普通 GLFW 窗口不会进入 OpenXR compositor；此时请先用本包完成 MuJoCo 双目相机验证，再进入下一步 Python OpenXR/pyopenxr 接入。

---

## 目录结构

```text
mujoco_vive_scripts/
├── README.md
├── pyproject.toml
├── requirements.txt
├── requirements-openxr-python.txt
├── models/
│   └── stereo_endoscope_test.xml
├── scripts/
│   ├── install_ubuntu2404.sh
│   ├── start_monado_hmd_only.sh
│   ├── test_openxr.sh
│   ├── run_save_frames.sh
│   ├── run_sbs_windowed.sh
│   └── run_sbs_fullscreen_monitor.sh
└── mujoco_vive_scripts/
    ├── __init__.py
    ├── openxr_env_check.py
    ├── list_monitors.py
    ├── save_stereo_frames.py
    └── mujoco_sbs_glfw.py
```

---

## 1. 系统依赖

```bash
cd ~/mujoco_vive_scripts
bash scripts/install_ubuntu2404.sh
```

或者手动安装：

```bash
sudo apt update
sudo apt install -y \
  python3.12-venv python3-pip python3-dev build-essential \
  libgl1 libegl1 libglvnd0 libglfw3 libglfw3-dev \
  libx11-6 libxrandr2 libxi6 libxcursor1 libxinerama1 \
  mesa-utils libnvidia-egl-wayland1 \
  libopenxr-loader1 libopenxr-utils libopenxr1-monado monado-service monado-cli xr-hardware
```

---

## 2. Python 环境

```bash
cd ~/mujoco_vive_scripts
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

如果要做 Python OpenXR/pyopenxr 探测：

```bash
pip install -r requirements-openxr-python.txt
```

---

## 3. Monado/OpenXR 快速检查

保持你已经验证成功的约束：

```text
基站关闭
手柄关闭
KDE Wayland
不要 unset DISPLAY
不要 unset WAYLAND_DISPLAY
```

终端 1：

```bash
cd ~/mujoco_vive_scripts
bash scripts/start_monado_hmd_only.sh
```

终端 2：

```bash
cd ~/mujoco_vive_scripts
bash scripts/test_openxr.sh
```

如果 `hello_xr -G Vulkan` 和 `hello_xr -G OpenGL` 都能显示，说明当前 OpenXR 链路可用。

---

## 4. MuJoCo 双目离屏测试：保存左右眼图片

这一步不需要 VIVE，也不需要 Monado。它验证 MuJoCo 相机本身是否正确。

```bash
cd ~/mujoco_vive_scripts
source .venv/bin/activate
bash scripts/run_save_frames.sh
```

输出在：

```text
out/
├── left.png
├── right.png
└── sbs.png
```

---

## 5. MuJoCo 双目 SBS 窗口测试

窗口模式：

```bash
cd ~/mujoco_vive_scripts
source .venv/bin/activate
bash scripts/run_sbs_windowed.sh
```

全屏到某个 GLFW monitor：

```bash
python -m mujoco_vive_scripts.list_monitors
python -m mujoco_vive_scripts.mujoco_sbs_glfw \
  --model models/stereo_endoscope_test.xml \
  --monitor 0 \
  --fullscreen \
  --fps 90
```

如果你要把 VIVE 当普通扩展显示器跑 SBS，请先停止 Monado：

```bash
pkill -9 monado-service hello_xr monado-cli 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"
```

然后运行：

```bash
bash scripts/run_sbs_fullscreen_monitor.sh 1
```

其中 `1` 换成 `list_monitors` 中 VIVE 对应的 monitor index。

---

## 6. 推荐验收顺序

```text
[1] start_monado_hmd_only.sh 能启动，VIVE 亮屏
[2] test_openxr.sh 里 Vulkan/OpenGL hello_xr 均成功
[3] save_stereo_frames.py 能生成 left/right/sbs 图片
[4] mujoco_sbs_glfw.py 在普通桌面窗口中显示左右目
[5] 如果 VIVE 暴露为普通 monitor，则全屏 SBS 到 VIVE
[6] 下一步再做 Python OpenXR swapchain 接入
```

---

## 7. 关于当前 Lighthouse/基站问题

你当前环境里已经观察到：

```text
基站关闭：Monado + VIVE Pro + hello_xr 成功
基站开启：可能触发 Lighthouse pulse report / buffer overflow
```

所以本阶段建议继续：

```text
基站关闭
手柄关闭
只验证双目显示
```

后续如果需要定位/姿态跟踪，再单独测试最新 Monado 源码版或 ASAN 构建。
