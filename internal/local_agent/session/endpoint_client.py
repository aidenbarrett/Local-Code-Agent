"""LLM client boundary owned by the Session Hub endpoint arbiter.

This is deliberately a small adapter, not another scheduler. It translates one
logical model call into the existing EndpointRequest vocabulary and delegates the
actual queue/lease/quarantine lifecycle to EndpointCallAdapter.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from ..llm.client import LLMClient
from .endpoint_call import EndpointCallAdapter
from .endpoint_lease import EndpointRequest, EndpointRole


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
        result = self._adapter.call(
            self._request(),
            self._client.chat,
            messages,
            tools,
            max_tokens,
            timeout=self._acquire_timeout,
        )
        return result.value


class ManagedWorkerClientFactory:
    """Create a worker client only after durable execution identity exists."""

    def __init__(self, raw_factory, adapter: EndpointCallAdapter, *, session_id: str):
        if not callable(raw_factory):
            raise TypeError("managed worker client factory requires a callable raw factory")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("managed worker client factory requires a session id")
        self._raw_factory = raw_factory
        self._adapter = adapter
        self._session_id = session_id

    def __call__(self):
        raise RuntimeError("managed worker client requires admitted task authority")

    def for_task(self, task_id: str, execution_epoch: int) -> ManagedLLMClient:
        return ManagedLLMClient(
            self._raw_factory(),
            self._adapter,
            role=EndpointRole.WORKER,
            session_id=self._session_id,
            task_id=task_id,
            execution_epoch=execution_epoch,
        )


__all__ = ["ManagedLLMClient", "ManagedWorkerClientFactory"]
