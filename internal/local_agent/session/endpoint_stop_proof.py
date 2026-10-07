"""Endpoint-side proof that a call Stop cut off is no longer running.

Stop closes the response; closing is never proof the server stopped. The endpoint
lease stays quarantined until an observation of the server itself says the request
is gone. This module is the generic half: what a proof source answers and how long
Stop waits for it. Which source a profile has is declared in its configuration and
built at the model-client edge (``llm/client.py``), so nothing here knows a runtime.

A source that cannot be read, or reads anything but a clean idle, proves nothing.
An idle reading is proof for the cut-off request only because Stop cuts a response
after the server was seen working on it: the request had left any queue and held
the server, so an idle server is one that has dropped it.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final, Protocol

_POLL_INTERVAL_S: Final = 0.25


class EndpointStopProof(Protocol):
    """Observes one physical endpoint from the server side."""

    @property
    def kind(self) -> str:
        """The ``ModelConfig.stop_proof`` value this source implements."""
        ...

    @property
    def settle_timeout_s(self) -> float:
        """How long a cut-off call may take to leave the server before giving up."""
        ...

    def idle(self, timeout_s: float) -> bool | None:
        """True: proved idle. False: observed busy. None: could not observe.

        One observation, finished (or abandoned as None) within ``timeout_s``.
        """
        ...


def await_idle(proof: EndpointStopProof, *, poll_interval_s: float = _POLL_INTERVAL_S,
               clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    """True only for an idle observation completed inside the settle bound.

    One absolute deadline. Every observation and every pause is clamped to the
    time that remains, and an idle reading that completes after the deadline is
    not accepted: proof that arrives late is not proof within the bound.
    """
    deadline = clock() + proof.settle_timeout_s
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        idle = proof.idle(remaining)
        if idle is True and clock() <= deadline:
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(poll_interval_s, remaining))


__all__ = ["EndpointStopProof", "await_idle"]
