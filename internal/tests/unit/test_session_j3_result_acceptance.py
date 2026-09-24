from local_agent.session.contracts import TaskOutcome, TaskResult


def test_j3_result_render_keeps_source_scope_and_coverage():
    result = TaskResult(
        "task-j3",
        TaskOutcome.NO_VERDICT,
        "Sources: src/scheduler.cpp:42-67\n"
        "Scope: src/scheduler.cpp lines 35-75\n"
        "Missing coverage: callers were not searched.",
        False,
        reason_code="missing_evidence",
    )
    rendered = result.render()
    assert "src/scheduler.cpp:42-67" in rendered
    assert "Scope:" in rendered
    assert "Missing coverage:" in rendered
    assert "Controller: no_verdict" in rendered


def test_j3_invalid_result_reason_is_not_success_shaped():
    result = TaskResult(
        "task-j3-invalid",
        TaskOutcome.NO_VERDICT,
        "Repository result could not be accepted.",
        False,
        reason_code="invalid_input",
    )
    rendered = result.render()
    assert "Controller: no_verdict" in rendered
    assert "reason: invalid_input" in rendered
