"""Safe regression checks: fake input devices/inhibitors, no GUI or real lock.
Run: .venv/bin/python -m unittest -v test_locker
"""
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

    def test_other_backends_already_locked_are_noops(self):
        for cls in (backend.LinuxInputLocker, backend.WaylandLocker, app.InputLocker):
            with self.subTest(backend=cls.__name__):
                locker = cls()
                locker.lock_active = True
                with patch.object(backend, '_start_inhibit') as inhibit:
                    self.assertTrue(locker.start_lock())
                    inhibit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
