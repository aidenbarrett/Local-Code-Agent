from pathlib import Path
import sys


INTERNAL = Path(__file__).resolve().parents[2]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))

from evaluation.endpoints import SUCCESS_OUTCOMES  # noqa: E402
from local_agent.provenance import outcome_contract_sha256  # noqa: E402


GEN2_OUTCOME_CONTRACT_SHA256 = "63c4bf907baa0cf2e406696bb7d65cdf4eef8feac206408fb7e4ffc3cb26efa8"


def test_generation2_success_vocabulary_is_frozen_before_collection():
    assert SUCCESS_OUTCOMES == frozenset({"pass", "escalated_pass"})


def test_generation2_outcome_contract_identity_is_frozen_before_collection():
    assert outcome_contract_sha256() == GEN2_OUTCOME_CONTRACT_SHA256
