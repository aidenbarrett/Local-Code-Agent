from __future__ import annotations

import signal
from uuid import uuid4

from local_agent.session.cancellation import OwnedProcessHandle, ProcessContainment
from local_agent.session.process_stop import PosixProcessGroupStopper


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _handle(*, containment=ProcessContainment.POSIX_PROCESS_GROUP, birth_token="123:1.000000"):
    kwargs = {
        "task_id": str(uuid4()),
        "execution_epoch": 0,
        "pid": 123,
        "birth_token": birth_token,
        "containment": containment,
    }
    if containment is ProcessContainment.POSIX_PROCESS_GROUP:
        kwargs["containment_id"] = 123
    return OwnedProcessHandle(**kwargs)


def _stopper(*, birth_token=lambda _pid: "123:1.000000", alive=None, signal_group=None):
    clock = FakeClock()
    return PosixProcessGroupStopper(
        birth_token=birth_token,
        group_alive=alive or (lambda _pgid: True),
        signal_group=signal_group or (lambda _pgid, _sig: None),
        clock=clock.clock,
        sleeper=clock.sleep,
        poll_interval_s=0.1,
    ), clock


def test_unsupported_containment_is_not_signalled_or_reported_stopped():
    signals = []
    stopper, _clock = _stopper(signal_group=lambda pgid, sig: signals.append((pgid, sig)))
    result = stopper.stop(_handle(containment=ProcessContainment.DIRECT_CHILD))
    assert result.attempted is False
    assert result.known_stopped is False
    assert result.reason == "unsupported_containment"
    assert signals == []


def test_already_absent_group_is_known_stopped_without_signal():
    signals = []
    stopper, _clock = _stopper(
        alive=lambda _pgid: False,
        signal_group=lambda pgid, sig: signals.append((pgid, sig)),
    )
    result = stopper.stop(_handle())
    assert result.known_stopped is True
    assert result.reason == "already_stopped"
    assert signals == []


def test_missing_or_changed_owner_identity_refuses_to_signal_live_group():
    for token, reason in [
        (None, "owner_identity_unavailable"),
        ("123:2.000000", "owner_identity_changed"),
    ]:
        signals = []
        stopper, _clock = _stopper(
            birth_token=lambda _pid, token=token: token,
            alive=lambda _pgid: True,
            signal_group=lambda pgid, sig: signals.append((pgid, sig)),
        )
        result = stopper.stop(_handle())
        assert result.attempted is False
        assert result.known_stopped is False
        assert result.reason == reason
        assert signals == []


def test_term_success_proves_group_stopped_without_kill():
    signals = []
    checks = {"count": 0}

    def alive(_pgid):
        checks["count"] += 1
        return checks["count"] < 3

    stopper, _clock = _stopper(
        alive=alive,
        signal_group=lambda pgid, sig: signals.append((pgid, sig)),
    )
    result = stopper.stop(_handle(), term_grace_s=1, kill_grace_s=1)
    assert result.known_stopped is True
    assert result.reason == "stopped_after_term"
    assert result.term_sent is True
    assert result.kill_sent is False
    assert signals == [(123, signal.SIGTERM)]


def test_term_timeout_escalates_to_kill_and_requires_observed_group_exit():
    sigkill = getattr(signal, "SIGKILL", 9)
    signals = []
    killed = {"value": False}

    def signal_group(pgid, sig):
        signals.append((pgid, sig))
        if sig == sigkill:
            killed["value"] = True

    def alive(_pgid):
        return not killed["value"]

    stopper, _clock = _stopper(alive=alive, signal_group=signal_group)
    result = stopper.stop(_handle(), term_grace_s=0.2, kill_grace_s=0.2)
    assert result.known_stopped is True
    assert result.reason == "stopped_after_kill"
    assert result.term_sent is True
    assert result.kill_sent is True
    assert signals == [(123, signal.SIGTERM), (123, sigkill)]


def test_group_still_alive_after_kill_is_not_cleanup_proof():
    signals = []
    stopper, _clock = _stopper(
        alive=lambda _pgid: True,
        signal_group=lambda pgid, sig: signals.append((pgid, sig)),
    )
    result = stopper.stop(_handle(), term_grace_s=0.1, kill_grace_s=0.1)
    assert result.known_stopped is False
    assert result.reason == "process_group_still_alive"
    assert result.kill_sent is True


def test_signal_permission_failure_is_not_reported_as_stopped():
    def denied(_pgid, _sig):
        raise PermissionError("no permission")

    stopper, _clock = _stopper(alive=lambda _pgid: True, signal_group=denied)
    result = stopper.stop(_handle())
    assert result.attempted is True
    assert result.known_stopped is False
    assert result.reason == "term_signal_failed"
