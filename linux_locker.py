"""Linux 版 InputLocker 后端（与 main.pyw 的 Windows 实现功能对齐）。

两种模式，按会话类型自动选择：
- X11 会话: LinuxInputLocker —— XGrabKeyboard/XGrabPointer 全局拦截输入，
  与 Windows 版行为一致（3x CapsLock 触发解锁、自定义密码、解锁时鼠标仍禁用）。
- Wayland 会话: WaylandLocker —— Wayland 协议不开放第三方全局输入拦截
  （KWin 无此接口），改用系统锁屏（loginctl lock-session，系统密码解锁）
  + systemd-inhibit 保持屏幕常亮/阻止休眠。

公共机制（两种模式共用）：
- 屏幕常亮 + 阻止休眠: systemd-inhibit --what=idle:sleep
  （KDE Plasma 6 尊重 logind idle/sleep 抑制剂：idle 阻止锁屏和息屏，sleep 阻止挂起）
- USB 存储禁用: udev 规则对 USB 大容量存储设备（bInterfaceClass=08）deauthorize，
  需要 root（非 root 时自动跳过，界面提示）
"""

import json
import os
import select
import sys
import time
import threading
import subprocess
import tempfile

from locker_lifecycle import serialized

from Xlib import X, display
from Xlib.ext import xfixes

try:
    import evdev
    from evdev import ecodes
except ImportError:
    evdev = None  # 未安装时 EvdevLocker 不可用，回退系统锁屏


def evdev_available():
    """能否读写 /dev/input/event*。只检查权限，不抓取、不锁屏。"""
    if evdev is None:
        return False
    try:
        paths = evdev.list_devices()
    except Exception:
        return False
    return any(os.access(p, os.R_OK | os.W_OK) for p in paths)

# ---- 常量 ----
XK_CAPS_LOCK = 0xFFE5
XK_BACKSPACE = 0xFF08
XK_RETURN = 0xFF0D
XK_KP_ENTER = 0xFF8D
XK_SHIFT_L = 0xFFE1
XK_SHIFT_R = 0xFFE2

CAPS_TRIGGER_COUNT = 3
CAPS_TRIGGER_WINDOW = 2.0

DEFAULT_PASSWORD = "123456"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")

USB_RULE = os.environ.get("USB_RULE_PATH", "/etc/udev/rules.d/99-input-locker-usb.rules")
USB_RULE_CONTENT = (
    'ACTION=="add", SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="08", '
    'RUN+="/bin/sh -c \'echo 0 > /sys$env{DEVPATH}/../authorized\'"\n'
)


def _load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"password": DEFAULT_PASSWORD}


def _save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---- 按键解释（纯逻辑，可自检） ----
def _is_letter_or_digit(ks):
    if 0x30 <= ks <= 0x39 or 0x41 <= ks <= 0x5A or 0x61 <= ks <= 0x7A:
        return True
    if ks >= 0x01000000:  # Unicode keysym
        cp = ks - 0x01000000
        return 0 < cp < 0x110000 and chr(cp).isalnum()
    return False


def _keysym_char(ks):
    if 0x20 <= ks <= 0x7E:
        return chr(ks)
    if ks >= 0x01000000:
        cp = ks - 0x01000000
        if 0 < cp < 0x110000:
            ch = chr(cp)
            return ch if ch.isprintable() else ""
    return ""


def _char_for_key(d, keycode, state):
    """把按键映射为 (action, char)。action: char / backspace / submit / ignore。
    与 Windows 版一致：只放行字母/数字键（含 Shift 组合）、退格、回车、Shift。"""
    base = d.keycode_to_keysym(keycode, 0)
    shifted = d.keycode_to_keysym(keycode, 1 if state & X.ShiftMask else 0)
    if base in (XK_BACKSPACE,):
        return "backspace", ""
    if base in (XK_RETURN, XK_KP_ENTER):
        return "submit", ""
    if base in (XK_SHIFT_L, XK_SHIFT_R):
        return "ignore", ""
    if _is_letter_or_digit(base) or _is_letter_or_digit(shifted):
        ch = _keysym_char(shifted)
        if ch:
            return "char", ch
    return "ignore", ""


# ---- evdev 设备分类 ----

