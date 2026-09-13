from __future__ import annotations

import os


def test_monkeypatch_delenv_matches_pytest_shape(monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_COMPAT_PROBE", "1")
    monkeypatch.delenv("LOCAL_AGENT_COMPAT_PROBE")
    assert "LOCAL_AGENT_COMPAT_PROBE" not in os.environ
    monkeypatch.delenv("LOCAL_AGENT_COMPAT_PROBE", raising=False)
