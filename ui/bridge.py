"""Carrying daemon events into the Qt event loop.

The stream from the daemon is read with a blocking call, and doing that on the GUI thread
would freeze the window for as long as the daemon takes to say anything -- which, between
events, is forever. So it happens on a :class:`QThread` and crosses over as signals.

Two rules the worker keeps, because breaking either is a crash rather than a glitch:

* it never touches a widget, a model or anything else owned by the GUI thread;
* what it emits is a plain dictionary, not an object it still holds a reference to.

Qt delivers a cross-thread emission as a queued connection by itself, so the slot runs on
the GUI thread and may do whatever it likes.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread, Signal

from client.api import Bridge

#: Reconnection backoff. Capped low: the daemon restarting during a build is ordinary, and
#: the window should catch up in seconds rather than minutes.
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0)


class BridgeWorker(QThread):
    """Follows the daemon's event stream, reconnecting until told to stop."""

    event = Signal(dict)
    connection = Signal(bool, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop = threading.Event()
        self._bridge: Bridge | None = None

    def stop(self) -> None:
        self._stop.set()

    @property
    def bridge(self) -> Bridge | None:
        return self._bridge

    def run(self) -> None:  # noqa: D102 - QThread's entry point
        attempt = 0
        while not self._stop.is_set():
            bridge = Bridge.connect()
            self._bridge = bridge
            self.connection.emit(bridge.online, "")

            if bridge.online:
                attempt = 0
                try:
                    for payload in bridge.events(self._stop):
                        if self._stop.is_set():
                            return
                        self.event.emit(dict(payload))
                except Exception as exc:  # noqa: BLE001 - reconnection is the whole point
                    self.connection.emit(False, str(exc))
                else:
                    self.connection.emit(False, "")

            if self._stop.is_set():
                return

            # Interruptible sleep, so stopping the window does not wait out the backoff.
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.05)
