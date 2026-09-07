"""Safe regression checks: fake input devices/inhibitors, no GUI or real lock.
Run: .venv/bin/python -m unittest -v test_locker
"""
import ctypes
import datetime
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import linux_locker as backend
import macos_locker

loader = importlib.machinery.SourceFileLoader('locker_app', str(Path(__file__).with_name('main.pyw')))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)  # Import only; main() and Tk windows are never started.


class FakeDevice:
    def __init__(self):
        self.closed = False
    def close(self):
        self.closed = True


class LockerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.plan = Path(self.directory.name) / 'input-locker-plan.json'
        self.command = self.plan.with_name('input-locker-command.json')
        self.ack = self.plan.with_name('input-locker-ack.json')
        for module, name in ((backend, '_load_config'), (app, 'load_config')):
            context = patch.object(module, name, return_value={})
            context.start()
            self.addCleanup(context.stop)
        self.inhibitors = []
        def inhibit():
            proc = Mock()
            self.inhibitors.append(proc)
            return proc
        for context in (patch.object(backend, '_start_inhibit', inhibit),
                        patch.object(backend, '_usb_storage')):
            context.start()
            self.addCleanup(context.stop)
        self.device = FakeDevice()
        self.locker = backend.EvdevLocker()
        self.locker._open_devices = Mock(return_value={'keyboard': self.device})
        self.locker._handle_events = lambda: time.sleep(0.005)
        self.locker._scan_new = lambda: None
        self.addCleanup(self.locker.stop_lock)
        self.watcher = app.ScheduleWatcher(self.locker, str(self.plan),
                                          lambda *args: None, str(self.command), str(self.ack))

    def send(self, cmd, ident='test-id'):
        self.command.write_text(json.dumps({'cmd': cmd, 'id': ident}))
        self.watcher._check_command()
        return json.loads(self.ack.read_text())

    def test_due_plan_then_lock_command_stays_locked_until_unlock(self):
        now = datetime.datetime.fromisoformat('2026-09-05T10:06:15+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:11:03+08:00'))]))
        self.watcher._check_plan(now)
        first_thread = self.locker._thread
        self.assertEqual(self.send('lock')['result'], 'ok')
        self.assertTrue(self.locker.lock_active)
        self.assertFalse(self.device.closed)
        self.assertIs(self.locker._thread, first_thread)
        self.locker._open_devices.assert_called_once()
        self.assertEqual(len(self.inhibitors), 1)
        self.inhibitors[0].terminate.assert_not_called()
        self.watcher._check_plan(now + datetime.timedelta(minutes=4))
        self.assertTrue(self.locker.lock_active)
        self.watcher._check_plan(now + datetime.timedelta(minutes=5))
        self.assertFalse(self.locker.lock_active)
        self.assertTrue(self.device.closed)
        self.inhibitors[0].terminate.assert_called_once()
        self.inhibitors[0].wait.assert_called_once()

    def test_expired_once_does_not_lock_on_restart(self):
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:11:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertFalse(self.locker.lock_active)
        self.locker._open_devices.assert_not_called()
        self.assertEqual(self.inhibitors, [])

    def test_later_once_still_runs_after_earlier_once_unlocks(self):
        now = datetime.datetime.fromisoformat('2026-09-05T10:05:00+08:00')
        self.plan.write_text(json.dumps([
            dict(mode='once', when=dict(at='2026-09-05T10:05:00+08:00'),
                 unlock=dict(at='2026-09-05T10:15:00+08:00')),
            dict(mode='once', when=dict(at='2026-09-05T11:05:00+08:00'),
                 unlock=dict(at='2026-09-05T11:20:00+08:00'))]))
        # 第一条锁定
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        # 第一条解锁
        self.watcher._check_plan(now + datetime.timedelta(minutes=10))
        self.assertFalse(self.locker.lock_active)
        # 第二条锁定（bug 下被第一条过期 once 的 return squash，永不触发）
        self.watcher._check_plan(now + datetime.timedelta(hours=1))
        self.assertTrue(self.locker.lock_active)
        # 第二条解锁
        self.watcher._check_plan(now + datetime.timedelta(hours=1, minutes=15))
        self.assertFalse(self.locker.lock_active)

    def test_expired_plan_before_new_plan_does_not_block(self):
        """Regression: expired plan before new plan must not block the new one.
        (old bug: iterating to an expired once plan returned early, so the
        new plan behind it never fired)"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([
            dict(mode='once', when=dict(at='2026-09-05T09:00:00+08:00'),
                 unlock=dict(at='2026-09-05T09:10:00+08:00')),  # expired, first
            dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                 unlock=dict(at='2026-09-05T10:16:03+08:00')),  # new plan
        ]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        self.locker._open_devices.assert_called_once()

    def test_expired_plan_does_not_unlock_active_lock(self):
        """Regression: after locking, an expired plan (unlock in the past)
        must not unlock the active lock. (old bug: expired unlock_due fired
        _do_unlock, so the lock was released right after being taken)"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        # new plan triggers the lock first
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        # plan file gets overwritten: expired plan inserted before current one
        self.plan.write_text(json.dumps([
            dict(mode='once', when=dict(at='2026-09-05T09:00:00+08:00'),
                 unlock=dict(at='2026-09-05T09:10:00+08:00')),  # expired
            dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                 unlock=dict(at='2026-09-05T10:16:03+08:00')),  # current
        ]))
        self.watcher._check_plan(now + datetime.timedelta(seconds=1))
        self.assertTrue(self.locker.lock_active)  # expired plan must not unlock
        # due time: must unlock
        self.watcher._check_plan(now + datetime.timedelta(minutes=5))
        self.assertFalse(self.locker.lock_active)

    def test_mixed_plan_chaos_keeps_invariant(self):
        """Chaos: random mix of expired/future/current plans, invariant check:
        once locked, no expired plan may unlock; the current plan must unlock
        at its due time."""
        import random
        rng = random.Random(42)
        base = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        for trial in range(20):
            tasks = []
            for _ in range(rng.randint(0, 4)):
                past = base - datetime.timedelta(minutes=rng.randint(30, 300))
                tasks.append(dict(mode='once',
                    when=dict(at=(past).isoformat()),
                    unlock=dict(at=(past + datetime.timedelta(minutes=10)).isoformat())))
            tasks.append(dict(mode='once',
                when=dict(at='2026-09-05T10:06:03+08:00'),
                unlock=dict(at='2026-09-05T10:16:03+08:00')))
            rng.shuffle(tasks)
            self.plan.write_text(json.dumps(tasks))
            # lock time: must lock
            self.watcher._check_plan(base)
            self.assertTrue(self.locker.lock_active,
                            'trial %d: new plan blocked by expired plans' % trial)
            # 1s after lock: expired plans must not unlock
            self.watcher._check_plan(base + datetime.timedelta(seconds=1))
            self.assertTrue(self.locker.lock_active,
                            'trial %d: expired plan unlocked active lock' % trial)
            # unlock time: must unlock
            self.watcher._check_plan(base + datetime.timedelta(minutes=5))
            self.assertFalse(self.locker.lock_active,
                             'trial %d: current plan did not unlock' % trial)
            # reset for next trial
            self.locker.stop_lock()
            self.watcher._fired.clear()

    def test_concurrent_lock_calls_start_one_worker(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.locker.start_lock(), range(16)))
        self.assertTrue(all(results))
        self.locker._open_devices.assert_called_once()
        self.assertEqual(len(self.inhibitors), 1)
        self.assertFalse(self.device.closed)

    def test_start_waits_for_concurrent_stop_to_finish(self):
        entered, release = threading.Event(), threading.Event()
        self.locker._handle_events = lambda: (entered.set(), release.wait(2))
        self.assertTrue(self.locker.start_lock())
        self.assertTrue(entered.wait(1))
        def stop():
            with self.locker._state_lock:
                stopping.set()
                return self.locker.stop_lock()
        stopping = threading.Event()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                stopped = pool.submit(stop)
                self.assertTrue(stopping.wait(1))
                restarted = pool.submit(self.locker.start_lock)
                release.set()
                self.assertTrue(stopped.result(timeout=3))
                self.assertTrue(restarted.result(timeout=3))
        finally:
            release.set()
        self.assertEqual(self.locker._open_devices.call_count, 2)

    def test_failed_lock_and_unlock_acknowledge_actual_result_and_id(self):
        self.locker._open_devices.side_effect = PermissionError('device busy')
        result = self.send('lock', 'failed-lock')
        self.assertEqual(result['result'], 'error')
        self.assertEqual(result['id'], 'failed-lock')
        self.assertIn('device busy', result['error'])
        self.assertFalse(self.locker.lock_active)
        self.inhibitors[0].terminate.assert_called_once()
        with patch.object(self.locker, 'stop_lock', return_value=False):
            self.assertEqual(self.send('unlock')['result'], 'error')
        self.assertEqual(self.send('bad')['result'], 'error')

    def test_new_command_during_execution_is_not_deleted(self):
        self.watcher.status_cb = lambda *args: self.command.write_text(
            json.dumps({'cmd': 'unlock', 'id': 'next'}))
        result = self.send('lock', 'first')
        self.assertEqual(result['id'], 'first')
        self.assertEqual(json.loads(self.command.read_text())['id'], 'next')
        self.watcher.status_cb = lambda *args: None
        self.watcher._check_command()
        self.assertEqual(json.loads(self.ack.read_text())['id'], 'next')
        self.assertFalse(self.locker.lock_active)

    def test_worker_failure_releases_devices_and_inhibitor(self):
        entered, release = threading.Event(), threading.Event()
        def crash():
            entered.set()
            release.wait(2)
            raise RuntimeError('worker failed')
        self.locker._handle_events = crash
        try:
            self.assertTrue(self.locker.start_lock())
            self.assertTrue(entered.wait(1))
        finally:
            release.set()
        self.locker._thread.join(timeout=2)
        self.assertFalse(self.locker.lock_active)
        self.assertTrue(self.device.closed)
        self.inhibitors[0].terminate.assert_called_once()

    def test_partial_grab_failure_closes_previously_opened_devices(self):
        first, second = Mock(), Mock()
        second.grab.side_effect = OSError('busy')
        with patch.object(backend.evdev, 'list_devices', return_value=['a', 'b']), \
             patch.object(backend.evdev, 'InputDevice', side_effect=[first, second]), \
             patch.object(backend, '_device_kind', return_value='keyboard'):
            with self.assertRaises(PermissionError):
                backend.EvdevLocker._open_devices(self.locker)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_timeout_is_failure_and_live_old_worker_blocks_restart(self):
        # No sleeps/real thread: model a worker stuck during startup.
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(backend.threading, 'Thread', return_value=worker), \
             patch.object(self.locker._ready, 'wait', return_value=False):
            self.assertFalse(self.locker.start_lock())
            self.assertFalse(self.locker.start_lock())
        self.assertEqual(len(self.inhibitors), 1)
        worker.start.assert_called_once()
        worker.is_alive.return_value = False

    def test_wayland_command_errors_and_stale_poll(self):
        locker = backend.WaylandLocker()
        with patch.object(backend.subprocess, 'run', side_effect=OSError('loginctl failed')):
            self.assertFalse(locker.start_lock())
            self.assertFalse(locker.lock_active)
            self.assertTrue(locker._get_active())  # 查询失败不可被当成已解锁。
        self.inhibitors[0].terminate.assert_called_once()
        locker.lock_active = True
        locker._poll_thread = object()  # 模拟另一个新锁的轮询线程。
        with patch.object(locker, '_get_active') as active:
            locker._poll()
            active.assert_not_called()
        self.assertTrue(locker.lock_active)

    def test_startup_probe_does_not_lock(self):
        with patch.object(backend, 'evdev', None):
            self.assertFalse(backend.evdev_available())
        with patch.object(backend.evdev, 'list_devices', return_value=['/dev/input/event0']), \
             patch.object(backend.os, 'access', return_value=True):
            self.assertTrue(backend.evdev_available())
        self.locker._open_devices.assert_not_called()
        self.assertEqual(self.inhibitors, [])

    def test_other_backends_already_locked_are_noops(self):
        for cls in (backend.LinuxInputLocker, backend.WaylandLocker,
                    app.InputLocker, macos_locker.MacInputLocker):
            with self.subTest(backend=cls.__name__):
                locker = cls()
                locker.lock_active = True
                with patch.object(backend, '_start_inhibit') as inhibit, \
                     patch.object(macos_locker, '_is_trusted') as trusted, \
                     patch.object(macos_locker, '_start_caffeinate') as caff:
                    self.assertTrue(locker.start_lock())
                    inhibit.assert_not_called()
                    trusted.assert_not_called()
                    caff.assert_not_called()


