# VR 画面周期性卡顿 — omega 触觉驱动与 xHCI 干扰分析

**日期**: 2026-08-27
**现象**: `xr-live-video`（内窥镜立体视频 → VIVE Pro）与
`~/projects/surgical_dual_arm_ires` 的 teleop 同时运行时，VR 画面周期性卡顿
（体感像"游戏丢包"，每次约 0.5–1s，间隔约 10–30s，随会话变密）。

## 结论（TL;DR）

触发源是 **Force Dimension omega 触觉主手的 libdhd 驱动**：它以 1kHz 轮询
两个 omega 设备（`libusb_bulk_transfer`，内部自带 `pthread_create` +
`pthread_setschedparam` 控制线程）。omega 与采集卡、VIVE IMU 同在 xHCI
控制器 `0000:80:14.0` 上；dhd 的 RT 线程高频锤打 xHCI → 抢占处理 UVC
完成的 kworker → 控制器锁竞争 → **UVC 帧交付延迟（采集卡顿）+ VIVE IMU
延迟（monado 合成器 timing 破坏，渲染卡顿）同时发生**。

排除项（有实验依据）：内窥镜视频源、采集盒、USB 线/口带宽、GPU/浏览器
（meshcat）、monado 合成器本身、Xwayland。

## 证据链

### 1. A/B 实验（templog.md 统计）

| 场景 | 时长 | 采集丢帧事件(每2s<110帧) | UPL≥5ms | 卡顿 |
|------|------|--------------------------|---------|------|
| 仅 xr-live-video | 6m52s | **0** | 0 | 无 |
| xr-live-video + teleop（Edge/meshcat 关）| 4m28s | **31** | 3 | 有 |
| xr-live-video + teleop 空闲（omega 拔掉）| — | 仅 1 次 | 1 次 | 几乎无 |
| xr-live-video + teleop 空闲（omega 插着读位置）| — | 大量 | 周期性 | 明显 |

丢帧时左右眼帧计数严格同步（67/67、68/68、109/109…），说明是共同上游，
而非单路故障。

### 2. 埋点日志（决定性）

```
[XR GAP] frame loop stalled 62ms @t=33083.5 ...
[CAP GAP] eye=L 141ms @t=33083.6 select_timeouts_in_gap=0 (0=thread starved, >0=device silent)
[CAP GAP] eye=R 167ms @t=33083.6 select_timeouts_in_gap=0 ...
```

- `select_timeouts_in_gap=0`：采集线程等待 select 期间没有超时——设备一直在
  交货，恢复后立即有帧。但每 ~150ms 才拿到一波帧。
- `[XR GAP]`（渲染循环 30–91ms 停顿，正常 11ms）与 `[CAP GAP]` 时间戳重合：
  渲染线程不碰 USB，却同步卡顿 → 两者共同依赖的组件（xHCI/内核）出问题。
- LIVE 行 `TO=0/0`：整个会话无一次 0.2s 级 select 超时，无设备错误。

### 3. dmesg（卡顿期间）

```
19:25:28 usb 1-10.2: new high-speed USB device ... 1451:0301 omega.x haptic device
19:25:31 usb 1-10.3: new high-speed USB device ... 1451:0402 omega.x haptic device
（之后直到卡顿结束，没有任何 xhci/usb/uvc 事件）
```

无枚举、无复位、无 error → 不是设备级故障，是**静默的延迟/锁竞争**。

### 4. libdhd 静态证据

`libdhd.so.3.17.7`（`~/.local/lib/`）：

```
U pthread_create
U pthread_setschedparam / pthread_getschedparam
U timerfd_create / timerfd_settime / nanosleep
dhdComUSB-libusb.cpp   （libusb-1.0, libusb_bulk_transfer, ...）
FDO_THREAD_PRIORITY_DEFAULT/HIGH/LOW/MAX/MIN
```

→ dhd 打开设备时会创建自己的 I/O 控制线程并调整其调度优先级；通信层是
libusb 同步 bulk 传输。

### 5. 系统条件（让 RT 假设成立）

- `capsh --print`：用户 bounding set 含 `cap_sys_nice` → dhd 设 RT 优先级**会成功**。
- `/proc/sys/kernel/sched_rt_runtime_us` = 950000（RT 可用 95% CPU 时间）。
- 全部 20 核 `scaling_governor=powersave`（Intel HWP）。
- USB 拓扑：采集卡 `2-1`/`2-4`、VIVE 链路盒 `2-9`、omega `1-10.2`/`1-10.3`
  全部挂在同一个 xHCI 控制器 `0000:80:14.0`（该控制器挂 PCIe 00:06.0 之下，
  后者 18:14 出现过一次 Correctable RxErr，未见关联）。

### 6. 单次异常的解释

omega 拔掉时（A/B 第 3 行）唯一一次卡顿：haptic 线程每秒重试 `dhd.open()`
（`reconnect_delay_s=1.0`），每次尝试触发 libusb 全设备扫描
（`libusb_get_device_list` + open/close 链），偶发尖峰与卡顿重合。

## 缓解方案与状态

| # | 方案 | 状态 |
|---|------|------|
| 1 | **teleop 降低轮询率** `haptic_poll_hz` 1000→200（映射循环仅 50Hz，200Hz 余量 4×）；可选 `force_feedback=False` 再砍一半 USB 写流量 | 待验证（2026-08-27 已改配置）|
| 2 | LD_PRELOAD shim 拦截 `pthread_setschedparam`，让 dhd 控制线程保持普通优先级（若验证到 FF 线程满核跑）| 备用 |
| 3 | `taskset` 把 teleop 钉在固定核，隔离纯 CPU 竞争 | 备用（对锁竞争无效）|
| 长期 | Force Dimension SDK 通用行为；teleop 50Hz 映射无需 1kHz 位置采样，建议默认低轮询 | — |

### 验证方法（复现时）

```bash
# 卡顿发生时另一终端：
ps -eLo pid,tid,cls,pri,pcpu,comm --sort=-pcpu | head -20
# 看 teleop python 进程里是否有 cls=FF/RR、pri 高、%CPU 满核的线程
cat /proc/<teleop_pid>/task/<tid>/sched | head -3   # 确认调度策略
```

若 FF 线程实锤 → 上方案 2；若降频后卡顿消失/按比例减轻 → 方案 1 即长期修复。

## 相关埋点（已合入 xr-live-video，commit 4091c01）

- LIVE 行：`DT=`（打印间隔）、`TO=L/R`（select 超时计数）
- `[CAP GAP]`：采集停顿事件 `eye/gap_ms/@t/select_timeouts_in_gap`
- `[XR GAP]`：渲染循环停顿（>30ms，正常 11ms）
- 判别规则：timeouts=0 → 线程被饿死或 <200ms 的设备交付延迟；>0 → 设备静默
