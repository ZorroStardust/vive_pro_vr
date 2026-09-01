# VIVE Pro / VIVE P110 在 Ubuntu 24.04 + Monado/OpenXR 下的调试记录

## 1. 目标

本文记录将一套 **HTC VIVE Pro / VIVE P110 VR 套件**接入 Ubuntu 24.04，用作 MuJoCo 医疗机器人双目内镜仿真的 OpenXR 显示端的调试过程。

当前阶段目标不是完整接入 MuJoCo，而是先完成最小显示链路：

```text
Ubuntu 24.04
→ NVIDIA RTX 5060 Ti
→ VIVE Pro / P110 HMD
→ Monado OpenXR runtime
→ VkDisplayKHR direct display
→ hello_xr Vulkan 示例程序
→ 头显中可见立体画面
```

最终验证结果：

```text
[成功] VIVE Pro 被 Linux 识别为 non-desktop HMD
[成功] Monado 能识别 VIVE Pro
[成功] Monado 能通过 VkDisplayKHR direct mode 接管 HMD
[成功] hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local 能在头显中显示立体场景
[成功] 头显中可见几个立方体和视野中心蓝色方块
```

---

## 2. 系统环境

测试环境：

```text
OS: Ubuntu 24.04
Session: GNOME Wayland
GPU: NVIDIA GeForce RTX 5060 Ti
NVIDIA Driver: 580.167.08
Kernel module: nvidia-driver-580-open / nvidia-dkms-580-open
HMD: HTC VIVE Pro / VIVE P110
OpenXR runtime: Monado
```

确认当前桌面会话类型：

```bash
echo $XDG_SESSION_TYPE
```

实际输出：

```text
wayland
```

确认 NVIDIA 驱动：

```bash
nvidia-smi
```

关键输出：

```text
Driver Version: 580.167.08
GPU: NVIDIA GeForce RTX 5060 Ti
```

确认 NVIDIA open kernel module：

```bash
dpkg -l | grep -i nvidia | grep 580
modinfo -F license nvidia
```

实际环境中使用的是：

```text
nvidia-dkms-580-open
nvidia-driver-580-open
license: Dual MIT/GPL
```

---

## 3. VIVE USB 设备检查

连接 VIVE Link Box 后，检查 USB 设备：

```bash
lsusb | grep -iE "0bb4|vive|htc|28de"
```

典型识别结果包括：

```text
0bb4:030e HTC USB2.0 Hub
0bb4:030a HTC USB2.0 Hub
0bb4:0306 HTC Vive Hub Bluetooth 4.1
0bb4:030b HTC VIVE Pro Multimedia Audio
0bb4:0309 HTC VIVE Pro
0bb4:030c HTC VIVE Pro Multimedia Camera
28de:2101 Valve Watchman Dongle
28de:2300 Valve LHR
```

说明：

```text
0bb4:*  是 HTC / VIVE 相关 USB 设备
28de:*  是 Valve Lighthouse / Watchman 相关设备
```

其中对当前显示链路最重要的是：

```text
0bb4:0309 HTC VIVE Pro
```

---

## 4. DRM / DisplayPort 设备检查

检查 DRM connector：

```bash
for f in /sys/class/drm/card*-*/status; do
  echo "$f: $(cat "$f")"
done
```

实际关键结果：

```text
/sys/class/drm/card1-DP-1/status: connected
/sys/class/drm/card1-DP-2/status: connected
/sys/class/drm/card1-DP-3/status: connected
/sys/class/drm/card1-HDMI-A-1/status: disconnected
```

进一步检查 VIVE 所在 connector：

```bash
cat /sys/class/drm/card1-DP-3/status
cat /sys/class/drm/card1-DP-3/modes
```

期望输出：

```text
connected
2880x1600
```

使用 `modetest` 可看到更完整信息：

```bash
modetest | grep -A20 -B5 "DP-3"
```

关键结果：

```text
DP-3 connected
2880x1600 90.04
non-desktop value: 1
```

结论：

```text
VIVE Pro 已经被 NVIDIA DRM 正确识别；
其显示模式为 2880x1600@90.04Hz；
该设备被标记为 non-desktop=1。
```

