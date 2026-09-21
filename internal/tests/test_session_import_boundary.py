from __future__ import annotations

import builtins
import importlib
import sys


def test_conversation_store_import_does_not_require_jsonschema(monkeypatch):
    """Direct chat storage must not pull in the gateway/event-contract stack."""
    for name in list(sys.modules):
        if name == "local_agent.session" or name.startswith("local_agent.session."):
            sys.modules.pop(name, None)

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = importlib.import_module("local_agent.session.conversation_store")
    assert module.__name__ == "local_agent.session.conversation_store"


def test_public_gateway_import_remains_available():
    from local_agent.session import ConversationGateway
    from local_agent.session.conversation_gateway import ConversationGateway as Direct

    assert ConversationGateway is Direct
