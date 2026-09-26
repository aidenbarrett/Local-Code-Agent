"""Human-readable Session Hub result semantics derived only from durable task facts.

This module is presentation, not authority. It never upgrades a controller verdict,
infers success from worker prose, or turns partial evidence into whole-task verification.
"""
from __future__ import annotations

from .task_read_model import TaskSnapshot


_SCOPE_LABELS = {
    "full_build": "full current-tree build proof",
    "full_test": "full current-tree test proof",
    "targeted_build": "targeted build evidence",
    "targeted_test": "targeted test evidence",
    "observed_build_failure": "observed build failure",
    "observed_test_failure": "observed test failure",
    "none": "no current proof scope",
}


def _scope_text(scope: str | None) -> str | None:
    if scope is None:
        return None
    # Proof-bound verdict scopes include request identity after a semicolon.
    proof_scope = scope.split(";", 1)[0].strip()
    return _SCOPE_LABELS.get(proof_scope, proof_scope.replace("_", " "))


def render_result_summary(task: TaskSnapshot) -> str:
    """Render one concise result line without creating new outcome authority."""
    if not isinstance(task, TaskSnapshot):
        raise TypeError("result summary requires TaskSnapshot")

    verdict = task.verdict
    scope = _scope_text(task.verdict_scope)
    reason = task.verdict_reason

    if verdict is None:
        return "Result: IN PROGRESS — no durable verdict yet."

    if verdict == "VERIFIED":
        detail = scope or "controller verification passed"
        return f"Result: VERIFIED — {detail}."

    if verdict == "FAILED":
        detail = "verification did not establish the requested result"
        if scope in {"targeted build evidence", "targeted test evidence"}:
            detail += f"; {scope} does not certify the whole request"
        return f"Result: FAILED — {detail}."

    if verdict == "REFUSED":
        suffix = "" if not reason else f" ({reason.replace('_', ' ')})"
        return f"Result: REFUSED — the controller did not authorize or execute the requested work{suffix}."

    if verdict == "NO_VERDICT":
        if reason in {"cleanup_unknown", "cancel_unreconciled"}:
            return "Result: NO VERDICT — final outcome is unknown because cleanup is not fully reconciled."
        suffix = "" if not reason else f" ({reason.replace('_', ' ')})"
        return f"Result: NO VERDICT — a safe final result could not be established{suffix}."

    if verdict == "NOT_REQUIRED":
        return "Result: COMPLETED — verification was not required by the controller contract."

    # Durable schema validation should reject unknown verdicts before presentation, but
    # the UI still fails closed if a malformed fixture or future value reaches this seam.
    return f"Result: UNKNOWN — unsupported durable verdict {verdict!r}."


__all__ = ["render_result_summary"]
