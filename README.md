# 🔒 input-locker

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> ⬇️ **[下载最新版 InputLocker.exe](https://github.com/he-zhiyuan/input-locker/releases/latest)**

Windows / Linux / macOS 输入设备锁定工具。适用于笔记本电脑的展示/防误触场景，锁定后键盘、鼠标禁用，屏幕保持常亮，通过 CapsLock+密码解锁。Windows / Linux 还可禁用 USB 存储。

## ✨ 功能

| 功能 | 说明 |
|------|------|
| ⌨️ 键盘锁定 | 禁用所有按键，仅放行 CapsLock（触发解锁）和字母/数字键（输入密码） |
| 🖱️ 鼠标锁定 | 锁定期间全程禁用，光标隐藏，解锁模式下同样不可用 |
| 💾 USB存储禁用 | 禁用U盘等USB存储设备（不影响笔记本自带键盘/触控板；macOS 不支持） |
| 💡 屏幕常亮 | 禁用屏保和自动息屏 |
| 🛡️ 防杀后台 | 阻止系统休眠（Windows: `SetThreadExecutionState`；Linux: `systemd-inhibit`；macOS: `caffeinate`） |
| 🔑 密码保护 | 支持修改密码，持久化保存到 config.json |
| 🔄 崩溃恢复 | 注册 `atexit` 回调，程序异常退出时自动恢复系统设置 |

## 🔓 解锁流程

```
连按3次 CapsLock → 窗口弹出聚焦 → 输入密码 → Enter 解锁
```

- ⏱️ 3次 CapsLock 需在 2 秒内完成
- 📉 锁定后窗口自动最小化，按3次 CapsLock 后窗口弹出置顶
- 🔤 密码输入仅支持字母、数字、退格和回车
- 🚫 解锁模式下鼠标仍不可用，防止他人操作电脑
- 👻 锁定期间屏幕光标暂时隐藏，解锁后恢复原状态
- ❌ 密码错误自动关闭解锁模式，需重新触发

> 💡 Windows、Linux（X11 / 有 input 权限的 Wayland）与 macOS 解锁流程相同，均为上述自定义密码流程；
> 仅当 Wayland 无 input 权限回退到系统锁屏时，才改用系统密码在锁屏界面解锁。

## 🚀 使用方法

### Windows

```bash
# 需要管理员权限（.pyw 无控制台窗口）
pythonw main.pyw
```

打包成 EXE：

```powershell
.\build.ps1
```

打包后右键 `dist\InputLocker.exe` → 以管理员身份运行。

### Linux

#### 1. 安装依赖

> Arch/Manjaro 的系统 Python 受 PEP668 保护，直接 `pip install` 会拒绝；
> 建议用自带的 venv（evdev 编译需要内核头文件，Manjaro 可先 `sudo pacman -S linux-headers`）。

```bash
cd input-locker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

#### 2. 添加 input 组权限（Wayland 自定义解锁必需）

程序用 evdev 内核级抓取锁定输入（`EVIOCGRAB`），需要能读写 `/dev/input/event*`。
非 root 用户需加入 `input` 组并**重新登录**（注销/重启后权限才生效）：

```bash
sudo usermod -aG input $USER
# 然后注销重新登录，或重启
```

**验证是否已生效**（应能看到一串设备）：

```bash
ls -l /dev/input/event*  # 检查 /dev/input 目录权限
id                        # 输出里应包含 input 组
```

> 没加 input 组也能启动，但会回退系统锁屏模式（用系统密码解锁），并弹出提示。
> 或者直接用 root 跑（`sudo .venv/bin/python main.pyw`）也能获得完整功能，但这样 USB 存储禁用
> 也会一并启用（见下文）。

#### 3. 启动

**必须在图形桌面环境里启动**（终端需继承 DISPLAY/WAYLAND_DISPLAY 等变量，例如 KDE 桌面的 Konsole，
而不是 SSH/纯 tty/IDE 内置终端）：

```bash
cd input-locker
.venv/bin/python main.pyw
```

启动后主界面显示「锁定系统」，点它即开始锁定。

按会话类型自动选择后端：

| 会话 | 后端 | 行为 |
|------|------|------|
| **X11**（Xorg） | `LinuxInputLocker` | XGrabKeyboard/XGrabPointer 全局拦截，与 Windows 版完全一致：3x CapsLock + 自定义密码解锁，解锁时鼠标仍禁用，光标隐藏 |
| **Wayland**（KDE Plasma 等） | `EvdevLocker`（优先） | evdev `EVIOCGRAB` 内核级设备抓取，与显示服务器无关，X11/Wayland 均有效：3x CapsLock + 自定义密码解锁，鼠标/触摸屏同样抓取 |
| **Wayland** 无设备权限时 | `WaylandLocker`（回退） | 回退**系统锁屏**（`loginctl lock-session`，系统密码解锁）+ `systemd-inhibit` 保持屏幕常亮/阻止休眠 |

> ⚠️ Wayland 下使用自定义解锁需对 `/dev/input/event*` 有读写权限（root 或 `input` 组成员）——**配置方法见上方「步骤 2」**。无权限时自动回退系统锁屏并在界面提示（系统密码解锁）。
>
> Linux 下无需 root 即可锁定输入；**USB 存储禁用需要 root**（非 root 时自动跳过并在界面提示）。如需 USB 禁用，用 `sudo .venv/bin/python main.pyw` 运行。

#### 4. 开机自动启动（可选）

登录进桌面时自动启动（挂在 `graphical-session.target` 下，KDE Plasma 6 已自动把 `WAYLAND_DISPLAY` 等变量导入用户管理器，无需额外配置）：

```bash
cp input-locker.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable input-locker.service   # 下次登录自动启动
systemctl --user disable input-locker.service  # 取消自启
```

> ⚠️ **不需要 `loginctl enable-linger`**：linger 是给无图形会话的后台服务的（开机即启动）；这是 GUI 应用，开机时没有显示服务器，启动会失败，且没桌面可锁。挂 `graphical-session.target` 会在登录、桌面就绪后才启动，才是正确时机。
>
> 更简单的替代：写一个 `~/.config/autostart/input-locker.desktop`（桌面环境自带自启机制），systemd 方案的优势是可用 `systemctl --user` 统一管理/看状态。

### macOS

#### 1. 安装依赖

```bash
cd input-locker
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

#### 2. 授予辅助功能权限（必需）

系统设置 → 隐私与安全性 → 辅助功能，允许运行本程序的 **Terminal / python3 / IDE**。
首次锁定时系统可能弹出授权提示；**未授权时锁定会失败**（不会假装成功）。

#### 3. 启动

在图形桌面会话里启动（本机 Terminal 或屏幕共享）。纯 SSH 通常没有 GUI，也无法完成辅助功能授权：

```bash
cd input-locker
.venv/bin/python main.pyw
```

默认计划文件：`~/Downloads/input-locker-plan.json`（与 Linux 相同）。

后端：`MacInputLocker`（`CGEventTap` 过滤键鼠）。解锁流程与 Windows / Linux-evdev 相同：2 秒内连按 3 次 CapsLock，输入自定义密码，Enter 提交。屏幕常亮用 `caffeinate -dimsu`，光标用 `CGDisplayHideCursor`。

#### 限制

- **无 USB 存储禁用**（macOS 没有对等的廉价实现）。
- 若在系统设置里把 CapsLock 改成了其他键，3× CapsLock 解锁不会触发。
- 无系统锁屏回退。
- SSH 会话通常没有事件轻击 / GUI；远程检查以 `python -m unittest -v test_locker` 和 import 探测为准。

## 计划 / MCP 命令回执

- 已锁定时重复 `lock` 为成功的空操作；UI 与计划线程的锁定/解锁入口串行执行，不会重新抓取设备或覆盖现有 inhibit。
- 命令格式：`{"cmd":"lock","id":"唯一命令ID"}`，也接受旧版不带 `id` 的命令。ack 原样回传 `id`，`result` 为 `ok` 或 `error`，失败时附 `error` 原因。`ok` 表示该次操作成功，不是持续锁定状态监测。
- 消费者先把命令原子移动到 `.processing` 再执行，ack 原子写入；执行中收到的新命令不会被旧命令删除。
- MCP 服务端必须核对 ack 的 `id` 与本次命令一致；更新协议时需同时更新并重启 input-locker 和 aide。
- once 只在 `when.at ≤ now < unlock.at` 期间自动上锁；解锁时刻已过则重启也不会再锁。Linux 启动只查 evdev 权限、窗口最小化，不探测锁屏、不弹权限对话框。

无真实锁屏的回归检查（模拟设备、无需显示会话）：

```bash
.venv/bin/python -m unittest -v test_locker
```

## 🔑 修改密码

1. 点击主界面「修改密码」按钮
2. 输入当前密码 → 输入新密码 → 确认新密码
3. 密码保存到 `config.json`，下次启动自动加载

## 📁 项目结构

```
input-locker/
├── main.pyw          # 主程序（.pyw 无控制台窗口）
├── linux_locker.py   # Linux 后端（X11 抓取 / evdev 抓取 / Wayland 系统锁屏）
├── macos_locker.py   # macOS 后端（CGEventTap）
├── locker_lifecycle.py # UI / 计划线程的生命周期互斥
├── test_locker.py    # 模拟设备回归检查（不锁真实键鼠）
├── icon.ico          # EXE 应用图标
├── build.ps1         # PowerShell 打包脚本
├── requirements.txt  # Python 依赖
├── .gitignore        # Git 忽略规则
├── README.md         # 说明文档
└── config.json       # 密码配置（运行后自动生成，不提交到git）
```

## ⚠️ 注意事项

- 🏷️ **Windows 必须以管理员身份运行**；Linux 无需 root（USB 禁用除外）；macOS 需要辅助功能权限，无需 root
- 💻 本程序专为笔记本电脑设计（自带键盘非 USB，可安全禁用 USB 存储）
- ⚡ Windows 下 Ctrl+Alt+Del 是安全序列，低级键盘钩子无法拦截，始终可用作紧急手段；Linux X11 下杀掉进程即可释放抓取（X 服务器会自动解除）
- 🔌 USB存储禁用仅影响 USB 大容量存储设备（U盘等），不影响笔记本自带键盘/触控板
- 🔒 Windows 锁定时无法关闭窗口，必须先解锁；Linux Wayland 下可关闭（系统锁屏不受影响）
- 🔄 程序崩溃时会通过 `atexit` 尝试恢复设置

## 🆘 紧急恢复

如果程序崩溃且设置未恢复，在命令行（管理员）执行：

```cmd
reg add "HKLM\SYSTEM\CurrentControlSet\Services\USBSTOR" /v Start /t REG_DWORD /d 3 /f
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /d 1 /f
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveTimeOut /d 600 /f
```
