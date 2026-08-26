# Xwayland DRM-lease UAF — 修复与本地重建流程

## 背景

`pixi run monado-stop`（或 VIVE 插拔导致的 DRM lease 撤销）会触发 KDE :1
Xwayland 崩溃。根因是 Xwayland 23.2.6 中 `xwayland-drm-lease.c` 的
use-after-free：

1. KWin 发送 `wp_drm_lease_connector_v1.withdrawn` →
   `lease_connector_handle_withdrawn()` 直接 `xwl_output_remove()`，
   释放了仍被 `rrLease->outputs[]` 引用的 `xwl_output`；
2. 随后 `wp_drm_lease_v1.finished` → `drm_lease_handle_finished()` →
   `xwl_randr_lease_cleanup_outputs()` 遍历悬垂指针 →
   SEGV（`Segmentation fault at address 0x6d000000d4`）。

完整崩溃证据链见 `xwayland_crash_analysis.md`。

## 修复内容

Backport 两个上游 xorg/xserver 提交（`patches/xwayland-drm-lease-uaf-fix.patch`）：

| 提交 | 说明 |
|------|------|
| `f6cd168d` | "Do not remove output on withdraw if leased" — 实际修复：withdrawn 时仅置 `withdrawn_connector` 标志，lease 结束后由 `xwl_randr_lease_free_outputs()` 安全释放 |
| `b67e0233` | DRM lease 分配路径的 NULL 检查加固 |

修改文件：`hw/xwayland/xwayland-drm-lease.c`、`hw/xwayland/xwayland-output.h`。
Ubuntu noble **不会** backport 此修复，必须本地重建。

配套：monado 侧 `patches/monado-vive-xwayland-teardown.patch`
（`comp_window_direct_nvidia.c` 退出时先 `vkReleaseDisplayEXT` + `XSync`
再关 X 连接），两者互补，缺一不可。

## 本地重建流程（从零开始）

```bash
# 1. 获取源码（需启用 deb-src 源；版本必须与已安装的 0.8 一致）
cd ~/xwayland-build
apt source xwayland                       # → xwayland-23.2.6/

# 2. 初始化 git，记录未修改基线（便于日后 diff 复查）
cd xwayland-23.2.6
git init && git add -A && git commit -m base

# 3. 打补丁（仓库内 patches/ 下的合并补丁）
patch -p1 < /path/to/vive_pro_vr/patches/xwayland-drm-lease-uaf-fix.patch
git diff   # 应只改 xwayland-drm-lease.c 与 xwayland-output.h 两个文件

# 4. 安装构建依赖（一次性）
sudo apt build-dep xwayland

# 5. 构建（二进制包，产物在父目录 ~/xwayland-build/）
dpkg-buildpackage -us -uc -b
#   产物：xwayland_23.2.6-1ubuntu0.8_amd64.deb
#         xwayland-dbgsym_23.2.6-1ubuntu0.8_amd64.ddeb（调试符号，崩溃分析用）

# 6. 安装
sudo dpkg -i ~/xwayland-build/xwayland_23.2.6-1ubuntu0.8_amd64.deb

# 7. 【关键】锁定版本，防止 apt 覆盖（见下方 2026-08-26 事故）
sudo apt-mark hold xwayland
apt-mark showhold | grep xwayland         # 确认 hold 生效

# 8. 重启 Xwayland 加载新二进制（KWin 按 XwaylandCrashPolicy=1 自动拉起）
pkill -9 -x Xwayland
```

## 验证补丁已生效

```bash
# BuildID 必须与本地构建产物一致（发行版二进制为 d262b443...）
file /usr/bin/Xwayland            # 期望 BuildID[sha1]=2b56b7d2...
# 或与 deb 内容比对：
dpkg-deb -x ~/xwayland-build/xwayland_23.2.6-1ubuntu0.8_amd64.deb /tmp/xw && \
  file /tmp/xw/usr/bin/Xwayland
```

防回归验证：插拔 VIVE Link Box 或跑一轮
`pixi run monado-detached` → `pixi run monado-stop`，
确认无 "Xwayland has crashed" 通知、journalctl 无 SIGSEGV。

## 事故记录：修复被 apt 静默覆盖（2026-08-26）

- 2026-08-10 本地重建并 dpkg 安装补丁版（BuildID `2b56b7d2`）。
- 2026-08-11 一次 `apt upgrade` 以**同版本重装**方式
  （`xwayland (0.8, 0.8)`）恢复了发行版原版二进制（BuildID `d262b443`）。
  原因：本地 deb 无 archive origin，apt 将其判定为需要"修复"。
- 2026-08-26 14:35 插入 VIVE → Xwayland 再次崩溃，
  签名与原始分析逐字节一致（SEGV at `0x6d000000d4`，
  `xwl_randr_lease_cleanup_outputs`）。
- 处置：重装补丁版 deb + `apt-mark hold xwayland`。

**教训**：任何以 dpkg 安装的本地重建包，装完必须立即
`sudo apt-mark hold <pkg>`；否则下一次 `apt upgrade` /
`unattended-upgrade` 会用同版本发行包覆盖。

## 维护提示

- 若未来 Ubuntu 发布新版本 xwayland（如 0.9 / 24.x），升级前先
  `apt-mark unhold xwayland`，并检查新版本上游是否已包含 `f6cd168d`
  （该修复已在较新的 xorg/xserver 上游，23.2.6 系列不会收到）。
  若已包含则无需再打补丁；未包含则重复上述重建流程打在新版本上。
- 升级后 Xwayland 需重启才加载新二进制（`pkill -9 -x Xwayland`）。
- 调试符号 ddeb 可在新崩溃发生时用 `apt install` 安装后
  `addr2line`/gdb 定位，参考 `xwayland_crash_analysis.md` 的方法。