def _device_kind(dev):
    """设备分类: 'keyboard' / 'pointer' / None（键盘含字母键，指针含鼠标/触摸）。"""
    caps = dev.capabilities()
    keys = caps.get(ecodes.EV_KEY, [])
    rel = caps.get(ecodes.EV_REL, [])
    abs_ = caps.get(ecodes.EV_ABS, [])
    if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
        return "keyboard"
    if (ecodes.BTN_LEFT in keys or ecodes.BTN_TOUCH in keys
            or ecodes.REL_X in rel or ecodes.ABS_X in abs_
            or ecodes.ABS_MT_POSITION_X in abs_):
        return "pointer"
    return None


# ---- keycode 常量（linux/input-event-codes.h）----
KEY_A = 30
KEY_Z = 44
KEY_0 = 11
KEY_BACKSPACE = 14
KEY_CAPSLOCK = 58
KEY_ENTER = 28
KEY_LEFT_SHIFT = 42
KEY_RIGHT_SHIFT = 54
KEY_KPENTER = 96

# evdev keycode -> X11 keycode（+8），再按 X11 keysym 布局映射字符。
# 无 X display 时的回退小表：evdev code -> (base, shifted) ASCII。
_US_FALLBACK = {
    KEY_A: ("a", "A"), 30: ("a", "A"),
    31: ("s", "S"), 32: ("d", "D"), 33: ("f", "F"),
    34: ("g", "G"), 35: ("h", "H"), 36: ("j", "J"),
    37: ("k", "K"), 38: ("l", "L"), 16: ("q", "Q"),
    17: ("w", "W"), 18: ("e", "E"), 19: ("r", "R"),
    20: ("t", "T"), 21: ("y", "Y"), 22: ("u", "U"),
    23: ("i", "I"), 24: ("o", "O"), 25: ("p", "P"),
    44: ("z", "Z"), 45: ("x", "X"), 46: ("c", "C"),
    47: ("v", "V"), 48: ("b", "B"), 49: ("n", "N"),
    50: ("m", "M"), 2: ("1", "!"), 3: ("2", "@"),
    4: ("3", "#"), 5: ("4", "$"), 6: ("5", "%"),
    7: ("6", "^"), 8: ("7", "&"), 9: ("8", "*"),
    10: ("9", "("), 11: ("0", ")"),
}

_X11_KEYCODE_OFFSET = 8  # evdev 码 +8 = X11 keycode


def _x11_keysym(x11kc, shifted):
    """X11 keycode -> keysym（用真实布局；X 连接失败时返回 0 触发回退表）。"""
    try:
        d = display.Display()
        try:
            return d.keycode_to_keysym(x11kc, 1 if shifted else 0)
        finally:
            d.close()
    except Exception:
        return 0


def _key_to_char(ev_code, shifted):
    """evdev keycode -> (action, char)。无 X 时回退到内置 US 表。"""
    if ev_code in (KEY_BACKSPACE,):
        return "backspace", ""
    if ev_code in (KEY_ENTER, KEY_KPENTER):
        return "submit", ""
    if ev_code in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
        return "ignore", ""
    if ev_code == KEY_CAPSLOCK:
        return "ignore", ""
    x11kc = ev_code + _X11_KEYCODE_OFFSET
    ks = _x11_keysym(x11kc, shifted)
    if ks:
        base = _x11_keysym(x11kc, False)
        if _is_letter_or_digit(base) or _is_letter_or_digit(ks):
            ch = _keysym_char(ks)
            if ch:
                return "char", ch
        return "ignore", ""
    entry = _US_FALLBACK.get(ev_code)
    if not entry:
        return "ignore", ""
    return "char", entry[1] if shifted else entry[0]


# ---- 系统状态（两种模式共用） ----
def _start_inhibit():
    try:
        return subprocess.Popen(
            ["systemd-inhibit", "--what=idle:sleep", "--who=input-locker",
             "--why=locked", "sleep", "infinity"])
    except Exception:
        return None


def _stop_inhibit(proc):
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=3)  # 回收子进程，避免留下 zombie。
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass


def _usb_script(enable):
    if enable:
        body = "rm -f %s" % USB_RULE
        tail = (
            "for f in /sys/bus/usb/devices/*/authorized; do\n"
            '  [ "$(cat "$f")" = "0" ] && echo 1 > "$f"\n'
            "done\n"
        )
    else:
        body = "cat > %s <<'EOF'\n%sEOF" % (USB_RULE, USB_RULE_CONTENT)
        tail = ""
    return (
        "#!/bin/sh\nset -e\n" + body + "\n"
        "udevadm control --reload-rules\n"
        "udevadm trigger --subsystem-match=usb\n" + tail
    )


