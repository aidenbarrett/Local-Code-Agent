from __future__ import annotations

import pytest

from devtools.check_frozen_experiment_diff import frozen_experiment_changes


def test_frozen_experiment_paths_are_detected_across_separators():
    assert frozen_experiment_changes([
        "README.md",
        "internal/experiments/gen1/rows.json",
        r"internal\experiments\gen1\manifest.json",
    ]) == (
        "internal/experiments/gen1/manifest.json",
        "internal/experiments/gen1/rows.json",
    )


def test_non_experiment_paths_are_not_blocked():
    assert frozen_experiment_changes([
        "internal/local_agent/session/contracts.py",
        "internal/docs/project-history.md",
        "internal/measurement/new_generation.py",
    ]) == ()


def test_path_traversal_fails_closed():
    with pytest.raises(ValueError, match="traverse"):
        frozen_experiment_changes(["internal/experiments/../docs/file.md"])
