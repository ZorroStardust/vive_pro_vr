# Xwayland 崩溃分析（monado-stop 时 DRM lease use-after-free）

> 2026-08-10 记录。症状：每次 `pixi run monado-stop` 后 KDE 弹
> "Xwayland has crashed" 通知，VSCode 等 X11 应用短暂断连。
> **已修复**：backport 上游 xorg-server 两个提交重建 xwayland。

## 已修复记录（2026-08-10）

- 修复：`xwayland` 23.2.6-1ubuntu0.8 本地重建，backport 两个上游提交：
  - **`f6cd168d`** "xwayland: Do not remove output on withdraw if leased"
    （Olivier Fourdan, 2026-01-25，修复 `ef181265` 引入的 UAF）——核心修复
  - **`b67e0233`** "hw/xwayland: fix missing NULL checks in DRM lease
    allocation paths"（2026-04-28）——加固（分配失败返回 BadAlloc）
- 补丁文件：`patches/xwayland-drm-lease-uaf-fix.patch`
  （合并两个提交，已验证可干净应用到 23.2.6 源码树）
- 构建：`~/xwayland-build/`（xwayland 源包 + 官方 debian 打包配置）：
  ```bash
  cd ~/xwayland-build/xwayland-23.2.6
  git apply xwayland-drm-lease-uaf-fix.patch    # 源码已含补丁，无需再打
  DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -uc -us -b
  sudo dpkg -i ../xwayland_23.2.6-1ubuntu0.8_amd64.deb
  ```
  构建依赖：debhelper meson quilt libdecor-0-dev libdrm-dev libepoxy-dev
  libgcrypt-dev libgbm-dev libnvidia-egl-wayland-dev libpixman-1-dev
  libtirpc-dev libxcvt-dev libxfont-dev libxkbfile-dev libxshmfence-dev
  libxv-dev mesa-common-dev（其余随系统已装）。
- 安装后需重启 Xwayland 使新二进制生效：`pkill -9 -x Xwayland`（崩溃路径
  KWin 会自动重启）；若 Xwayland 被正常退出（SIGTERM），KWin 不会自动拉起，
  需 `kwin_wayland --replace`（屏幕闪一下）。
- 验证（2026-08-10）：安装 + 重启 Xwayland 后，monado start/stop 循环
  不再触发 Xwayland 崩溃；`journalctl` 无 `kwin_xwl: Xwayland process
  crashed`。
- 上游状态：该修复在 xorg-server master（2026-01），Ubuntu noble 的
  xwayland 23.2.6 不会获得 backport（noble 已冻结版本），本地重建是唯一途径。

## 时间线

1. 2026-06-24 起：`pixi run monado-stop`（SIGINT 优雅退出）后 KDE 频繁报
   "Xwayland has crashed"，VSCode 闪烁。AGENTS.md 记录此问题并配置
   `XwaylandCrashPolicy=1`（KWin 自动重启 Xwayland）+ `kde-recover` 脚本兜底。
2. 2026-08-09：monado 溢出补丁会话中，monado-stop 仍 100% 触发崩溃
   （journal 显示 00:30:35 / 00:31:02 两次 `kwin_xwl: Xwayland process
   crashed`）。
3. 2026-08-09 20:53 崩溃的 apport 报告（`/var/crash/_usr_bin_Xwayland.1000.crash`，
   属主 zorro 可直接读）提供了完整证据链。
4. 2026-08-10：定位为 Xwayland DRM lease UAF，backport 修复重建，验证通过。

## 根因（证据链完整）

### 1. 崩溃现象（apport 报告 + journal）

- apport 报告：`Signal: 6 (SIGABRT)`，无 Stacktrace 段，但内嵌 37MB CoreDump
  （gzip 压缩的 base64）。
- gdb 分析 core：abort 由 Xwayland 主线程内 `wl_display_dispatch_queue_pending`
  → libffi → Wayland 事件回调链中触发。
- journal 捕获 Xwayland 自己的 FatalError backtrace + 报错文本：

```text
(EE) Backtrace:
(EE) 0: /usr/bin/Xwayland (0x5f6b482f0000+0x173202)   ← OsSigHandler
(EE) 1: /usr/bin/Xwayland (0x5f6b482f0000+0x173546)
(EE) 2: /lib/x86_64-linux-gnu/libc.so.6 (0x45330)     ← raise
(EE) 3: /usr/bin/Xwayland (0x5f6b482f0000+0x3dca3)    ← Wayland 回调
(EE) 4-6: libffi
(EE) 7-9: libwayland-client（wl_display_dispatch_queue_pending）
(EE) ...
(EE) Segmentation fault at address 0x6d000000d4
Fatal server error:
(EE) Caught signal 11 (Segmentation fault). Server aborting
```

