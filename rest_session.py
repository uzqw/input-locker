"""Event-sourced rest-lock sessions shared with aide over WSL files.

The request stream is written by aide. The result stream is written only by
InputLocker. Final event files are immutable; projections are rebuilt on every
reconcile, so a process restart does not depend on in-memory task indexes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import uuid

SCHEMA = "input-locker.event/v1"
REQUESTED = "rest.requested"
LOCKED = "rest.locked"
OBSERVED = "rest.observed"
UNLOCKED = "rest.unlocked"
FAILED = "rest.failed"
EXPIRED = "rest.expired"
TERMINAL = {UNLOCKED, FAILED, EXPIRED}


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (f".tmp-{path.name}-{uuid.uuid4().hex}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


@dataclass
class Segment:
    locked_at: datetime
    locked_through: datetime
    unlocked_at: datetime | None = None
    reason: str | None = None

    def as_dict(self):
        out = {
            "lockedAt": iso(self.locked_at),
            "lockedThrough": iso(self.locked_through),
        }
        if self.unlocked_at is not None:
            out["unlockedAt"] = iso(self.unlocked_at)
        if self.reason:
            out["endReason"] = self.reason
        return out


@dataclass
class Session:
    session_id: str
    lock_at: datetime
    unlock_at: datetime
    source: str = "rest-break"
    reason: str = ""
    # Kept in the event shape for replaying old requests; early unlock is
    # intentionally always allowed.
    min_unlock_seconds: int = 0
    phase: str = "waiting"
    segments: list[Segment] = field(default_factory=list)
    last_error: str = ""
    updated_at: datetime | None = None

    def as_dict(self):
        return {
            "id": self.session_id,
            "phase": self.phase,
            "lockAt": iso(self.lock_at),
            "unlockAt": iso(self.unlock_at),
            "source": self.source,
            "reason": self.reason,
            "minUnlockSeconds": self.min_unlock_seconds,
            "segments": [s.as_dict() for s in self.segments],
            "lastError": self.last_error or None,
            "updatedAt": iso(self.updated_at or self.lock_at),
        }


class EventStore:
    """Immutable request/result event files; each side has one writer."""

    def __init__(self, root):
        self.root = Path(root)
        self.requests = self.root / "requests"
        self.results = self.root / "results"
        self.requests.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)

    def _read_dir(self, directory):
        events = []
        if not directory.is_dir():
            return events
        for path in sorted(directory.glob("*.json")):
            try:
                with open(path, encoding="utf-8") as f:
                    event = json.load(f)
                if (event.get("schema") == SCHEMA and event.get("eventId")
                        and event.get("sessionId") and event.get("type")):
                    events.append(event)
            except (OSError, ValueError, AttributeError):
                # A malformed immutable event is ignored, never treated as an
                # empty stream; the valid prefix remains replayable.
                continue
        return events

    def events(self):
        return self._read_dir(self.requests) + self._read_dir(self.results)

    def emit(self, directory, session_id, event_type, data=None, now=None, causation_id=None):
        event = {
            "schema": SCHEMA,
            "eventId": uuid.uuid4().hex,
            "sessionId": session_id,
            "type": event_type,
            "recordedAt": iso(now or utc_now()),
            "data": data or {},
        }
        if causation_id:
            event["causationId"] = causation_id
        _atomic_write(directory / (event["eventId"] + ".json"), event)
        return event

    def emit_result(self, session_id, event_type, data=None, now=None, causation_id=None):
        return self.emit(self.results, session_id, event_type, data, now, causation_id)


def _request_from_event(event):
    if event.get("type") != REQUESTED:
        return None
    data = event.get("data") or {}
    lock_at = parse_time(data.get("lockAt"))
    unlock_at = parse_time(data.get("unlockAt"))
    if not lock_at or not unlock_at or lock_at >= unlock_at:
        return None
    return Session(
        session_id=event["sessionId"],
        lock_at=lock_at,
        unlock_at=unlock_at,
        source=data.get("source", "rest-break"),
        reason=data.get("reason", ""),
        min_unlock_seconds=int(data.get("minUnlockSeconds", 0) or 0),
        updated_at=parse_time(event.get("recordedAt")),
    )


def project(events, now=None):
    """Replay request/result events into sessions, independent of file order."""
    now = now or utc_now()
    sessions = {}
    priority = {REQUESTED: 0, LOCKED: 1, OBSERVED: 2, UNLOCKED: 3, FAILED: 3, EXPIRED: 3}
    ordered = sorted(events, key=lambda e: (
        e.get("recordedAt", ""), priority.get(e.get("type"), 9), e.get("eventId", "")
    ))
    for event in ordered:
        sid = event.get("sessionId")
        if event.get("type") == REQUESTED:
            request = _request_from_event(event)
            if request and sid not in sessions:
                sessions[sid] = request
            continue
        session = sessions.get(sid)
        if not session:
            continue
        data = event.get("data") or {}
        recorded = parse_time(event.get("recordedAt")) or now
        session.updated_at = recorded
        if event.get("type") == LOCKED:
            locked_at = parse_time(data.get("lockedAt")) or recorded
            through = parse_time(data.get("lockedThrough")) or locked_at
            session.segments.append(Segment(locked_at, through))
            session.phase = "active"
        elif event.get("type") == OBSERVED:
            through = parse_time(data.get("lockedThrough")) or recorded
            if session.segments and session.segments[-1].unlocked_at is None:
                if through > session.segments[-1].locked_through:
                    session.segments[-1].locked_through = through
                session.phase = "active"
        elif event.get("type") == UNLOCKED:
            unlocked = parse_time(data.get("unlockedAt")) or recorded
            through = parse_time(data.get("lockedThrough")) or unlocked
            if session.segments and session.segments[-1].unlocked_at is None:
                segment = session.segments[-1]
                segment.locked_through = max(segment.locked_through, through)
                segment.unlocked_at = unlocked
                segment.reason = data.get("reason", "unknown")
            session.phase = "ended"
        elif event.get("type") == FAILED:
            session.last_error = str(data.get("error", "lock failed"))
            if not data.get("retryable", False):
                session.phase = "failed"
        elif event.get("type") == EXPIRED:
            session.phase = "skipped"
    for session in sessions.values():
        if session.phase == "waiting" and now >= session.unlock_at:
            session.phase = "skipped"
    return sessions


class RestSessionController:
    """Own rest-lock lifecycle; callers never call the backend for rest sessions."""

    def __init__(self, locker, events_dir, status_cb=None, clock=utc_now):
        self.locker = locker
        self.store = EventStore(events_dir)
        self.status_cb = status_cb or (lambda _msg, _ok: None)
        self.clock = clock
        self._lock = threading.RLock()

    def sessions(self, now=None):
        return project(self.store.events(), now or self.clock())

    def _emit_status(self, msg, ok):
        try:
            self.status_cb(msg, ok)
        except Exception:
            pass

    def _active(self, now):
        return [s for s in self.sessions(now).values() if s.phase == "active"]

    def tick(self, now=None):
        now = now or self.clock()
        with self._lock:
            sessions = self.sessions(now)
            for session in sorted(sessions.values(), key=lambda s: s.lock_at):
                if session.phase in {"ended", "failed", "skipped"}:
                    continue
                if now < session.lock_at:
                    continue
                if session.phase == "waiting" and now >= session.unlock_at:
                    self.store.emit_result(session.session_id, EXPIRED, {"reason": "window_missed"}, now)
                    continue
                if session.phase == "waiting":
                    if self.locker.lock_active:
                        continue
                    if self.locker.start_lock():
                        self.store.emit_result(session.session_id, LOCKED, {
                            "lockedAt": iso(now), "lockedThrough": iso(now),
                        }, now)
                        self._emit_status("计划休息已锁定", True)
                    else:
                        self.store.emit_result(session.session_id, FAILED, {
                            "action": "lock", "error": str(getattr(self.locker, "_lock_error", "lock failed")),
                            "retryable": True,
                        }, now)
                        self._emit_status("计划休息锁定失败", False)
                    continue
                # Active session: a restart may have released the backend. Reconcile
                # the desired state from the event log instead of trusting memory.
                if not self.locker.lock_active:
                    if now < session.unlock_at and self.locker.start_lock():
                        self.store.emit_result(session.session_id, LOCKED, {
                            "lockedAt": iso(now), "lockedThrough": iso(now),
                        }, now)
                        self._emit_status("计划休息已恢复锁定", True)
                        continue
                    through = session.segments[-1].locked_through if session.segments else now
                    self.store.emit_result(session.session_id, UNLOCKED, {
                        "unlockedAt": iso(through), "lockedThrough": iso(through), "reason": "failure",
                    }, now)
                    self._emit_status("计划休息锁定意外结束", False)
                    continue
                last = session.segments[-1].locked_through if session.segments else session.lock_at
                if now - last >= timedelta(seconds=30):
                    self.store.emit_result(session.session_id, OBSERVED, {
                        "lockedThrough": iso(now),
                    }, now)
                if now >= session.unlock_at:
                    if self.locker.stop_lock():
                        self.store.emit_result(session.session_id, UNLOCKED, {
                            "unlockedAt": iso(now), "lockedThrough": iso(now), "reason": "scheduled",
                        }, now)
                        self._emit_status("计划休息完成", True)
                    else:
                        self._emit_status("计划休息解锁失败，正在重试", False)

    def unlock(self, reason="password", now=None):
        now = now or self.clock()
        with self._lock:
            active = self._active(now)
            if not active:
                return bool(self.locker.stop_lock()), "no_rest_session"
            session = active[0]
            if not self.locker.stop_lock():
                return False, "解锁失败，请重试"
            self.store.emit_result(session.session_id, UNLOCKED, {
                "unlockedAt": iso(now), "lockedThrough": iso(now), "reason": reason,
            }, now)
            self._emit_status("休息提前结束" if reason == "password" else "计划休息已解锁", True)
            return True, "ok"

    def manual_lock(self):
        """Manual UI lock remains outside rest accounting but uses the same backend seam."""
        return bool(self.locker.start_lock())
