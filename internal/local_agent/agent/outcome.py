"""What actually happened to a task, as a category you can count.

"It failed" is three different things wearing the same coat, and conflating them
makes the whole measurement useless:

  the model got it wrong                  -> a model problem, escalate
  the model could not verify its answer   -> a model problem, escalate
  the environment would not let it try    -> an infrastructure problem

That third one is the one that poisons a report. If cmake is not on PATH, or
policy forbids running tests, or the operator declined an approval, then the
cheap tier did not fail at engineering. Escalating it just burns the strong tier
on the same wall and then blames it for the result. So it becomes BLOCKED, it
does not escalate, and it is reported separately.

Which case a tool result falls into is decided by its typed `execution_status`
and `reason` (see `tools.base`), never by matching English in its summary. An
earlier version of this file did the latter and it was fragile rubbish.
"""

from __future__ import annotations

from enum import Enum


class Outcome(str, Enum):
    PASS = "pass"                      # cheap tier, or the only tier, got there
    ESCALATED_PASS = "escalated_pass"  # the strong tier rescued it
    ESCALATED_FAIL = "escalated_fail"  # neither tier got there
    FAIL = "fail"                      # single tier, no escalation available
    BLOCKED = "blocked"                # the environment prevented verification

    @property
    def succeeded(self) -> bool:
        return self in (Outcome.PASS, Outcome.ESCALATED_PASS)

    @property
    def completed_locally(self) -> bool:
        """Did the local stack finish it, at either tier?

        This is the number that speaks to cloud displacement. BLOCKED is not a
        local success, but it is not evidence against the local stack either.
        """
        return self.succeeded

    @property
    def needed_the_strong_tier(self) -> bool:
        return self in (Outcome.ESCALATED_PASS, Outcome.ESCALATED_FAIL)


def classify(
    *,
    succeeded: bool,
    escalated: bool,
    escalation_available: bool,
    blocked_reason: str | None,
) -> Outcome:
    """Decide the category once, in one place, so every report agrees."""
    if blocked_reason and not succeeded:
        return Outcome.BLOCKED
    if escalated:
        return Outcome.ESCALATED_PASS if succeeded else Outcome.ESCALATED_FAIL
    if succeeded:
        return Outcome.PASS
    return Outcome.FAIL if not escalation_available else Outcome.ESCALATED_FAIL
