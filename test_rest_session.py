import datetime
import json
from pathlib import Path
import tempfile
import unittest

from rest_session import EventStore, RestSessionController, project, iso, utc_now


UTC = datetime.timezone.utc


class FakeLocker:
    def __init__(self):
        self.lock_active = False
        self.starts = 0
        self.stops = 0
        self.fail_start = False
        self.fail_stop = False

    def start_lock(self):
        self.starts += 1
        if self.fail_start:
            return False
        self.lock_active = True
        return True

    def stop_lock(self):
        self.stops += 1
        if self.fail_stop:
            return False
        self.lock_active = False
        return True


class RestSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EventStore(self.tmp.name)
        self.base = datetime.datetime(2026, 9, 8, 3, 0, tzinfo=UTC)
        self.locker = FakeLocker()
        self.controller = RestSessionController(self.locker, self.tmp.name, clock=lambda: self.base)

    def request(self, sid="s1", start=0, duration=10, minimum=180):
        self.store.emit(self.store.requests, sid, "rest.requested", {
            "lockAt": iso(self.base + datetime.timedelta(minutes=start)),
            "unlockAt": iso(self.base + datetime.timedelta(minutes=start + duration)),
            "minUnlockSeconds": minimum,
            "source": "rest-break",
        }, self.base)

    def test_full_rest_emits_real_lock_and_scheduled_unlock(self):
        self.request()
        self.controller.tick(self.base)
        self.assertTrue(self.locker.lock_active)
        self.assertEqual(project(self.store.events(), self.base)["s1"].phase, "active")
        self.controller.tick(self.base + datetime.timedelta(minutes=10))
        session = project(self.store.events(), self.base)["s1"]
        self.assertEqual(session.phase, "ended")
        self.assertEqual(session.segments[0].reason, "scheduled")
        self.assertEqual((session.segments[0].unlocked_at - session.segments[0].locked_at).total_seconds(), 600)

    def test_password_unlock_before_minimum_is_rejected_without_event(self):
        self.request()
        self.controller.tick(self.base)
        ok, message = self.controller.unlock("password", self.base + datetime.timedelta(minutes=2))
        self.assertFalse(ok)
        self.assertIn("继续休息", message)
        self.assertTrue(self.locker.lock_active)
        self.assertEqual(len(self.store._read_dir(self.store.results)), 1)

    def test_password_unlock_after_three_minutes_is_terminal(self):
        self.request()
        self.controller.tick(self.base)
        ok, _ = self.controller.unlock("password", self.base + datetime.timedelta(minutes=3))
        self.assertTrue(ok)
        session = project(self.store.events(), self.base)["s1"]
        self.assertEqual(session.phase, "ended")
        self.assertEqual(session.segments[0].reason, "password")
        self.assertEqual((session.segments[0].unlocked_at - session.segments[0].locked_at).total_seconds(), 180)
        self.controller.tick(self.base + datetime.timedelta(minutes=5))
        self.assertEqual(self.locker.starts, 1)

    def test_request_is_idempotent_under_repeated_ticks(self):
        self.request()
        for _ in range(10):
            self.controller.tick(self.base)
        self.assertEqual(self.locker.starts, 1)
        self.assertEqual(len(self.store._read_dir(self.store.results)), 1)

    def test_missed_window_is_terminal_and_never_locks(self):
        self.request(start=0)
        self.controller.tick(self.base + datetime.timedelta(minutes=11))
        session = project(self.store.events(), self.base + datetime.timedelta(minutes=11))["s1"]
        self.assertEqual(session.phase, "skipped")
        self.assertFalse(self.locker.lock_active)
        self.controller.tick(self.base + datetime.timedelta(minutes=12))
        self.assertEqual(self.locker.starts, 0)

    def test_result_stream_is_replayable_after_controller_restart(self):
        self.request()
        self.controller.tick(self.base)
        restarted = RestSessionController(self.locker, self.tmp.name, clock=lambda: self.base)
        ok, _ = restarted.unlock("command", self.base + datetime.timedelta(minutes=4))
        self.assertTrue(ok)
        session = project(self.store.events(), self.base)["s1"]
        self.assertEqual(session.phase, "ended")
        self.assertEqual(session.segments[0].reason, "command")

    def test_restart_relocks_active_session_before_deadline(self):
        self.request()
        self.controller.tick(self.base)
        self.locker.lock_active = False  # old process died and released devices
        restarted = RestSessionController(self.locker, self.tmp.name, clock=lambda: self.base)
        restarted.tick(self.base + datetime.timedelta(minutes=2))
        session = project(self.store.events(), self.base)["s1"]
        self.assertTrue(self.locker.lock_active)
        self.assertEqual(len(session.segments), 2)
        self.assertEqual(session.phase, "active")

    def test_lock_failure_is_retryable_and_never_claims_success(self):
        self.request()
        self.locker.fail_start = True
        self.controller.tick(self.base)
        self.assertEqual(project(self.store.events(), self.base)["s1"].phase, "waiting")
        self.locker.fail_start = False
        self.controller.tick(self.base + datetime.timedelta(seconds=1))
        session = project(self.store.events(), self.base)["s1"]
        self.assertEqual(session.phase, "active")
        self.assertEqual(len(session.segments), 1)

    def test_immutable_events_have_unique_files_and_no_shared_tmp(self):
        self.request()
        files = list(Path(self.store.requests).glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertFalse(list(Path(self.store.requests).glob("*.tmp")))
        body = json.loads(files[0].read_text())
        self.assertEqual(body["schema"], "input-locker.event/v1")


if __name__ == "__main__":
    unittest.main()
