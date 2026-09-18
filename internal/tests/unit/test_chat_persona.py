from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


INTERNAL = Path(__file__).resolve().parents[2]


def _chat():
    path = INTERNAL / "scripts" / "chat.py"
    spec = spec_from_file_location("chat_persona_test_target", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(chat):
    resolved = chat._resolve("qwen3-8b-npu")
    assert resolved is not None
    return resolved


def _golden_persona_off_messages():
    return [
        {
            "role": "system",
            "content": (
                "You are a language model running entirely on this machine, with no network "
                "access. You are served by OpenVINO Model Server on the NPU of the user's "
                "computer. The model is Qwen3-8B (INT4).\n\n"
                "You are running inside a plain terminal chat program. There is no window, "
                "no button and no menu. The person types a line and presses Enter. To leave, "
                "they press Enter on an empty line, or Ctrl-C while at the prompt.\n\n"
                "You have no tools, no access to the filesystem, and no ability to run "
                "commands. This program is separate from Local Code Agent, which is the part "
                "of this project that gives a model controlled repository access and verifies "
                "its work independently. You are not that, and you cannot speak for it.\n\n"
                "If you are asked something about this program, this project or this machine "
                "that you have not been told here, say you do not know. Do not guess at "
                "feature names, buttons or commands."
            ),
        },
        {"role": "user", "content": "hello"},
    ]


def test_persona_off_preserves_exact_messages_handed_to_client(monkeypatch):
    chat = _chat()
    profile, _, config = _config(chat)
    captured = []

    class FakeClient:
        def __init__(self, _config):
            pass

        def chat(self, messages):
            captured.append(messages)
            return SimpleNamespace(
                content="hi",
                stats=SimpleNamespace(ttft_s=None, decode_tok_s=None),
            )

    import builtins
    import local_agent.llm.client as client_module

    monkeypatch.setattr(chat, "_ensure_server", lambda *a, **k: True)
    monkeypatch.setattr(client_module, "OpenAICompatibleClient", FakeClient)
    answers = iter(["hello", ""])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    assert chat.converse("qwen3-8b-npu", profile, config, persona=None) == 0
    assert captured == [_golden_persona_off_messages()]


def test_persona_is_separate_and_contract_remains_first():
    chat = _chat()
    _, _, config = _config(chat)
    persona = chat.Persona(name="test", version="1", style="Be terse.")
    contract = chat._system_message(config)

    messages = chat._messages_for_turn([contract], "hello", persona)

    assert messages[0] == contract
    assert messages[1]["role"] == "system"
    assert "Your willingness to disagree, to point out a mistake, and to say you do not know does not." in messages[1]["content"]
    assert messages[1]["content"].endswith("Tone profile: Be terse.")
    assert messages[2] == {"role": "user", "content": "hello"}


def test_persona_schema_rejects_unknown_fields(tmp_path):
    chat = _chat()
    path = tmp_path / "persona.toml"
    path.write_text(
        'name = "bad"\nversion = "1"\nstyle = "Brief."\npermissions = "anything"\n',
        encoding="utf-8",
    )

    with pytest.raises(chat.PersonaError, match="unsupported persona field"):
        chat.load_persona(path)


def test_persona_schema_requires_only_nonempty_strings(tmp_path):
    chat = _chat()
    path = tmp_path / "persona.toml"
    path.write_text('name = "test"\nversion = "1"\nstyle = ""\n', encoding="utf-8")

    with pytest.raises(chat.PersonaError, match="non-empty string"):
        chat.load_persona(path)


def test_missing_persona_warns_and_falls_back_to_off(tmp_path):
    chat = _chat()
    warnings = []
    term = SimpleNamespace(status=lambda role, message: warnings.append((role, message)))

    assert chat._load_requested_persona(str(tmp_path / "missing.toml"), term) is None
    assert warnings and warnings[0][0] == "warn"
    assert warnings[0][1].startswith("Persona disabled:")


def test_named_persona_preset_loads():
    chat = _chat()
    warnings = []
    term = SimpleNamespace(status=lambda role, message: warnings.append((role, message)))
    persona = chat._load_requested_persona("aiden", term)
    assert warnings == []
    assert persona is not None
    assert persona.name == "aiden"
    assert persona.version == "3"
    assert len(persona.style) > 1000


def test_chat_sampling_default_is_separate_from_agent_profile():
    chat = _chat()
    _, _, base = _config(chat)

    assert base.temperature == 0.2
    assert chat.CHAT_DEFAULT_TEMPERATURE == 0.7


def test_chat_temperature_override_does_not_mutate_named_profile(monkeypatch):
    chat = _chat()
    captured = []

    monkeypatch.setattr(chat, "_ensure_server", lambda *a, **k: captured.append(a[1].temperature) or True)
    monkeypatch.setattr(chat, "converse", lambda _name, _profile, config, _persona: captured.append(config.temperature) or 0)

    assert chat.main(["qwen3-8b-npu", "--temperature", "0.7"]) == 0
    assert captured == [0.7, 0.7]
    assert chat.MODEL_PRESETS["ptl-npu-8b"].temperature == 0.2


def test_chat_temperature_rejects_out_of_range_value():
    chat = _chat()

    with pytest.raises(SystemExit):
        chat.main(["qwen3-8b-npu", "--temperature", "2.1"])


def test_aiden_v3_does_not_embed_calibration_answers():
    chat = _chat()
    persona = chat.load_persona(chat.SOURCE_ROOT / "personas" / "aiden.toml")

    assert 'Natural:' not in persona.style
    assert 'Wrong:' not in persona.style
    assert 'Not much lad. What\'s happening?' not in persona.style
