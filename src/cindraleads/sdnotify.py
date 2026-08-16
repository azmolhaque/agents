"""sd_notify, in forty lines of stdlib.

`WatchdogSec=` is the difference between "the worker process exists" and "the worker
loop is still going round". A Python process wedged on a socket read with no timeout,
or deadlocked on the SQLite write lock, stays perfectly alive from systemd's point of
view and does no work forever. The watchdog is the only thing that catches that, and it
requires the process to say so periodically.

The real `systemd` Python bindings need a C build and the `libsystemd` headers, which
on the Pi means apt packages and a compile step for what is, underneath, a datagram
written to a Unix socket named in `$NOTIFY_SOCKET`. This does that.

Every function is a no-op when the variable is unset, so the same `cindra work` runs
identically under systemd, under a terminal, and in the test suite. That matters more
than it sounds: a worker that only behaves correctly when supervised is a worker whose
behaviour you cannot reproduce when debugging it.
"""

from __future__ import annotations

import os
import socket
import time

from cindraleads.logging import get_logger

__all__ = [
    "available",
    "notify",
    "notify_ready",
    "notify_status",
    "notify_stopping",
    "notify_watchdog",
    "watchdog_interval_seconds",
]

log = get_logger("cindraleads.sdnotify")


def available() -> bool:
    return bool(os.environ.get("NOTIFY_SOCKET"))


def notify(state: str) -> bool:
    """Send one datagram. False if there was nowhere to send it."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    # A leading '@' is systemd's spelling of the abstract namespace, whose sockets are
    # addressed with a leading NUL byte.
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as exc:
        # Never fatal. Losing the notification costs supervision, but raising here
        # would mean the watchdog killed the worker for failing to say it was alive.
        log.debug("sdnotify_failed", state=state, error=str(exc))
        return False
    return True


def notify_ready(status: str = "") -> bool:
    payload = "READY=1"
    if status:
        payload += f"\nSTATUS={status}"
    return notify(payload)


def notify_watchdog() -> bool:
    return notify("WATCHDOG=1")


def notify_status(status: str) -> bool:
    return notify(f"STATUS={status}")


def notify_stopping() -> bool:
    return notify("STOPPING=1")


def watchdog_interval_seconds() -> float:
    """How often to ping, or 0 when no watchdog is configured.

    Half of `WatchdogUSEC`, per systemd's own recommendation: pinging at exactly the
    deadline means any scheduling jitter reads as a hang.

    `WATCHDOG_PID` guards the case that matters -- systemd sets the variables in the
    environment, and a child process inherits them. Without this check a subprocess
    would happily keep petting the watchdog for a parent that had already wedged.
    """
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return 0.0
    owner = os.environ.get("WATCHDOG_PID")
    if owner and owner != str(os.getpid()):
        return 0.0
    try:
        micros = int(raw)
    except ValueError:
        return 0.0
    return max(0.0, micros / 2_000_000)


class Watchdog:
    """Rate-limits the pings so a hot loop does not spam the socket."""

    def __init__(self, interval_seconds: float | None = None) -> None:
        self.interval = (
            watchdog_interval_seconds() if interval_seconds is None else interval_seconds
        )
        self._last = 0.0

    @property
    def enabled(self) -> bool:
        return self.interval > 0

    def pet(self, *, now: float | None = None) -> bool:
        """Ping if due. Returns whether a datagram was actually sent."""
        if not self.enabled:
            return False
        stamp = time.monotonic() if now is None else now
        if stamp - self._last < self.interval:
            return False
        self._last = stamp
        return notify_watchdog()