class MacLockerTests(unittest.TestCase):
    def setUp(self):
        self.locker = macos_locker.MacInputLocker()
        self.addCleanup(self.locker.stop_lock)
        self.inhibitors = []
        def caffeinate():
            proc = Mock()
            self.inhibitors.append(proc)
            return proc
        for context in (
            patch.object(macos_locker, '_is_trusted', return_value=True),
            patch.object(macos_locker, '_start_caffeinate', caffeinate),
            patch.object(macos_locker, '_stop_caffeinate'),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.locker._install_tap = lambda: None
        self.locker._hide_cursor = lambda: setattr(self.locker, '_cursor_hidden', True)
        self.locker._run_loop = lambda: self._spin()
        orig_cleanup = self.locker._cleanup
        def cleanup():
            self.locker._show_cursor()
            orig_cleanup()
        self.locker._cleanup = cleanup

    def _spin(self):
        while self.locker.lock_active:
            time.sleep(0.005)

    def test_untrusted_and_missing_caffeinate_fail_visibly(self):
        with patch.object(macos_locker, '_is_trusted', return_value=False):
            self.assertFalse(self.locker.start_lock())
        self.assertFalse(self.locker.lock_active)
        self.assertEqual(self.inhibitors, [])
        with patch.object(macos_locker, '_start_caffeinate', return_value=None):
            self.assertFalse(self.locker.start_lock())
        self.assertFalse(self.locker.lock_active)

    def test_tap_failure_and_timeout_restore_and_block_restart(self):
        self.locker._install_tap = Mock(side_effect=RuntimeError('tap null'))
        self.assertFalse(self.locker.start_lock())
        self.assertFalse(self.locker.lock_active)
        self.assertFalse(self.locker._cursor_hidden)
        macos_locker._stop_caffeinate.assert_called()
        worker = Mock()
        worker.is_alive.return_value = True
        with patch.object(macos_locker.threading, 'Thread', return_value=worker), \
             patch.object(self.locker._ready, 'wait', return_value=False):
            self.assertFalse(self.locker.start_lock())
            self.assertFalse(self.locker.start_lock())
        worker.start.assert_called_once()
        worker.is_alive.return_value = False

    def test_lock_stop_and_caps_password_submit(self):
        self.assertTrue(self.locker.start_lock())
        self.assertTrue(self.locker.lock_active)
        self.assertEqual(len(self.inhibitors), 1)
        seen = []
        self.locker.set_ui_callbacks(
            on_password=lambda p: seen.append(('p', p)),
            on_submit=lambda p: seen.append(('s', p)),
        )
        off, on = 0, macos_locker.kCGEventFlagMaskAlphaShift
        caps = macos_locker.kVK_CapsLock
        # 灯初始灭：每次按压驱动 toggle 灯亮（on），我们设回灭，所以事件流是 on,on,on。
        with patch.object(macos_locker, '_set_caps_lock_state') as restore:
            for _ in range(3):
                self.locker._handle(macos_locker.kCGEventFlagsChanged, on, caps)
            # 每次翻转后设回翻转前状态（灯灭）
            self.assertEqual(restore.call_count, 3)
            restore.assert_called_with(False)
        self.assertTrue(self.locker.unlock_mode)
        self.locker._handle(macos_locker.kCGEventKeyDown, 0, 0, 'a')
        self.locker._handle(macos_locker.kCGEventKeyDown, 0, macos_locker.kVK_Delete)
        self.locker._handle(macos_locker.kCGEventKeyDown, 0, 0, 'b')
        self.locker._handle(macos_locker.kCGEventKeyDown, 0, macos_locker.kVK_Return)
        self.assertEqual(seen, [('p', 'a'), ('p', ''), ('p', 'b'), ('s', 'b')])
        self.assertTrue(self.locker.stop_lock())
        self.assertFalse(self.locker.lock_active)
        self.assertFalse(self.locker._cursor_hidden)

    def test_caps_three_presses_when_led_starts_on(self):
        """锁定时 CapsLock 灯已亮：旧逻辑只数 off→on 沿，需按 6 次；
        应数翻转（caps != prev），3 次按压即可。每次翻转后设回灯亮。"""
        self.assertTrue(self.locker.start_lock())
        off, on = 0, macos_locker.kCGEventFlagMaskAlphaShift
        caps = macos_locker.kVK_CapsLock
        self.locker._caps_on = True  # 灯初始亮（锁屏前 CapsLock 开着）
        with patch.object(macos_locker, '_set_caps_lock_state') as restore:
            # 灯亮时每次按压驱动 toggle 灯灭（off），设回亮 → 事件流 off,off,off
            for _ in range(3):
                self.locker._handle(macos_locker.kCGEventFlagsChanged, off, caps)
            self.assertEqual(restore.call_count, 3)
            restore.assert_called_with(True)
        self.assertTrue(self.locker.unlock_mode)
        self.assertTrue(self.locker.stop_lock())

    def test_caps_restore_echo_does_not_desync_or_double_count(self):
        """IOHIDSetModifierLockState 同步回灌 flagsChanged 时不得再计数、不得打乱 _caps_on。"""
        self.assertTrue(self.locker.start_lock())
        on = macos_locker.kCGEventFlagMaskAlphaShift
        caps = macos_locker.kVK_CapsLock
        def restore(prev):
            echo = on if prev else 0
            self.locker._handle(macos_locker.kCGEventFlagsChanged, echo, caps)
            return True
        with patch.object(macos_locker, '_set_caps_lock_state', side_effect=restore):
            for _ in range(3):
                self.locker._handle(macos_locker.kCGEventFlagsChanged, on, caps)
        self.assertTrue(self.locker.unlock_mode)
        self.assertFalse(self.locker._caps_on)
        self.assertTrue(self.locker.stop_lock())

    def test_caps_stale_press_expires_then_three_more_unlock(self):
        """按 1 下后超过 2s 窗口，再匀速 3 下仍应解锁（不得卡死）。"""
        self.assertTrue(self.locker.start_lock())
        on = macos_locker.kCGEventFlagMaskAlphaShift
        caps = macos_locker.kVK_CapsLock
        t = [100.0]
        with patch.object(macos_locker.time, 'time', lambda: t[0]), \
             patch.object(macos_locker, '_set_caps_lock_state', return_value=True):
            self.locker._handle(macos_locker.kCGEventFlagsChanged, on, caps)
            self.assertFalse(self.locker.unlock_mode)
            t[0] += 3.0
            for _ in range(3):
                t[0] += 0.1
                self.locker._handle(macos_locker.kCGEventFlagsChanged, on, caps)
            self.assertTrue(self.locker.unlock_mode)
        self.assertTrue(self.locker.stop_lock())

    def test_disabled_tap_reenables_or_stops(self):
        event = object()
        self.locker._tap = object()
        self.locker._q = Mock()
        self.locker._q.CGEventTapIsEnabled.return_value = True
        self.assertIs(
            self.locker._tap_callback(
                None, macos_locker.kCGEventTapDisabledByTimeout, event, None),
            event,
        )
        self.locker._q.CGEventTapEnable.assert_called_once_with(self.locker._tap, True)
        self.locker.lock_active = True
        self.locker._q.CGEventTapIsEnabled.return_value = False
        self.locker._tap_callback(
            None, macos_locker.kCGEventTapDisabledByUserInput, event, None)
        self.assertFalse(self.locker.lock_active)

    def test_swallow_keys_and_concurrent_start(self):
        self.locker._q = Mock()
        self.locker._q.CGEventGetFlags.return_value = 0
        self.locker._q.CGEventGetIntegerValueField.return_value = 0
        self.locker._q.CGEventKeyboardGetUnicodeString = lambda *a: None
        self.assertIsNone(
            self.locker._tap_callback(None, macos_locker.kCGEventKeyDown, 1, None))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.locker.start_lock(), range(16)))
        self.assertTrue(all(results))
        self.assertEqual(len(self.inhibitors), 1)

    def test_main_factory_darwin_and_unknown(self):
        fake = Mock()
        lockapp = Mock()
        lockapp.return_value.run = Mock()
        with patch.multiple(app, IS_WINDOWS=False, IS_LINUX=False, IS_DARWIN=True), \
             patch.object(app.atexit, 'register'), \
             patch.object(macos_locker, 'MacInputLocker', return_value=fake), \
             patch.object(app, 'LockApp', lockapp), \
             patch.object(app, 'InputLocker') as windows:
            app.main()
            macos_locker.MacInputLocker.assert_called_once_with()
            lockapp.assert_called_once_with(fake, False)
            windows.assert_not_called()
        with patch.multiple(app, IS_WINDOWS=False, IS_LINUX=False, IS_DARWIN=False), \
             patch.object(app, 'messagebox'), \
             patch.object(app.sys, 'exit', side_effect=SystemExit(1)) as exited, \
             patch.object(app, 'InputLocker') as windows, \
             patch.object(app, 'LockApp') as lockapp2:
            with self.assertRaises(SystemExit):
                app.main()
            windows.assert_not_called()
            lockapp2.assert_not_called()
            exited.assert_called_once_with(1)


