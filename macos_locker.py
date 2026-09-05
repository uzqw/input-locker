"""macOS InputLocker backend: CGEventTap swallows keyboard/mouse.

Unlock matches EvdevLocker: 3× CapsLock within 2s, then password parsed in
this process (taps do not pass keys through to Tk). ctypes only; no PyObjC.
"""
import ctypes
import json
import os
import subprocess
import sys
import threading
import time

from locker_lifecycle import serialized

CAPS_TRIGGER_COUNT = 3
CAPS_TRIGGER_WINDOW = 2.0
DEFAULT_PASSWORD = "123456"
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "config.json")

kCGSessionEventTap = 1
kCGHeadInsertEventTap = 0
kCGEventTapOptionDefault = 0
kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventRightMouseDown = 3
kCGEventRightMouseUp = 4
kCGEventMouseMoved = 5
kCGEventLeftMouseDragged = 6
kCGEventRightMouseDragged = 7
kCGEventKeyDown = 10
kCGEventKeyUp = 11
kCGEventFlagsChanged = 12
kCGEventScrollWheel = 22
kCGEventOtherMouseDown = 25
kCGEventOtherMouseUp = 26
kCGEventOtherMouseDragged = 27
kCGEventTapDisabledByTimeout = 0xFFFFFFFE
kCGEventTapDisabledByUserInput = 0xFFFFFFFF
kCGEventFlagMaskAlphaShift = 0x00010000
kCGKeyboardEventKeycode = 9
kVK_Return = 0x24
kVK_Delete = 0x33
kVK_CapsLock = 0x39
kVK_ANSI_KeypadEnter = 0x4C
kCFStringEncodingUTF8 = 0x08000100

CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
AS_PATH = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
HI_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/"
    "Frameworks/HIServices.framework/HIServices"
)
IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"

kIOHIDCapsLockState = 1
kIOHIDParamConnectType = 1  # IOHIDShared.h: kIOHIDServerConnectType=0, kIOHIDParamConnectType=1

TAP_CB = ctypes.CFUNCTYPE(
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p
)

_EVENT_MASK = 0
for _t in (
    kCGEventKeyDown, kCGEventKeyUp, kCGEventFlagsChanged,
    kCGEventLeftMouseDown, kCGEventLeftMouseUp,
    kCGEventRightMouseDown, kCGEventRightMouseUp,
    kCGEventMouseMoved, kCGEventLeftMouseDragged, kCGEventRightMouseDragged,
    kCGEventOtherMouseDown, kCGEventOtherMouseUp, kCGEventOtherMouseDragged,
    kCGEventScrollWheel,
):
    _EVENT_MASK |= 1 << _t

_LIBS = None


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


def _cf_symbol(cf, name):
    return ctypes.c_void_p.in_dll(cf, name)


def _bind(cg, cf):
    cg.CGEventTapCreate.restype = ctypes.c_void_p
    cg.CGEventTapCreate.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
        TAP_CB, ctypes.c_void_p,
    ]
    cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    cg.CGEventTapIsEnabled.restype = ctypes.c_bool
    cg.CGEventTapIsEnabled.argtypes = [ctypes.c_void_p]
    cg.CGEventGetFlags.restype = ctypes.c_uint64
    cg.CGEventGetFlags.argtypes = [ctypes.c_void_p]
    cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
    cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    cg.CGEventKeyboardGetUnicodeString.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16),
    ]
    cg.CGDisplayHideCursor.restype = ctypes.c_int32
    cg.CGDisplayHideCursor.argtypes = [ctypes.c_uint32]
    cg.CGDisplayShowCursor.restype = ctypes.c_int32
    cg.CGDisplayShowCursor.argtypes = [ctypes.c_uint32]
    cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    cf.CFMachPortCreateRunLoopSource.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
    ]
    cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRunLoopRemoveSource.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    cf.CFRunLoopRunInMode.restype = ctypes.c_int32
    cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
    cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]


