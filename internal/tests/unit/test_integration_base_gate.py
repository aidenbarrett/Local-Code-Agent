from __future__ import annotations

from devtools.check_integration_base import integration_base_error, main


def test_pull_request_to_main_is_merge_eligible():
    assert integration_base_error("pull_request", "main") is None
    assert integration_base_error("pull_request", "main", draft=True) is None
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "main",
        "--draft",
        "false",
    ]) == 0


def test_ready_stacked_pull_request_is_early_ci_only_and_fails_gate():
    error = integration_base_error("pull_request", "feature/parent", draft=False)

    assert error is not None
    assert "early CI" in error
    assert "feature/parent" in error
    assert "main" in error
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "feature/parent",
        "--draft",
        "false",
    ]) == 2


def test_draft_stacked_pull_request_stays_green_but_is_not_candidate(capsys):
    assert integration_base_error("pull_request", "feature/parent", draft=True) is None
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "feature/parent",
        "--draft",
        "true",
    ]) == 0
    assert "not an integration candidate" in capsys.readouterr().out


def test_pull_request_without_base_fails_closed_even_when_draft():
    error = integration_base_error("pull_request", "", draft=True)

    assert error is not None
    assert "cannot determine" in error
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "",
        "--draft",
        "true",
    ]) == 2


def test_non_pull_request_events_are_not_blocked():
    assert integration_base_error("push", "") is None
    assert integration_base_error("workflow_dispatch", "feature/parent") is None
    assert main(["--event-name", "push", "--base-ref", "", "--draft", "false"]) == 0


def test_invalid_draft_value_fails_closed():
    assert main([
        "--event-name",
        "pull_request",
        "--base-ref",
        "main",
        "--draft",
        "maybe",
    ]) == 2
