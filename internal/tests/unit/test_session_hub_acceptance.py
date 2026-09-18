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


def test_event_kind_collection_preserves_duplicates_for_corruption_detection():
    acceptance = _acceptance()
    fake = {
        "oneOf": [
            {"properties": {"kind": {"const": "task.admitted"}}},
            {"properties": {"kind": {"const": "task.admitted"}}},
            {"properties": {"kind": {"const": "task.closed"}}},
        ]
    }
    kinds = acceptance._collect_dotted_kind_consts(fake)
    assert kinds == ["task.admitted", "task.admitted", "task.closed"]
    assert len(kinds) != len(set(kinds))


def test_required_event_vocabulary_is_a_floor_not_an_exact_count():
    acceptance = _acceptance()
    grown = set(acceptance.V1_REQUIRED_EVENT_KINDS) | {"future.example"}
    assert acceptance.V1_REQUIRED_EVENT_KINDS.issubset(grown)


def test_private_session_storage_is_not_reachable_from_application_code():
    acceptance = _acceptance()
    assert acceptance._application_reaches_private_storage() == []


def test_outcome_acceptance_is_behavioural_not_prose_presence():
    acceptance = _acceptance()
    # This executes the product/schema mapping and construction invariants. It
    # deliberately does not inspect design prose for words such as NO_VERDICT.
    acceptance.outcome_gates()
