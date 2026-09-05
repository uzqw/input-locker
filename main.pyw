import ctypes
import sys
import threading
import time
import atexit
import traceback
import json
import os
import datetime
import subprocess
from tkinter import messagebox, filedialog
import customtkinter as ctk
from locker_lifecycle import serialized

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_DARWIN = sys.platform == "darwin"
IS_WAYLAND = IS_LINUX and (
    os.environ.get("XDG_SESSION_TYPE") == "wayland"
    or bool(os.environ.get("WAYLAND_DISPLAY"))
)

if IS_WINDOWS:
    import ctypes.wintypes
    import winreg
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
ES_SYSTEM_REQUIRED = 0x00000001

VK_CAPITAL = 0x14
VK_BACK = 0x08
VK_RETURN = 0x0D
VK_SHIFT = 0x10
CURSOR_SHOWING = 0x00000001

LETTER_KEYS = set(range(0x41, 0x5B))
NUMBER_KEYS = set(range(0x30, 0x3A))

CAPS_LOCK_TRIGGER_COUNT = 3
CAPS_LOCK_TRIGGER_WINDOW = 2.0

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
ICON_FILE = os.path.join(RESOURCE_DIR, "icon.ico")
DEFAULT_PASSWORD = "123456"
# 与 gitea-commits 共享的计划文件默认位置（Windows 侧 Downloads，LLM 通过 API 写入）
if os.environ.get("USERPROFILE"):
    DEFAULT_PLAN_FILE = os.path.join(
        os.environ["USERPROFILE"], "Downloads", "input-locker-plan.json"
    )
elif IS_LINUX or IS_DARWIN:
    DEFAULT_PLAN_FILE = os.path.expanduser("~/Downloads/input-locker-plan.json")
else:
    DEFAULT_PLAN_FILE = ""


def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"password": DEFAULT_PASSWORD}


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except:
        pass


WEEKDAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _recurring_matches(when, now):
    days = set(when.get("days") or [])
    if not days:
        return False
    weekdays = {WEEKDAY_MAP[d] for d in days if d in WEEKDAY_MAP}
    if "weekdays" in days:
        weekdays |= {0, 1, 2, 3, 4}
    if "weekends" in days:
        weekdays |= {5, 6}
    if "daily" in days:
        weekdays = set(range(7))
    if now.weekday() not in weekdays:
        return False
    t = (when.get("time") or "")[:5]
    return bool(t) and t == now.strftime("%H:%M")


if IS_WINDOWS:
    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_int),
            ("scanCode", ctypes.c_int),
            ("flags", ctypes.c_int),
            ("time", ctypes.c_int),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_void_p))
        ]


    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", ctypes.c_int * 2),
            ("hwnd", ctypes.c_void_p),
            ("wHitTestCode", ctypes.c_int),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_void_p))
        ]


    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("hCursor", ctypes.wintypes.HANDLE),
            ("ptScreenPos", ctypes.wintypes.POINT),
        ]


    KBDHOOKPROC = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(KBDLLHOOKSTRUCT)
    )
    MOUSEHOOKPROC = ctypes.CFUNCTYPE(
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(MSLLHOOKSTRUCT)
    )


