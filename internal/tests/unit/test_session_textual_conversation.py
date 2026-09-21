from __future__ import annotations

from local_agent.session.conversation_store import (
    OpenConversation,
    Session,
    Turn,
    new_session,
)
from local_agent.session.textual_conversation import (
    CanonicalConversationProvider,
    ConversationProjectionError,
    project_conversation,
)


def test_projection_preserves_exact_raw_user_and_assistant_text():
    session = new_session("p", "m", "d")
    session.turns.extend(
        [
            Turn("user", "  Build it exactly like this.  ", "2026-09-21T00:00:00Z", 0),
            Turn("assistant", "Line one\nLine two", "2026-09-21T00:00:01Z", 0),
        ]
    )

    projected = project_conversation(session)

    assert [(entry.role, entry.text) for entry in projected] == [
        ("user", "  Build it exactly like this.  "),
        ("assistant", "Line one\nLine two"),
    ]


def test_projection_does_not_create_system_or_task_result_turns():
    session = new_session("p", "m", "d")
    session.turns.append(Turn("user", "hello", "2026-09-21T00:00:00Z", 0))

    projected = project_conversation(session)

    assert len(projected) == 1
    assert projected[0].role == "user"
    assert projected[0].text == "hello"


def test_provider_reads_latest_owned_in_memory_conversation(tmp_path):
    session = new_session("p", "m", "d")
    owned = OpenConversation(session=session, _runtime_root=tmp_path)
    provider = CanonicalConversationProvider(owned)

    assert provider() == ()
    session.turns.append(Turn("user", "one", "2026-09-21T00:00:00Z", 0))
    assert [(entry.role, entry.text) for entry in provider()] == [("user", "one")]


def test_projection_fails_closed_on_malformed_stored_role():
    session = new_session("p", "m", "d")
    session.turns.append(Turn("system", "not raw chat", "2026-09-21T00:00:00Z", 0))

    try:
        project_conversation(session)
    except ConversationProjectionError as exc:
        assert "unsupported role" in str(exc)
    else:
        raise AssertionError("malformed stored role was projected")


def test_projection_requires_canonical_session_type():
    try:
        project_conversation(object())
    except TypeError as exc:
        assert "requires Session" in str(exc)
    else:
        raise AssertionError("non-session projection was accepted")