这也是 GNOME Displays 不把它显示成普通扩展屏的原因。

---

## 5. 为什么 GNOME Displays 看不到 VIVE

VIVE Pro 在 Linux DRM 中被标记为：

```text
non-desktop=1
```

这表示它不是普通桌面显示器，而是 VR HMD。GNOME Displays 通常不会把这种设备作为普通扩展屏显示。

因此以下路线不可行或不稳定：

```text
MuJoCo / GLFW
→ 直接把窗口全屏到 VIVE 扩展屏
```

因为 VIVE 并没有作为普通 monitor 暴露给 GNOME 桌面环境。

最终采用的路线是：

```text
OpenXR client
→ Monado
→ VkDisplayKHR direct mode
→ VIVE Pro
```

---

## 6. Monado / OpenXR 安装

Ubuntu 24.04 中包名不是简单的 `monado`，而是多个拆分包。

搜索：

```bash
apt-cache search monado
```

安装：

```bash
sudo apt install -y \
  monado-service \
  monado-cli \
  monado-gui \
  libopenxr1-monado \
  libopenxr-loader1 \
  libopenxr-dev \
  libopenxr-utils \
  xr-hardware \
  vulkan-tools \
  wayland-utils \
  tmux
```

确认关键程序存在：

```bash
which monado-service
which monado-cli
which hello_xr
```

实际结果：

```text
/usr/bin/monado-service
/usr/bin/monado-cli
/usr/bin/hello_xr
```

确认 OpenXR runtime manifest：

```bash
ls /usr/share/openxr/1/openxr_monado.json
ls /etc/xdg/openxr/1/active_runtime.json
```

测试中显式使用：

```bash
export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
```

---

## 7. udev / 权限配置

初始运行 `monado-service` 时曾出现 USB 权限问题：

```text
libusb_open failed: LIBUSB_ERROR_ACCESS
Could not open Vive device.
Unable to find HMD
```

处理方式：

```bash
sudo apt install -y xr-hardware
sudo udevadm control --reload-rules
sudo udevadm trigger
```

将当前用户加入相关组：

```bash
sudo usermod -aG video,render,input,plugdev $USER
```

然后重新登录或重启：

```bash
sudo reboot
```

确认用户组：

```bash
groups
```

建议包含：

```text
video render input plugdev
```

注意：不要用 `sudo` 直接运行 OpenXR 客户端或脚本。
否则 socket 路径可能变成：

```text
/run/user/0/monado_comp_ipc
```

而普通用户应使用：

```text
/run/user/1000/monado_comp_ipc
```

---

## 8. Vulkan / GPU 检查

确认 Vulkan 能看到 NVIDIA GPU：

```bash
vulkaninfo | grep -iE "deviceName|driverName|driverID" -A2 -B2
```

关键输出应包含：

```text
deviceName = NVIDIA GeForce RTX 5060 Ti
driverID = DRIVER_ID_NVIDIA_PROPRIETARY
driverName = NVIDIA
```

如果同时看到 `llvmpipe`，不一定是问题。
关键是 OpenXR / Vulkan client 最终应选择 NVIDIA GPU。

在成功日志中，`hello_xr` 显示：

```text
Sorted order:
[0] NVIDIA GeForce RTX 5060 Ti
[1] llvmpipe
```

说明 Vulkan 设备排序正确。

---

## 9. Wayland 路线测试与失败原因

当前桌面环境为：

```text
GNOME Wayland
```

检查 Wayland DRM lease：

```bash
wayland-info | grep -iE "drm_lease|wp_drm_lease|lease"
```

实际没有相关输出。说明当前 GNOME Wayland 没有向应用暴露 `wp_drm_lease_device_v1`。

因此 Monado 的 Wayland direct lease 路线不可用。

曾测试：

```bash
XRT_COMPOSITOR_FORCE_WAYLAND=1 \
XRT_COMPOSITOR_WAYLAND_CONNECTOR=DP-3 \
monado-service
```

结果：

```text
Target backend wayland initialized
```

但这是 Wayland Windowed 模式，会在普通桌面显示窗口，不会真正输出到 VIVE HMD。