class InputLocker:
    def __init__(self):
        self._state_lock = threading.RLock()
        self.kb_hook_handle = None
        self.mouse_hook_handle = None
        self.lock_active = False
        self.unlock_password = load_config().get("password", DEFAULT_PASSWORD)

        self._kb_hook_proc = None
        self._mouse_hook_proc = None
        self._msg_thread = None
        self._lock_ready = threading.Event()
        self._lock_error = None

        self.caps_lock_press_times = []
        self.unlock_mode = False
        self._unlock_mode_changed = False

        self._original_screensaver_active = None
        self._original_screensaver_timeout = None
        self._cursor_hidden = False

    def set_password(self, new_password):
        self.unlock_password = new_password
        cfg = load_config()
        cfg["password"] = new_password
        save_config(cfg)

    def _get_screensaver_settings(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop")
            active, _ = winreg.QueryValueEx(key, "ScreenSaveActive")
            timeout, _ = winreg.QueryValueEx(key, "ScreenSaveTimeOut")
            winreg.CloseKey(key)
            return str(active), str(timeout)
        except:
            return "1", "600"

    def _set_screensaver_settings(self, active, timeout):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "ScreenSaveActive", 0, winreg.REG_SZ, str(active))
            winreg.SetValueEx(key, "ScreenSaveTimeOut", 0, winreg.REG_SZ, str(timeout))
            winreg.CloseKey(key)
        except:
            pass

    def _enable_screen_always_on(self):
        self._original_screensaver_active, self._original_screensaver_timeout = self._get_screensaver_settings()
        self._set_screensaver_settings("0", "0")
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)

    def _hide_cursor(self):
        if self._cursor_hidden:
            return

        cursor_info = CURSORINFO()
        cursor_info.cbSize = ctypes.sizeof(cursor_info)
        try:
            is_visible = bool(user32.GetCursorInfo(ctypes.byref(cursor_info))) and bool(
                cursor_info.flags & CURSOR_SHOWING
            )
        except Exception:
            is_visible = True

        if is_visible:
            user32.ShowCursor(False)
            self._cursor_hidden = True

    def _restore_cursor(self):
        if self._cursor_hidden:
            user32.ShowCursor(True)
            self._cursor_hidden = False

    def _restore_screen_settings(self):
        if self._original_screensaver_active is not None and self._original_screensaver_timeout is not None:
            self._set_screensaver_settings(self._original_screensaver_active, self._original_screensaver_timeout)
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)

    def _disable_usb_storage(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\USBSTOR", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "Start", 0, winreg.REG_DWORD, 4)
            winreg.CloseKey(key)
        except:
            pass

    def _enable_usb_storage(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\USBSTOR", 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "Start", 0, winreg.REG_DWORD, 3)
            winreg.CloseKey(key)
        except:
            pass

    def _kb_hook_callback(self, nCode, wParam, lParam):
        if nCode >= 0 and self.lock_active:
            try:
                vk_code = lParam.contents.vkCode

                if vk_code == VK_CAPITAL and (wParam == WM_KEYDOWN or wParam == WM_SYSKEYDOWN):
                    now = time.time()
                    self.caps_lock_press_times.append(now)
                    self.caps_lock_press_times = [
                        t for t in self.caps_lock_press_times
                        if now - t < CAPS_LOCK_TRIGGER_WINDOW
                    ]
                    if len(self.caps_lock_press_times) >= CAPS_LOCK_TRIGGER_COUNT:
                        self.caps_lock_press_times.clear()
                        self.unlock_mode = not self.unlock_mode
                        self._unlock_mode_changed = True
                    return 1

                if self.unlock_mode:
                    allowed = LETTER_KEYS | NUMBER_KEYS | {VK_BACK, VK_RETURN, VK_SHIFT}
                    if vk_code in allowed:
                        return user32.CallNextHookEx(
                            self.kb_hook_handle, nCode, wParam,
                            ctypes.cast(lParam, ctypes.POINTER(ctypes.c_void_p))
                        )
                    return 1

                return 1

            except:
                return 1

        return user32.CallNextHookEx(
            self.kb_hook_handle, nCode, wParam,
            ctypes.cast(lParam, ctypes.POINTER(ctypes.c_void_p))
        )

    def _mouse_hook_callback(self, nCode, wParam, lParam):
        if nCode >= 0 and self.lock_active:
            return 1

        return user32.CallNextHookEx(
            self.mouse_hook_handle, nCode, wParam,
            ctypes.cast(lParam, ctypes.POINTER(ctypes.c_void_p))
        )

    def _install_hooks(self):
        self._kb_hook_proc = KBDHOOKPROC(self._kb_hook_callback)
        self.kb_hook_handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_hook_proc, 0, 0)
        if not self.kb_hook_handle:
            raise ctypes.WinError(ctypes.get_last_error())

        self._mouse_hook_proc = MOUSEHOOKPROC(self._mouse_hook_callback)
        self.mouse_hook_handle = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_hook_proc, 0, 0)
        if not self.mouse_hook_handle:
            self.remove_keyboard_hook()
            raise ctypes.WinError(ctypes.get_last_error())

    def remove_keyboard_hook(self):
        if self.kb_hook_handle:
            user32.UnhookWindowsHookEx(self.kb_hook_handle)
            self.kb_hook_handle = None

    def _remove_mouse_hook(self):
        if self.mouse_hook_handle:
            user32.UnhookWindowsHookEx(self.mouse_hook_handle)
            self.mouse_hook_handle = None

    def _message_loop(self):
        msg = ctypes.wintypes.MSG()
        while self.lock_active:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0012:  # WM_QUIT
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)

    def _lock_worker(self):
        """在专用线程安装钩子并跑消息循环（低级钩子回调必须由安装线程的消息循环驱动）。
        start_lock 可从任意线程调用；钩子与消息循环始终在同一线程。"""
        try:
            self._install_hooks()
            self._lock_error = None
            self._lock_ready.set()
            self._message_loop()
        except Exception as e:
            self._lock_error = e
            self._lock_ready.set()
        finally:
            # 消息循环退出：卸钩子并恢复系统状态
            self.lock_active = False
            try:
                self.remove_keyboard_hook()
            except Exception:
                pass
            try:
                self._remove_mouse_hook()
            except Exception:
                pass
            try:
                self._enable_usb_storage()
            except Exception:
                pass
            try:
                self._restore_screen_settings()
            except Exception:
                pass
            try:
                self._restore_cursor()
            except Exception:
                pass

    @serialized
    def start_lock(self):
        if self.lock_active:
            return True
        if self._msg_thread and self._msg_thread.is_alive():
            return False
        try:
            self.lock_active = True
            self.unlock_mode = False
            self._unlock_mode_changed = False
            self.caps_lock_press_times.clear()

            self._lock_ready.clear()
            self._lock_error = None

            self._enable_screen_always_on()
            self._hide_cursor()
            self._disable_usb_storage()

            self._msg_thread = threading.Thread(target=self._lock_worker, daemon=True)
            self._msg_thread.start()
            # 等钩子装好或失败（最多 5 秒），确认锁定真正生效
            if not self._lock_ready.wait(timeout=5):
                raise TimeoutError("输入钩子启动超时")
            if self._lock_error:
                raise self._lock_error
            if not self.kb_hook_handle and not self.mouse_hook_handle:
                raise RuntimeError("hooks not installed")
            return True
        except Exception as e:
            self._lock_error = e
            # 标记退出，worker 线程的 finally 会自清理；这里不重复恢复系统状态
            self.lock_active = False
            self._lock_ready.set()
            return False

    @serialized
    def stop_lock(self):
        try:
            self.unlock_mode = False
            self._unlock_mode_changed = False
            if not self.lock_active and not (self._msg_thread and self._msg_thread.is_alive()):
                return True
            self.lock_active = False
            # 向消息循环线程投递 WM_QUIT，让它退出并自行清理
            if self._msg_thread and self._msg_thread.is_alive():
                user32.PostThreadMessageW(self._msg_thread.ident, 0x0012, 0, 0)
                self._msg_thread.join(timeout=3)
                if self._msg_thread.is_alive():
                    self._lock_error = TimeoutError("输入钩子线程尚未退出")
                    return False
            # worker 的 finally 已卸钩子并恢复系统；这里兜底再恢复一次（幂等）
            self._enable_usb_storage()
            self._restore_screen_settings()
            self._restore_cursor()
            return True
        except:
            return False

    def cancel_unlock_mode(self):
        self.unlock_mode = False
        self._unlock_mode_changed = True

    @serialized
    def emergency_restore(self):
        self.lock_active = False
        self.unlock_mode = False
        try:
            self.remove_keyboard_hook()
        except:
            pass
        try:
            self._remove_mouse_hook()
        except:
            pass
        try:
            self._enable_usb_storage()
        except:
            pass
        try:
            self._restore_screen_settings()
        except:
            pass
        try:
            self._restore_cursor()
        except:
            pass


