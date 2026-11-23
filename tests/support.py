"""Shared test helpers.

The waits here are **deadline-based, not iteration-based**. A fixed loop such as
``for _ in range(300): time.sleep(0.02)`` silently encodes an assumption about
machine speed — roughly six seconds of patience — and then fails on a loaded CI
runner while passing on a fast laptop. Tests must wait for a *condition*, not
for a number of ticks.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")


def wait_for(
    condition: Callable[[], Optional[T]],
    *,
    timeout: float = 120.0,
    interval: float = 0.02,
    message: str = "condition was not met in time",
) -> T:
    """Poll ``condition`` until it returns something truthy, or fail.

    ``timeout`` is deliberately generous: it exists to turn a hang into a clear
    failure, not to enforce a performance budget.
    """
    deadline = time.monotonic() + timeout
    last: Optional[T] = None
    while time.monotonic() < deadline:
        last = condition()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{message} (waited {timeout:.0f}s)")


def wait_for_status(read, *, settled: str = "running", timeout: float = 120.0) -> dict:
    """Wait until a job record stops reporting the ``settled`` status."""

    def poll():
        job = read()
        return job if job.get("status") != settled else None

    return wait_for(poll, timeout=timeout, message="the job never finished")
