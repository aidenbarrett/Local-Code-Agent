"""``ProcessTreeJob.terminate_and_confirm`` proves each member ended (#445).

Windows CI showed the job's ``ActiveProcesses`` at zero while a member's process handle
was still unsignaled, so the accounting count alone over-claimed cleanup. These run the
real algorithm on any host against a scripted kernel: the native regression that a real
descendant is signaled lives in ``test_runtime_preflight.py`` and runs on Windows CI.
"""
from __future__ import annotations

import ctypes
from typing import Any

import pytest

from local_agent.tools import windows_job

GONE = "gone"            # OpenProcess -> ERROR_INVALID_PARAMETER
DENIED = "denied"        # OpenProcess -> ERROR_ACCESS_DENIED


class FakeKernel:
    """A scripted job: members, per-member signal times, late joiners, accounting."""

    def __init__(self, members, *, signal_after=None, open_result=None, late=(),
                 accounting_lag=0, list_short_once=False):
        self.members = list(members)
        self.signal_after = dict(signal_after or {})   # pid -> waits before signaled
        self.open_result = dict(open_result or {})     # pid -> GONE | DENIED
        self.late = list(late)                         # joiners after first terminate
        self.accounting_lag = accounting_lag
        self.list_short_once = list_short_once
        self.terminated = False
        self.terminate_calls = 0
        self.handles: dict[int, int] = {}
        self.open_handles: set[int] = set()
        self.wait_batches: list[tuple[int, int]] = []
        self.last_error = 0
        self.list_queries = 0

    # -- QueryInformationJobObject
    def QueryInformationJobObject(self, handle, cls, ptr, size, retlen):  # noqa: N802
        info: Any = ptr._obj
        if cls == windows_job._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS:
            live = [p for p in self.members if not self._signaled(p)]
            if not live and self.accounting_lag:
                self.accounting_lag -= 1
                info.ActiveProcesses = 1
            else:
                info.ActiveProcesses = len(live)
            return True
        assert cls == windows_job._JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS
        self.list_queries += 1
        live = [p for p in self.members if not self._signaled(p)]
        capacity = len(info.ProcessIdList)
        info.NumberOfAssignedProcesses = len(live)
        shown = live[:capacity]
        if self.list_short_once and self.list_queries == 1 and live:
            shown = live[:1]      # the job grew between calls: a partial list
        info.NumberOfProcessIdsInList = len(shown)
        for i, pid in enumerate(shown):
            info.ProcessIdList[i] = pid
        if len(shown) < len(live):
            self.last_error = windows_job._ERROR_MORE_DATA
            return False
        return True

    def TerminateJobObject(self, handle, code):  # noqa: N802
        self.terminate_calls += 1
        self.terminated = True
        if self.late:
            self.members.extend(self.late)   # a child spawned during termination
            self.late = []
        return True

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        assert access == windows_job._SYNCHRONIZE
        result = self.open_result.get(pid)
        if result == GONE:
            self.last_error = windows_job._ERROR_INVALID_PARAMETER
            return None
        if result == DENIED:
            self.last_error = 5
            return None
        handle = 1000 + pid
        self.handles[handle] = pid
        self.open_handles.add(handle)
        return handle

    def WaitForMultipleObjects(self, count, array, wait_all, ms):  # noqa: N802
        assert wait_all and count <= windows_job._MAXIMUM_WAIT_OBJECTS
        self.wait_batches.append((count, ms))
        pids = [self.handles[array[i]] for i in range(count)]
        for pid in pids:
            if pid in self.signal_after and self.signal_after[pid] > 0:
                self.signal_after[pid] -= 1
        if all(self._signaled(pid) for pid in pids):
            return windows_job._WAIT_OBJECT_0
        return 0x102  # WAIT_TIMEOUT

    def CloseHandle(self, handle):  # noqa: N802
        self.open_handles.discard(handle)
        return True

    def _signaled(self, pid):
        return self.terminated and self.signal_after.get(pid, 0) == 0


def _job(kernel: FakeKernel, monkeypatch) -> windows_job.ProcessTreeJob:
    monkeypatch.setattr(windows_job, "_last_error", lambda: kernel.last_error)
    job = object.__new__(windows_job.ProcessTreeJob)
    job._k32 = kernel
    job._handle = 1
    return job


def test_confirms_only_when_every_member_handle_is_signaled(monkeypatch):
    kernel = FakeKernel([11, 12], signal_after={12: 1})
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is True
    assert kernel.open_handles == set()


