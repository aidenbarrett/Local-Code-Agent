"""Refactored guards still reject malformed authority values at public entrypoints."""
from __future__ import annotations

from uuid import uuid4

import pytest

from local_agent.session.cancellation import (
    CancelRequest, CancellationSource, CancellationToken, EpochFence, OwnedProcessHandle,
    ProcessContainment,
)
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRequest, PendingPosition, QueueClass
from local_agent.session.value_validation import (
    require_exact_keys,
    require_integer,
    require_nonempty_string,
    require_nonnegative_number,
    require_sha256,
    require_string_tuple,
)


@pytest.mark.parametrize("value", [None, True, False, -1, 1.0, "0", []])
def test_malformed_epochs_keep_the_same_refusal(value):
    with pytest.raises(ValueError, match="initial execution epoch must be a nonnegative integer"):
        EpochFence(value)
    with pytest.raises(ValueError, match="execution epoch must be a nonnegative integer"):
        EpochFence().accepts(value)
    with pytest.raises(ValueError, match="cancel request execution epoch must be nonnegative"):
        CancelRequest(str(uuid4()), str(uuid4()), value, CancellationSource.USER)
    with pytest.raises(ValueError, match="cancellation token execution epoch must be nonnegative"):
        CancellationToken(str(uuid4()), value)
    with pytest.raises(ValueError, match="class queue position must be a nonnegative integer"):
        PendingPosition(QueueClass.WORK, value)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "1", []])
def test_positive_identity_and_queue_bounds_still_refuse(value):
    with pytest.raises(ValueError, match="owned process pid must be positive"):
        OwnedProcessHandle(str(uuid4()), 0, value, "birth", ProcessContainment.DIRECT_CHILD)
    for field in ("work_pending_limit", "chat_pending_limit_per_session"):
        with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
            EndpointArbiter("endpoint", **{field: value})


@pytest.mark.parametrize("value", [None, 0, False, "", " \t", []])
def test_text_identity_guards_still_refuse(value):
    with pytest.raises(ValueError, match="endpoint id must be nonempty"):
        EndpointArbiter(value)
    with pytest.raises(ValueError, match="endpoint id must be nonempty"):
        EndpointRequest(str(uuid4()), value, "conversation", session_id="session")
    with pytest.raises(ValueError, match="endpoint quarantine reason must be nonempty"):
        EndpointArbiter("endpoint").quarantine(value)
    with pytest.raises(ValueError, match="cancel reason code must be nonempty"):
        CancelRequest(str(uuid4()), str(uuid4()), 0, CancellationSource.USER, reason_code=value)
    with pytest.raises(ValueError, match="owned process birth token must be nonempty"):
        OwnedProcessHandle(str(uuid4()), 0, 1, value, ProcessContainment.DIRECT_CHILD)


def test_valid_values_are_not_coerced_or_trimmed():
    class IdentityInt(int):
        pass

    number = IdentityInt(0)
    text = " endpoint "
    assert require_integer(number, minimum=0, message="bad") is number
    assert require_nonempty_string(text, message="bad") is text
    assert EndpointArbiter(text).endpoint_id == text
    assert EpochFence(number).current is number


def test_collection_validators_return_typed_copies_without_coercion():
    values = ["one", "two"]
    mapping = {"name": object()}

    assert require_string_tuple(values, message="bad") == ("one", "two")
    assert require_exact_keys(mapping, {"name"}, message="bad") is mapping


@pytest.mark.parametrize("value", [None, False, 0, ["ok", ""], ["ok", 1]])
def test_string_tuple_validator_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid strings"):
        require_string_tuple(value, message="invalid strings")


@pytest.mark.parametrize("value", [None, False, [], {"wrong": "value"}, {"name": 1, "extra": 2}])
def test_exact_keys_validator_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid mapping"):
        require_exact_keys(value, {"name"}, message="invalid mapping")


@pytest.mark.parametrize("value", [None, False, -1, -0.5, "0", [], float("nan"), float("inf")])
def test_nonnegative_number_validator_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid timeout"):
        require_nonnegative_number(value, message="invalid timeout")


@pytest.mark.parametrize("value", [None, False, "", "g" * 64, "a" * 63, "A" * 64])
def test_sha256_validator_rejects_malformed_values(value):
    with pytest.raises(ValueError, match="invalid digest"):
        require_sha256(value, message="invalid digest")


def test_new_scalar_validators_preserve_valid_values():
    digest = "a" * 64
    timeout = 0.5
    assert require_sha256(digest, message="bad") is digest
    assert require_nonnegative_number(timeout, message="bad") is timeout


@pytest.mark.parametrize("value", ["build_target:0", b"build_target:0"])
def test_string_tuple_validator_refuses_a_bare_string_instead_of_splitting_it(value):
    with pytest.raises(ValueError, match="invalid ids"):
        require_string_tuple(value, message="invalid ids")
