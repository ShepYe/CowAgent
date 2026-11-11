"""Serialize configuration/roster commits and track their process-local revision."""
import threading
from contextlib import contextmanager

lock = threading.RLock()
_revision = 0


def current():
    with lock:
        return _revision


@contextmanager
def writing():
    global _revision
    with lock:
        yield
        _revision += 1
