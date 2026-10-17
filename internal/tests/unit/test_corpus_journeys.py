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
                   "Q04-candidate-scripted", "Q05-stop-build", "Q08-source-truth"]


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
    for jid in ("Q06-repo-explain", "Q07-fix-model", "Q09-fix-source-model"):
        assert results[jid]["status"] == "UNKNOWN"


def _walkthrough():
    import importlib.util
    import sys

    name = "lca_demo_walkthrough"
    if name not in sys.modules:
        script = Path(journeys.__file__).resolve().parent / "demo-walkthrough.py"
        spec = importlib.util.spec_from_file_location(name, script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def test_the_walkthrough_repository_fails_only_the_seeded_tests_and_holds_user_work(tmp_path):
    cache = _cache()
    walkthrough = _walkthrough()
    repo = walkthrough.prepare(tmp_path / "walk", cache)

    status = journeys._git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    assert f"A  {walkthrough.STAGED[0]}" in status
    assert f" M {walkthrough.UNSTAGED[0]}" in status
    assert walkthrough.UNTRACKED[0] in status
    assert journeys._git(repo, "rev-list", "--all", "--count").strip() == "1"
    ok, log = journeys.independent_profile_suite(repo)
    assert not ok
    fault = corpus.load_manifest()["cxxopts"].fault(walkthrough.FAULT_ID)
    assert journeys.ctest_failed_names(log) == sorted(fault.expect)
    assert fault.kind == "source"