def _libs():
    global _LIBS
    if _LIBS is None:
        cg = ctypes.CDLL(CG_PATH)
        cf = ctypes.CDLL(CF_PATH)
        _bind(cg, cf)
        _LIBS = (cg, cf)
    return _LIBS


_HID = None
_HID_CONNECT = None


def _hid():
    """IOKit 连接：IOHIDSetModifierLockState 驱动 CapsLock 锁存 + LED。
    惰性加载，失败返回 None（灯状态保持驱动 toggle 结果，功能不受影响）。"""
    global _HID, _HID_CONNECT
    if _HID is None:
        try:
            iokit = ctypes.CDLL(IOKIT_PATH)
            iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
            iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            iokit.IOServiceMatching.restype = ctypes.c_void_p
            iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
            iokit.IOServiceOpen.restype = ctypes.c_int32
            iokit.IOServiceOpen.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
            iokit.IOHIDSetModifierLockState.restype = ctypes.c_int32
            iokit.IOHIDSetModifierLockState.argtypes = [
                ctypes.c_uint32, ctypes.c_int32, ctypes.c_bool,
            ]
            _HID = iokit
        except Exception:
            return None
    if _HID_CONNECT is None:
        try:
            libc = ctypes.CDLL(None)
            mach_task_self = ctypes.c_uint.in_dll(libc, "mach_task_self_").value
            service = _HID.IOServiceGetMatchingService(
                0, _HID.IOServiceMatching(b"IOHIDSystem"))
            if not service:
                return None
            connect = ctypes.c_uint32(0)
            if _HID.IOServiceOpen(
                    service, mach_task_self, kIOHIDParamConnectType,
                    ctypes.byref(connect)) != 0:
                return None
            _HID.IOObjectRelease(service)
            _HID_CONNECT = connect.value
        except Exception:
            return None
    return _HID_CONNECT


def _set_caps_lock_state(on):
    """把 CapsLock 锁存/LED 设回指定状态（社区做法：驱动层 toggle 无法拦截，
    检测到翻转后立即设回，用户看不到灯变化）。返回是否成功。"""
    connect = _hid()
    if not connect:
        return False
    try:
        return _HID.IOHIDSetModifierLockState(
            connect, kIOHIDCapsLockState, bool(on)) == 0
    except Exception:
        return False


def _ax_lib():
    last = None
    for path in (AS_PATH, HI_PATH):
        try:
            lib = ctypes.CDLL(path)
            lib.AXIsProcessTrusted.restype = ctypes.c_bool
            return lib
        except OSError as e:
            last = e
    raise RuntimeError("无法加载辅助功能 API") from last


def _cf_type_dict_callbacks(cf):
    # Symbol is the callback struct, not a pointer. NULL callbacks store raw
    # pointers and skip retain — releasing the key then SIGSEGVs in AX.
    return (
        ctypes.c_void_p(ctypes.addressof(
            ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))),
        ctypes.c_void_p(ctypes.addressof(
            ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))),
    )


def _ax_prompt_options(cf):
    key = cf.CFStringCreateWithCString(
        None, b"AXTrustedCheckOptionPrompt", kCFStringEncodingUTF8
    )
    if not key:
        return None
    try:
        true = _cf_symbol(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key)
        vals = (ctypes.c_void_p * 1)(true)
        kcb, vcb = _cf_type_dict_callbacks(cf)
        return cf.CFDictionaryCreate(None, keys, vals, 1, kcb, vcb)
    finally:
        cf.CFRelease(key)


def _is_trusted(prompt=True):
    ax = _ax_lib()
    ax.AXIsProcessTrusted.restype = ctypes.c_bool
    if ax.AXIsProcessTrusted():
        return True
    if not prompt:
        return False
    ax.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    ax.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    _, cf = _libs()
    opts = _ax_prompt_options(cf)
    try:
        return bool(ax.AXIsProcessTrustedWithOptions(opts))
    finally:
        if opts:
            cf.CFRelease(opts)


