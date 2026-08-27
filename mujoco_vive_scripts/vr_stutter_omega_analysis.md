# VR 画面周期性卡顿 — omega 触觉设备与 xHCI 干扰分析

**日期**: 2026-08-27（更新：受控实验后）
**现象**: `xr-live-video`（内窥镜立体视频 → VIVE Pro）与
`~/projects/surgical_dual_arm_ires` 的 teleop 同时运行时，VR 画面周期性卡顿
（体感像"游戏丢包"，每次约 0.5–1s，间隔约 5–30s）。

## 结论（TL;DR，受控实验后定稿）

**只要 Force Dimension omega.x 主手 USB 插在本机（即使没有任何软件打开它），
其固件就以 5.0s 为周期在 USB 总线上产生一个 ~0.9s 的扰动事件，导致同一
xHCI 控制器（本机唯一的 `0000:80:14.0`）上的等时视频流（两张采集卡）与
VIVE 链路盒一起周期性丢帧。**

关键性质：

- **与软件完全无关**：不 open、不轮询、不跑 teleop 也能复现（受控实验）；
  `haptic_poll_hz` 从 1000→200 **无任何效果**。
- 精确 **5.0s 周期**（30 个簇，间隔 4.9–5.1s），每簇 ~0.9s，期间每眼以
  ~6.7Hz 交付（正常 60fps），每簇每眼丢 ~50 帧 ≈ 18% 帧损失。
- 内核日志几乎无痕：仅偶发 `uvcvideo 2-4:1.1: Failed to resubmit video
  URB (-1)`；无 reset、无枚举事件、无错误。
- 本机只有 **一个 USB 控制器**（`lspci`：仅 80:14.0，无雷电/第二 xHCI），
  无法通过换口规避；采集卡、VIVE、omega 全在这一个控制器上。

## 证据链

### 1. A/B 实验（templog.md 统计）

| 场景 | 采集丢帧事件 | 卡顿 |
|------|-------------|------|
| 仅 xr-live-video | **0**（6m52s）| 无 |
| xr-live-video + teleop（Edge 关）| **31**（4m28s）| 有 |
| teleop 空闲 + omega 拔掉 | 仅 1 次 | 几乎无 |
| teleop 空闲 + omega 插着 | 大量，~5s 周期 | 明显 |
| teleop + haptic_poll_hz=200 | 不变 | 依旧 |

### 2. 受控复现实验（决定性，2026-08-27 20:22–20:27）

实验设计：裸 `StereoCapture`（无 GL/OpenXR/teleop）+ dhd 探针，分阶段：
A) 打开设备不轮询 30s，B) 1000Hz 轮询 60s，C) 回到只打开 30s；
对照组：omega 插着但**完全不打开** 30s。

结果（`/tmp/opencode/cap_gaps.txt`、`phase_control.txt`）：

```
clusters: 30    intervals: [4.8, 4.9, 5.1, 4.9, 5.0, 5.1, 5.0, ...]  ← 精确 5.0s
每簇: dur≈0.9s, ~12 个 gap 事件（每眼 ~6），gap≈150ms（= 6.7Hz 交付）
对照组（插着但不打开）: 同样的 5.0s 簇 → 触发条件是"插着"，与软件无关
IRQ: 采集时 ~12.5k/s，簇期间略降（等时完成变少）
kernel: 20:26:03/20:26:35 偶发 "uvcvideo 2-4/2-1: Failed to resubmit video URB (-1)"
```

### 3. 埋点日志（用户会话）

```
[XR GAP] frame loop stalled 62ms @t=33083.5 ...
[CAP GAP] eye=L 141ms @t=33083.6 select_timeouts_in_gap=0 ...
```

- `select_timeouts_in_gap=0` + 150ms 稳定间隔 = 设备交付被节流到 ~6.7Hz，
  不是线程被饿死（饿死会恢复后突发补帧）。
- `[XR GAP]` 与 `[CAP GAP]` 同步：monado 合成器也受同一扰动影响
  （VIVE 链路盒在同一控制器上）。

### 4. 已排除的假设（有据）

| 假设 | 排除依据 |
|------|---------|
| libdhd RT 优先级线程抢占 | 有效 capability 集为空（`Current: =`）、`RLIMIT_RTPRIO=0`（`ulimit -r`=0，limits.d 仅 pipewire 有 rtprio）→ `pthread_setschedparam(SCHED_FIFO)` 必然 EPERM；实测 dhd 只开一个 SCHED_OTHER 的 `libusb_event` 线程（0% CPU）|
| USB 事务速率/带宽饱和 | 轮询率 1000→200Hz 症状零变化；open-idle 阶段（几乎零事务）照样 5s 簇；IRQ 速率无尖峰 |
| CPU 竞争/GIL | 簇在无 teleop、无 Qt、无 IK 的裸实验里同样出现 |
| 内窥镜源/采集盒/线材 | 拔 omega 即零丢帧，同一源同一线 |
| GPU/浏览器/meshcat | Edge 关闭仍复现；裸实验无 GL |
| USB 枚举/复位风暴 | 卡顿期间 dmesg/journalctl 无任何 USB 事件 |

### 5. libdhd 静态证据（仅供参考）

`libdhd.so.3.17.7`：`pthread_create` + `pthread_setschedparam` + timerfd +
`libusb_bulk_transfer`（`dhdComUSB-libusb.cpp`）。但这些与卡顿无关——
扰动在设备未打开时也存在，纯属固件/电气层行为。

## 缓解方案与状态

| # | 方案 | 状态 |
|---|------|------|
| 1 | ~~降低轮询率~~ `haptic_poll_hz` 1000→200 | **无效**（保留无害）|
| 2 | **PCIe USB3 扩展卡**（Renesas/ASMedia xHCI）插 omega——把 5s 扰动隔离到第二控制器 | **推荐长期修复**（本机无第二控制器）|
| 3 | 不遥操作时**拔掉 omega** | 已验证有效（templog 实验 3）|
| 4 | **强制挂起 omega 端口**（VR-only 会话软件替代拔线）：`script_test/omega_usb_suspend.sh off/on`（sudo 写 `power/level=suspend`）| 待验证；遥操作前须 `on` 恢复 |
| 5 | omega 固件升级 / 询问 Force Dimension | 远期 |

### 验证方法（复现时）

```bash
script_test/omega_usb_suspend.sh off    # 挂起后观察 xr-live-video 是否不再有 [CAP GAP]
script_test/omega_usb_suspend.sh on     # 恢复
```

## 相关埋点（已合入 xr-live-video，commit 4091c01）

- LIVE 行：`DT=`（打印间隔）、`TO=L/R`（select 超时计数）
- `[CAP GAP]`：采集停顿事件 `eye/gap_ms/@t/select_timeouts_in_gap`
- `[XR GAP]`：渲染循环停顿（>30ms，正常 11ms）