结论：

```text
GNOME Wayland + Wayland Windowed 可运行，但不是目标路线；
GNOME Wayland 当前没有 DRM lease，不能直接通过 Wayland direct 接管 HMD；
最终改走 TTY + VkDisplayKHR direct mode。
```

---

## 10. 为什么必须使用 TTY

在图形桌面里运行 Monado VkDisplayKHR direct mode 时曾出现：

```text
vkCreateSwapchainKHR: VK_ERROR_INITIALIZATION_FAILED
```

原因是：

```text
GNOME Wayland / X11 compositor 正占用显示设备；
Monado VkDisplayKHR direct backend 需要独占显示硬件；
在图形会话中不容易拿到 DRM master / direct display 控制权。
```

切换到纯 TTY 后，Monado 可以成功接管 VIVE。

切换 TTY：

```bash
sudo chvt 3
```

或者使用快捷键：

```text
Ctrl + Alt + F3
```

回到图形桌面：

```text
Ctrl + Alt + F2
```

或根据系统配置尝试：

```text
Ctrl + Alt + F1
Ctrl + Alt + F7
```

---

## 11. Monado direct display 成功参数

当前机器上，VIVE Pro 对应的 Vulkan display index 是：

```text
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2
```

成功选择的显示设备：

```text
Will use display: HTC Corporation VIVE Pro (DP-4)
2880x1600@90.04
```

注意：

```text
DRM connector 中是 card1-DP-3；
Monado / Vulkan display 日志中显示为 DP-4；
二者编号不一定完全一致。
```

Monado 启动核心环境：

```bash
export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR=/run/user/$(id -u)
unset DISPLAY
unset WAYLAND_DISPLAY
```

Monado direct mode 参数：

```bash
XRT_LOG=debug \
XRT_COMPOSITOR_LOG=debug \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
XRT_COMPOSITOR_DESIRED_MODE=0 \
monado-service
```

成功标志：

```text
Selected Vulkan Display Direct-Mode backend
Will use display: HTC Corporation VIVE Pro
2880x1600@90.04
Created listening socket /run/user/1000/monado_comp_ipc
BEGIN_SESSION
```

---

## 12. 遇到过的关键问题与原因

### 12.1 `pkill -f hello_xr` 误杀脚本

旧脚本名：

```text
run_vive_hello_xr_tty.sh
```

其中包含：

```bash
pkill -f hello_xr
```

由于脚本文件名本身包含 `hello_xr`，`pkill -f hello_xr` 会匹配完整命令行，从而把脚本自己杀掉。

现象：

```text
zsh: terminated ./run_vive_hello_xr_tty.sh
```

修正：

```bash
pkill -x hello_xr
pkill -x monado-service
```

不要用：

```bash
pkill -f hello_xr
```

---

### 12.2 后台运行 monado-service 导致 stdin epoll 错误

早期脚本将 `monado-service` 直接放后台：

```bash
monado-service > monado.log 2>&1 &
```

出现：

```text
ERROR [init_epoll] epoll_ctl(stdin) failed '-1'
ERROR [init_all] Failed to init ipc main loop!
```

原因：

```text
monado-service 需要一个真实终端 / 伪终端；
直接后台运行时 stdin 状态不符合它的 epoll 预期。
```

解决：

```text
使用 tmux 给 monado-service 提供伪终端。
```

---

### 12.3 hello_xr 参数丢失

曾经脚本中通过嵌套 `bash -lc "..."` 运行 `hello_xr`，导致参数被错误展开，实际执行成：

```bash
hello_xr
```

而不是：

```bash
hello_xr -g Vulkan
```

现象：

```text
GraphicsPlugin parameter is required
```

修正：

```text
不要在 tmux send-keys 里复杂嵌套引号；
将 monado 和 hello_xr 分别写成独立脚本，再由 tmux 执行。
```

正确的 hello_xr 参数：

```bash
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v
```

---

### 12.4 `0 active app session(s)`

如果 Monado 日志一直是：

```text
Doing warm start, 0 active app session(s).
Stopped native session, 0 active app session(s).
```

说明：

