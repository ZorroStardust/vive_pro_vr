# 双路实时视频与 teleop 同时运行时卡顿：完整诊断与修复记录

- **问题时间**：2026-08-27 ～ 2026-09-01
- **影响命令**：`pixi run xr-live-video`、`pixi run xr-live-video-screen`
- **关联项目**：`~/projects/surgical_dual_arm_ires` teleop
- **最终状态**：已修复；2026-09-01 联合运行确认卡顿消失
- **修复提交**：`f1d0d53`

## 1. 最终结论

根因不是 Force Dimension Omega 主手、力反馈、USB 固件周期扰动、VIVE HMD、
Monado，也不是摄像头 USB 原始带宽不足。

根因是旧版视频采集线程对左右眼每一帧执行 NumPy YUV→RGB 转换：

```text
2 路 × 1920 × 1080 × 60 fps
= 248,832,000 pixel/s
```

单眼 1080p YUYV→RGB 实测平均约 20 ms，p95 约 24～26 ms，最慢约
34～36 ms；NV12 转换也约 19.2 ms。单帧 60 Hz 的时间预算只有 16.67 ms，
因此单眼转换已经经常超过预算，双眼还会产生大量临时数组、内存分配和内存带宽
竞争。

teleop 加入后，视频转换、渲染、Python 调度、机械臂 SDK/IPC 共同竞争 CPU 与
内存资源。结果同时表现为：

- VR/桌面实时视频停顿；
- teleop 打印 `[CANFD SLOW]`；
- 两支机械臂的 CANFD 发送延迟变大，控制周期可能错过 deadline。

因此这不是“仅影响 VR 的显示问题”。旧实现的负载确实会影响遥操作命令时序；
`result=0` 只表示 CANFD 调用最终成功，不表示它在期望的控制周期内完成。

正式修复将 YUYV/NV12→RGB 移到 GPU fragment shader。采集线程现在只复制原始
YUV 帧，随后立即 QBUF，再发布原始帧。联合运行已确认卡顿消失。

## 2. 原始现象

初始复现条件：

1. 在 `vive_pro_vr` 中启动 `pixi run xr-live-video`；
2. 在 `surgical_dual_arm_ires` 中启动 teleop；
3. VR 画面开始频繁卡顿；
4. 每次明显卡顿附近，teleop 会出现类似日志：

```text
[CANFD SLOW] arm2: 9.5 ms result=0
[CANFD SLOW] arm1: 6.2 ms result=0
[CANFD SLOW] arm1: 17.0 ms result=0
```

`surgical_dual_arm_ires/src/arm_control/two_arm.py` 中的计时覆盖
`driver.stream_joint()`，并包含 `_canfd_lock` 内的驱动调用。当前两臂在同一
worker 中串行发送；50 Hz 周期预算为 20 ms。当 arm1 与 arm2 单次调用分别耗时
数毫秒到十几毫秒时，两次调用之和很容易占满或超过整个周期。

这条同步现象是重要证据：视频卡顿和控制延迟是同一个系统负载问题的两个表现，
而不是 VR 自身的孤立故障。

## 3. 排查过程

### 3.1 第一阶段：怀疑 Omega/力反馈（后来被证伪）

由于只有启动 teleop 后症状才明显，最初怀疑：

- Omega USB 设备扰动共享 xHCI；
- 1 kHz 主手轮询抢占 CPU；
- 力反馈写入导致周期性阻塞；
- 某一只 Omega 或控制器级故障影响摄像头/VIVE。

这个方向一度被错误地写成“Omega 固件每 5 秒干扰 USB”的确定结论，并据此建议
增加 PCIe USB 控制器、拔掉 Omega 或挂起 Omega USB 端口。

后续独立实验不支持这个结论：

- 分别只接入一只 Omega，卡顿模式没有形成可用于定位单个设备的差异；
- 关闭力反馈后仍然卡顿；
- 两只 Omega 同时连接并以约 999/1000 Hz 读取时，双路摄像头可以稳定运行；
- 两只 Omega 以 1 kHz 读取并执行零力写入时，双路摄像头仍可稳定运行；
- 项目的 `StereoCapture` 与同样的 DHD/Omega 访问组合也能稳定运行。

所以 Omega 的存在与早期日志之间至多是相关性，不能构成因果关系。Omega、DHD
轮询和力反馈均不是本次卡顿根因。

### 3.2 第二阶段：将 HMD/OpenXR 与实时视频解耦

关键对照：

