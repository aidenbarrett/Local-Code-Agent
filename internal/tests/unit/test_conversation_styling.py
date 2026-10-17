"""Conversation colours: speakers, recorded verdicts and diff lines.

Styling is layered on the plain projection and never changes it: the styled text's
plain form is exactly render_conversation, and user or controller text is appended
literally, never parsed as markup.
"""
from __future__ import annotations

from uuid import uuid4

from local_agent.session.task_read_model import OriginTurn, TaskSnapshot
from local_agent.session.textual_hub import (
    ConversationEntry,
    load_palette,
    render_conversation,
    render_conversation_text,
)

PALETTE = load_palette("neon")
TURN = OriginTurn("conv", 0, "a" * 64)
DIFF_ANSWER = (
    "Candidate change prepared in an isolated copy.\n"
    "Preview:\n"
    "diff --git a/src/x.cpp b/src/x.cpp\n"
    "--- a/src/x.cpp\n"
    "+++ b/src/x.cpp\n"
    "@@ -1,2 +1,2 @@\n"
    " int keep;\n"
    "-    ++count;\n"
    "+    ++count_;\n"
    "Candidate patch sha256 abc."
)


def _task(verdict: str | None, answer: str, *, closed: bool = True) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=str(uuid4()), admitted_sequence=1, last_sequence=1,
        state="completed" if closed else "running", execution_epoch=0,
        origin_kind="user_rule", repository_id="repo", skill="fix-build-failure",
        deadline_utc="2030-01-01T00:00:00Z", verdict=verdict,
        verdict_reason="verification_passed" if verdict else None,
        closed_sequence=1 if closed else None, result_answer=answer, origin_turn=TURN,
    )


def _style_of(text, needle: str) -> str:
    start = text.plain.index(needle)
    styles = [str(span.style) for span in text.spans
              if span.start <= start and span.end >= start + len(needle)]
    return " ".join(styles)


def test_styled_plain_text_is_exactly_the_plain_projection():
    entries = (ConversationEntry("user", "fix it", TURN), ConversationEntry("assistant", "ok"))
    tasks = (_task("VERIFIED", DIFF_ANSWER),)
    assert render_conversation_text(entries, tasks, PALETTE).plain == \
        render_conversation(entries, tasks)
    assert render_conversation_text((), (), PALETTE).plain == render_conversation(())


def test_verdicts_take_their_semantic_colour():
    for verdict, role in (("VERIFIED", "verified"), ("FAILED", "fail"), ("NO_VERDICT", "warn")):
        text = render_conversation_text((ConversationEntry("user", "go", TURN),),
                                        (_task(verdict, "detail"),), PALETTE)
        assert PALETTE[role] in _style_of(text, verdict.replace("_", " "))


def test_diff_lines_are_coloured_only_inside_the_diff():
    text = render_conversation_text((ConversationEntry("user", "fix it", TURN),),
                                    (_task("VERIFIED", DIFF_ANSWER),), PALETTE)
    assert PALETTE["verified"] in _style_of(text, "+    ++count_;")
    assert PALETTE["fail"] in _style_of(text, "-    ++count;")
    assert PALETTE["muted"] in _style_of(text, "@@ -1,2 +1,2 @@")
    assert _style_of(text, "Candidate patch sha256 abc.") == ""
    assert _style_of(text, "Candidate change prepared") == ""


def test_text_that_looks_like_markup_or_a_diff_outside_a_diff_stays_literal():
    entries = (ConversationEntry("user", "[bold red]-not a removal[/]", TURN),)
    text = render_conversation_text(entries, (_task("FAILED", "- a bullet, not a diff"),), PALETTE)
    assert "[bold red]-not a removal[/]" in text.plain
    assert _style_of(text, "[bold red]-not a removal[/]") == ""
    assert _style_of(text, "- a bullet, not a diff") == ""