class ScheduleWatcher:
    """读取计划JSON + 命令文件：命中锁定/解锁条件则自动执行。
    计划任务结构：{mode, when(锁定条件), unlock(解锁条件), confirm}。
    命令文件：{"cmd": "lock"|"unlock", "id": "唯一命令ID"}；ack 回传 id 与实际结果。
    旧命令可不带 id。先认领命令再执行，避免删除执行期间新到的命令。
    """
    def __init__(self, locker, file_var, status_cb, command_file, ack_file):
        self.locker = locker
        self.file_var = file_var
        self.status_cb = status_cb
        self.command_file = command_file
        self.ack_file = ack_file
        self._fired = {}  # (action, idx, date) -> date，防同一天重复触发
        self._stop = False

    def _plan_path(self):
        path = load_config().get("schedule_file") or self.file_var
        return path if path else ""

    def _load_tasks(self):
        path = self._plan_path()
        if not path or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                tasks = json.load(f)
            if isinstance(tasks, dict):
                tasks = [tasks]
            return tasks if isinstance(tasks, list) else []
        except Exception:
            return []

    def _match(self, when, mode, now):
        """when 条件是否命中。"""
        if not when:
            return False
        if mode == "once":
            try:
                at = datetime.datetime.fromisoformat(when.get("at"))
                # at 带时区时用 aware 的本地 now 比较，否则 naive 对 naive（否则 TypeError 被 except 吞掉，once 永不触发）
                return at <= (now.astimezone() if at.tzinfo else now)
            except Exception:
                return False
        return _recurring_matches(when, now)

    def _do_lock(self, task):
        ok = self.locker.start_lock()
        confirm = task.get("confirm", "")
        msg = ("计划触发自动锁定" if ok else "自动锁定失败") + (f"：{confirm}" if confirm else "")
        try:
            self.status_cb(msg, ok)
        except Exception:
            traceback.print_exc()  # UI 更新失败不能掩盖实际锁定结果。
        return ok

    def _do_unlock(self, task):
        ok = self.locker.stop_lock()
        confirm = task.get("confirm", "")
        msg = ("计划触发自动解锁" if ok else "自动解锁失败") + (f"：{confirm}" if confirm else "")
        try:
            self.status_cb(msg, ok)
        except Exception:
            traceback.print_exc()
        return ok

    def _check_plan(self, now):
        for i, t in enumerate(self._load_tasks()):
            mode = t.get("mode")
            when = t.get("when") or {}
            unlock = t.get("unlock") or {}
            today = now.strftime("%Y-%m-%d")
            # once 任务用 at 时间做防重 key（只触发一次），recurring 用日期（每天一次）
            lock_key = ("lock", i, when.get("at") if mode == "once" else today)
            unlock_key = ("unlock", i, unlock.get("at") if mode == "once" else today)
            lock_due = self._match(when, mode, now)
            unlock_due = self._match(unlock, mode, now)
            if mode == "once" and unlock_due:
                # 解锁时刻已过：过期 once 重启不再上锁，也不必先清空计划。
                if self.locker.lock_active and self._fired.get(unlock_key) != unlock_key[2]:
                    self._fired[unlock_key] = unlock_key[2]
                    self._do_unlock(t)
                return
            if lock_due:
                if not self.locker.lock_active and self._fired.get(lock_key) != lock_key[2]:
                    self._fired[lock_key] = lock_key[2]
                    self._do_lock(t)
                    return
            if unlock_due:
                if self.locker.lock_active and self._fired.get(unlock_key) != unlock_key[2]:
                    self._fired[unlock_key] = unlock_key[2]
                    self._do_unlock(t)
                    return

    def _check_command(self):
        plan = self._plan_path()
        d = os.path.dirname(plan) if plan else ""
        command_file = os.path.join(d, "input-locker-command.json") if d else self.command_file
        ack_file = os.path.join(d, "input-locker-ack.json") if d else self.ack_file
        if not command_file:
            return
        processing = command_file + ".processing"
        try:
            os.replace(command_file, processing)
        except FileNotFoundError:
            return
        except OSError:
            traceback.print_exc()
            return
        cmd = {}
        action = None
        result = "error"
        error = ""
        try:
            with open(processing, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            if not isinstance(cmd, dict):
                raise ValueError("命令必须是 JSON 对象")
            action = cmd.get("cmd")
            if action == "lock":
                ok = self._do_lock(cmd)
            elif action == "unlock":
                ok = self._do_unlock(cmd)
            else:
                raise ValueError("bad cmd: " + str(action))
            if ok:
                result = "ok"
            else:
                cause = getattr(self.locker, "_error", None) or getattr(self.locker, "_lock_error", None)
                error = str(cause or (str(action) + " failed"))
        except Exception as e:
            error = str(e)
        ack = {"cmd": action, "result": result,
               "ts": datetime.datetime.now().isoformat()}
        if isinstance(cmd, dict) and "id" in cmd:
            ack["id"] = cmd["id"]
        if error:
            ack["error"] = error
        try:
            if ack_file:
                with open(ack_file + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(ack, f, ensure_ascii=False, indent=2)
                os.replace(ack_file + ".tmp", ack_file)
            os.remove(processing)
        except OSError:
            traceback.print_exc()

    def run(self):
        while not self._stop:
            try:
                self._check_command()
                self._check_plan(datetime.datetime.now())
            except Exception:
                pass
            time.sleep(1)

    def stop(self):
        self._stop = True


class LockApp:
    BG = "#f2f2f7"
    CARD_BG = "#ffffff"
    ACCENT = "#007aff"
    GREEN = "#34c759"
    RED = "#ff3b30"
    TEXT = "#1c1c1e"
    SUBTEXT = "#8e8e93"
    BORDER = "#d1d1d6"

    @staticmethod
    def _set_window_icon(window):
        try:
            window.iconbitmap(ICON_FILE)
        except Exception:
            pass

    def __init__(self, locker, wayland_fallback=False):
        self.locker = locker
        self._last_unlock_mode = False
        self.wayland_fallback = wayland_fallback

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("dark-blue")

        self.root = ctk.CTk()
        self.root.title("Input Locker")
        self._set_window_icon(self.root)
        self.root.geometry("400x680")
        self.root.resizable(False, False)
        self.root.configure(fg_color=self.BG)

        self._build_ui()
        if IS_LINUX:
            self.root.after(0, self.root.iconify)  # 后台服务，启动不抢焦点。

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Alt-F4>", lambda e: "break")

        if hasattr(self.locker, "set_ui_callbacks"):
            self.locker.set_ui_callbacks(self._linux_password_cb, self._linux_submit_cb)

        self._poll_state()

    def _linux_password_cb(self, pwd):
        self.root.after(0, lambda: self._set_password_display(pwd))

    def _set_password_display(self, pwd):
        self.password_entry.delete(0, "end")
        self.password_entry.insert(0, pwd)

    def _linux_submit_cb(self, pwd):
        self.root.after(0, lambda: self.unlock(pwd))

    def _build_schedule_ui(self, container):
        schedule_cfg = load_config().get("schedule_file") or DEFAULT_PLAN_FILE or ""
        self.schedule_frame = ctk.CTkFrame(container, fg_color=self.CARD_BG, corner_radius=12)
        self.schedule_frame.pack(fill="x", pady=(8, 0))

        ctk.CTkLabel(
            self.schedule_frame, text="计划文件（LLM 通过接口写入，自动生效）",
            font=ctk.CTkFont(size=12), text_color=self.SUBTEXT, anchor="w"
        ).pack(fill="x", padx=16, pady=(12, 4))

        self.schedule_path = ctk.CTkLabel(
            self.schedule_frame, text="未选择", wraplength=250,
            font=ctk.CTkFont(size=11), text_color=self.SUBTEXT, anchor="w"
        )
        self.schedule_path.pack(fill="x", padx=16)

        self.schedule_file_var = schedule_cfg
        if schedule_cfg:
            self.schedule_path.configure(text=schedule_cfg)

        ctk.CTkButton(
            self.schedule_frame, text="选择计划文件", command=self._choose_schedule_file,
            width=140, height=30, corner_radius=15,
            fg_color=self.BG, hover_color="#e5e5ea",
            text_color=self.SUBTEXT, border_width=1, border_color=self.BORDER,
            font=ctk.CTkFont(size=12)
        ).pack(pady=(8, 12), padx=16)

        # 命令/ack 文件与计划文件同目录，供 gitea-commits 接口下发 lock/unlock 命令
        plan_dir = os.path.dirname(schedule_cfg) if schedule_cfg else ""
        command_file = os.path.join(plan_dir, "input-locker-command.json") if plan_dir else ""
        ack_file = os.path.join(plan_dir, "input-locker-ack.json") if plan_dir else ""

        self.watcher = ScheduleWatcher(
            self.locker, self.schedule_file_var, self._schedule_status,
            command_file, ack_file
        )
        self._watch_thread = threading.Thread(target=self.watcher.run, daemon=True)
        self._watch_thread.start()

    def _schedule_status(self, msg, ok):
        self.root.after(0, lambda: self._apply_lock_state(msg, ok))

    def _apply_lock_state(self, msg, ok):
        """计划/命令触发锁定或解锁后，同步 UI 状态。"""
        self.msg_label.configure(text=msg, text_color=self.GREEN if ok else self.RED)
        locked = self.locker.lock_active
        if locked:
            self.status_label.configure(text="已锁定", text_color=self.RED)
            self.lock_button.configure(state="disabled", fg_color="#c7c7cc")
            self.change_pw_button.configure(state="disabled")
        else:
            self.status_label.configure(text="未锁定", text_color=self.GREEN)
            self.lock_button.configure(state="normal", fg_color=self.ACCENT)
            self.change_pw_button.configure(state="normal")
            self.unlock_frame.pack_forget()
            self._last_unlock_mode = False
            self.root.attributes('-topmost', False)

    def _choose_schedule_file(self):
        initial = os.path.dirname(self.schedule_file_var) if self.schedule_file_var else \
            (os.path.expanduser("~/Downloads") if (IS_LINUX or IS_DARWIN)
             else os.path.join(os.environ.get("USERPROFILE", ""), "Downloads"))
        path = filedialog.askopenfilename(
            title="选择计划文件",
            initialdir=initial if os.path.isdir(initial) else None,
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
        )
        if not path:
            return
        self.schedule_file_var = path
        self.schedule_path.configure(text=path)
        cfg = load_config()
        cfg["schedule_file"] = path
        save_config(cfg)
        self.msg_label.configure(text="已加载计划文件", text_color=self.GREEN)

    def _build_ui(self):
        container = ctk.CTkFrame(self.root, fg_color=self.BG)
        container.pack(fill="both", expand=True, padx=24, pady=24)

        if getattr(self, "wayland_fallback", False):
            fallback_card = ctk.CTkFrame(container, fg_color=self.RED, corner_radius=12)
            fallback_card.pack(fill="x", pady=(0, 12))
            ctk.CTkLabel(
                fallback_card,
                text=("⚠ 无输入设备权限，已回退系统锁屏（系统密码解锁）\n"
                      "运行 sudo usermod -aG input $USER 后重新登录，\n"
                      "即可使用 3x CapsLock + 自定义密码 解锁"),
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color="#ffffff", justify="left", wraplength=320,
            ).pack(fill="x", padx=14, pady=10)

        icon_label = ctk.CTkLabel(
            container, text="🔒", font=ctk.CTkFont(size=48),
            text_color=self.TEXT
        )
        icon_label.pack(pady=(16, 4))

        ctk.CTkLabel(
            container, text="Input Locker",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            text_color=self.TEXT
        ).pack(pady=(0, 4))

        self.status_label = ctk.CTkLabel(
            container, text="未锁定",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self.GREEN
        )
        self.status_label.pack(pady=(0, 20))

        self.lock_button = ctk.CTkButton(
            container, text="锁定系统", command=self.lock,
            width=260, height=48, corner_radius=24,
            fg_color=self.ACCENT, hover_color="#0070e0",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold")
        )
        self.lock_button.pack(pady=(0, 12))

        self.msg_label = ctk.CTkLabel(
            container, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.GREEN, wraplength=340
        )
        self.msg_label.pack(pady=(0, 12))

        self.unlock_frame = ctk.CTkFrame(container, fg_color="transparent")

        ctk.CTkLabel(
            self.unlock_frame, text="输入密码解锁",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self.SUBTEXT
        ).pack(pady=(0, 10))

        self.password_entry = ctk.CTkEntry(
            self.unlock_frame, show="•", width=260, height=44,
            corner_radius=12, placeholder_text="密码",
            fg_color=self.CARD_BG, border_color=self.BORDER,
            text_color=self.TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=15)
        )
        self.password_entry.pack(pady=(0, 12))

        self.unlock_button = ctk.CTkButton(
            self.unlock_frame, text="解锁", command=self.unlock,
            width=260, height=44, corner_radius=22,
            fg_color=self.GREEN, hover_color="#28b84c",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        )
        self.unlock_button.pack(pady=(0, 4))

        self.password_entry.bind("<Return>", lambda e: self.unlock())

        self.change_pw_button = ctk.CTkButton(
            container, text="修改密码", command=self._open_change_password,
            width=120, height=34, corner_radius=17,
            fg_color=self.CARD_BG, hover_color="#e5e5ea",
            text_color=self.SUBTEXT, border_width=1, border_color=self.BORDER,
            font=ctk.CTkFont(family="Segoe UI", size=12)
        )
        self.change_pw_button.pack(pady=(12, 4))

        hint_card = ctk.CTkFrame(container, fg_color=self.CARD_BG, corner_radius=12)
        hint_card.pack(fill="x", pady=(0, 4))

        is_first_run = not os.path.exists(CONFIG_FILE)

        if IS_LINUX and IS_WAYLAND:
            if getattr(self, "wayland_fallback", False):
                hints = [
                    "锁定后: 触发系统锁屏（系统密码解锁），屏幕常亮，USB存储禁用",
                    "解锁: 在系统锁屏界面输入系统密码",
                ]
            else:
                hints = [
                    "锁定后: 键盘/鼠标禁用（evdev 内核级抓取），屏幕常亮，USB存储禁用",
                    "解锁: 连按3次 CapsLock → 输入密码 → Enter",
                    "解锁模式下密码错误自动关闭",
                ]
        else:
            hints = [
                "锁定后: 键盘/鼠标禁用，USB存储禁用，屏幕常亮",
                "解锁: 连按3次 CapsLock → 输入密码 → Enter",
                "解锁模式下密码错误自动关闭",
            ]
        if is_first_run:
            hints.append(f"初始密码: {self.locker.unlock_password}，请及时修改")

        for h in hints:
            ctk.CTkLabel(
                hint_card, text=h,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                text_color=self.SUBTEXT, anchor="w"
            ).pack(fill="x", padx=14, pady=2)

        ctk.CTkLabel(hint_card, text="").pack(pady=2)

        self._build_schedule_ui(container)

    def _open_change_password(self):
        if self.locker.lock_active:
            return

        win = ctk.CTkToplevel(self.root)
        win.title("修改密码")
        self._set_window_icon(win)
        win.after(350, lambda: self._set_window_icon(win))
        win.geometry("360x420")
        win.resizable(False, False)
        win.configure(fg_color=self.BG)
        win.grab_set()

        ctk.CTkLabel(
            win, text="修改密码",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=self.TEXT
        ).pack(pady=(24, 20))

        form = ctk.CTkFrame(win, fg_color="transparent")
        form.pack(padx=32, fill="x")

        ctk.CTkLabel(
            form, text="当前密码",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.SUBTEXT, anchor="w"
        ).pack(fill="x", pady=(0, 4))
        old_entry = ctk.CTkEntry(
            form, show="•", height=40, corner_radius=10,
            fg_color=self.CARD_BG, border_color=self.BORDER,
            text_color=self.TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=14)
        )
        old_entry.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            form, text="新密码",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.SUBTEXT, anchor="w"
        ).pack(fill="x", pady=(0, 4))
        new_entry = ctk.CTkEntry(
            form, show="•", height=40, corner_radius=10,
            fg_color=self.CARD_BG, border_color=self.BORDER,
            text_color=self.TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=14)
        )
        new_entry.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            form, text="确认新密码",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.SUBTEXT, anchor="w"
        ).pack(fill="x", pady=(0, 4))
        confirm_entry = ctk.CTkEntry(
            form, show="•", height=40, corner_radius=10,
            fg_color=self.CARD_BG, border_color=self.BORDER,
            text_color=self.TEXT,
            font=ctk.CTkFont(family="Segoe UI", size=14)
        )
        confirm_entry.pack(fill="x", pady=(0, 16))

        msg_label = ctk.CTkLabel(
            win, text="",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self.RED
        )
        msg_label.pack()

        def do_change():
            old = old_entry.get()
            new = new_entry.get()
            confirm = confirm_entry.get()

            if old != self.locker.unlock_password:
                msg_label.configure(text="当前密码错误", text_color=self.RED)
                return

            if not new:
                msg_label.configure(text="新密码不能为空", text_color=self.RED)
                return

            if new != confirm:
                msg_label.configure(text="两次输入的新密码不一致", text_color=self.RED)
                return

            self.locker.set_password(new)
            messagebox.showinfo("成功", "密码已修改!", parent=win)
            win.destroy()

        ctk.CTkButton(
            win, text="确认修改", command=do_change,
            width=200, height=42, corner_radius=21,
            fg_color=self.ACCENT, hover_color="#0070e0",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
        ).pack(pady=8)

    def _poll_state(self):
        try:
            if self.locker._unlock_mode_changed:
                self.locker._unlock_mode_changed = False
                new_mode = self.locker.unlock_mode
                if new_mode != self._last_unlock_mode:
                    self._last_unlock_mode = new_mode
                    self._update_unlock_ui(new_mode)
            # 锁定意外失效（如 X 连接断开）时同步 UI
            if not self.locker.lock_active and str(self.lock_button.cget("state")) == "disabled":
                self._apply_lock_state("已解锁", True)
        except Exception:
            pass
        self.root.after(100, self._poll_state)

    def _update_unlock_ui(self, unlock_mode):
        if unlock_mode:
            self.root.after(50, self._restore_and_focus)
            self.unlock_frame.pack(after=self.msg_label, pady=8)
            self.password_entry.delete(0, 'end')
            self.msg_label.configure(text="解锁模式已开启 — 请输入密码", text_color=self.GREEN)
        else:
            self.unlock_frame.pack_forget()
            if self.locker.lock_active:
                self.msg_label.configure(text="已锁定 — 连按3次 CapsLock 解锁", text_color=self.GREEN)

    def _restore_and_focus(self):
        self.root.attributes('-topmost', True)
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        # mutter 下 Tk 的 deiconify/lift 不可靠（窗口仍 HIDDEN 或被全屏窗口盖住），
        # 用 xdotool 强制激活置顶；无 xdotool 时静默回退。
        try:
            subprocess.run(
                ["xdotool", "search", "--name", "Input Locker", "windowactivate"],
                timeout=2, capture_output=True,
            )
        except Exception:
            pass
        try:
            self.password_entry._entry.focus_set()
        except Exception:
            self.password_entry.focus_set()

    def lock(self):
        success = self.locker.start_lock()
        if success:
            self.status_label.configure(text="已锁定", text_color=self.RED)
            self.lock_button.configure(state="disabled", fg_color="#c7c7cc")
            self.change_pw_button.configure(state="disabled")
            if IS_WAYLAND and getattr(self, "wayland_fallback", False):
                self.msg_label.configure(
                    text="已锁定 — 系统锁屏已启动，用系统密码解锁",
                    text_color=self.GREEN
                )
            else:
                msg = "已锁定 — 连按3次 CapsLock 解锁"
                if IS_LINUX and getattr(self.locker, "usb_storage_skipped", False):
                    msg += "（USB 禁用需 root，已跳过）"
                self.msg_label.configure(text=msg, text_color=self.GREEN)
            self.root.after(200, lambda: self.root.iconify())
        else:
            self.msg_label.configure(text="锁定失败!", text_color=self.RED)

    def unlock(self, password=None):
        if password is None:
            password = self.password_entry.get()
        if password == self.locker.unlock_password:
            if self.locker.stop_lock():
                self.status_label.configure(text="未锁定", text_color=self.GREEN)
                self.lock_button.configure(state="normal", fg_color=self.ACCENT)
                self.change_pw_button.configure(state="normal")
                self.root.attributes('-topmost', False)
                self.root.attributes('-fullscreen', False)
                self.unlock_frame.pack_forget()
                self._last_unlock_mode = False
                self.msg_label.configure(text="已成功解锁", text_color=self.GREEN)
            else:
                self.msg_label.configure(text="解锁失败，请重试", text_color=self.RED)
        else:
            self.locker.cancel_unlock_mode()
            self.unlock_frame.pack_forget()
            self._last_unlock_mode = False
            self.password_entry.delete(0, 'end')
            self.root.attributes('-fullscreen', False)
            self.msg_label.configure(text="密码错误 — 连按3次 CapsLock 重新解锁", text_color=self.RED)

    def on_close(self):
        # Wayland 下系统锁屏才是真正的锁，关闭本程序不会解锁系统，允许关闭
        if self.locker.lock_active and not IS_WAYLAND:
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def _make_linux_locker():
    """Linux 后端工厂：Wayland 下优先 EvdevLocker（自定义 3x CapsLock+密码 解锁），
    失败时回退 WaylandLocker（系统锁屏）。探测只查权限，不真正锁屏。"""
    if not IS_WAYLAND:
        from linux_locker import LinuxInputLocker
        return LinuxInputLocker(), False
    from linux_locker import EvdevLocker, WaylandLocker, evdev_available
    if evdev_available():
        return EvdevLocker(), False
    return WaylandLocker(), True


def main():
    wayland_fallback = False
    if IS_WINDOWS:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            executable = sys.executable
            if executable.endswith("python.exe"):
                executable = executable[:-10] + "pythonw.exe"
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", executable, " ".join(sys.argv), None, 0
            )
            if result <= 32:
                messagebox.showerror("错误", "需要管理员权限!\n\n请右键选择'以管理员身份运行'")
            sys.exit(0)
        locker = InputLocker()
    elif IS_LINUX:
        locker, wayland_fallback = _make_linux_locker()
    elif IS_DARWIN:
        from macos_locker import MacInputLocker
        locker = MacInputLocker()
    else:
        msg = "不支持的系统: %s" % sys.platform
        try:
            messagebox.showerror("错误", msg)
        except Exception:
            sys.stderr.write(msg + "\n")
        sys.exit(1)

    atexit.register(locker.emergency_restore)
    app = LockApp(locker, wayland_fallback)
    try:
        app.run()
    except Exception:
        locker.emergency_restore()
        traceback.print_exc()


if __name__ == "__main__":
    main()
