"""Q01-Q05 on a real third-party repository (cxxopts), deterministic and model-free (#418).

These build a real CMake project several times, so they run in the dedicated
``qualification`` CI job on Linux and Windows, which restores or fetches the pinned
corpus and sets ``LCA_QUALIFICATION_CACHE``. Elsewhere they skip, saying why;
``LCA_REQUIRE_QUALIFICATION=1`` turns a missing cache into a failure instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from test_acceptance_corpus import corpus, journeys

DETERMINISTIC_Q = ["Q01-build-test", "Q02-compile-diagnosis", "Q03-test-truth",
                   "Q04-candidate-scripted", "Q05-stop-build"]


def _cache() -> Path:
    raw = os.environ.get("LCA_QUALIFICATION_CACHE")
    required = os.environ.get("LCA_REQUIRE_QUALIFICATION") == "1"
    if not raw:
        if required:
            pytest.fail("LCA_REQUIRE_QUALIFICATION=1 but LCA_QUALIFICATION_CACHE is not set")
        pytest.skip("real-repository qualification runs in the qualification CI job "
                    "(set LCA_QUALIFICATION_CACHE to a fetched cache)")
    cache = Path(raw)
    entry = corpus.load_manifest()["cxxopts"]
    if corpus.cached_copy(entry, cache) is None:
        pytest.fail(f"cxxopts is not fetched into {cache}")
    return cache


def test_the_deterministic_real_repository_journeys_pass(tmp_path):
    cache = _cache()
    out = tmp_path / "q"
    argv = ["--corpus", "cxxopts", "--corpus-cache", str(cache), "--output", str(out)]
    for jid in DETERMINISTIC_Q:
        argv += ["--only", jid]
    code = journeys.main(argv)

    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    results = {j["id"]: j for j in report["journeys"]}
    failures = {jid: (results[jid]["status"], results[jid]["reason"])
                for jid in DETERMINISTIC_Q if results[jid]["status"] != "PASS"}
    assert not failures, json.dumps(failures, indent=1)
    assert code == 0
    metrics = report["corpus"]["metrics"]
    assert metrics["completed_verified"] == len(DETERMINISTIC_Q)
    assert all(metrics["unrelated_work_preserved"][jid] is True for jid in DETERMINISTIC_Q)
    assert metrics["rate_claimed"] is False
    # Model journeys never pretend: without a model they are UNKNOWN with the reason.
    for jid in ("Q06-repo-explain", "Q07-fix-model"):
        assert results[jid]["status"] == "UNKNOWN"