```text
Monado 自己启动了；
但 OpenXR client 没有真正连接或没有创建 session。
```

常见原因：

```text
hello_xr 参数错误
OpenXR runtime 没设对
socket 路径不对
脚本用 sudo 运行
hello_xr 被提前杀掉
```

成功后应出现：

```text
Started native session, 1 active app session(s).
```

---

### 12.5 基站不是当前阶段的关键变量

开启或关闭 Lighthouse 基站，可能影响 tracking / pose，但不应是 HMD direct display 点亮的决定因素。

当前阶段只验证显示链路：

```text
OpenXR client
→ Monado
→ VIVE HMD
```

因此建议排查显示阶段时先不引入基站变量。
等 `hello_xr` 稳定出图后，再考虑 Lighthouse tracking。

---

## 13. 残留状态清理

如果出现以下情况：

```text
一直 waiting for Monado IPC socket
VIVE 红灯
VIVE 不亮屏
之前成功过但下一次失败
Monado 卡在早期初始化
```

先清理残留：

```bash
tmux kill-session -t vive_stage1 2>/dev/null || true
pkill -9 hello_xr 2>/dev/null || true
pkill -9 monado-service 2>/dev/null || true
rm -f /run/user/$(id -u)/monado_comp_ipc
```

然后物理重置 VIVE Link Box：

```text
1. 拔掉 Link Box 电源
2. 拔掉 Link Box USB
3. 等 10–15 秒
4. 先插 Link Box 电源
5. 再插 USB
6. 等待 5 秒左右
```

再次检查：

```bash
lsusb | grep -iE "0bb4|vive|htc|28de"
cat /sys/class/drm/card1-DP-3/status
cat /sys/class/drm/card1-DP-3/modes
```

期望：

```text
connected
2880x1600
```

---

## 14. 最终成功脚本

当前成功脚本为：

```bash
~/vive_stage1_tmux.sh
```

脚本内容：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

SESSION="vive_stage1"
RUN_SECONDS="${RUN_SECONDS:-90}"
LOGDIR="$HOME/vive_openxr_test_$(date +%Y%m%d_%H%M%S)"

mkdir -p "$LOGDIR"
ln -sfn "$LOGDIR" "$HOME/vive_openxr_latest"

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/driver.log") 2>&1

echo "started at $(date)"
echo "LOGDIR=$LOGDIR"
echo "XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

tmux kill-session -t "$SESSION" 2>/dev/null || true
pkill -x hello_xr 2>/dev/null || true
pkill -x monado-service 2>/dev/null || true
rm -f "$XDG_RUNTIME_DIR/monado_comp_ipc"
sleep 1

cat > "$LOGDIR/run_monado.sh" <<'MONADO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/monado.log") 2>&1

echo "[monado] starting at $(date)"
echo "[monado] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[monado] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

XRT_LOG=debug \
XRT_COMPOSITOR_LOG=debug \
XRT_COMPOSITOR_PRINT_MODES=1 \
XRT_COMPOSITOR_FORCE_VK_DISPLAY=2 \
XRT_COMPOSITOR_DESIRED_MODE=0 \
monado-service

echo "[monado] exited with $?"
MONADO_EOF

cat > "$LOGDIR/run_hello.sh" <<'HELLO_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

export XR_RUNTIME_JSON=/usr/share/openxr/1/openxr_monado.json
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
unset DISPLAY
unset WAYLAND_DISPLAY

exec > >(tee -a "$LOGDIR/hello_xr.log") 2>&1

echo "[hello] starting at $(date)"
echo "[hello] XR_RUNTIME_JSON=$XR_RUNTIME_JSON"
echo "[hello] XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
echo "[hello] socket:"
ls -l "$XDG_RUNTIME_DIR/monado_comp_ipc" || true

echo
echo "[hello] help:"
hello_xr -h || true

echo
echo "[hello] running:"
echo "hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v"

XR_LOADER_DEBUG=all \
timeout --foreground "${RUN_SECONDS:-90}s" \
hello_xr -g Vulkan -ff Hmd -vc Stereo -s Local -v

echo "[hello] exit status: $?"
HELLO_EOF

