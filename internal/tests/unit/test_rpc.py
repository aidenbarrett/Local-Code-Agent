from __future__ import annotations

import io
import json

from local_agent.config import ModelConfig
from local_agent.rpc.stdio import StdioServer


def _server(root):
    return StdioServer(root, ModelConfig(), stdin=io.StringIO(), stdout=io.StringIO())


def test_ping_and_listings(sandbox):
    server = _server(sandbox.root)
    assert server.handle({"id": 1, "method": "ping"})["repo"] == "cpp-sandbox"

    skills = server.handle({"id": 2, "method": "skills"})["skills"]
    assert {s["name"] for s in skills} >= {"build-and-test", "git-review"}

    tools = server.handle({"id": 3, "method": "tools"})["tools"]
    by_name = {t["name"]: t for t in tools}
    assert by_name["read_file"]["risk"] == "read"
    assert by_name["apply_patch"]["risk"] == "dangerous"


def test_route_over_rpc(sandbox):
    server = _server(sandbox.root)
    ranking = server.handle(
        {"id": 4, "method": "route", "params": {"task": "the test segfaults"}}
    )["ranking"]
    assert ranking[0][0] == "diagnose-test-failure"


def test_unknown_method_is_an_error_line(sandbox):
    out = io.StringIO()
    server = StdioServer(
        sandbox.root,
        ModelConfig(),
        stdin=io.StringIO('{"id": 9, "method": "self_destruct"}\n'),
        stdout=out,
    )
    server.serve_forever()
    payload = json.loads(out.getvalue().strip())
    assert payload["id"] == 9
    assert "unknown method" in payload["error"]