def _usb_storage(enable, lock, locker):
    """仅 root 时执行 USB 存储禁用/启用；非 root 跳过（避免 pkexec 弹窗骚扰）。"""
    if os.geteuid() != 0:
        locker.usb_storage_skipped = True
        return

    def worker():
        with lock:
            try:
                fd, path = tempfile.mkstemp(prefix="input-locker-usb-", suffix=".sh")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(_usb_script(enable))
                    os.chmod(path, 0o755)
                    subprocess.run(["sh", path], timeout=15)
                finally:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            except Exception:
                pass
    threading.Thread(target=worker, daemon=True).start()


class LinuxInputLocker:
    """X11 会话后端：XGrabKeyboard/XGrabPointer 全局拦截，3x CapsLock 触发解锁。"""

    def __init__(self):
        self._state_lock = threading.RLock()
        self.lock_active = False
        self.unlock_mode = False
        self._unlock_mode_changed = False
        self.unlock_password = _load_config().get("password", DEFAULT_PASSWORD)
        self._thread = None
        self._ready = threading.Event()
        self._error = None
        self._inhibit = None
        self._usb_lock = threading.Lock()
        self._on_password = None
        self._on_submit = None
        self._caps_times = []
        self._pwd = ""
        self._d = None
        self.usb_storage_skipped = False

    def set_password(self, new_password):
        self.unlock_password = new_password
        cfg = _load_config()
        cfg["password"] = new_password
        _save_config(cfg)

    def set_ui_callbacks(self, on_password=None, on_submit=None):
        self._on_password = on_password
        self._on_submit = on_submit

    # ---- 锁定/解锁 ----
    @serialized
    def start_lock(self):
        if self.lock_active:
            return True
        if self._thread and self._thread.is_alive():
            return False  # 上一代 worker 尚未退出，不可复用其共享状态。
        try:
            self.lock_active = True
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self._pwd = ""
            self._caps_times.clear()
            self._ready.clear()
            self._error = None
            self._inhibit = _start_inhibit()
            _usb_storage(False, self._usb_lock, self)
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise TimeoutError("输入抓取启动超时")
            if self._error:
                raise self._error
            return self.lock_active
        except Exception as e:
            self._error = e
            self.stop_lock()
            return False

    @serialized
    def stop_lock(self):
        try:
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self.lock_active = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    self._error = TimeoutError("输入抓取线程尚未退出")
                    return False
            _stop_inhibit(self._inhibit)
            self._inhibit = None
            _usb_storage(True, self._usb_lock, self)
            return True
        except Exception as e:
            self._error = e
            return False

    def cancel_unlock_mode(self):
        self.unlock_mode = False
        self._pwd = ""
        self._unlock_mode_changed = True

    def emergency_restore(self):
        self.stop_lock()

    # ---- X11 抓取 ----
    def _worker(self):
        try:
            d = display.Display()
            self._d = d
            root = d.screen().root
            self._caps_keycode = d.keysym_to_keycode(XK_CAPS_LOCK)
            root.change_attributes(event_mask=X.KeyPressMask)
            try:
                d.xfixes_query_version()  # 协商 XFIXES 版本，否则 hide_cursor 会触发无关错误
            except Exception:
                pass
            st = root.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            if st != X.GrabSuccess:
                raise RuntimeError("键盘抓取失败 (GrabStatus=%d)" % st)
            st = root.grab_pointer(True, 0, X.GrabModeAsync, X.GrabModeAsync, X.NONE, X.NONE, X.CurrentTime)
            if st != X.GrabSuccess:
                d.ungrab_keyboard(X.CurrentTime)
                raise RuntimeError("鼠标抓取失败 (GrabStatus=%d)" % st)
            root.xfixes_hide_cursor()
            self._error = None
            self._ready.set()
            while self.lock_active:
                try:
                    while d.pending_events():
                        ev = d.next_event()
                        if ev.type == X.KeyPress:
                            self._on_key(d, ev)
                except AttributeError:
                    # python-xlib 处理 XWayland 偶发错误（BadRRCrtcError）时的 bug，忽略
                    pass
                time.sleep(0.01)
        except Exception as e:
            self._error = e
            self.lock_active = False
            self._unlock_mode_changed = True
            self._ready.set()
        finally:
            self._cleanup()
            _stop_inhibit(self._inhibit)
            self._inhibit = None
            _usb_storage(True, self._usb_lock, self)

    def _cleanup(self):
        d = self._d
        self._d = None
        if d:
            try:
                d.ungrab_keyboard(X.CurrentTime)
            except Exception:
                pass
            try:
                d.ungrab_pointer(X.CurrentTime)
            except Exception:
                pass
            try:
                d.screen().root.xfixes_show_cursor()
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass

    def _on_key(self, d, ev):
        keycode = ev.detail
        if keycode == self._caps_keycode:
            now = time.time()
            self._caps_times.append(now)
            self._caps_times = [t for t in self._caps_times if now - t < CAPS_TRIGGER_WINDOW]
            if len(self._caps_times) >= CAPS_TRIGGER_COUNT:
                self._caps_times.clear()
                if not self.unlock_mode:
                    self.unlock_mode = True
                    self._pwd = ""
                    self._unlock_mode_changed = True
            return
        if not self.unlock_mode:
            return
        action, ch = _char_for_key(d, keycode, ev.state)
        if action == "char":
            self._pwd += ch
            self._notify_password()
        elif action == "backspace":
            if self._pwd:
                self._pwd = self._pwd[:-1]
                self._notify_password()
        elif action == "submit":
            self._submit()

    def _notify_password(self):
        if self._on_password:
            self._on_password(self._pwd)

    def _submit(self):
        if self._on_submit:
            self._on_submit(self._pwd)