chmod +x "$LOGDIR/run_monado.sh" "$LOGDIR/run_hello.sh"

echo "[start tmux monado]"
tmux new-session -d -s "$SESSION" -n monado "env LOGDIR='$LOGDIR' bash '$LOGDIR/run_monado.sh'"

echo "[wait socket]"
for i in $(seq 1 40); do
  if [ -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
    echo "Monado IPC socket ready after ${i}s" | tee "$LOGDIR/result.txt"
    break
  fi

  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "FAILED: tmux/monado session exited before socket was created" | tee "$LOGDIR/result.txt"
    break
  fi

  sleep 1
done

if [ ! -S "$XDG_RUNTIME_DIR/monado_comp_ipc" ]; then
  echo "FAILED: no Monado IPC socket" | tee -a "$LOGDIR/result.txt"
  tmux capture-pane -t "$SESSION:0.0" -p -S -3000 > "$LOGDIR/monado_capture_failed.log" 2>/dev/null || true
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  exit 1
fi

echo "[start tmux hello]"
tmux split-window -h -t "$SESSION:0.0" "env LOGDIR='$LOGDIR' RUN_SECONDS='$RUN_SECONDS' bash '$LOGDIR/run_hello.sh'"

echo "hello_xr started, waiting ${RUN_SECONDS}s..."
sleep "$RUN_SECONDS"
sleep 5

tmux capture-pane -t "$SESSION:0.0" -p -S -4000 > "$LOGDIR/monado_capture_after_hello.log" 2>/dev/null || true
tmux capture-pane -t "$SESSION:0.1" -p -S -4000 > "$LOGDIR/hello_xr_capture.log" 2>/dev/null || true

{
  echo
  echo "=== summary ==="
  grep -hEi "Will use display|2880x1600|Created listening|BEGIN_SESSION|active app|client|frame|layer|projection|session|state|visible|focused|running|error|failed|exit status|GraphicsPlugin" \
    "$LOGDIR"/monado.log \
    "$LOGDIR"/monado_capture_after_hello.log \
    "$LOGDIR"/hello_xr.log \
    "$LOGDIR"/hello_xr_capture.log 2>/dev/null | tail -n 400 || true
} | tee -a "$LOGDIR/result.txt"

tmux kill-session -t "$SESSION" 2>/dev/null || true

echo "finished at $(date)" | tee -a "$LOGDIR/result.txt"
echo "LOGDIR=$LOGDIR"
```

赋予执行权限：

```bash
chmod +x ~/vive_stage1_tmux.sh
```

---

## 15. 运行流程

### 15.1 运行前清理

```bash
tmux kill-session -t vive_stage1 2>/dev/null || true
pkill -9 hello_xr 2>/dev/null || true
pkill -9 monado-service 2>/dev/null || true
rm -f /run/user/$(id -u)/monado_comp_ipc
```

必要时重置 Link Box：

```text
拔电源 + 拔 USB
等待 10–15 秒
先插电源
再插 USB
```

### 15.2 切换到 TTY

```bash
sudo chvt 3
```

或者：

```text
Ctrl + Alt + F3
```

登录普通用户，不要使用 root。

### 15.3 运行脚本

```bash
~/vive_stage1_tmux.sh
```

运行期间：

```text
普通显示器可能黑屏
VIVE 可能从红灯变绿灯
VIVE 内应出现 hello_xr 的立体场景
```

### 15.4 回到桌面

脚本默认运行约 90 秒后结束。
回到桌面：

```text
Ctrl + Alt + F2
```

查看最新日志：

```bash
LOGDIR=~/vive_openxr_latest

cat "$LOGDIR/result.txt"

echo
echo "=== hello_xr ==="
cat "$LOGDIR/hello_xr.log"

echo
echo "=== Monado session/frame ==="
grep -hEi "active app|client|frame|layer|projection|session|state|visible|focused|running|error|failed" \
  "$LOGDIR"/monado.log "$LOGDIR"/monado_capture_after_hello.log 2>/dev/null | tail -n 300
```

---

## 16. 成功判据

### 16.1 头显内视觉结果

成功时，VIVE 头显中可以看到：

```text
几个立方体
视野中心附近的蓝色方块
立体场景
```

### 16.2 Monado 日志成功标志

```text
Will use display: HTC Corporation VIVE Pro
2880x1600@90.04
Created listening socket /run/user/1000/monado_comp_ipc
BEGIN_SESSION
Started native session, 1 active app session(s)
```

其中最关键的是：

```text
1 active app session(s)
```

这表示 OpenXR client 已经成功连接 Monado 并创建 active session。

### 16.3 hello_xr 日志成功标志

```text
RuntimeName=Monado(XRT)
Selected devices
Head: 'HTC Vive Pro (vive)'
Using system 1 for form factor XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY
View Configuration Type: XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO
Environment Blend Mode: XR_ENVIRONMENT_BLEND_MODE_OPAQUE
```

并且不应再出现：

```text
GraphicsPlugin parameter is required
```

---

## 17. 关于掉帧日志

成功后 Monado 日志中出现大量：

```text
Frame late by 11.11ms
Dropping old missed frame in favour for completed new frame
```

解释：

```text
11.11ms 正好对应 90Hz 的一个帧周期；
说明当前测试链路有帧节奏或调度延迟问题。
```

这不影响“显示链路已打通”的结论。

后续接入 MuJoCo 时需要关注：

```text
OpenXR frame loop
Vulkan queue priority
CPU/GPU 同步
MuJoCo 渲染耗时
是否使用 CPU readback
是否保持 90Hz
```

但在当前阶段可以暂时接受。

---

## 18. 当前阶段结论

当前调试阶段已经完成：

```text
[已完成] Ubuntu 24.04 + RTX 5060 Ti + VIVE Pro 硬件识别
[已完成] USB / DRM / EDID / non-desktop 检查
[已完成] udev 权限修复
[已完成] Monado / OpenXR runtime 安装
[已完成] 排除 GNOME Wayland 普通窗口路线
[已完成] 确认 GNOME Wayland 无 DRM lease
[已完成] 使用 TTY + VkDisplayKHR direct mode 接管 VIVE
[已完成] 使用 tmux 解决 monado-service stdin/pty 问题
[已完成] 修复 hello_xr 参数丢失问题
[已完成] hello_xr Vulkan stereo 场景成功显示在 VIVE 中
```

当前可作为后续 MuJoCo 接入的稳定基础链路：

```text
MuJoCo 双目相机渲染
→ 自定义 OpenXR client
→ Vulkan/OpenXR swapchain
→ Monado
→ VIVE Pro / P110
```

---

## 19. 下一阶段建议

下一阶段不建议直接接完整 MuJoCo，而建议先写一个最小自定义 OpenXR client：

```text
左眼显示纯红色
右眼显示纯蓝色
不使用手柄
不依赖复杂 tracking
只验证左右眼 swapchain 提交
```

成功后再替换为：

```text
MuJoCo left camera texture
MuJoCo right camera texture
```

推荐阶段：

```text
Stage 1A: hello_xr 出图                        [已完成]
Stage 1B: 自定义 OpenXR client 显示左右纯色      [下一步]
Stage 1C: 自定义 OpenXR client 显示测试图片      [下一步]
Stage 1D: MuJoCo 双目相机画面接入 OpenXR        [后续]
Stage 2: 加入内镜圆形视场 / 畸变 / 暗角          [后续]
Stage 3: 优化 GPU texture path / 低延迟           [后续]
```

---

## 20. 后续事件记录：双路 live video + teleop 卡顿（已解决）

2026-08-27 至 2026-09-01 排查了双路 1920×1080@60 实时内窥镜视频与
teleop 同时运行时的画面及 CANFD 调度卡顿。最终根因是采集线程中的双路 NumPy
YUV→RGB 转换，不是 Omega、力反馈、USB/xHCI、HMD 或 Monado。

修复后改为发布原始 YUV，并在 GPU fragment shader 中转换；真实联合运行确认
卡顿消失。完整实验矩阵、错误归因复盘、性能数据和修复结构见：

`mujoco_vive_scripts/live_video_teleop_stutter_analysis.md`