def test_accounting_at_zero_is_not_proof_while_a_handle_is_unsignaled(monkeypatch):
    # The #445 Windows shape: the member never signals within the bound.
    kernel = FakeKernel([11], signal_after={11: 10_000})
    monkeypatch.setattr(FakeKernel, "QueryInformationJobObject",
                        _accounting_says_zero(FakeKernel.QueryInformationJobObject))
    assert _job(kernel, monkeypatch).terminate_and_confirm(0.05) is False
    assert kernel.open_handles == set()


def _accounting_says_zero(original):
    def query(self, handle, cls, ptr, size, retlen):
        if cls == windows_job._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS:
            ptr._obj.ActiveProcesses = 0
            return True
        return original(self, handle, cls, ptr, size, retlen)
    return query


def test_a_partial_process_id_list_is_grown_and_retried(monkeypatch):
    members = list(range(100, 140))           # more than the first 16-slot buffer
    kernel = FakeKernel(members, list_short_once=True)
    job = _job(kernel, monkeypatch)
    assert set(job.member_pids()) == set(members)
    assert kernel.list_queries >= 2


def test_a_job_that_never_lists_completely_cannot_be_confirmed(monkeypatch):
    kernel = FakeKernel([11, 12])

    def always_short(self, handle, cls, ptr, size, retlen):
        if cls == windows_job._JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS:
            ptr._obj.NumberOfAssignedProcesses = len(ptr._obj.ProcessIdList) + 1
            ptr._obj.NumberOfProcessIdsInList = 0
            self.last_error = windows_job._ERROR_MORE_DATA
            return False
        return True

    monkeypatch.setattr(FakeKernel, "QueryInformationJobObject", always_short)
    job = _job(kernel, monkeypatch)
    with pytest.raises(windows_job.JobContainmentError, match="never became complete"):
        job.member_pids()
    assert job.terminate_and_confirm(5) is False


def test_a_member_that_joins_during_termination_is_tracked_and_ended(monkeypatch):
    kernel = FakeKernel([11], late=[21], signal_after={21: 1})
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is True
    assert 1021 in kernel.handles             # the late joiner got its own handle
    assert kernel.terminate_calls == 2        # and the job was terminated again
    assert kernel.open_handles == set()


def test_more_than_64_members_wait_in_batches_under_one_deadline(monkeypatch):
    # Each wait batch costs one second of a fake clock: the budgets must shrink
    # from one absolute deadline, never restart at the full timeout per batch.
    clock = {"t": 0.0}
    monkeypatch.setattr(windows_job.time, "monotonic", lambda: clock["t"])
    original = FakeKernel.WaitForMultipleObjects

    def costly(self, count, array, wait_all, ms):
        result = original(self, count, array, wait_all, ms)
        clock["t"] += 1.0
        return result

    monkeypatch.setattr(FakeKernel, "WaitForMultipleObjects", costly)
    kernel = FakeKernel(list(range(1, 151)))
    _job(kernel, monkeypatch).terminate_and_confirm(2.5)
    sizes = [count for count, _ms in kernel.wait_batches]
    assert sizes[:3] == [64, 64, 22]
    assert [ms for _count, ms in kernel.wait_batches[:3]] == [2500, 1500, 500]


def test_an_already_exited_member_is_not_a_failure(monkeypatch):
    kernel = FakeKernel([11, 12], open_result={12: GONE})
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is True


def test_any_other_open_failure_is_unconfirmed(monkeypatch):
    kernel = FakeKernel([11, 12], open_result={12: DENIED})
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is False
    assert kernel.open_handles == set()


def test_timeout_is_unconfirmed_and_closes_every_handle(monkeypatch):
    kernel = FakeKernel([11, 12, 13], signal_after={13: 10_000})
    assert _job(kernel, monkeypatch).terminate_and_confirm(0.05) is False
    assert kernel.open_handles == set()


def test_accounting_that_trails_the_handles_is_waited_for_within_the_bound(monkeypatch):
    kernel = FakeKernel([11], accounting_lag=3)
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is True
    kernel = FakeKernel([11], accounting_lag=10_000)
    assert _job(kernel, monkeypatch).terminate_and_confirm(0.1) is False


def test_a_query_failure_is_unconfirmed_and_closes_every_handle(monkeypatch):
    kernel = FakeKernel([11, 12])
    calls = {"n": 0}
    original = FakeKernel.QueryInformationJobObject

    def failing(self, handle, cls, ptr, size, retlen):
        if cls == windows_job._JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS:
            calls["n"] += 1
            if calls["n"] == 2:                   # the post-termination snapshot
                self.last_error = 6
                return False
        return original(self, handle, cls, ptr, size, retlen)

    monkeypatch.setattr(FakeKernel, "QueryInformationJobObject", failing)
    assert _job(kernel, monkeypatch).terminate_and_confirm(5) is False
    assert kernel.open_handles == set()


