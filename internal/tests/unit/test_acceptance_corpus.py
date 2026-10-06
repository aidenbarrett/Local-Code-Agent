"""Real-repository qualification corpus (#418): the manifest fails closed, a missing
cache is UNKNOWN rather than a silent fetch, and disposable copies never touch the cache.

The Q journeys themselves run in ``test_corpus_journeys.py`` (a dedicated CI job)."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INTERNAL = Path(__file__).resolve().parents[2]
SCRIPT = INTERNAL / "scripts" / "acceptance-journeys.py"


def _journeys():
    if "lca_acceptance_journeys" in sys.modules:
        return sys.modules["lca_acceptance_journeys"]
    spec = importlib.util.spec_from_file_location("lca_acceptance_journeys", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["lca_acceptance_journeys"] = module
    spec.loader.exec_module(module)
    return module


journeys = _journeys()
corpus = journeys.corpus

OVERLAY = """[repo]
name = "tiny"
build_dir = "build"
run_dir = ".local-agent/runs"
default_profile = "debug"

[profiles.debug]
configure = ["cmake", "-S", ".", "-B", "build"]
build = ["cmake", "--build", "build"]
test = ["ctest", "--test-dir", "build"]
"""

PATCH = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-old
+new
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@e.invalid", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _upstream(tmp_path: Path) -> tuple[Path, str]:
    """A tiny local 'third-party' repository whose .gitignore ignores dotfiles."""
    root = tmp_path / "upstream"
    root.mkdir()
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    (root / ".gitignore").write_text(".*\n!.gitignore\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "upstream")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _manifest(tmp_path: Path, *, commit: str, url: str = "https://example.invalid/tiny",
              licence: str | None = "MIT", overlay: str = OVERLAY, patch: str = PATCH,
              kind: str = "test") -> Path:
    base = tmp_path / "acceptance"
    (base / "tiny").mkdir(parents=True, exist_ok=True)
    (base / "tiny" / "local-agent.toml").write_text(overlay, encoding="utf-8")
    (base / "tiny" / "fault.patch").write_text(patch, encoding="utf-8")
    licence_line = f'licence = "{licence}"\n' if licence is not None else ""
    (base / "corpus.toml").write_text(
        "[[repository]]\n"
        f'name = "tiny"\nurl = "{url}"\ncommit = "{commit}"\n{licence_line}'
        'overlay = "tiny/local-agent.toml"\n'
        "[[repository.faults]]\n"
        f'id = "seed"\npatch = "tiny/fault.patch"\nkind = "{kind}"\nexpect = ["a"]\n',
        encoding="utf-8",
    )
    return base / "corpus.toml"


def test_the_shipped_manifest_is_valid_and_pins_cxxopts():
    entries = corpus.load_manifest()
    cxxopts = entries["cxxopts"]
    assert cxxopts.commit == "639cdbad93d45e74fd781e4cf6b0ac798eeec2aa"
    assert cxxopts.licence == "MIT"
    assert {f.kind for f in cxxopts.faults} == {"compile", "test"}
    for fault in cxxopts.faults:
        text = fault.patch.read_text(encoding="utf-8")
        # Every quoted context line carries the upstream licence with it.
        assert "Permission is hereby granted, free of charge" in text
        assert "Copyright (c) 2014 Jarryd Beck" in text


@pytest.mark.parametrize(("change", "message"), [
    ({"commit": "639cdbad"}, "full 40-hex SHA"),
    ({"commit": "Z" * 40}, "full 40-hex SHA"),
    ({"licence": None}, r"licence \(SPDX\) is required"),
    ({"licence": "GPL-3.0-only"}, "not an accepted permissive licence"),
    ({"overlay": "[repo]\nname = 'x'\n"}, "no build profiles"),
    ({"kind": "runtime"}, "kind must be one of"),
    ({"url": "http://example.invalid/tiny"}, "url must be https"),
])
def test_an_invalid_manifest_fails_closed(tmp_path, change, message):
    fields = {"commit": "a" * 40, **change}
    with pytest.raises(corpus.CorpusError, match=message):
        corpus.load_manifest(_manifest(tmp_path, **fields))


def test_a_missing_patch_file_fails_closed(tmp_path):
    path = _manifest(tmp_path, commit="a" * 40)
    (path.parent / "tiny" / "fault.patch").unlink()
    with pytest.raises(corpus.CorpusError, match="does not exist"):
        corpus.load_manifest(path)


def test_a_fault_patch_that_does_not_apply_at_the_commit_fails_closed(tmp_path):
    upstream, commit = _upstream(tmp_path)
    broken = PATCH.replace("-old", "-not what upstream has")
    entry = corpus.load_manifest(_manifest(tmp_path, commit=commit, patch=broken))["tiny"]
    entry = corpus.CorpusRepository(entry.name, upstream.as_uri(), entry.commit, entry.licence,
                                    entry.overlay, entry.faults)
    cache = tmp_path / "cache"
    cached = corpus.cache_path(entry, cache)
    cached.parent.mkdir()
    shutil.copytree(upstream, cached)
    with pytest.raises(corpus.CorpusError, match="does not apply"):
        corpus.check_faults(entry, cached)


def test_a_cached_copy_at_the_wrong_commit_is_an_error_not_a_miss(tmp_path):
    upstream, _commit = _upstream(tmp_path)
    entry = corpus.load_manifest(_manifest(tmp_path, commit="b" * 40))["tiny"]
    cached = corpus.cache_path(entry, tmp_path / "cache")
    cached.parent.mkdir()
    shutil.copytree(upstream, cached)
    with pytest.raises(corpus.CorpusError, match="is not commit"):
        corpus.cached_copy(entry, tmp_path / "cache")
    assert corpus.cached_copy(entry, tmp_path / "elsewhere") is None


def test_disposable_copies_carry_overlay_and_fault_and_never_touch_the_cache(tmp_path):
    upstream, commit = _upstream(tmp_path)
    entry = corpus.load_manifest(_manifest(tmp_path, commit=commit))["tiny"]
    cached = corpus.cache_path(entry, tmp_path / "cache")
    cached.parent.mkdir()
    shutil.copytree(upstream, cached)
    before = _git(cached, "status", "--porcelain=v1", "--ignored")
    head = _git(cached, "rev-parse", "HEAD")

    copy = corpus.disposable_copy(entry, cached, tmp_path / "copy", fault=entry.faults[0])

    assert (copy / "a.txt").read_text(encoding="utf-8") == "new\n"
    # The overlay is committed even though upstream's .gitignore ignores dotfiles,
    # so candidate worktrees built from HEAD carry the same policy.
    assert ".local-agent.toml" in _git(copy, "ls-files")
    assert _git(copy, "status", "--porcelain=v1") == ""
    assert _git(copy, "remote") == ""
    assert _git(cached, "status", "--porcelain=v1", "--ignored") == before
    assert _git(cached, "rev-parse", "HEAD") == head


def test_without_a_cached_copy_every_corpus_journey_is_unknown_and_nothing_is_fetched(
    tmp_path, monkeypatch, capsys,
):
    def no_network(*_args, **_kwargs):
        raise AssertionError("an offline run fetched")

    monkeypatch.setattr(corpus, "fetch", no_network)
    out = tmp_path / "run"
    code = journeys.main(["--corpus", "cxxopts", "--corpus-cache", str(tmp_path / "empty"),
                          "--output", str(out)])
    assert code == 0
    report = json.loads((out / "journeys.json").read_text(encoding="utf-8"))
    statuses = {j["id"]: (j["status"], j["reason"]) for j in report["journeys"]}
    assert set(statuses) == {jid for jid, *_ in journeys.CORPUS_JOURNEYS}
    for status, reason in statuses.values():
        assert status == "UNKNOWN"
        assert "not fetched" in reason and "--fetch" in reason
    assert report["corpus"]["metrics"]["completed_verified"] == 0
    assert report["preconditions"]["corpus"]["ready"] is False
    assert "Real repository cxxopts" in (out / "summary.txt").read_text(encoding="utf-8")


def test_fetch_needs_a_named_corpus(tmp_path, capsys):
    with pytest.raises(SystemExit) as refusal:
        journeys.main(["--fetch", "--output", str(tmp_path / "x")])
    assert refusal.value.code == 2
    assert "--fetch needs --corpus" in capsys.readouterr().err


def test_an_unknown_corpus_is_refused(tmp_path, capsys):
    with pytest.raises(SystemExit):
        journeys.main(["--corpus", "nope", "--output", str(tmp_path / "x")])
    assert "not in the manifest" in capsys.readouterr().err


def test_fetch_clones_the_pinned_commit_into_the_cache(tmp_path):
    upstream, commit = _upstream(tmp_path)
    _git(upstream, "commit", "--allow-empty", "-qm", "later")  # HEAD moves past the pin
    entry = corpus.load_manifest(_manifest(tmp_path, commit=commit))["tiny"]
    entry = corpus.CorpusRepository(entry.name, upstream.as_uri(), entry.commit, entry.licence,
                                    entry.overlay, entry.faults)
    cache = tmp_path / "cache"
    fetched = corpus.fetch(entry, cache)
    assert _git(fetched, "rev-parse", "HEAD").strip() == commit
    assert corpus.fetch(entry, cache) == fetched  # idempotent
    assert not list(cache.glob("*.partial"))
