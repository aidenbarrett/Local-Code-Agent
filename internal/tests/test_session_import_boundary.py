from __future__ import annotations

import os
import subprocess
import sys


_JSONSCHEMA_FREE_IMPORT = """
import builtins
import importlib

real_import = builtins.__import__


def guarded_import(name, *args, **kwargs):
    if name == "jsonschema" or name.startswith("jsonschema."):
        raise ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
    return real_import(name, *args, **kwargs)


builtins.__import__ = guarded_import
module = importlib.import_module("local_agent.session.conversation_store")
assert module.__name__ == "local_agent.session.conversation_store"
"""


def test_conversation_store_import_does_not_require_jsonschema():
    """Direct chat storage must not pull in the gateway/event-contract stack.

    Checked in a fresh interpreter. Evicting ``local_agent.session`` from this process's
    module registry would leave every class imported earlier bound to a stale module, so
    later monkeypatches of the re-imported module would silently miss it (#454 CI).
    """
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    done = subprocess.run(  # noqa: S603 - this interpreter, fixed probe
        [sys.executable, "-c", _JSONSCHEMA_FREE_IMPORT],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr


def test_public_gateway_import_remains_available():
    from local_agent.session import ConversationGateway
    from local_agent.session.conversation_gateway import ConversationGateway as Direct

    assert ConversationGateway is Direct