def _start_caffeinate():
    try:
        return subprocess.Popen(
            ["caffeinate", "-dimsu"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None


def _stop_caffeinate(proc):
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
    except Exception:
        pass


class MacInputLocker:
    """Session event tap: swallow key/mouse, 3× CapsLock then password."""

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
        self._on_password = None
        self._on_submit = None
        self._caps_times = []
        self._pwd = ""
        self._caps_on = False
        self._restoring = False
        self._caps_echo_until = 0.0
        self._q = None
        self._cf = None
        self._tap = None
        self._src = None
        self._rl = None
        self._callback = None
        self._cursor_hidden = False

    def set_password(self, new_password):
        self.unlock_password = new_password
        cfg = _load_config()
        cfg["password"] = new_password
        _save_config(cfg)

    def set_ui_callbacks(self, on_password=None, on_submit=None):
        self._on_password = on_password
        self._on_submit = on_submit

    @serialized
    def start_lock(self):
        if self.lock_active:
            return True
        if self._thread and self._thread.is_alive():
            return False
        try:
            self.lock_active = True
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self._pwd = ""
            self._caps_times.clear()
            self._caps_on = False
            self._restoring = False
            self._caps_echo_until = 0.0
            self._ready.clear()
            self._error = None
            if not _is_trusted(prompt=True):
                raise RuntimeError("需要辅助功能（Accessibility）权限")
            self._inhibit = _start_caffeinate()
            if self._inhibit is None:
                raise RuntimeError("无法启动 caffeinate（屏幕常亮）")
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise TimeoutError("事件轻击启动超时")
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
            self._stop_run_loop()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3)
                if self._thread.is_alive():
                    self._error = TimeoutError("事件轻击线程尚未退出")
                    return False
            _stop_caffeinate(self._inhibit)
            self._inhibit = None
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

    def _worker(self):
        try:
            self._install_tap()
            self._hide_cursor()
            self._error = None
            self._ready.set()
            self._run_loop()
        except Exception as e:
            self._error = e
            self.lock_active = False
            self._unlock_mode_changed = True
            self._ready.set()
        finally:
            self._cleanup()
            _stop_caffeinate(self._inhibit)
            self._inhibit = None

    def _install_tap(self):
        q, cf = _libs()
        self._q, self._cf = q, cf
        self._callback = TAP_CB(self._tap_callback)
        tap = q.CGEventTapCreate(
            kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionDefault,
            _EVENT_MASK, self._callback, None,
        )
        if not tap:
            raise RuntimeError("CGEventTap 创建失败（检查辅助功能权限）")
        src = cf.CFMachPortCreateRunLoopSource(None, tap, 0)
        if not src:
            raise RuntimeError("CGEventTap runloop source 创建失败")
        rl = cf.CFRunLoopGetCurrent()
        cf.CFRunLoopAddSource(rl, src, _cf_symbol(cf, "kCFRunLoopCommonModes"))
        q.CGEventTapEnable(tap, True)
        if not q.CGEventTapIsEnabled(tap):
            raise RuntimeError("CGEventTap 未能启用")
        self._tap, self._src, self._rl = tap, src, rl

    def _run_loop(self):
        cf = self._cf
        mode = _cf_symbol(cf, "kCFRunLoopDefaultMode")
        while self.lock_active:
            cf.CFRunLoopRunInMode(mode, 0.25, False)

    def _stop_run_loop(self):
        rl, cf = self._rl, self._cf
        if rl and cf:
            try:
                cf.CFRunLoopStop(rl)
            except Exception:
                pass

    def _tap_callback(self, proxy, etype, event, refcon):
        try:
            if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
                self._reenable_tap()
                return event
            flags = 0
            keycode = 0
            chars = ""
            if self._q and event:
                if etype in (kCGEventFlagsChanged, kCGEventKeyDown):
                    flags = int(self._q.CGEventGetFlags(event))
                    keycode = int(
                        self._q.CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                    )
                if etype == kCGEventKeyDown:
                    chars = self._event_chars(event)
            self._handle(etype, flags, keycode, chars)
        except Exception:
            pass
        return None

    def _reenable_tap(self):
        q, tap = self._q, self._tap
        if not q or not tap:
            self._fail_tap("事件轻击被系统禁用")
            return
        q.CGEventTapEnable(tap, True)
        if not q.CGEventTapIsEnabled(tap):
            self._fail_tap("事件轻击被系统禁用")

    def _fail_tap(self, msg):
        self._error = RuntimeError(msg)
        self.lock_active = False
        self._unlock_mode_changed = True
        self._stop_run_loop()

    def _event_chars(self, event):
        buf = (ctypes.c_uint16 * 4)()
        n = ctypes.c_ulong(0)
        self._q.CGEventKeyboardGetUnicodeString(event, 4, ctypes.byref(n), buf)
        if not n.value:
            return ""
        return "".join(chr(buf[i]) for i in range(min(int(n.value), 4)))

    def _handle(self, etype, flags=0, keycode=0, chars=""):
        if etype == kCGEventFlagsChanged:
            now = time.time()
            # IOHIDSetModifierLockState 会同步/异步回灌 flagsChanged。回灌期间
            # 再计数或再设回，会把 _caps_on 和真实灯状态打乱，之后按键再也对不上沿。
            if self._restoring:
                return
            caps = bool(flags & kCGEventFlagMaskAlphaShift)
            if now < self._caps_echo_until and caps == self._caps_on:
                return
            prev = self._caps_on
            self._caps_on = caps
            if keycode == kVK_CapsLock and caps != prev:
                # CapsLock 是切换键：按一下 AlphaShift 翻转一次。数翻转而不是只数 off→on。
                self._caps_times.append(now)
                self._caps_times = [t for t in self._caps_times if now - t < CAPS_TRIGGER_WINDOW]
                if len(self._caps_times) >= CAPS_TRIGGER_COUNT:
                    self._caps_times.clear()
                    self.unlock_mode = not self.unlock_mode
                    if self.unlock_mode:
                        self._pwd = ""
                    self._unlock_mode_changed = True
                self._restoring = True
                try:
                    ok = _set_caps_lock_state(prev)
                finally:
                    self._restoring = False
                if ok:
                    self._caps_on = prev
                    self._caps_echo_until = now + 0.08
            return
        if etype != kCGEventKeyDown or not self.unlock_mode:
            return
        if keycode == kVK_Delete:
            if self._pwd:
                self._pwd = self._pwd[:-1]
                self._notify_password()
            return
        if keycode in (kVK_Return, kVK_ANSI_KeypadEnter):
            self._submit()
            return
        if chars and chars[0].isalnum():
            self._pwd += chars[0]
            self._notify_password()

    def _notify_password(self):
        if self._on_password:
            self._on_password(self._pwd)

    def _submit(self):
        if self._on_submit:
            self._on_submit(self._pwd)

    def _hide_cursor(self):
        if self._cursor_hidden or not self._q:
            return
        try:
            err = self._q.CGDisplayHideCursor(0)
        except Exception:
            return
        if err == 0:
            self._cursor_hidden = True

    def _show_cursor(self):
        if not self._cursor_hidden:
            return
        try:
            if self._q:
                self._q.CGDisplayShowCursor(0)
        except Exception:
            pass
        self._cursor_hidden = False

    def _cleanup(self):
        self._show_cursor()
        q, cf = self._q, self._cf
        tap, src, rl = self._tap, self._src, self._rl
        self._tap = self._src = self._rl = None
        if cf and src and rl:
            try:
                cf.CFRunLoopRemoveSource(rl, src, _cf_symbol(cf, "kCFRunLoopCommonModes"))
            except Exception:
                pass
        if q and tap:
            try:
                q.CGEventTapEnable(tap, False)
            except Exception:
                pass
        if cf and tap:
            try:
                cf.CFMachPortInvalidate(tap)
            except Exception:
                pass
        if cf:
            for obj in (src, tap):
                if obj:
                    try:
                        cf.CFRelease(obj)
                    except Exception:
                        pass