### 2. 反汇编定位崩溃函数（0x3dca3 附近）

```asm
mov 0x38(%rdx),%eax      ; rrLease->numOutputs
mov 0x40(%rdx),%rcx      ; rrLease->outputs 数组
mov (%rcx,%rax,8),%rcx   ; outputs[i]
mov 0x88(%rcx),%rcx      ; outputs[i]->devPrivate  ← 悬垂指针
movq $0x0,0x68(%rcx)     ; xwl_output->lease = NULL ← SEGV（0x6d000000d4）
```

### 3. 源码匹配（xwayland 23.2.6 源包）

`hw/xwayland/xwayland-drm-lease.c`：

```c
static void
xwl_randr_lease_cleanup_outputs(RRLeasePtr rrLease)
{
    for (i = 0; i < rrLease->numOutputs; ++i) {
        output = rrLease->outputs[i]->devPrivate;   // 无 NULL 检查
        output->lease = NULL;                        // ← UAF
    }
}
```

该函数被 `drm_lease_handle_finished()`（`wp_drm_lease_v1.finished`
事件回调，经 libffi 分发——与 backtrace 完全吻合）内联。

### 4. 崩溃链路（use-after-free）

```text
monado-stop → XCloseDisplay / vkReleaseDisplayEXT → NVIDIA 驱动释放 RandR lease
  → Xwayland 向 KWin 撤销 DRM lease（wp_drm_lease）
  → KWin 发送 wp_drm_lease_connector_v1.withdrawn
  → Xwayland lease_connector_handle_withdrawn():
        xwl_output_remove(data);        ← 释放 xwl_output（仍被 rrLease->outputs[] 引用）
  → KWin 发送 wp_drm_lease_v1.finished
  → drm_lease_handle_finished() → xwl_randr_lease_cleanup_outputs():
        outputs[i]->devPrivate 悬垂 → output->lease = NULL → SEGV
```

上游 `ef181265`（2023-07 "Clean up drm lease when terminating"）引入的
引用计数缺口：output 提前释放、lease 清理时仍引用。

### 5. 上游修复（f6cd168d, 2026-01-25）

- `xwl_output` 增加 `Bool withdrawn_connector` 标志；
- `lease_connector_handle_withdrawn`：置标志，若正在 lease 中**不释放**
  output（`if (xwl_output->lease) return;`）；
- 新增 `xwl_randr_lease_free_outputs`：lease `finished` 时对 withdrawn 的
  output 先置空 `rrLease->outputs[i]->devPrivate` 再移除，杜绝 UAF；
- `xwl_randr_lease_cleanup_outputs` 加 `if (output)` 防御。

### 6. 与 monado 侧的关系

monado 侧（`comp_window_direct_nvidia_destroy`）此前从不调用
`vkReleaseDisplayEXT`（对比 randr 路径有调用）——这是真实缺陷，已补
（`patches/monado-vive-xwayland-teardown.patch`），解决 DP-3 释放问题；
但它**不是** Xwayland 崩溃的根因——无论 release 还是断开连接触发 lease
撤销，都会走进 Xwayland 的 UAF 路径。根因 100% 在 Xwayland。

## 遗留事项

- [ ] monado 侧 `vkReleaseDisplayEXT` 补丁是否消除 "DP-3 stays disabled"
      问题：需重启系统后验证（首轮 monado run 后 DP-3 状态）。
- [ ] 若未来 apt 升级 xwayland（覆盖 /usr/bin/Xwayland），按上文重建命令
      重打补丁（`~/xwayland-build` 已保留完整源码 + 补丁）。
- [ ] 上游 `f6cd168d` 尚未进入任何 Ubuntu 发布版（截至 2026-08），如社区
      有 backport 需求可关注 xorg/xserver MR #2184 系列。

## 相关环境

- xwayland 2:23.2.6-1ubuntu0.8（Ubuntu noble，本地重建版，2026-08-10）
- KDE Plasma（kwin_wayland 6.x，XwaylandCrashPolicy=1）
- monado-service 21.0.0+git2905.e26a272c1~dfsg1-2build2（本地修复版）
- 崩溃报告：`/var/crash/_usr_bin_Xwayland.1000.crash`（2026-08-09 20:53，
  5.6MB 文本，含 37MB core 的 base64+gzip）
