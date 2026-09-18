from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


INTERNAL = Path(__file__).resolve().parents[2]


def _acceptance():
    path = INTERNAL / "scripts" / "accept-session-hub.py"
    spec = spec_from_file_location("accept_session_hub", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_session_hub_foundation_acceptance_gates_pass():
    acceptance = _acceptance()
    assert acceptance.main() == 0


def test_acceptance_script_is_additive_and_names_future_safety_boundaries():
    text = (INTERNAL / "scripts" / "accept-session-hub.py").read_text(encoding="utf-8")
    for phrase in (
        "NO_VERDICT",
        "endpoint arbitration",
        "deterministic routing",
        "off-thread blocking work",
        "unknown effects",
        "source mutation remains disabled",
    ):
        assert phrase in text
