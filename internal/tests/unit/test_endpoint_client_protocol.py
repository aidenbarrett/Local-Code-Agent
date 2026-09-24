from __future__ import annotations

import inspect

from local_agent.session.endpoint_client import ManagedLLMClient


def test_managed_client_keeps_orchestrator_chat_call_shape():
    signature = inspect.signature(ManagedLLMClient.chat)
    assert list(signature.parameters) == ["self", "messages", "tools", "max_tokens"]
    assert signature.parameters["tools"].default is None
    assert signature.parameters["max_tokens"].default is None