def test_process_id_list_layout_matches_windows_x64():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("layout pinned for 64-bit hosts")
    layout = windows_job._process_id_list_type(4)
    assert layout.ProcessIdList.offset == 8
    assert ctypes.sizeof(layout) == 8 + 4 * 8


# ------------------------------------------------------------------ runner settlement


class _CountingJob:
    """A job whose accounting reports ``count`` and whose proof answers ``proved``."""

    def __init__(self, count, proved):
        self.count, self.proved, self.proof_calls = count, proved, 0

    def active_processes(self):
        return self.count

    def terminate_and_confirm(self, timeout_s):
        self.proof_calls += 1
        return self.proved


def _settle(job):
    from local_agent.tools import process_runner

    tree = process_runner._Tree(job, "job_object")
    process_runner._end_strays(None, tree)
    return tree


def test_a_zero_count_after_a_normal_exit_still_needs_the_member_proof():
    # Accounting says the job is empty, but a member is still running: not proved.
    tree = _settle(_CountingJob(count=0, proved=False))
    assert tree.stray_descendants == 0
    assert tree.cleanup_confirmed is False
    assert tree.strays_unconfirmed is True


def test_a_proved_empty_job_after_a_normal_exit_claims_nothing_extra():
    job = _CountingJob(count=0, proved=True)
    tree = _settle(job)
    assert job.proof_calls == 1
    assert tree.cleanup_confirmed is None and tree.strays_unconfirmed is False


def test_strays_are_confirmed_only_by_the_member_proof():
    tree = _settle(_CountingJob(count=2, proved=True))
    assert tree.stray_descendants == 2 and tree.cleanup_confirmed is True
    tree = _settle(_CountingJob(count=2, proved=False))
    assert tree.cleanup_confirmed is False and tree.strays_unconfirmed is True


# ------------------------------------------------------------------ evidence after the bound


def _clocked(monkeypatch, *, slow_list_query=None, slow_accounting=False, cost=2.0):
    """Patch the module clock and make one chosen observation cost ``cost`` seconds."""
    clock = {"t": 0.0}
    monkeypatch.setattr(windows_job.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(windows_job.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    original = FakeKernel.QueryInformationJobObject
    seen = {"lists": 0}

    def query(self, handle, cls, ptr, size, retlen):
        result = original(self, handle, cls, ptr, size, retlen)
        if cls == windows_job._JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS and result:
            seen["lists"] += 1
            if seen["lists"] == slow_list_query:
                clock["t"] += cost
        if cls == windows_job._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS and slow_accounting:
            clock["t"] += cost
        return result

    monkeypatch.setattr(FakeKernel, "QueryInformationJobObject", query)
    return clock


def test_a_snapshot_completed_after_the_bound_is_not_proof(monkeypatch):
    # Astra's case on 9a73978: the first complete PID snapshot runs 0 s -> 2 s under
    # a 1 s bound; every later observation is clean. It must not confirm.
    _clocked(monkeypatch, slow_list_query=1)
    kernel = FakeKernel([11])
    assert _job(kernel, monkeypatch).terminate_and_confirm(1.0) is False
    assert kernel.open_handles == set()


def test_a_final_snapshot_completed_after_the_bound_is_not_proof(monkeypatch):
    _clocked(monkeypatch, slow_list_query=2)       # the post-termination snapshot
    kernel = FakeKernel([11])
    assert _job(kernel, monkeypatch).terminate_and_confirm(1.0) is False


def test_final_accounting_completed_after_the_bound_is_not_proof(monkeypatch):
    _clocked(monkeypatch, slow_accounting=True)
    kernel = FakeKernel([11])
    assert _job(kernel, monkeypatch).terminate_and_confirm(1.0) is False


def test_the_same_observations_inside_the_bound_confirm(monkeypatch):
    _clocked(monkeypatch, slow_list_query=1, cost=0.5)
    kernel = FakeKernel([11])
    assert _job(kernel, monkeypatch).terminate_and_confirm(1.0) is True


def test_only_wait_object_0_counts_as_all_signaled(monkeypatch):
    kernel = FakeKernel([11, 12])
    original = FakeKernel.WaitForMultipleObjects

    def index_result(self, count, array, wait_all, ms):
        result = original(self, count, array, wait_all, ms)
        return 1 if result == windows_job._WAIT_OBJECT_0 else result

    monkeypatch.setattr(FakeKernel, "WaitForMultipleObjects", index_result)
    assert _job(kernel, monkeypatch).terminate_and_confirm(1.0) is False
