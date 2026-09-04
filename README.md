# 🔒 input-locker

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> ⬇️ **[下载最新版 InputLocker.exe](https://github.com/he-zhiyuan/input-locker/releases/latest)**

Windows / Linux 输入设备锁定工具。适用于笔记本电脑的展示/防误触场景，锁定后键盘、鼠标禁用，USB存储禁用，屏幕保持常亮，通过 CapsLock+密码解锁。

## ✨ 功能

| 功能 | 说明 |
|------|------|
| ⌨️ 键盘锁定 | 禁用所有按键，仅放行 CapsLock（触发解锁）和字母/数字键（输入密码） |
| 🖱️ 鼠标锁定 | 锁定期间全程禁用，光标隐藏，解锁模式下同样不可用 |
| 💾 USB存储禁用 | 禁用U盘等USB存储设备（不影响笔记本自带键盘/触控板） |
| 💡 屏幕常亮 | 禁用屏保和自动息屏 |
| 🛡️ 防杀后台 | 阻止系统休眠（Windows: `SetThreadExecutionState`；Linux: `systemd-inhibit`） |
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

```bash
# 依赖（evdev 从源码需要内核头文件；Manjaro/Arch 系统 python 受 PEP668 保护，
# 建议用 venv 或加 --break-system-packages）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.pyw
```

按会话类型自动选择后端：

| 会话 | 后端 | 行为 |
|------|------|------|
| **X11**（Xorg） | `LinuxInputLocker` | XGrabKeyboard/XGrabPointer 全局拦截，与 Windows 版完全一致：3x CapsLock + 自定义密码解锁，解锁时鼠标仍禁用，光标隐藏 |
| **Wayland**（KDE Plasma 等） | `EvdevLocker`（优先） | evdev `EVIOCGRAB` 内核级设备抓取，与显示服务器无关，X11/Wayland 均有效：3x CapsLock + 自定义密码解锁，鼠标/触摸屏同样抓取 |
| **Wayland** 无设备权限时 | `WaylandLocker`（回退） | 回退**系统锁屏**（`loginctl lock-session`，系统密码解锁）+ `systemd-inhibit` 保持屏幕常亮/阻止休眠 |

> ⚠️ Wayland 下使用自定义解锁需对 `/dev/input/event*` 有读写权限（root 或 `input` 组成员）。加入 input 组后**重新登录**生效：
>
> ```bash
> sudo usermod -aG input $USER
> # 重新登录后生效
> ```
>
> 无权限时自动回退系统锁屏并在界面提示（系统密码解锁）。Linux 下无需 root 即可锁定输入；**USB 存储禁用需要 root**（非 root 时自动跳过并在界面提示）。如需 USB 禁用，用 `sudo python3 main.pyw` 运行。

## 🔑 修改密码

1. 点击主界面「修改密码」按钮
2. 输入当前密码 → 输入新密码 → 确认新密码
3. 密码保存到 `config.json`，下次启动自动加载

## 📁 项目结构

```
input-locker/
├── main.pyw          # 主程序（.pyw 无控制台窗口）
├── linux_locker.py   # Linux 后端（X11 抓取 / evdev 抓取 / Wayland 系统锁屏）
├── icon.ico          # EXE 应用图标
├── build.ps1         # PowerShell 打包脚本
├── requirements.txt  # Python 依赖
├── .gitignore        # Git 忽略规则
├── README.md         # 说明文档
└── config.json       # 密码配置（运行后自动生成，不提交到git）
```

## ⚠️ 注意事项

- 🏷️ **Windows 必须以管理员身份运行**；Linux 无需 root（USB 禁用除外）
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
