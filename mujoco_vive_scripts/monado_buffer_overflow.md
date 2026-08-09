# Monado "buffer overflow detected" 崩溃分析（VIVE Pro）

> 2026-08-09 记录。症状：monado-service 运行中或启动时以 SIGABRT 崩溃，
> stderr 打印 `*** buffer overflow detected ***: terminated`。
> **已修复**（2026-08-09）：本地重建 monado-service 并打补丁，见文末
> 「已修复记录」。HMD 传感器 MCU 垃圾状态（硬件侧）仍需断电重启处理。

## 已修复记录（2026-08-09）

- 修复内容：`_print_v2_pulse()`（`src/xrt/drivers/vive/vive_device.c`）改用
  逐字节赋值，`char data_str[33]`（32 字符 + NUL），不再用 `sprintf` 循环。
  行为完全等价，越界彻底消除。补丁文件：
  `patches/monado-vive-print-v2-pulse-overflow.patch`。
- 构建：Ubuntu 源包 `monado_21.0.0+git2905.e26a272c1~dfsg1.orig.tar.xz`
  （archive.ubuntu.com）解到 `~/monado-build/`，CMake 构建：
  ```bash
  cd ~/monado-build
  # CMakeLists.txt 已本地修改：set(GIT_DESC "GIT-NOTFOUND")
  # （发行版二进制嵌入的版本串是 "GIT-NOTFOUND"，客户端库在 IPC 握手时
  #   严格校验该串；不能传 -DGIT_DESC=GIT-NOTFOUND，CMake 会把以
  #   "-NOTFOUND" 结尾的值当 false 而忽略。IPC_IGNORE_VERSION=1 是备用。）
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTING=OFF -DBUILD_DOC=OFF \
    -DXRT_HAVE_SYSTEM_CJSON=ON -DXRT_OPENXR_INSTALL_ACTIVE_RUNTIME=OFF \
    -DXRT_OPENXR_INSTALL_ABSOLUTE_RUNTIME_PATH=OFF \
    -DXRT_FEATURE_STEAMVR_PLUGIN=OFF -DXRT_BUILD_DRIVER_OHMD=OFF
  cmake --build build -j$(nproc) --target monado-service
  sudo install -m755 build/src/xrt/targets/service/monado-service /usr/local/bin/monado-service
  ```
  验证版本串：`strings build/.../monado-service | grep -x GIT-NOTFOUND`。
- 依赖：cmake, glslang-tools, libglib2.0-dev, libvulkan-dev, libhidapi-dev,
  libusb-1.0-0-dev, libudev-dev, libeigen3-dev, libcjson-dev, libx11-dev,
  libx11-xcb-dev, libxcb-randr0-dev, libxrandr-dev, libxxf86vm-dev,
  libwayland-dev, wayland-protocols, libegl1-mesa-dev, libgl1-mesa-dev,
  libglvnd-dev, libdbus-1-dev, libbsd-dev, libsystemd-dev
  （未装 opencv/gstreamer/uvc/sdl2 dev 包 → 对应特性自动关闭）。
- apt 升级 monado 后需按上述命令重建（`/usr/local/bin` 不会被 apt 覆盖，
  但升级后无需立即重建——只有崩溃复现才需要）。
- 上游 main 分支（2026 年）仍存在此 bug，补丁可作 MR 提交素材。
- 验证（2026-08-09）：HMD 劣化状态（会发 V2 脉冲报告）下 trace 级运行
  6+ 分钟无崩溃，旧崩溃点（运行 1m14s 处）已越过；`xr-surgical` 客户端
  正常连接（版本串 `GIT-NOTFOUND` 与发行版客户端库精确匹配后无
  IPC 版本冲突）。修复前同状态必然在收到脉冲报告时 SIGABRT。

### 部署细节（源码 / 二进制 / 执行优先级）

