from __future__ import annotations

from internal.devtools.check_integration_base import integration_base_error, main


def test_pull_request_to_main_is_merge_eligible():
    assert integration_base_error("pull_request", "main") is None
    assert main(["--event-name", "pull_request", "--base-ref", "main"]) == 0


def test_stacked_pull_request_is_early_ci_only():
    error = integration_base_error("pull_request", "feature/parent")

    assert error is not None
    assert "early CI" in error
    assert "feature/parent" in error
    assert "main" in error
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "feature/parent",
    ]) == 2


def test_pull_request_without_base_fails_closed():
    error = integration_base_error("pull_request", "")

    assert error is not None
    assert "cannot determine" in error
    assert main(["--event-name", "pull_request", "--base-ref", ""]) == 2


def test_non_pull_request_events_are_not_blocked():
    assert integration_base_error("push", "") is None
    assert integration_base_error("workflow_dispatch", "feature/parent") is None
    assert main(["--event-name", "push", "--base-ref", ""]) == 0