class MacAxPromptTests(unittest.TestCase):
    def test_bind_sets_dictionary_create_argtypes(self):
        cg, cf = Mock(), Mock()
        macos_locker._bind(cg, cf)
        self.assertEqual(len(cf.CFDictionaryCreate.argtypes), 6)
        self.assertIs(cf.CFDictionaryCreate.argtypes[3], ctypes.c_long)
        self.assertIs(cf.CFDictionaryCreate.argtypes[4], ctypes.c_void_p)
        self.assertIs(cf.CFDictionaryCreate.argtypes[5], ctypes.c_void_p)

    def test_ax_prompt_options_uses_cf_type_callbacks_then_releases_key(self):
        order = []
        cf = Mock()
        cf.CFStringCreateWithCString.return_value = 0x1001
        kcb, vcb = ctypes.c_void_p(0x2001), ctypes.c_void_p(0x2002)

        def create(allocator, keys, vals, n, got_kcb, got_vcb):
            order.append(('create', keys[0], vals[0], n, got_kcb, got_vcb))
            return 0x3001

        cf.CFDictionaryCreate.side_effect = create
        cf.CFRelease.side_effect = lambda x: order.append(('release', x))
        with patch.object(macos_locker, '_cf_symbol', return_value=ctypes.c_void_p(0x1002)), \
             patch.object(macos_locker, '_cf_type_dict_callbacks', return_value=(kcb, vcb)):
            opts = macos_locker._ax_prompt_options(cf)
        self.assertEqual(opts, 0x3001)
        self.assertEqual(order[0][0], 'create')
        self.assertEqual(order[0][1], 0x1001)
        self.assertEqual(order[0][3], 1)
        self.assertEqual(order[0][4], kcb)
        self.assertEqual(order[0][5], vcb)
        self.assertIsNotNone(order[0][4])
        self.assertIsNotNone(order[0][5])
        self.assertEqual(order[1], ('release', 0x1001))

    def test_is_trusted_prompt_releases_options(self):
        ax = Mock()
        ax.AXIsProcessTrusted.return_value = False
        ax.AXIsProcessTrustedWithOptions.return_value = False
        cf = Mock()
        with patch.object(macos_locker, '_ax_lib', return_value=ax), \
             patch.object(macos_locker, '_libs', return_value=(None, cf)), \
             patch.object(macos_locker, '_ax_prompt_options', return_value=0x4001):
            self.assertFalse(macos_locker._is_trusted(prompt=True))
        ax.AXIsProcessTrustedWithOptions.assert_called_once_with(0x4001)
        cf.CFRelease.assert_called_once_with(0x4001)


class CfSymbolTests(unittest.TestCase):
    def test_cf_symbol_matches_in_dll_pointer(self):
        libc = ctypes.CDLL(None)
        self.assertEqual(
            macos_locker._cf_symbol(libc, "stdout").value,
            ctypes.c_void_p.in_dll(libc, "stdout").value,
        )


if __name__ == '__main__':
    unittest.main()