| 实验 | 结果 | 推论 |
|---|---|---|
| HMD 测试卡单独运行 | 不卡 | HMD/Monado 基本链路正常 |
| HMD 测试卡 + teleop | 不卡 | teleop 本身不会必然拖垮 OpenXR/HMD |
| 桌面 live video + teleop | 卡 | 无 HMD/Monado 仍复现，问题在实时视频路径或共享系统资源 |

这组实验排除了“HMD 控制器级扰动”作为必要条件，并把范围收敛到摄像头实时视频
处理链路。

### 3.3 第三阶段：分辨捕获、双眼上传和像素处理负载

随后进行了四个正交实验：

| 实验 | 像素处理量 | 结果 |
|---|---:|---|
| 基线：双路 1920×1080@60 | 248.8 Mpixel/s | 卡顿、伴随 CANFD SLOW |
| A：双路 1920×1080@30 | 124.4 Mpixel/s | 明显改善，无卡顿、无 CANFD SLOW |
| B：双路 1280×720@60 | 110.6 Mpixel/s | 明显改善，无卡顿、无 CANFD SLOW |
| C：1920×1080@60，mono-to-both-eyes | 捕获/转换仍为 248.8 Mpixel/s | 无改善 |
| 1920×1080@60，强制 NV12 | USB 字节量降低，但仍做全帧 CPU 转换 | 无改善 |

这组结果直接指向“每秒在 CPU 上处理的像素数”：

- A、B 分别通过降低帧率和分辨率，把像素处理量降到约一半，症状同时消失；
- C 只减少了显示侧的一只纹理使用，但左右采集线程仍各自转换整帧，所以无改善；
- NV12 减少 USB 原始数据量，却没有移除 NumPy 全帧转换，所以无改善。

如果根因是原始 USB 带宽，NV12 应该明显改善；实际没有。相反，只要降低 CPU
像素处理量，YUYV 也能稳定。这是区分 USB 带宽与 CPU 转换瓶颈的决定性证据。

### 3.4 CPU 转换基准

旧版转换函数的本机测量：

| 格式/尺寸 | 单眼转换耗时 |
|---|---:|
| YUYV 1920×1080 | 平均约 20 ms，p95 约 24～26 ms，最大约 34～36 ms |
| NV12 1920×1080 | 平均约 19.2 ms |
| YUYV 1280×720 | 平均约 8.4 ms |

旧采集循环为：

```text
DQBUF
  → mmap 帧复制
  → NumPy YUV→RGB（大量临时数组和内存流量）
  → QBUF
  → 发布 RGB
```

也就是说，昂贵转换不仅发生在两个采集线程中，还发生在归还 V4L2 buffer 之前。

## 4. 为什么早期 Omega/USB 结论是错误的

旧文档 `vr_stutter_omega_analysis.md` 的结论已删除，原因如下。

### 4.1 测量点包含了 CPU 转换耗时

早期 `[CAP GAP]` 使用相邻两次用户态 DQBUF 返回时刻计算间隔。但旧循环在两次
DQBUF 之间执行 NumPy 转换和 QBUF，因此该间隔包含：

- CPU YUV→RGB；
- Python/NumPy 调度等待；
- 内存分配和回收；
- 线程被其他进程/线程延迟的时间；
- 真正的设备等待时间。

它不是纯粹的 USB 帧到达时间，不能据此认定“采集卡在 USB 层停止交付”。

### 4.2 `select_timeouts_in_gap=0` 被反向解释

旧结论把 `select_timeouts_in_gap=0` 解释成“设备被节流”。实际上，如果设备真的
在 `select()` 中长时间静默，更可能积累 timeout；零 timeout 更符合采集线程没有
及时回到 `select()`，即被 CPU 转换或调度延迟。

### 4.3 gap 事件不是驱动丢帧计数

早期只统计用户态间隔，没有使用 V4L2 kernel sequence 作为帧连续性依据。修复时
加入 kernel sequence 后还发现这两张采集卡的正常序列步长是 2；若机械地把
`delta > 1` 当成丢帧，会把每个正常帧都误报成一次 drop。现在实现会先学习每个
设备的正常 sequence stride，再统计真正的序列跳变。

### 4.4 后续可重复实验直接反驳

高频 Omega 读取、零力写入、关闭力反馈等实验均没有让独立双路捕获出现对应的
周期性故障；而降低视频像素处理量在不改变 Omega/USB 拓扑的情况下立即消除了
卡顿。因而旧因果链不成立。

