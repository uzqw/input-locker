# Input Locker

Windows / Linux / macOS 输入锁定工具。锁定后键盘、鼠标不可用，屏幕保持常亮；连按 3 次 CapsLock，输入密码解锁。适合笔记本展示、防误触。

[下载 Windows 版](https://github.com/he-zhiyuan/input-locker/releases/latest) · [MIT License](LICENSE)

## 平台支持

三端都支持锁定键盘、鼠标，以及同一套解锁流程。

| | Windows | Linux | macOS |
|---|---|---|---|
| 键盘 / 鼠标锁定 | ✅ | ✅ | ✅ |
| 3× CapsLock + 密码解锁 | ✅ | ✅ | ✅ |
| 屏幕常亮、阻止休眠 | ✅ | ✅ | ✅ |
| USB 存储禁用 | ✅ | ✅ 需 root | ❌ |
| 运行权限 | 管理员 | `input` 组（完整功能） | 辅助功能 |

Linux Wayland 若没有 `/dev/input` 权限，会回退到系统锁屏（用系统密码解锁）。其余情况三端行为一致。

## 解锁

```
2 秒内连按 3 次 CapsLock → 输入密码 → Enter
```

默认密码 `123456`，可在界面里修改（写入 `config.json`）。解锁时鼠标仍不可用。密码只接受字母和数字。

## 使用

三端都需要图形桌面，不要用纯 SSH / tty。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Windows**（管理员）：`pythonw main.pyw`  
打包：`.\build.ps1`，再以管理员运行 `dist\InputLocker.exe`。

**Linux**：先 `sudo usermod -aG input $USER`，注销再登录，然后：

```bash
.venv/bin/python main.pyw
```

USB 禁用需要 root：`sudo .venv/bin/python main.pyw`。可选自启：先建启动包装脚本，再拷 service 文件：

```bash
# 启动包装脚本（service 用 %h/.local/bin/input-locker，不写死仓库路径）
cat > ~/.local/bin/input-locker <<'EOF'
#!/bin/sh
exec "$HOME/input-locker/.venv/bin/python" "$HOME/input-locker/main.pyw"
EOF
chmod +x ~/.local/bin/input-locker
# 把仓库里的 input-locker.service 拷到用户 systemd 目录后启用
cp input-locker.service ~/.config/systemd/user/
systemctl --user enable --now input-locker.service
```

> 上面 `$HOME/input-locker` 是示例，请改成你实际的仓库路径。

**macOS**：系统设置 → 隐私与安全性 → 辅助功能，允许 Terminal / python3，然后：

```bash
.venv/bin/python main.pyw
```

未授权时锁定会失败。若把 CapsLock 改成了别的键，解锁不会触发。

## 休息会话事件协议

aide 的 rest-break 使用 `input-locker-events/` 目录与本程序通信：

- `requests/*.json` 由 aide 发布 `rest.requested`，只表示锁定意图；
- `results/*.json` 由 InputLocker 发布 `rest.locked`、`rest.observed`、
  `rest.unlocked`、`rest.failed` 等事实；
- 所有事件不可变，使用唯一临时文件写完后原子替换；
- 会话由稳定 `sessionId` 关联，进程重启后从事件重建，不依赖内存任务下标；
- 密码提前解锁记录 `reason=password`，rest-break 只按实际锁定区间计算休息；
- 默认必须锁定满 180 秒才能使用密码解锁，管理员 command 可绕过该限制。

事件目录默认与计划文件同目录，也可用 `INPUT_LOCKER_EVENTS_DIR` 覆盖。

## 注意

- 面向笔记本自带键盘；USB 禁用只拦 U 盘，不拦内置键盘 / 触控板。
- Windows 紧急出口：Ctrl+Alt+Del。Linux 杀掉进程即可释放抓取。
- 崩溃时会尝试自动恢复。Windows 若 USB 仍被禁用：

```cmd
reg add "HKLM\SYSTEM\CurrentControlSet\Services\USBSTOR" /v Start /t REG_DWORD /d 3 /f
```