- **源码**：`~/monado-build/` —— 官方源包
  `monado_21.0.0+git2905.e26a272c1~dfsg1.orig.tar.xz` 解压而成，与发行版
  `-2build2` 完全同源。两处本地修改：`src/xrt/drivers/vive/vive_device.c`
  （溢出补丁）与 `CMakeLists.txt`（`set(GIT_DESC "GIT-NOTFOUND")`）。
- **编译产物**：`~/monado-build/build/src/xrt/targets/service/monado-service`
  （1.1MB Release）。
- **安装**：`sudo install -m755` 复制到 `/usr/local/bin/monado-service`；
  `/usr/bin/monado-service`（2.3MB 原版，仍带 bug）保持不动。
- **执行优先级 = PATH 目录顺序**（非别名/非 symlink）：
  - PATH 中 `/usr/local/bin` 排在 `/usr/bin` 之前，shell 按序取第一个匹配：
    `which -a monado-service` → `/usr/local/bin/...`（生效）→ `/usr/bin/...`（被遮蔽）→ `/bin/...`（软链到 /usr/bin）。
  - 客户端库 `libopenxr_monado.so` 用 `os_find_system_command("monado-service")`
    沿 PATH 找进程名，所有 OpenXR 客户端自动连接到修复版。
  - `apt` 永不写 `/usr/local/bin`，发行版升级不会覆盖修复版；
    回退只需删除 `/usr/local/bin/monado-service`。
- **注意**：systemd user unit（`/usr/lib/systemd/user/monado.service`）的
  `ExecStart` 用绝对路径会绕开修复版；本项目 pixi 脚本均经 PATH 查找，不受影响。

## 时间线

1. `xr-live-video` 会话运行数分钟后，monado 日志先出现大量
   `Frame late by 11.11ms` / `Dropping old missed frame`，随后
   `*** buffer overflow detected ***: terminated`，monado 崩溃（apport 落盘
   `/var/crash/_usr_bin_monado-service.1000.crash`）。
2. 之后 `pixi run start-monado` **启动即崩**，同样 abort，崩溃点在
   VIVE 设备初始化阶段：

```text
INFO [vive_device_create] Firmware version 0
INFO [vive_device_create] Hardware revision: 0 rev 0.0.0
WARN [vive_get_imu_range_report] Gyroscope or accelerometer range too large.
WARN [vive_get_imu_range_report] Gyroscope: 177
WARN [vive_get_imu_range_report] Accelerometer: 112
INFO [vive_device_create] Vive gyroscope range     0.000000
INFO [vive_device_create] Vive accelerometer range 0.000000
*** buffer overflow detected ***: terminated
```

3. HMD（Link Box）断电重启后，monado 恢复正常，未再崩溃。

## 根因（证据链完整）

### 1. HMD 传感器状态异常（诱因）

- `vive_get_imu_range_report` 读到 gyro_range=177 / accel_range=112
  （有效值只有 0~4，对应 MPU-6500 的 ±250~2000 dps 档位）。
- 传感器固件版本读为 0。
- 结论：VIVE Pro 传感器 MCU（0bb4:0309 上的 HID 接口）处于异常状态，
  返回垃圾报告流。**断电重启 Link Box 可恢复。**

### 2. monado 驱动缺陷（直接崩溃点）

崩溃发生在 `_print_v2_pulse()`（`src/xrt/drivers/vive/vive_device.c`，
monado 21.0.0+git2905.e26a272c1，Ubuntu noble 包）：

```c
char data_str[32];
for (int k = 0; k < 32; k++) {
    ...
    if (m)
        sprintf(&data_str[k], "%d", d);   /* 写 2 字节（数字+NUL） */
    else
        sprintf(&data_str[k], "_");       /* 写 2 字节（'_'+NUL）   */
}
```

- k=31 时 `&data_str[31]` 只剩 1 字节，sprintf 写 2 字节 → 1 字节栈越界。
- GCC 将 `sprintf` 编译为 `__sprintf_chk(buf+k, 2, 32-k, fmt)`，
  glibc 检测到输出长度 ≥ slen → `__chk_fail()` → `SIGABRT`。
