"""The Windows service control manager handshake.

Windows only, and imported only when ``--service`` is the role, so nothing here is loaded on
Linux or in a normal desktop launch.

The SCM starts the registered executable and then expects it to call
``StartServiceCtrlDispatcher`` within about thirty seconds. An executable that just runs its
own code fails with error 1053, "the service did not respond in a timely fashion" -- which
is why this exists at all rather than ``sc create`` pointing straight at the resolver.

``PRESHUTDOWN`` is accepted alongside ``STOP`` deliberately. Plain ``SHUTDOWN`` gives a
service a few seconds at most, and taking the resolution rules down is a registry write plus
a cache flush. Preshutdown is the notification that grants a longer, configurable window.
"""

from __future__ import annotations

import asyncio
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from core import log
from daemon.runner import Runner, run_forever
from system.service_win import DISPLAY_NAME, SERVICE_NAME

#: How long to wait for the resolver to finish removing its rules before giving up on a
#: clean stop. Comfortably longer than a PowerShell round trip plus a cache flush.
STOP_TIMEOUT_SECONDS = 30


class HostBridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME

    _svc_accepted_ = (
        win32service.SERVICE_ACCEPT_STOP | win32service.SERVICE_ACCEPT_PRESHUTDOWN
    )

    def __init__(self, args) -> None:
        super().__init__(args)
        self._stopped = win32event.CreateEvent(None, 0, 0, None)
        self._runner: Runner | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._done = threading.Event()

    # ---- the SCM's entry points ---------------------------------------------------

    def SvcDoRun(self) -> None:  # noqa: N802 - the SCM's name
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 - the SCM shows nothing useful otherwise
            log.error(f"service: {exc!r}")
            servicemanager.LogErrorMsg(f"HostBridge stopped: {exc!r}")
        finally:
            self._done.set()
            win32event.SetEvent(self._stopped)

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._runner = Runner()
        await run_forever(self._runner)

    def SvcStop(self) -> None:  # noqa: N802 - the SCM's name
        self._request_stop()

    def SvcPreShutdown(self) -> None:  # noqa: N802 - the SCM's name
        self._request_stop()

    def _request_stop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        runner, loop = self._runner, self._loop
        if runner is not None and loop is not None:
            # From the SCM's thread into the resolver's loop. request_stop touches an
            # asyncio.Event, which is not thread-safe to set directly.
            loop.call_soon_threadsafe(runner.request_stop)
        # Wait for the shutdown path -- which removes the rules -- before telling the SCM
        # we are done, or Windows may terminate us mid-removal.
        self._done.wait(STOP_TIMEOUT_SECONDS)
        win32event.SetEvent(self._stopped)


def run() -> int:
    """Hand this process to the service control manager."""
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(HostBridgeService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
