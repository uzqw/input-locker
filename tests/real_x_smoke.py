#!/usr/bin/env python3
"""真实 X 环境冒烟测试（10.0.0.7 Fedora 42 GNOME/X11，Display=:1）。

覆盖风险点 3：真实 X 下 input-locker 的抓取/解锁行为。
通过 XTEST 注入合成输入（XTEST 事件与真实输入走同一路径，抓取激活时会被
重定向到抓取方——probe 窗口收不到即证明抓取生效），并用命令文件/计划文件
驱动运行中的 input-locker 服务。

用法（在 10.0.0.7 上）：
    DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority \
        .venv/bin/python tests/real_x_smoke.py

子测试：
  A 命令锁/解锁：lock 命令 → 键盘被真抓取；unlock 命令 → 抓取释放。
  B 物理解锁路径：lock 命令 → XTEST 连按 3 次 CapsLock → XTEST 输入密码+回车
    → 抓取释放（用户无鼠标/无窗口也能解锁的真链路）。
  C 抓取冲突：另一客户端已持有键盘抓取时 lock 命令 → ack 报 error，
    且不得抢别人的抓取；unlock 不得误放别人的抓取。
  D ScheduleWatcher 端到端：写一条几秒后到点的真实计划 → 服务到点真锁、
    到点真解（走记忆解锁/文件解锁）。

安全：任一步骤发现已处于锁定/有 pending 命令 → 跳过（不打扰可能在处理的
其他会话）；物理解锁失败会用 unlock 命令兜底，绝不把机器锁死。
"""
import json
import os
import subprocess
import sys
import time

from Xlib import X, XK, display
from Xlib.ext import xtest

LOCKER_DIR = os.environ.get("LOCKER_DIR", os.path.expanduser("~/wp/github/input-locker"))
PLAN = os.path.join(LOCKER_DIR, "input-locker-plan.json")
CMDF = os.path.join(LOCKER_DIR, "input-locker-command.json")
ACKF = os.path.join(LOCKER_DIR, "input-locker-ack.json")
PASSWORD = "123456"  # config.json 未设 password 时的默认值

d = display.Display()
_root = d.screen().root
CAPS = d.keysym_to_keycode(XK.XK_Caps_Lock)
RET = d.keysym_to_keycode(XK.XK_Return)

_results = []


def report(name, ok, note=""):
    _results.append((name, ok, note))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" — " + note) if note else ""))


def probe_free():
    """probe 窗口能否收到 XTEST 按键。能收到=键盘空闲；收不到=被抓取。"""
    probe = _root.create_window(0, 0, 10, 10, 0, d.screen().root_depth,
                                event_mask=X.KeyPressMask)
    probe.map()
    d.sync()
    time.sleep(0.3)
    d.set_input_focus(probe, X.RevertToParent, X.CurrentTime)
    d.sync()
    time.sleep(0.15)
    xtest.fake_input(d, X.KeyPress, 65)
    d.sync()
    time.sleep(0.25)
    xtest.fake_input(d, X.KeyRelease, 65)
    d.sync()
    time.sleep(0.15)
    got = 0
    while d.pending_events():
        ev = d.next_event()
        if ev.type == X.KeyPress:
            got += 1
    probe.destroy()
    d.sync()
    return got == 1