- 该函数由 `_print_pulse_report_v2()` 调用，处理
  `VIVE_HEADSET_LIGHTHOUSE_V2_PULSE_REPORT_ID`（59 字节）报告——
  **HMD 只要发来一条合法的 V2 脉冲报告就必然崩溃**（与数据内容无关）。

### 3. Core dump 反汇编证据

`apport-unpack /var/crash/_usr_bin_monado-service.1000.crash /tmp/mc`
得到 151MB core（`Stacktrace` 文件不存在，需自行 gdb）：

```text
#6  __GI___fortify_fail (msg="buffer overflow detected")
#7  __GI___chk_fail ()
#8  __vsprintf_internal (iovsprintf.c:67)
#9  ___sprintf_chk (s, flag=2, slen, format) (sprintf_chk.c:40)
#10 0x55d8cee88dfb in monado-service            ← 调用点
#11 start_thread                                 ← 传感器工作线程
```

调用点反汇编（monado-service PIE，无符号）：

```asm
0x55d8cee88de5:  mov    %r14,%rdx          ; slen = r14（剩余字节数）
0x55d8cee88dea:  mov    $0x2,%esi          ; fortify flag
0x55d8cee88def:  lea    "_"(%rip),%rcx     ; 格式串 "_"
0x55d8cee88df6:  call   __sprintf_chk@plt  ; r14=1 → slen=1，写 2 字节 → abort
```

辅助证据：
- 两个格式串分别为 `"%d"` 和 `"_"`（x/s 读出）—— 与 `_print_v2_pulse` 完全吻合。
- 栈上 `data_str` 内容为 `"___00000000____..."` —— 正在逐位构建的脉冲数据字符串。
- 崩溃线程栈顶仅 `start_thread → 调用点`，说明静态函数被内联进传感器线程函数。

### 4. 触发链路

```text
HMD 传感器 MCU 状态异常（IMU range 垃圾、固件版本 0）
  → 传感器线程（vive_sensors_run_thread）读到 59 字节 V2 脉冲报告
  → _print_pulse_report_v2 → _print_v2_pulse
  → sprintf(&data_str[31], "_") → __sprintf_chk 检测到越界 → SIGABRT
```

视频会话中的崩溃同理：会话期间 sensors 线程一直运行，HMD 状态劣化后
开始发 V2 脉冲报告（或基站开启时光学脉冲触发），驱动即崩。

### 5. 上游状态

**上游 main 分支（2026 年）仍存在同样的 `sprintf(&data_str[k], ...)` 循环**——
升级 monado 也修不了这个 bug。规避方式只有：
- 保持基站/手柄关闭（AGENTS.md 已有此约定，正是该 bug 的规避）；
- HMD 状态异常时断电重启 Link Box；
- 如需彻底修复：向 monado 提补丁（改用 `data_str[32]` 逐字节赋值 + 末尾 NUL，
  或在循环里直接写 `data_str[k] = m ? ('0'+d) : '_'`）。

## 遗留事项

- [x] ~~若崩溃复现：`sudo apport-unpack /var/crash/_usr_bin_monado-service.1000.crash /tmp/mc`
      后 `gdb /usr/bin/monado-service /tmp/mc/CoreDump` 复核 backtrace。~~（已完成，
      见上文证据链）
- [x] ~~可向上游 monado 提交 `_print_v2_pulse` 修复补丁。~~（本地已修复，
      补丁在 `patches/`，可作 MR 素材）
- [ ] 帧迟到 11.11ms（客户端半速 45fps）问题未深挖，与本次崩溃可能同源
      （HMD 显示链路劣化），HMD 重启后需复测。

## 相关环境

- monado-service 21.0.0+git2905.e26a272c1~dfsg1-2build2（Ubuntu noble）
- 二进制 stripped；gdb 15.1 支持 debuginfod，但 debuginfod.ubuntu.com 不可达
- 崩溃报告：`/var/crash/_usr_bin_monado-service.1000.crash`（root，12MB）