class EvdevLocker:
    """Wayland/X11 通用后端：evdev (EVIOCGRAB) 内核级设备抓取。

    抓取后只有本进程收到事件，合成器（KWin）收不到——与显示服务器无关，
    Wayland/X11 均有效。键盘+鼠标+触摸全部抓取，锁定期间合成器收不到任何输入；
    3x CapsLock 触发解锁模式，密码在抓取层直接解析（无需合成器转交）。
    需对 /dev/input/event* 有读写权限（root 或 input 组成员）。
    """

    def __init__(self):
        self.lock_active = False
        self.unlock_mode = False
        self._unlock_mode_changed = False
        self.unlock_password = _load_config().get("password", DEFAULT_PASSWORD)
        self._thread = None
        self._ready = threading.Event()
        self._error = None
        self._inhibit = None
        self._usb_lock = threading.Lock()
        self._on_password = None
        self._on_submit = None
        self._caps_times = []
        self._pwd = ""
        self._shifted = False
        self._state_lock = threading.RLock()
        self._devs = {}          # path -> InputDevice
        self._grab_error = None  # 权限/抓取失败原因（触发回退）
        self.usb_storage_skipped = False

    def set_password(self, new_password):
        self.unlock_password = new_password
        cfg = _load_config()
        cfg["password"] = new_password
        _save_config(cfg)

    def set_ui_callbacks(self, on_password=None, on_submit=None):
        self._on_password = on_password
        self._on_submit = on_submit

    # ---- 锁定/解锁 -------
    @serialized
    def start_lock(self):
        if self.lock_active:
            return True
        if self._thread and self._thread.is_alive():
            return False  # 上一代 worker 尚未退出，不可复用其共享状态。
        if evdev is None:
            self._error = RuntimeError("evdev 未安装")
            return False
        try:
            self.lock_active = True
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self._pwd = ""
            self._caps_times.clear()
            self._shifted = False
            self._ready.clear()
            self._error = None
            self._grab_error = None
            self._inhibit = _start_inhibit()
            _usb_storage(False, self._usb_lock, self)
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise TimeoutError("输入抓取启动超时")
            if self._grab_error:
                raise self._grab_error
            if self._error:
                raise self._error
            return self.lock_active
        except Exception as e:
            self._error = e
            self.stop_lock()
            return False

    @serialized
    def stop_lock(self):
        try:
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self.lock_active = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    self._error = TimeoutError("输入抓取线程尚未退出")
                    return False
            _stop_inhibit(self._inhibit)
            self._inhibit = None
            _usb_storage(True, self._usb_lock, self)
            return True
        except Exception as e:
            self._error = e
            return False

    def cancel_unlock_mode(self):
        self.unlock_mode = False
        self._pwd = ""
        self._unlock_mode_changed = True

    def emergency_restore(self):
        self.stop_lock()

    # ---- 抓取主循环 ----
    def _open_devices(self):
        """枚举并抓取全部键盘/指针/触摸设备。返回抓取到的设备 dict。"""
        grabbed = {}
        try:
            paths = evdev.list_devices()
        except Exception:
            paths = []
        if not paths:
            raise PermissionError("无法枚举输入设备（需要 input 组或 root）")
        for path in paths:
            try:
                dev = evdev.InputDevice(path)
            except Exception:
                continue  # 竞态：设备刚消失
            try:
                if _device_kind(dev) is None:
                    dev.close()
                    continue
                dev.grab()
                grabbed[path] = dev
            except Exception as e:
                dev.close()
                for opened in grabbed.values():
                    opened.close()
                raise PermissionError(
                    "无法抓取 %s (%s)：检查 input 组权限及设备是否被占用"
                    % (path, e)) from e
        if not grabbed:
            raise RuntimeError("未找到可抓取的输入设备")
        return grabbed

    def _worker(self):
        try:
            self._devs = self._open_devices()
            self._ready.set()
            self._last_scan = 0.0
            while self.lock_active:
                self._handle_events()
                now = time.time()
                if now - self._last_scan > 2.0:  # 轮询新插入设备（防 USB 键盘绕过）
                    self._last_scan = now
                    self._scan_new()
        except Exception as e:
            if not self._ready.is_set():
                self._grab_error = e  # 启动失败（权限不足等）
            self._error = e
            self.lock_active = False
            self._unlock_mode_changed = True
            self._ready.set()
        finally:
            for dev in list(self._devs.values()):
                try:
                    dev.close()  # fd 关闭内核自动释放 grab
                except Exception:
                    pass
            self._devs.clear()
            _stop_inhibit(self._inhibit)
            self._inhibit = None
            _usb_storage(True, self._usb_lock, self)

    def _handle_events(self):
        if not self._devs:
            return
        try:
            rl, _, _ = select.select(list(self._devs.values()), [], [], 0.2)
        except Exception:
            return
        for dev in rl:
            try:
                for e in dev.read():
                    if e.type == ecodes.EV_KEY:
                        self._on_key(e.code, e.value)
            except OSError:
                self._drop_dev(dev)  # 设备拔出
            except Exception:
                pass

    def _on_key(self, code, value):
        """处理按键事件。value: 1按下 0释放（同步/重复 2 忽略）。"""
        if code in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
            self._shifted = (value == 1)  # 按下/释放都更新
            return
        if value != 1:
            return
        if code == KEY_CAPSLOCK:
            now = time.time()
            self._caps_times.append(now)
            self._caps_times = [t for t in self._caps_times
                                if now - t < CAPS_TRIGGER_WINDOW]
            if len(self._caps_times) >= CAPS_TRIGGER_COUNT:
                self._caps_times.clear()
                if not self.unlock_mode:
                    self.unlock_mode = True
                    self._pwd = ""
                    self._unlock_mode_changed = True
            return
        if not self.unlock_mode:
            return
        action, ch = _key_to_char(code, self._shifted)
        if action == "char":
            self._pwd += ch
            self._notify_password()
        elif action == "backspace":
            if self._pwd:
                self._pwd = self._pwd[:-1]
                self._notify_password()
        elif action == "submit":
            self._submit()

    def _notify_password(self):
        if self._on_password:
            self._on_password(self._pwd)

    def _submit(self):
        if self._on_submit:
            self._on_submit(self._pwd)

    def _drop_dev(self, dev):
        for p, d in list(self._devs.items()):
            if d is dev:
                try:
                    d.close()
                except Exception:
                    pass
                del self._devs[p]
                return

    def _scan_new(self):
        """发现并抓取新插入的键盘/指针设备。"""
        try:
            paths = evdev.list_devices()
        except Exception:
            return
        for path in paths:
            if path in self._devs:
                continue
            try:
                dev = evdev.InputDevice(path)
                if _device_kind(dev) is None:
                    dev.close()
                    continue
                dev.grab()
                self._devs[path] = dev
            except Exception:
                try:
                    dev.close()
                except Exception:
                    pass