def wait_grabbed(want_grabbed, timeout=12.0):
    """等到 抓取/释放 状态，返回是否如愿。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_free() != want_grabbed:
            return True
        time.sleep(0.4)
    return False


def send_command(cmd, ident):
    with open(CMDF, "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "id": ident}, f)


def read_ack():
    for _ in range(20):
        if os.path.isfile(ACKF):
            try:
                with open(ACKF, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(0.3)
    return None


def type_keys(keycodes, delay=0.12):
    for kc in keycodes:
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        time.sleep(delay)
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()
        time.sleep(delay)


def cleanup_files():
    for p in (CMDF, CMDF + ".processing", ACKF):
        try:
            os.remove(p)
        except OSError:
            pass


def preflight():
    if not probe_free():
        print("SKIP: 键盘已被抓取（可能另一会话正在处理锁屏）——不打扰，退出。")
        return False
    if os.path.isfile(CMDF) or os.path.isfile(CMDF + ".processing"):
        print("SKIP: 存在 pending 命令文件，退出避免干扰。")
        return False
    return True


def test_command_lock_unlock():
    cleanup_files()
    send_command("lock", "smokeA-lock")
    ok_grab = wait_grabbed(True)
    ack = read_ack()
    ack_ok = bool(ack and ack.get("result") == "ok" and ack.get("id") == "smokeA-lock")
    send_command("unlock", "smokeA-unlock")
    ok_free = wait_grabbed(False)
    cleanup_files()
    report("A 命令锁→抓取", ok_grab, "键盘未被真抓取" if not ok_grab else "")
    report("A ack ok", ack_ok, "ack=%s" % ack)
    report("A 命令解锁→释放", ok_free, "解锁后抓取未释放" if not ok_free else "")


def test_physical_unlock():
    cleanup_files()
    send_command("lock", "smokeB-lock")
    if not wait_grabbed(True):
        report("B 物理解锁", False, "lock 未抓取，跳过")
        cleanup_files()
        return
    # 连按 3 次 CapsLock 进入解锁模式
    for _ in range(3):
        xtest.fake_input(d, X.KeyPress, CAPS); d.sync(); time.sleep(0.12)
        xtest.fake_input(d, X.KeyRelease, CAPS); d.sync(); time.sleep(0.12)
    time.sleep(0.5)
    # 输入密码 + 回车
    digit_kcs = [d.keysym_to_keycode(getattr(XK, "XK_" + ch)) for ch in PASSWORD]
    type_keys(digit_kcs + [RET])
    ok = wait_grabbed(False, timeout=10)
    if not ok:
        # 兜底：命令解锁，绝不把机器锁死
        send_command("unlock", "smokeB-fallback")
        wait_grabbed(False, timeout=8)
    cleanup_files()
    report("B 物理解锁(3xCapsLock+密码)", ok, "物理路径未解锁" if not ok else "")


def test_grab_conflict():
    cleanup_files()
    # 另一客户端持有键盘抓取
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "from Xlib import X, display; import time; d=display.Display();"
         "d.screen().root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime);"
         "d.sync(); time.sleep(10)"],
        env=dict(os.environ))
    time.sleep(1.0)
    held = not probe_free()
    if not held:
        proc.terminate(); proc.wait()
        report("C 抓取冲突", False, "未能建立外部抓取，跳过")
        return
    send_command("lock", "smokeC-lock")
    ack = read_ack()
    err_acked = bool(ack and ack.get("id") == "smokeC-lock" and ack.get("result") == "error")
    still_held = not probe_free()  # 锁失败不得抢别人的抓取
    send_command("unlock", "smokeC-unlock")  # unlock 不得误放别人的抓取
    time.sleep(1.5)
    not_stolen = not probe_free()
    proc.terminate(); proc.wait()
    time.sleep(1.0)
    freed_after = probe_free()
    cleanup_files()
    report("C 抓取冲突 lock 报 error", err_acked, "ack=%s" % ack)
    report("C 锁失败不抢别人抓取", still_held, "")
    report("C unlock 不误放别人抓取", not_stolen, "")
    report("C 外部释放后恢复空闲", freed_after, "")


def test_watcher_end_to_end():
    cleanup_files()
    # 备份并写入一条几秒后到点的真实计划
    old = None
    if os.path.isfile(PLAN):
        with open(PLAN, encoding="utf-8") as f:
            old = f.read()
    import datetime
    now = datetime.datetime.now().astimezone()
    lock_at = (now + datetime.timedelta(seconds=4)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    unlock_at = (now + datetime.timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    with open(PLAN, "w", encoding="utf-8") as f:
        json.dump([{"mode": "once", "when": {"at": lock_at},
                    "unlock": {"at": unlock_at}, "confirm": "realx-itest"}], f)
    try:
        locked_on_time = wait_grabbed(True, timeout=8)          # ~lock_at 锁
        freed_on_time = locked_on_time and wait_grabbed(False, timeout=10)  # ~unlock_at 解
        report("D Watcher 到点真锁", locked_on_time, "")
        report("D Watcher 到点真解", freed_on_time, "")
    finally:
        # 恢复计划文件（清空，避免干扰 cron）
        with open(PLAN, "w", encoding="utf-8") as f:
            f.write("[]")
        # 兜底：确保没有残留锁
        if not probe_free():
            send_command("unlock", "smokeD-fallback")
            wait_grabbed(False, timeout=8)
        cleanup_files()


def main():
    if not preflight():
        return 0
    test_command_lock_unlock()
    test_physical_unlock()
    test_grab_conflict()
    test_watcher_end_to_end()
    print("---")
    failed = [n for n, ok, _ in _results if not ok]
    print("共 %d 项断言，失败 %d 项" % (len(_results), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
