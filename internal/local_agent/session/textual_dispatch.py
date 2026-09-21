"""Non-blocking turn dispatch for the Textual Session Hub.

Textual owns the event loop; it must never call model inference, repository tools or
SQLite-heavy gateway work inline. This adapter moves exactly one conversation turn at a
time onto a worker thread and returns a Future. It grants no authority and interprets no
result: the existing ConversationGateway remains the routing/admission boundary.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock


class HubTurnBusy(RuntimeError):
    """The Session Hub already has one turn executing."""


class HubTurnDispatcherClosed(RuntimeError):
    """The Textual dispatcher no longer accepts work."""


class HubTurnDispatcher:
    """Serialize gateway turns off the UI thread with an explicit busy boundary."""

    def __init__(self, gateway) -> None:
        if not callable(getattr(gateway, "turn", None)):
            raise TypeError("Textual turn dispatcher requires a turn-capable gateway")
        self.gateway = gateway
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lca-hub-turn")
        self._lock = Lock()
        self._active: Future[str] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active is not None and not self._active.done()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _clear_active(self, future: Future[str]) -> None:
        with self._lock:
            if self._active is future:
                self._active = None

    def submit(self, text: str, *, explicit_mode=None) -> Future[str]:
        """Return immediately after scheduling one gateway turn.

        A second turn is refused rather than queued invisibly. The durable task/endpoint
        layers own their own queues; conversation ordering must stay obvious to the user.
        Exceptions remain attached to the returned Future for the UI to render honestly.
        """
        if not isinstance(text, str):
            raise TypeError("Session Hub turn text must be a string")
        with self._lock:
            if self._closed:
                raise HubTurnDispatcherClosed("Session Hub turn dispatcher is closed")
            if self._active is not None and not self._active.done():
                raise HubTurnBusy("Session Hub is already processing a turn")
            future: Future[str] = self._executor.submit(
                self.gateway.turn,
                text,
                explicit_mode=explicit_mode,
            )
            self._active = future
            future.add_done_callback(self._clear_active)
            return future

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting turns; an already-started turn is never silently abandoned."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def __enter__(self) -> "HubTurnDispatcher":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False


__all__ = ["HubTurnBusy", "HubTurnDispatcher", "HubTurnDispatcherClosed"]
