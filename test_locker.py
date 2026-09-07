"""Safe regression checks: fake input devices/inhibitors, no GUI or real lock.
Run: .venv/bin/python -m unittest -v test_locker
"""
import ctypes
import datetime
import importlib.machinery
import importlib.util
import json
import os
import random
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
# setUp 会把 app.load_config patch 成返回 {}；这里留住真实实现，供损坏配置的测试用。
_REAL_LOAD_CONFIG = app.load_config


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

    # ---- 检查间隙竞态（风险点 1）：两次检查之间计划文件被 aide 改写 ----

    def _writer_thread(self, states, stop, atomic=True):
        """模拟 aide 的写循环：tmp+rename 原子写（或 open('w') 非原子写）。"""
        i = 0
        while not stop.is_set():
            tasks = states[i % len(states)]
            if atomic:
                tmp = str(self.plan) + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(tasks, f)
                os.replace(tmp, str(self.plan))
            else:
                with open(str(self.plan), 'w', encoding='utf-8') as f:
                    f.write(json.dumps(tasks))
                    f.flush()
            i += 1
            time.sleep(0.001)  # 让出 GIL，避免写线程馈死检查/抓取线程

    def test_check_gap_rewrite_between_checks_keeps_invariants(self):
        """真实并发：写线程在两次检查之间反复改写计划文件（append 新计划/
        清理过期，与 aide 相同的 tmp+rename），watcher 每秒检查一次。
        不变量（对应两次线上 bug）：
        - 过期计划插入到当前计划前面，不阻塞/不误解锁当前锁定
        - 当前计划到点必须解锁（不被间隙里的改写错过）
        - 锁定期间被 append 的新计划，到点必须触发（不错过）
        """
        base = datetime.datetime.fromisoformat('2026-09-05T10:05:00+08:00')
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        p2 = dict(mode='once', when=dict(at='2026-09-05T10:12:00+08:00'),
                  unlock=dict(at='2026-09-05T10:22:00+08:00'))
        expired = dict(mode='once', when=dict(at='2026-09-05T09:00:00+08:00'),
                       unlock=dict(at='2026-09-05T09:10:00+08:00'))
        # P1/P2 始终在文件里（它们的锁/解锁时刻是本测试的确定性断言对象）；
        # 间隙改写体现在：过期计划的随机插入与任务顺序变化（aide append/清理的真实形态）。
        expired2 = dict(mode='once', when=dict(at='2026-09-05T08:00:00+08:00'),
                        unlock=dict(at='2026-09-05T08:10:00+08:00'))
        states = [[p1, p2], [expired, p1, p2], [p2, p1], [p1, expired, p2],
                  [expired, expired2, p1, p2], [p1, p2]]
        self.plan.write_text(json.dumps(states[0]))  # 计划文件始终存在（生产如此）
        stop = threading.Event()
        t = threading.Thread(target=self._writer_thread, args=(states, stop), daemon=True)
        t.start()
        try:
            for sec in range(0, 18 * 60):  # 10:05:00 -> 10:23:00
                now = base + datetime.timedelta(seconds=sec)
                self.watcher._check_plan(now)
                if 66 <= sec < 663:      # 10:06:06 ~ 10:16:02
                    self.assertTrue(self.locker.lock_active,
                        'sec %d: 当前锁定被过期计划误解锁' % sec)
                if 665 <= sec < 1019:    # P1 解锁后 P2 补锁（P2 恒在文件，664 必触发）
                    self.assertTrue(self.locker.lock_active,
                        'sec %d: 锁定期间 append 的新计划 P2 被错过' % sec)
                if 1021 <= sec < 1080:   # 10:22:01 之后 P2 已解锁
                    self.assertFalse(self.locker.lock_active,
                        'sec %d: P2 到点未解锁' % sec)
        finally:
            stop.set()
            t.join(timeout=2)
        # P1 锁过也解过；P2 锁过也解过——四条 fired 记录齐全，证明到点都没被错过。
        for key in (('lock', p1['when']['at']), ('unlock', p1['unlock']['at']),
                    ('lock', p2['when']['at']), ('unlock', p2['unlock']['at'])):
            self.assertEqual(self.watcher._fired.get(key), key[1],
                             'fired 缺失: %r' % (key,))

    def test_check_gap_chaos_concurrent_writer_keeps_invariant(self):
        """混沌 + 真实并发：写线程随机组合 过期/当前/未来 计划并原子改写，
        watcher 连续检查。不变量：被 P1 锁定后任何过期计划都不得解锁它；
        P1 到点必须解锁。"""
        rng = random.Random(7)
        base = datetime.datetime.fromisoformat('2026-09-05T10:05:00+08:00')
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        expired = dict(mode='once', when=dict(at='2026-09-05T09:00:00+08:00'),
                       unlock=dict(at='2026-09-05T09:10:00+08:00'))
        future = dict(mode='once', when=dict(at='2026-09-05T10:12:00+08:00'),
                      unlock=dict(at='2026-09-05T10:22:00+08:00'))
        self.plan.write_text(json.dumps([p1]))  # 计划文件始终存在（生产如此）
        stop = threading.Event()
        def writer():
            while not stop.is_set():
                tasks = [p1]
                if rng.random() < 0.5:
                    tasks.insert(0, expired)   # 过期计划插到当前计划前面（旧 bug 触发形）
                if rng.random() < 0.5:
                    tasks.append(future)       # 锁定期间 append 新计划
                tmp = str(self.plan) + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(tasks, f)
                os.replace(tmp, str(self.plan))
                time.sleep(0.001)  # 让出 GIL
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            for sec in range(0, 13 * 60):
                now = base + datetime.timedelta(seconds=sec)
                self.watcher._check_plan(now)
                if 66 <= sec < 663:
                    self.assertTrue(self.locker.lock_active,
                        'sec %d: 锁定被间隙改写误解锁' % sec)
        finally:
            stop.set()
            t.join(timeout=2)
        # P1 的锁与解锁动作都必须到点触发过（即便随后 future 补锁，动作不得被错过）。
        self.assertEqual(self.watcher._fired.get(('lock', p1['when']['at'])), p1['when']['at'])
        self.assertEqual(self.watcher._fired.get(('unlock', p1['unlock']['at'])), p1['unlock']['at'])

    def test_prune_in_gap_loses_unlock_then_memory_recovers(self):
        """真实生产链路风险：watcher 因卡顿/挂起错过 P1 的解锁瞬间，
        aide 的 cron 清理已把 P1 剪掉（unlock.at 已过）→ P1 的文件式解锁丢失。
        不变量：记忆解锁仍按 P1 的计划结束时刻放锁（不卡死、也不提前误解锁）；
        随后 append 的新计划照常触发。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        self.plan.write_text(json.dumps([p1]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)  # P1 锁上
        # 间隙：watcher 挂起跨过 10:16:03，aide 清理掉 P1 并 append 新计划 P2
        p2 = dict(mode='once', when=dict(at='2026-09-05T10:20:00+08:00'),
                  unlock=dict(at='2026-09-05T10:30:00+08:00'))
        self.plan.write_text(json.dumps([p2]))
        # watcher 恢复：P1 的计划结束时刻(10:16:03)已过 → 记忆解锁放锁（这是恢复，
        # 不是误解锁——误解锁是提前放；此处是到点该放但被挂起耽误了）。
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:17:00+08:00'))
        self.assertFalse(self.locker.lock_active, 'P1 到点后即便被清理也应放锁（不自愈=永久卡死）')
        # 新计划 P2 到点触发（不错过）
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:20:05+08:00'))
        self.assertTrue(self.locker.lock_active)
        self.assertEqual(self.locker._open_devices.call_count, 2)  # P1、P2 各 grab 一次
        # P2 到点解锁
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:30:00+08:00'))
        self.assertFalse(self.locker.lock_active)

    def test_plan_becomes_empty_while_locked_then_memory_recovers(self):
        """锁定中文件被清成 []（全部过期被清理）→ 当前锁不得被提前误解锁；
        但到属主计划结束时刻，即便文件仍空，记忆解锁也必须放锁（自愈）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        # 间隙：文件被清空（所有计划被清理）
        self.plan.write_text('[]')
        for sec in (1, 2, 3):
            self.watcher._check_plan(now + datetime.timedelta(seconds=sec))
            self.assertTrue(self.locker.lock_active, '空计划不得提前误解锁当前锁定')
        # 时钟走过 P1 的解锁时刻，文件仍空 → 记忆解锁放锁
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(self.locker.lock_active, '属主到点即便文件已空也应放锁')

    # ---- 运行中重启（watcher/进程重启后仍需正常）：进程死亡释放 grab，
    # 新进程靠文件状态恢复；_fired/_locked_unlock_at 是内存态会丢。 ----

    def _restart(self):
        """模拟进程重启：新 locker（grab 随旧进程死亡已释放，lock_active=False）
        + 新 watcher（_fired/记忆全丢），共享同一计划文件。返回新 locker/watcher。"""
        locker = backend.EvdevLocker()
        locker._open_devices = Mock(return_value={'keyboard': FakeDevice()})
        locker._handle_events = lambda: time.sleep(0.005)
        locker._scan_new = lambda: None
        self.addCleanup(locker.stop_lock)
        watcher = app.ScheduleWatcher(locker, str(self.plan),
                                      lambda *args: None, str(self.command), str(self.ack))
        return locker, watcher

    def test_restart_mid_window_relocks_and_unlocks_on_time(self):
        """锁定窗口内进程重启：grab 随旧进程死亡释放，新进程读到仍在窗口内的
        计划 → 重新上锁（恢复计划语义），到点正常解锁。重启后照样能用。"""
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        self.plan.write_text(json.dumps([p1]))
        # 旧进程：锁上
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00'))
        self.assertTrue(self.locker.lock_active)
        # 进程重启（旧 grab 释放）
        locker2, watcher2 = self._restart()
        self.assertFalse(locker2.lock_active)
        # 新进程：计划仍在窗口内 → 重新上锁
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:12:30+08:00'))
        self.assertTrue(locker2.lock_active, '重启后仍在窗口内应重新上锁')
        # 到点正常解锁
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(locker2.lock_active, '重启后到点应正常解锁')

    def test_restart_with_expired_plan_before_current_no_misunlock(self):
        """重启 + 过期计划插在前面：新进程 _fired 为空，但过期的不得误解锁，
        当前计划重新上锁并到点解锁。"""
        expired = dict(mode='once', when=dict(at='2026-09-05T09:00:00+08:00'),
                       unlock=dict(at='2026-09-05T09:10:00+08:00'))
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        self.plan.write_text(json.dumps([expired, p1]))  # 过期在前（旧 bug 触发形）
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00'))
        self.assertTrue(self.locker.lock_active)
        # 重启
        locker2, watcher2 = self._restart()
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:12:30+08:00'))
        self.assertTrue(locker2.lock_active, '重启后当前计划应重新上锁（不被前面过期计划阻塞）')
        # 过期计划不得在新进程里误解锁（新进程 _fired 为空，guard 要求本进程锁过）
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:13:00+08:00'))
        self.assertTrue(locker2.lock_active, '过期计划不得在重启后误解锁当前锁定')
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(locker2.lock_active)

    def test_restart_after_unlock_no_relock(self):
        """解锁时刻已过才重启：过期 once 不得在新进程里补锁（重启不翻旧账）。"""
        p1 = dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                  unlock=dict(at='2026-09-05T10:16:03+08:00'))
        self.plan.write_text(json.dumps([p1]))
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00'))
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(self.locker.lock_active)
        # 重启（计划文件里 P1 可能还没被 cron 清理）
        locker2, watcher2 = self._restart()
        watcher2._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:17:00+08:00'))
        self.assertFalse(locker2.lock_active, '解锁已过的计划在重启后不得补锁')
        locker2._open_devices.assert_not_called()

    # ---- 时钟跳变（风险点 4）：NTP 前进/倒退时 _fired 防重不得误判 ----

    def test_clock_forward_jump_past_unlock_still_unlocks(self):
        """锁定中时钟快进跨过解锁时刻 → 本进程锁过的锁必须被解开（不卡死）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        # NTP 快进到 10:30（跨过 10:16:03）
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:30:00+08:00'))
        self.assertFalse(self.locker.lock_active, '快进跨过解锁时刻必须解锁')

    def test_clock_forward_jump_over_whole_window_skips_lock(self):
        """未锁定时钟快进跨过整条 once（锁+解锁都过）→ 不得补锁（过期 once 不上锁）。"""
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:30:00+08:00'))
        self.assertFalse(self.locker.lock_active)
        self.locker._open_devices.assert_not_called()

    def test_clock_backward_jump_no_early_unlock_then_unlocks_at_due(self):
        """锁定中时钟倒退到锁之前 → 不得提前解锁；时钟再次走过解锁时刻仍要到点解锁。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        # NTP 倒退到 10:00（锁之前）：不得解锁
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:00:00+08:00'))
        self.assertTrue(self.locker.lock_active, '时钟倒退不得提前解锁')
        # 时钟再次走过 10:06:03：已锁过（fired），不得二次 grab
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:06:03+08:00'))
        self.assertTrue(self.locker.lock_active)
        self.locker._open_devices.assert_called_once()
        # 时钟走到 10:16:03：到点解锁
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(self.locker.lock_active, '倒退后再次到点仍须解锁')

    def test_clock_backward_jump_after_unlock_no_relock(self):
        """解锁后时钟倒退回到锁窗口内 → 不得重新上锁（once 防重 key=at 扛住时钟回拨）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00'))
        self.assertFalse(self.locker.lock_active)
        # 倒退回 10:10（锁窗口内）
        self.watcher._check_plan(datetime.datetime.fromisoformat('2026-09-05T10:10:00+08:00'))
        self.assertFalse(self.locker.lock_active, '解锁后时钟倒退不得重新上锁')
        self.locker._open_devices.assert_called_once()

    # ---- 原子写/容错（风险点 2+5）：读方绝不读半截 JSON；坏文件不得误动作 ----

    def test_load_tasks_concurrent_atomic_writes_always_complete(self):
        """原子写契约：写线程 tmp+rename（与 aide 相同）交替写两份不同计划，
        读线程 _load_tasks 每次只能读到完整的 A 或 B，绝不读到半截/混合。"""
        plan_a = [dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                       unlock=dict(at='2026-09-05T10:16:03+08:00'))]
        plan_b = [dict(mode='once', when=dict(at='2026-09-05T11:00:00+08:00'),
                       unlock=dict(at='2026-09-05T11:10:00+08:00')),
                  dict(mode='recurring', when=dict(days=['daily'], time='09:00'),
                       unlock=dict(days=['daily'], time='09:10'))]
        self.plan.write_text(json.dumps(plan_a))  # 初始文件必须存在（否则读到 []）
        stop = threading.Event()
        t = threading.Thread(target=self._writer_thread, args=([plan_a, plan_b], stop), daemon=True)
        t.start()
        try:
            for _ in range(2000):
                got = self.watcher._load_tasks()
                self.assertIn(got, (plan_a, plan_b), '读到半截/混合 JSON: %r' % (got,))
        finally:
            stop.set()
            t.join(timeout=2)

    def test_load_tasks_truncated_json_returns_empty_then_recovers(self):
        """写一半崩溃留下的半截文件：_load_tasks 吞异常返回 []，不抛异常、
        不误动作；下一份完整写入后立即恢复。"""
        self.plan.write_text('[{"mode": "once", "when": {"at": "2026')  # 半截
        self.assertEqual(self.watcher._load_tasks(), [])
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.watcher._check_plan(now)  # 不崩溃、不动作
        self.assertFalse(self.locker.lock_active)
        # 完整写入恢复
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active, '半截文件后完整写入必须恢复触发')

    def test_plan_garbage_bytes_returns_empty_and_keeps_lock(self):
        """计划文件是乱码字节：返回 []，锁定中的锁不得被放掉（失败安全=保持锁定）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        self.plan.write_bytes(b'\xff\xfe not json {{{')
        self.watcher._check_plan(now + datetime.timedelta(seconds=1))
        self.assertTrue(self.locker.lock_active, '乱码计划文件不得误解锁当前锁定')

    def test_plan_scalar_json_returns_empty(self):
        """计划文件是标量 JSON（42 / \"str\" / null）→ _load_tasks 返回 []，不动作。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        for body in ('42', '"str"', 'null', 'true'):
            self.plan.write_text(body)
            self.assertEqual(self.watcher._load_tasks(), [], 'body=%r' % body)
            self.watcher._check_plan(now)
            self.assertFalse(self.locker.lock_active)

    def test_plan_single_dict_task_fires(self):
        """契约：单个 dict（非数组）的计划也生效（_load_tasks 包成 [task]）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps(dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)

    def test_plan_non_dict_entry_does_not_kill_other_tasks(self):
        """计划数组混入非 dict 项（损坏）→ 跳过坏项，其余任务照常触发；
        不得整个检查报废（否则一个坏项永久禁用全部计划）。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([None, 42, "junk", dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)  # 不得抛异常
        self.assertTrue(self.locker.lock_active, '坏项不得阻塞后面的有效计划')

    def test_plan_non_dict_when_moment_does_not_crash(self):
        """任务的 when/unlock 不是 dict（损坏）→ 该任务不触发，但不崩溃、
        不影响其他任务。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([
            dict(mode='once', when='junk', unlock=None),  # 坏 when/unlock
            dict(mode='once', when=dict(at='2026-09-05T10:06:03+08:00'),
                 unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)

    def test_corrupt_config_falls_back_to_file_var(self):
        """config.json 损坏 → load_config 返回默认（无 schedule_file）→
        watcher 回退到 file_var 继续读计划（这也是 aide 每次 set 都重写
        schedule_file 指回来的原因）。"""
        bad = Path(self.directory.name) / 'config.json'
        bad.write_text('{broken')
        with patch.object(app, 'CONFIG_FILE', str(bad)), \
             patch.object(app, 'load_config', _REAL_LOAD_CONFIG):
            cfg = app.load_config()
            self.assertEqual(cfg.get('password'), app.DEFAULT_PASSWORD)
            self.assertNotIn('schedule_file', cfg)
            # watcher 的 _plan_path 走 file_var 回退，仍指向 self.plan
            self.assertEqual(self.watcher._plan_path(), str(self.plan))

    def test_invalid_command_json_acks_error_and_cleans_up(self):
        """命令文件是非法 JSON → ack result=error，命令文件被取走清理，
        不影响锁定状态。"""
        self.command.write_text('{broken')
        self.watcher._check_command()
        ack = json.loads(self.ack.read_text())
        self.assertEqual(ack['result'], 'error')
        self.assertFalse(self.command.exists())
        self.assertFalse(Path(str(self.command) + '.processing').exists())
        self.assertFalse(self.locker.lock_active)

    def test_lock_failure_is_retried_not_marked_fired(self):
        """锁定瞬时失败（如 grab 被别的程序短暂占用）不得把计划标记为已触发
        而永久跳过——下一秒必须重试。回归：旧代码先记 fired 再调 start_lock，
        一次失败就永久错过这次休息。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        real_start = self.locker.start_lock
        calls = {'n': 0}
        def flaky():
            calls['n'] += 1
            if calls['n'] == 1:
                return False  # 第一次瞬时失败
            return real_start()
        self.locker.start_lock = flaky
        self.watcher._check_plan(now)                                  # 失败，不记 fired
        self.assertFalse(self.locker.lock_active)
        self.watcher._check_plan(now + datetime.timedelta(seconds=1))  # 重试成功
        self.assertTrue(self.locker.lock_active, '瞬时失败后重试必须锁上')
        self.assertEqual(calls['n'], 2)

    def test_unlock_failure_is_retried_not_stuck(self):
        """解锁瞬时失败不得把计划标记为已解锁而永久卡死——下一秒必须重试。"""
        now = datetime.datetime.fromisoformat('2026-09-05T10:12:00+08:00')
        self.plan.write_text(json.dumps([dict(mode='once',
            when=dict(at='2026-09-05T10:06:03+08:00'),
            unlock=dict(at='2026-09-05T10:16:03+08:00'))]))
        self.watcher._check_plan(now)
        self.assertTrue(self.locker.lock_active)
        real_stop = self.locker.stop_lock
        calls = {'n': 0}
        def flaky():
            calls['n'] += 1
            if calls['n'] == 1:
                return False  # 第一次解锁瞬时失败
            return real_stop()
        self.locker.stop_lock = flaky
        due = datetime.datetime.fromisoformat('2026-09-05T10:16:03+08:00')
        self.watcher._check_plan(due)                                  # 解锁失败，不记 fired
        self.assertTrue(self.locker.lock_active, '解锁失败后应保持锁定待重试')
        self.watcher._check_plan(due + datetime.timedelta(seconds=1))  # 重试成功
        self.assertFalse(self.locker.lock_active, '解锁瞬时失败后重试必须放开')
        self.assertEqual(calls['n'], 2)

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
