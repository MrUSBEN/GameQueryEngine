"""Cooperative cancellation. A running job's progress() call raises Cancelled once the user asks to
stop, so every long loop that reports progress becomes a safe stopping point. Waits (rate limits,
retry back-offs) are cancelable too: they sleep in short slices and check the flag between them."""
import threading
import time


class Cancelled(Exception):
    pass


_local = threading.local()


def bind(event) -> None:
    """Called by the job runner: this thread now belongs to a job that `event` can cancel."""
    _local.event = event


def unbind() -> None:
    _local.event = None


def check() -> None:
    ev = getattr(_local, "event", None)
    if ev is not None and ev.is_set():
        raise Cancelled()


def sleep(seconds: float, step: float = 0.5, _sleep=time.sleep) -> None:
    """time.sleep that notices a Cancel within `step` seconds."""
    end = time.monotonic() + seconds
    while True:
        check()
        left = end - time.monotonic()
        if left <= 0:
            return
        _sleep(min(step, left))