基于错误结论创建的 `script_test/omega_usb_suspend.sh` 也已删除。不要再为了这个
问题挂起、拔掉 Omega，或购买额外 USB 控制器。

## 5. 正式修复

修复后的采集链路：

```text
采集线程：DQBUF → 复制原始 YUV → 立即 QBUF → 发布不可变 bytes
渲染线程：检测新 sequence → 上传 YUV 纹理 → GPU fragment shader 转 RGB
```

这不是端到端零拷贝：为了立即归还 MMAP buffer，仍有一次原始 YUV 字节复制；
但已经移除了每眼每帧的 CPU 色彩转换及其多个临时数组。

具体实现：

1. `v4l2_capture.py`
   - `StereoCapture` 发布原始 YUYV/NV12 bytes，不再在采集线程调用 `convert()`；
   - 原始帧复制后立即 QBUF；
   - 左右帧及各自应用 sequence 原子读取；
   - 增加 `copy_ms`、timeout、kernel sequence stride 和真实 drop 统计。
2. `xr_live_video.py`
   - YUYV 作为宽度减半的 RGBA8 packed texture 上传，通道对应 Y0/U/Y1/V；
   - shader 根据源像素奇偶选择 Y0/Y1，并保留水平方向线性插值；
   - NV12 分别上传 Y（R8）和 UV（RG8）纹理；
   - GPU shader 使用与旧 CPU 路径一致的 full-range YUV→RGB 矩阵；
   - 测试卡继续使用原 RGB 路径；
   - 桌面与 OpenXR 共用同一 renderer，因此同时得到修复。

## 6. 修复验证

### 6.1 无硬件 GL 验证

- Python `compileall` 与 `git diff --check` 通过；
- 在 compatibility-profile EGL 上编译 YUYV、NV12 shader；
- 使用合成 1080p 原始帧完成纹理上传和真实绘制；
- `glGetError() == GL_NO_ERROR`；
- YUYV/NV12 GPU 输出与旧 CPU 参考颜色逐像素对照，测试点误差为 0；
- RGB test-card 桌面回归通过。

### 6.2 双路摄像头实机验证

双路 1920×1080@60 桌面运行结果：

| 格式 | 帧率 | COPY（单眼） | 稳态 UPL（单眼） | timeout | drop |
|---|---:|---:|---:|---:|---:|
| YUYV | 约 60 fps | 约 0.3～0.5 ms | 约 0.25～0.33 ms | 0/0 | 0/0 |
| NV12 | 约 60 fps | 约 0.2～0.3 ms | 约 0.21～0.24 ms | 0/0 | 0/0 |

与旧版约 19～20 ms/眼的 CPU 转换相比，采集线程每帧主要工作降到亚毫秒级。

### 6.3 最终联合验收

2026-09-01，用户按原方式同时运行 live video 与 teleop，确认此前频繁卡顿已经
消失。该结果完成了从独立性能验证到真实联合场景的闭环。

## 7. 以后如何判断相似问题

运行时优先观察：

```text
COPY=L/R ms   原始帧复制耗时
UPL=ms        当前眼纹理上传调用耗时
TO=L/R        V4L2 select timeout 累计
DROP=L/R      按设备正常 sequence stride 校正后的驱动序列跳变
[CAP GAP]     用户态捕获循环长间隔；必须结合 TO/DROP/COPY 解读
[XR GAP]      XR/桌面 frame loop 长间隔
[CANFD SLOW]  teleop 的驱动调用/锁内耗时超过 5 ms
```

诊断原则：

- 不要把用户态循环间隔直接等同于 USB 丢帧；
- 不要只凭时间相关性确定硬件因果关系；
- 优先做分辨率、帧率、单/双路、测试卡、桌面/HMD 的正交实验；
- `CANFD SLOW result=0` 仍表示控制 deadline 受到影响；
- 如果今后在视频负载很低或视频完全关闭时仍持续出现 `CANFD SLOW`，再把它作为
  teleop 驱动 IPC、SDK 调用或双臂串行 worker 的独立问题排查。

## 8. 相关提交

| 提交 | 说明 | 当前判断 |
|---|---|---|
| `4091c01` | 加入 CAP/XR gap 诊断 | 埋点保留，但必须按本文口径解释 |
| `7ce8c64` | 初始 Omega/xHCI 归因 | 结论错误，已由本文取代 |
| `de5286d` | 强化 Omega 5 秒扰动结论及 suspend workaround | 结论错误，相关文档和脚本已删除 |
| `f1d0d53` | 原始 YUV 发布与 GPU YUV→RGB | 最终修复 |
