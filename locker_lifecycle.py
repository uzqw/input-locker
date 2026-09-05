"""Serialize public lock/unlock operations from the UI and schedule threads."""
from functools import wraps


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._state_lock:
            return method(self, *args, **kwargs)
    return call