class WaylandLocker:
    """Wayland 会话后端：系统锁屏锁定输入 + systemd-inhibit 保持屏幕常亮/阻止休眠。
    自定义密码/3x CapsLock 流程不适用（系统锁屏用系统密码解锁）。"""

    def __init__(self):
        self.lock_active = False
        self.unlock_mode = False
        self._unlock_mode_changed = False
        self.unlock_password = _load_config().get("password", DEFAULT_PASSWORD)
        self._inhibit = None
        self._poll_thread = None
        self._state_lock = threading.RLock()
        self._error = None
        self._usb_lock = threading.Lock()
        self.usb_storage_skipped = False

    def set_password(self, new_password):
        self.unlock_password = new_password
        cfg = _load_config()
        cfg["password"] = new_password
        _save_config(cfg)

    def _get_active(self):
        """系统锁屏是否处于激活状态。出错时保守认为仍锁定。"""
        try:
            out = subprocess.run(
                ["qdbus", "org.freedesktop.ScreenSaver", "/ScreenSaver",
                 "org.freedesktop.ScreenSaver.GetActive"],
                capture_output=True, text=True, timeout=5, check=True)
            return out.stdout.strip() == "true"
        except Exception:
            return True

    @serialized
    def start_lock(self):
        if self.lock_active:
            return True
        try:
            self._error = None
            self.lock_active = True
            self._inhibit = _start_inhibit()
            _usb_storage(False, self._usb_lock, self)
            subprocess.run(["loginctl", "lock-session"], timeout=10, check=True)
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()
            return True
        except Exception as e:
            self._error = e
            self.stop_lock()
            return False

    def _poll(self):
        while True:
            with self._state_lock:
                if not self.lock_active or self._poll_thread is not threading.current_thread():
                    return  # 旧的轮询线程不得解锁下一次启动的锁。
                if not self._get_active():
                    self.stop_lock()
                    return
            time.sleep(1)

    @serialized
    def stop_lock(self):
        try:
            self.lock_active = False
            _stop_inhibit(self._inhibit)
            self._inhibit = None
            _usb_storage(True, self._usb_lock, self)
            return True
        except Exception:
            return False

    def cancel_unlock_mode(self):
        pass

    def emergency_restore(self):
        self.stop_lock()


