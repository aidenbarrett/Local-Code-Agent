from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devtools import fetch_candidate_models as fetch
from local_agent.config import MODEL_PRESETS
from serving.serve import model_directory, model_repository


def test_catalogue_ids_are_unique_and_grouped() -> None:
    ids = [c.repo_id for c in fetch.CATALOGUE]
    assert len(ids) == len(set(ids))
    assert {c.group for c in fetch.CATALOGUE} == set(fetch.GROUPS)


def test_every_panther_lake_ovms_preset_model_is_in_the_catalogue() -> None:
    ids = {c.repo_id for c in fetch.CATALOGUE}
    preset_models = {
        config.model for name, config in MODEL_PRESETS.items()
        if name.startswith(("ptl-", "ceiling-")) and config.runtime == "ovms"
    }
    assert preset_models and preset_models <= ids


def test_download_target_is_where_serving_looks(tmp_path: Path) -> None:
    candidate = fetch.CATALOGUE[0]
    target = fetch.target_directory(tmp_path, candidate)
    target.mkdir(parents=True)
    (target / "openvino_model.xml").write_text("<net/>", encoding="utf-8")
    assert model_directory(model_repository(tmp_path), candidate.repo_id) == target
    assert tmp_path.absolute() in target.parents


def test_unknown_group_or_model_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown group"):
        fetch.selected(["nonsense"], [])
    with pytest.raises(ValueError, match="not in the catalogue"):
        fetch.selected([], ["someone/else"])


def test_no_selection_lists_without_network(capsys: pytest.CaptureFixture[str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_hub", lambda: pytest.fail("network used"))
    assert fetch.main([]) == 0
    out = capsys.readouterr().out
    assert all(c.repo_id in out for c in fetch.CATALOGUE)


def test_dry_run_prints_targets_without_network(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch, "_hub", lambda: pytest.fail("network used"))
    assert fetch.main(["--group", "chat", "--runtime-root", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    chat = [c for c in fetch.CATALOGUE if c.group == "chat"]
    assert chat and all(str(fetch.target_directory(tmp_path, c)) in out for c in chat)


class _FakeHub:
    def __init__(self, size: int) -> None:
        self.size = size
        self.downloaded: list[tuple[str, Path]] = []

    def HfApi(self) -> Any:  # noqa: N802 - mirrors the huggingface_hub name
        size = self.size
        return SimpleNamespace(model_info=lambda repo_id, files_metadata: SimpleNamespace(
            siblings=[SimpleNamespace(size=size)]))

    def snapshot_download(self, *, repo_id: str, local_dir: Path) -> None:
        self.downloaded.append((repo_id, local_dir))


def test_download_refuses_when_disk_is_short(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _FakeHub(size=10**15)
    monkeypatch.setattr(fetch, "_hub", lambda: hub)
    assert fetch.main(["--group", "chat", "--runtime-root", str(tmp_path)]) == 2
    assert hub.downloaded == []


def test_download_fetches_each_selected_model_into_the_store(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hub = _FakeHub(size=1)
    monkeypatch.setattr(fetch, "_hub", lambda: hub)
    assert fetch.main(["--group", "chat", "--runtime-root", str(tmp_path),
                       "--headroom-gib", "0"]) == 0
    chat = [c for c in fetch.CATALOGUE if c.group == "chat"]
    assert hub.downloaded == [(c.repo_id, fetch.target_directory(tmp_path, c)) for c in chat]


def test_missing_hub_library_is_a_clear_refusal(capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> Any:
        raise RuntimeError("huggingface_hub is not installed")
    monkeypatch.setattr(fetch, "_hub", missing)
    assert fetch.main(["--group", "chat"]) == 2
    assert "huggingface_hub is not installed" in capsys.readouterr().err
