"""An in-process publish/subscribe bus with bounded, droppable subscribers.

The daemon publishes; the control API turns each subscriber into a server-sent event
stream. The one rule that matters is at the bottom of :meth:`Bus.publish`: a subscriber
whose queue is full is **dropped**, never waited for.

That is not a nicety. A GUI paused in a debugger, or one whose socket has gone away without
a FIN, would otherwise apply backpressure all the way into the resolver's event loop and
stall name resolution for the whole machine. Losing a log line is a trivial cost; losing DNS
is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

#: Enough that a burst during a `compose up` is not dropped, small enough that a stalled
#: consumer is noticed within a second or two.
QUEUE_SIZE = 256

#: Kept so a GUI attaching late has something to show immediately.
HISTORY_SIZE = 200


@dataclass(frozen=True)
class Event:
    kind: str
    """``log`` | ``query`` | ``domains`` | ``status``."""

    payload: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, **self.payload}


class Subscription:
    """One consumer's view of the bus. Closed by leaving the context."""

    def __init__(self, bus: Bus) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.dropped = False

    async def __aenter__(self) -> Subscription:
        self._bus._attach(self)
        return self

    async def __aexit__(self, *exc) -> None:
        self._bus._detach(self)

    async def get(self) -> Event | None:
        return await self.queue.get()


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()
        self._history: deque[Event] = deque(maxlen=HISTORY_SIZE)

    # ---- subscribers --------------------------------------------------------------

    def subscribe(self) -> Subscription:
        return Subscription(self)

    def _attach(self, subscription: Subscription) -> None:
        self._subscribers.add(subscription)

    def _detach(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def history(self) -> Iterator[Event]:
        return iter(tuple(self._history))

    # ---- publishing ---------------------------------------------------------------

    def publish(self, kind: str, **payload: Any) -> Event:
        event = Event(kind=kind, payload=payload)
        self._history.append(event)

        for subscription in tuple(self._subscribers):
            try:
                subscription.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Dropped rather than awaited: a stalled consumer must never apply
                # backpressure into the resolver. The queue being full is exactly why the
                # closing sentinel cannot simply be appended -- one item is discarded to
                # make room for it, so the reader still wakes up and shuts itself down.
                subscription.dropped = True
                self._detach(subscription)
                with contextlib.suppress(asyncio.QueueEmpty, asyncio.QueueFull):
                    subscription.queue.get_nowait()
                    subscription.queue.put_nowait(None)
        return event

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            self._detach(subscription)
            with contextlib.suppress(asyncio.QueueFull):
                subscription.queue.put_nowait(None)