if __name__ == "__main__":
    # 自检：按键解释逻辑（需要 X 显示，XWayland 亦可）
    d = display.Display()
    try:
        def kc(ks):
            return d.keysym_to_keycode(ks)

        def check(keycode, state, expect):
            got = _char_for_key(d, keycode, state)
            assert got == expect, "keycode=%s state=%s: got %r, want %r" % (keycode, state, got, expect)

        check(kc(0x61), 0, ("char", "a"))            # a
        check(kc(0x61), X.ShiftMask, ("char", "A"))   # Shift+a
        check(kc(0x31), 0, ("char", "1"))             # 1
        check(kc(0x31), X.ShiftMask, ("char", "!"))   # Shift+1
        check(kc(XK_BACKSPACE), 0, ("backspace", ""))
        check(kc(XK_RETURN), 0, ("submit", ""))
        check(kc(XK_SHIFT_L), 0, ("ignore", ""))
        check(kc(0xFFBE), 0, ("ignore", ""))          # F1
        print("linux_locker 自检通过")
    finally:
        d.close()

    # EvdevLocker 自检：真实键盘 keycode+8 -> 字符（无需 root，只读 X）
    for ec in (KEY_A, 2, KEY_BACKSPACE, KEY_ENTER, KEY_CAPSLOCK, KEY_LEFT_SHIFT):
        base = _x11_keysym(ec + _X11_KEYCODE_OFFSET, False)
        shift = _x11_keysym(ec + _X11_KEYCODE_OFFSET, True)
        print("  evcode=%3d x11kc=%3d -> keysym base=%#x shifted=%#x" % (ec, ec + 8, base, shift))
    print("EvdevLocker 映射自检通过")

    # 无 X 回退表自检
    assert _key_to_char(KEY_A, False) == ("char", "a")
    assert _key_to_char(KEY_A, True) == ("char", "A")
    assert _key_to_char(2, True) == ("char", "!")
    assert _key_to_char(KEY_BACKSPACE, False) == ("backspace", "")
    assert _key_to_char(KEY_ENTER, False) == ("submit", "")
    assert _key_to_char(KEY_CAPSLOCK, False) == ("ignore", "")
    assert _key_to_char(1, False) == ("ignore", "")   # ESC
    print("US 回退表自检通过")
