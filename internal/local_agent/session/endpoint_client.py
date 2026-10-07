"""LLM client boundary owned by the Session Hub endpoint arbiter.

This is deliberately a small adapter, not another scheduler. It translates one
logical model call into the existing EndpointRequest vocabulary and delegates the
actual queue/lease/quarantine lifecycle to EndpointCallAdapter.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from threading import Event, Thread, local
from uuid import UUID, uuid4

from ..llm.client import LLMClient, OpenAICompatibleClient, StreamInterrupt
from ..llm.protocol import ChatResponse, InferenceInterruptedError, LLMTransportError
from .endpoint_call import EndpointCallAdapter, ManagedEndpointCall
from .endpoint_lease import EndpointRequest, EndpointRole, EndpointUnavailable
from .endpoint_stop_proof import EndpointStopProof, await_idle
from .value_validation import require_integer, require_nonempty_string


class ModelEndpointQuarantinedError(LLMTransportError):
    """The Hub refused to send this call: the endpoint is quarantined.

    Nothing was sent. An earlier call ended without proof that its inference
    stopped, so the arbiter no longer trusts the endpoint for this process. To the
    orchestrator this is an unavailable model, like any other transport failure;
    the conversation can tell the user the one thing that recovers it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason, cause="EndpointUnavailable", kind="unavailable")


# How long Stop may spend checking that the proof source answers before it cuts a
# call. If it cannot answer in this time, the call is not cut and runs to its end.
_CUT_CHECK_S = 1.0


class ManagedLLMClient:
    """Wrap one LLM client with the canonical in-process endpoint authority."""

    def __init__(
        self,
        client: LLMClient,
        adapter: EndpointCallAdapter,
        *,
        role: EndpointRole | str,
        session_id: str | None = None,
        task_id: str | None = None,
        execution_epoch: int | None = None,
        acquire_timeout: float | None = None,
    ) -> None:
        self._client = client
        self._adapter = adapter
        self._role = EndpointRole(role)
        self._session_id = session_id
        self._task_id = task_id
        self._execution_epoch = execution_epoch
        self._acquire_timeout = acquire_timeout
        self._validate_authority()

    def _validate_authority(self) -> None:
        if self._role is EndpointRole.CONVERSATION:
            if not isinstance(self._session_id, str) or not self._session_id.strip():
                raise ValueError("managed conversation client requires a session id")
            if self._task_id is not None or self._execution_epoch is not None:
                raise ValueError("managed conversation client cannot carry task authority")
            return
        if self._task_id is None:
            raise ValueError("managed worker client requires a task id")
        UUID(self._task_id)
        if (
            not isinstance(self._execution_epoch, int)
            or isinstance(self._execution_epoch, bool)
            or self._execution_epoch < 0
        ):
            raise ValueError("managed worker client requires a nonnegative execution epoch")

    def _request(self) -> EndpointRequest:
        return EndpointRequest(
            request_id=str(uuid4()),
            endpoint_id=self._adapter.runtime.endpoint_id,
            role=self._role,
            session_id=self._session_id,
            task_id=self._task_id,
            execution_epoch=self._execution_epoch,
        )

    def chat(self, messages, tools=None, max_tokens=None):
        # Admission and the call are separate so only a refusal to admit is
        # translated; whatever the model client raises propagates unchanged.
        try:
            call = self._adapter.begin(self._request(), timeout=self._acquire_timeout)
        except EndpointUnavailable as exc:
            raise ModelEndpointQuarantinedError(str(exc)) from exc
        proof = self._adapter.runtime.stop_proof
        client = self._client
        if (
            self._role is EndpointRole.WORKER
            and proof is not None
            and isinstance(client, OpenAICompatibleClient)
            and client.interruptible
        ):
            return self._chat_stoppable(call, proof, lambda interrupt: call.invoke(
                client.chat, messages, tools, max_tokens, interrupt=interrupt,
            ).value)
        return call.invoke(client.chat, messages, tools, max_tokens).value

    def _chat_stoppable(
        self, call: ManagedEndpointCall, proof: EndpointStopProof,
        invoke: Callable[[StreamInterrupt], ChatResponse],
    ) -> ChatResponse:
        """A worker call Stop can end early, on an endpoint that can prove it stopped.

        Stop fences the lease (quarantine) and sets its signal. The watcher cuts the
        response only while the proof source answers, so a cut is never made that
        could not later be proved; otherwise the call runs on and its normal return
        is the proof, exactly as without a proof source. After a cut the endpoint
        stays quarantined until the server is observed idle within the settle bound.
        """
        runtime = self._adapter.runtime
        try:
            signal = runtime.stop_signal(call.lease_id)
        except BaseException:
            call.release_without_call()  # nothing was sent
            raise
        interrupt = StreamInterrupt()
        finished = Event()

        def watch() -> None:
            # Bounded by construction: each wait is short and the one observation is
            # clamped to _CUT_CHECK_S, so teardown's join() cannot wait on it for
            # longer. A request() after the call finished finds the interrupt
            # detached and touches no connection.
            while not finished.is_set():
                if signal.wait(0.05):
                    if not finished.is_set() and proof.idle(_CUT_CHECK_S) is not None:
                        interrupt.request()
                    return

        watcher = Thread(target=watch, name="lca-endpoint-stop", daemon=True)
        watcher.start()
        try:
            return invoke(interrupt)
        except InferenceInterruptedError:
            # invoke() joined Stop's quarantine. Only the server can clear it.
            if await_idle(proof):
                call.reconcile(known_stopped=True)
            raise
        finally:
            finished.set()
            watcher.join()
            runtime.drop_stop_signal(call.lease_id)


class ManagedWorkerClientFactory:
    """Bind worker calls to the durable task/epoch active on the calling thread."""

    def __init__(self, raw_factory, adapter: EndpointCallAdapter, *, session_id: str):
        if not callable(raw_factory):
            raise TypeError("managed worker client factory requires a callable raw factory")
        require_nonempty_string(
            session_id, message="managed worker client factory requires a session id",
        )
        self._raw_factory = raw_factory
        self._adapter = adapter
        self._session_id = session_id
        self._authority = local()

    @contextmanager
    def bind_task(self, task_id: str, execution_epoch: int):
        UUID(task_id)
        require_integer(
            execution_epoch, minimum=0,
            message="worker endpoint authority requires a nonnegative execution epoch",
        )
        previous = getattr(self._authority, "value", None)
        self._authority.value = (task_id, execution_epoch)
        try:
            yield
        finally:
            self._authority.value = previous

    def cancel_execution(self, task_id: str, execution_epoch: int) -> tuple[str, ...]:
        """Fence endpoint dispatch for one exact durable task execution."""
        UUID(task_id)
        if type(execution_epoch) is not int or execution_epoch < 0:
            raise ValueError("worker endpoint cancellation requires a nonnegative execution epoch")
        return self._adapter.runtime.cancel_execution(task_id, execution_epoch)

    def __call__(self) -> ManagedLLMClient:
        authority = getattr(self._authority, "value", None)
        if authority is None:
            raise RuntimeError("managed worker client requires admitted task authority")
        task_id, execution_epoch = authority
        return ManagedLLMClient(
            self._raw_factory(),
            self._adapter,
            role=EndpointRole.WORKER,
            session_id=self._session_id,
            task_id=task_id,
            execution_epoch=execution_epoch,
        )


__all__ = ["ManagedLLMClient", "ManagedWorkerClientFactory", "ModelEndpointQuarantinedError"]
