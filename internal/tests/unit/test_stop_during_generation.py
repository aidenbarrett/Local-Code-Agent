"""Stop during model generation (#403 item 4).

Stop fences the endpoint lease at once. On a profile whose endpoint can prove it
stopped (llama-server ``/metrics``), Stop also cuts the streamed response, once the
server has been seen working on it, and the endpoint is released only when the server
is observed idle. Closing the connection is never the proof. Without a proof source,
the call runs to completion and that normal return is the proof, as before.
"""
from __future__ import annotations

import contextlib
import socket
import threading
import time
import types
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from local_agent.agent import Orchestrator, SkillLibrary
from local_agent.agent.outcome import Outcome
from local_agent.agent.state import HaltCause, Validity
from local_agent.config import MODEL_PRESETS, ModelConfig, load_repo_config
from local_agent.llm.client import (
    LlamaCppMetricsStopProof,
    OpenAICompatibleClient,
    ScriptedClient,
    StreamInterrupt,
    parse_llamacpp_idle,
    stop_proof_for,
)
from local_agent.llm.protocol import InferenceInterruptedError
from local_agent.session.endpoint_call import EndpointCallAdapter
from local_agent.session.endpoint_client import ManagedLLMClient
from local_agent.session.endpoint_lease import EndpointArbiter, EndpointRole
from local_agent.session.endpoint_runtime import EndpointRuntime
from local_agent.session.endpoint_stop_proof import await_idle
from local_agent.tools import build_registry
from serving import serve

REPO = Path(__file__).resolve().parent.parent.parent
BOUND_S = 5.0
MESSAGES = [{"role": "user", "content": "explain the repository"}]


# ------------------------------------------------------------------ proof source


IDLE = "# HELP x\nllamacpp:requests_processing 0\nllamacpp:requests_deferred 0\n"


@pytest.mark.parametrize(("text", "expected"), [
    (IDLE, True),
    ("llamacpp:requests_processing 1\nllamacpp:requests_deferred 0\n", False),
    ("llamacpp:requests_processing 0\nllamacpp:requests_deferred 2\n", False),
    ("llamacpp:requests_processing 0.0\nllamacpp:requests_deferred 0.0\n", True),
    # A gauge that is not there proves nothing, even if the other says idle.
    ("llamacpp:requests_processing 0\n", None),
    ("llamacpp:requests_processing zero\nllamacpp:requests_deferred 0\n", None),
    ("", None),
])
def test_only_a_clean_llamacpp_idle_reading_is_proof(text, expected):
    assert parse_llamacpp_idle(text) is expected


def test_an_unreadable_metrics_endpoint_proves_nothing():
    proof = LlamaCppMetricsStopProof("http://127.0.0.1:1/metrics", settle_timeout_s=1,
                                     fetch=lambda url, timeout: None)
    assert proof.idle(1.0) is None
    proof = LlamaCppMetricsStopProof("http://127.0.0.1:1/metrics", settle_timeout_s=1,
                                     fetch=lambda url, timeout: b"\xff\xfe")
    assert proof.idle(1.0) is None


def test_the_proof_reads_a_real_metrics_endpoint_directly(monkeypatch):
    import http.server

    replies = {"/metrics": (200, IDLE.encode())}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            status, body = replies.get(self.path, (404, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # A proxy must never answer for the server being proved.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    try:
        config = replace(MODEL_PRESETS["nuc-llama-8b"],
                         base_url=f"http://127.0.0.1:{httpd.server_port}/v1")
        proof = stop_proof_for(config)
        assert proof is not None and proof.idle(2.0) is True
        replies["/metrics"] = (501, b"metrics are disabled")   # started without --metrics
        assert proof.idle(1.0) is None
    finally:
        httpd.shutdown()
        httpd.server_close()


class _ClockedProof:
    """A proof on a fake clock: each observation takes ``cost`` seconds, capped by the
    timeout it is given, and returns the next scripted reading."""

    kind = "llamacpp_metrics"

    def __init__(self, clock, readings, *, settle_timeout_s, cost=0.0, overruns=False):
        self.clock = clock
        self.readings = iter(readings)
        self.settle_timeout_s = settle_timeout_s
        self.cost = cost
        self.overruns = overruns      # an observation that completes past its timeout
        self.timeouts: list[float] = []

    def idle(self, timeout_s):
        self.timeouts.append(timeout_s)
        if self.cost > timeout_s and not self.overruns:
            self.clock["t"] += timeout_s      # the fetch gave up at its bound
            return None
        self.clock["t"] += self.cost
        return next(self.readings)


def _await(proof, clock):
    def sleep(s):
        clock["t"] += s
    return await_idle(proof, clock=lambda: clock["t"], sleep=sleep)


def test_await_idle_needs_an_idle_reading_inside_the_bound():
    clock = {"t": 0.0}
    proof = _ClockedProof(clock, [None, False, True], settle_timeout_s=10.0)
    assert _await(proof, clock) is True

    clock = {"t": 0.0}
    proof = _ClockedProof(clock, iter(lambda: False, None), settle_timeout_s=10.0)
    assert _await(proof, clock) is False
    assert clock["t"] == pytest.approx(10.0)  # gave up at the bound, not after it


def test_an_idle_reading_completed_after_the_bound_is_not_proof():
    # Astra's case on 1c3b435: one observation runs from 0 s to 2 s against a 1 s
    # bound and reports idle. It completed late, so it proves nothing.
    clock = {"t": 0.0}
    proof = _ClockedProof(clock, [True], settle_timeout_s=1.0, cost=2.0, overruns=True)
    assert _await(proof, clock) is False
    # A source that respects its timeout gives up at the bound instead.
    clock = {"t": 0.0}
    proof = _ClockedProof(clock, [True], settle_timeout_s=1.0, cost=2.0)
    assert _await(proof, clock) is False
    assert clock["t"] <= 1.0 + 1e-9


def test_every_observation_and_pause_is_clamped_to_the_remaining_budget():
    clock = {"t": 0.0}
    proof = _ClockedProof(clock, iter(lambda: False, None), settle_timeout_s=0.6, cost=0.2)
    assert _await(proof, clock) is False
    assert clock["t"] <= 0.6 + 1e-9
    spent = 0.0
    for timeout in proof.timeouts:
        assert timeout <= 0.6 - spent + 1e-9
        spent += 0.2 + 0.25            # the observation, then one full pause
    assert proof.timeouts[-1] < 0.6     # the last fetch got only what was left


def test_the_metrics_fetch_bounds_the_whole_exchange(monkeypatch):
    # A server that answers headers, then dribbles the body slower than the bound.
    import http.server

    class Dribble(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            for _ in range(20):
                self.wfile.write(b"#")
                self.wfile.flush()
                time.sleep(0.1)

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Dribble)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        config = replace(MODEL_PRESETS["nuc-llama-8b"],
                         base_url=f"http://127.0.0.1:{httpd.server_port}/v1")
        proof = stop_proof_for(config)
        assert proof is not None
        started = time.monotonic()
        assert proof.idle(0.5) is None
        assert time.monotonic() - started < 1.5   # not 20 reads x the per-read timeout
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_profiles_declare_their_proof_source():
    for name in ("nuc-llama-30b", "nuc-llama-8b"):
        config = MODEL_PRESETS[name]
        proof = stop_proof_for(config)
        assert isinstance(proof, LlamaCppMetricsStopProof)
        assert proof.metrics_url == "http://127.0.0.1:8080/metrics"
        assert proof.settle_timeout_s == config.request_deadline_s
        assert config.identity()["stop_proof"] == "llamacpp_metrics"
    # OVMS has no proof source here yet: Stop keeps waiting for the call to return.
    for name, config in MODEL_PRESETS.items():
        if config.runtime != "llamacpp":
            assert config.stop_proof == "none", name
            assert stop_proof_for(config) is None


def test_an_unknown_or_unreachable_proof_source_is_refused():
    with pytest.raises(ValueError, match="unknown stop_proof"):
        stop_proof_for(replace(ModelConfig(), stop_proof="socket_closed"))
    with pytest.raises(ValueError, match="needs an http base_url"):
        stop_proof_for(replace(ModelConfig(), base_url="https://127.0.0.1:8080/v1",
                               stop_proof="llamacpp_metrics"))
    with pytest.raises(ValueError, match="needs an http base_url"):
        stop_proof_for(replace(ModelConfig(), base_url="unix:///run/llama.sock",
                               stop_proof="llamacpp_metrics"))


def test_managed_llama_server_exposes_metrics(tmp_path):
    plan = serve.make_plan("llama", MODEL_PRESETS["nuc-llama-8b"], tmp_path)
    assert "--metrics" in plan["args"]


# ------------------------------------------------------------------ the cut itself


def test_a_stop_before_the_server_is_seen_working_never_touches_the_socket():
    ours, peer = socket.socketpair()
    try:
        interrupt = StreamInterrupt()
        interrupt.attach(_SocketStream(ours))
        interrupt.request()
        assert interrupt.cut is False
        peer.sendall(b"still open\n")
        assert ours.recv(64) == b"still open\n"
        interrupt.server_active()
        assert interrupt.cut is True
    finally:
        ours.close()
        peer.close()


def test_a_stop_wakes_a_read_blocked_on_the_server():
    ours, peer = socket.socketpair()
    try:
        interrupt = StreamInterrupt()
        interrupt.attach(_SocketStream(ours))
        interrupt.server_active()
        woke = threading.Event()

        def blocked_read():
            # POSIX returns end-of-stream; Windows fails the read on the closed socket.
            with contextlib.suppress(OSError):
                ours.recv(64)
            woke.set()

        reader = threading.Thread(target=blocked_read, daemon=True)
        reader.start()
        time.sleep(0.05)
        assert not woke.is_set()
        interrupt.request()
        assert woke.wait(BOUND_S)
    finally:
        ours.close()
        peer.close()


def test_on_windows_the_cut_also_closes_the_socket(monkeypatch):
    # Winsock does not wake a blocked recv on shutdown; closing it does.
    from local_agent.llm import client as client_mod

    monkeypatch.setattr(client_mod.sys, "platform", "win32")
    ours, peer = socket.socketpair()
    try:
        interrupt = StreamInterrupt()
        interrupt.attach(_SocketStream(ours))
        interrupt.server_active()
        interrupt.request()
        assert ours.fileno() == -1
    finally:
        ours.close()
        peer.close()


def test_a_detached_interrupt_never_shuts_a_pooled_connection():
    ours, peer = socket.socketpair()
    try:
        interrupt = StreamInterrupt()
        interrupt.attach(_SocketStream(ours))
        interrupt.server_active()
        interrupt.detach()
        interrupt.request()
        peer.sendall(b"reused\n")
        assert ours.recv(64) == b"reused\n"
    finally:
        ours.close()
        peer.close()


# ------------------------------------------------------------------ end to end


class _SocketStream:
    """An SDK-shaped stream reading one chunk per line from a real socket."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        network = types.SimpleNamespace(
            get_extra_info=lambda key: sock if key == "socket" else None)
        self.response = types.SimpleNamespace(extensions={"network_stream": network})
        self.closed = False

    def __iter__(self):
        buffer = b""
        while True:
            data = self._sock.recv(4096)
            if not data:
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                delta = types.SimpleNamespace(content=line.decode(), tool_calls=None)
                yield types.SimpleNamespace(
                    choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
                    usage=None)

    def close(self):
        self.closed = True


class _Proof:
    kind = "llamacpp_metrics"

    def __init__(self, readings, settle_timeout_s=BOUND_S):
        self._readings = readings
        self.settle_timeout_s = settle_timeout_s
        self.calls = 0

    def idle(self, timeout_s):
        assert timeout_s > 0
        self.calls += 1
        return self._readings(self.calls)


class _Server:
    """One streamed call over a socketpair; the test plays the server side."""

    def __init__(self, proof):
        self.ours, self.peer = socket.socketpair()
        self.stream = _SocketStream(self.ours)
        self.runtime = EndpointRuntime(EndpointArbiter("http://127.0.0.1:8080/v1"),
                                       stop_proof=proof)
        raw = OpenAICompatibleClient(MODEL_PRESETS["nuc-llama-8b"])
        raw._client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: self.stream)))
        self.task_id, self.epoch = str(uuid.uuid4()), 0
        self.client = ManagedLLMClient(
            raw, EndpointCallAdapter(self.runtime), role=EndpointRole.WORKER,
            session_id="s", task_id=self.task_id, execution_epoch=self.epoch,
        )
        self.outcome: dict[str, object] = {}
        self.done = threading.Event()

    def start(self):
        def run():
            try:
                self.outcome["value"] = self.client.chat(MESSAGES)
            except BaseException as exc:  # noqa: BLE001 - the test inspects it
                self.outcome["error"] = exc
            finally:
                self.done.set()
        threading.Thread(target=run, daemon=True).start()
        _wait_for(lambda: self.runtime.arbiter.active_lease is not None)

    def stop(self):
        return self.runtime.cancel_execution(self.task_id, self.epoch)

    def close(self):
        self.ours.close()
        self.peer.close()


def _wait_for(predicate, bound=BOUND_S):
    deadline = time.monotonic() + bound
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached within the bound"
        time.sleep(0.01)


def test_stop_cuts_a_generating_call_and_releases_the_endpoint_once_proved_idle():
    # Busy while the call is live, idle once the server has dropped it.
    server = _Server(_Proof(lambda n: False if n == 1 else True))
    try:
        server.start()
        server.peer.sendall(b"first token\n")      # the server is seen working
        time.sleep(0.05)
        assert server.stop() == ()                  # fenced, not dispatched-away
        assert server.runtime.arbiter.quarantined
        assert server.done.wait(BOUND_S), "Stop must end a generating call promptly"
        error = server.outcome.get("error")
        assert isinstance(error, InferenceInterruptedError)
        assert error.kind == "stopped"
        assert server.stream.closed
        # Proved idle, so the fence is lifted and the endpoint serves the next call.
        assert not server.runtime.arbiter.quarantined
        assert server.runtime.arbiter.active_lease is None
    finally:
        server.close()


def test_without_an_idle_reading_the_endpoint_stays_quarantined():
    server = _Server(_Proof(lambda n: False, settle_timeout_s=0.3))
    try:
        server.start()
        server.peer.sendall(b"first token\n")
        time.sleep(0.05)
        server.stop()
        assert server.done.wait(BOUND_S)
        assert isinstance(server.outcome.get("error"), InferenceInterruptedError)
        # The socket is closed, but nothing showed the server stopped.
        assert server.runtime.arbiter.quarantined
    finally:
        server.close()


def test_an_unobservable_proof_source_means_stop_waits_for_the_normal_return():
    server = _Server(_Proof(lambda n: None))
    try:
        server.start()
        server.peer.sendall(b"first token\n")
        time.sleep(0.05)
        server.stop()
        time.sleep(0.2)
        assert not server.done.is_set(), "no cut may be made that could not be proved"
        server.peer.sendall(b"rest of the answer\n")
        server.peer.shutdown(socket.SHUT_WR)       # the server finishes normally
        assert server.done.wait(BOUND_S)
        assert "value" in server.outcome
        assert not server.runtime.arbiter.quarantined  # the normal return was the proof
    finally:
        server.close()


def test_a_stop_during_prefill_cuts_only_once_the_server_is_seen_working():
    server = _Server(_Proof(lambda n: True))
    try:
        server.start()
        server.stop()                               # nothing streamed yet
        time.sleep(0.2)
        assert not server.done.is_set()
        assert server.runtime.arbiter.quarantined
        server.peer.sendall(b"first token\n")
        assert server.done.wait(BOUND_S)
        assert isinstance(server.outcome.get("error"), InferenceInterruptedError)
        assert not server.runtime.arbiter.quarantined
    finally:
        server.close()


def test_a_profile_without_proof_keeps_the_old_contract():
    server = _Server(None)
    try:
        server.start()
        server.peer.sendall(b"first token\n")
        time.sleep(0.05)
        server.stop()
        time.sleep(0.2)
        assert not server.done.is_set()
        server.peer.shutdown(socket.SHUT_WR)
        assert server.done.wait(BOUND_S)
        assert "value" in server.outcome
        assert not server.runtime.arbiter.quarantined
    finally:
        server.close()


def test_a_finished_call_leaves_no_stop_signal_behind():
    server = _Server(_Proof(lambda n: True))
    try:
        server.start()
        server.peer.sendall(b"answer\n")
        server.peer.shutdown(socket.SHUT_WR)
        assert server.done.wait(BOUND_S)
        assert "value" in server.outcome
        assert server.runtime._stop_signals == {}
    finally:
        server.close()


# ------------------------------------------------------------------ the worker's view


def test_a_stopped_model_call_is_its_own_halt_and_never_escalates(sandbox):
    def stopped(_messages):
        raise InferenceInterruptedError("Stop ended the model call while reading (3 chunks received)")

    repo = load_repo_config(sandbox.root)
    registry, _, _ = build_registry(repo)
    skills = SkillLibrary.discover(REPO / "skills")
    result = Orchestrator(repo, registry, ScriptedClient([stopped]), skills).run(
        "anything", skill_name="repo-navigation")
    assert result.state.halt_cause is HaltCause.STOPPED
    assert result.state.validity is Validity.INVALID_STOPPED
    assert result.outcome is Outcome.BLOCKED
    assert "stopped by the user" in (result.state.halt_reason or "")


def _watchers():
    return [t for t in threading.enumerate() if t.name == "lca-endpoint-stop" and t.is_alive()]


def test_no_stop_watcher_survives_a_call_stopped_or_not():
    for stop in (False, True):
        server = _Server(_Proof(lambda n: True))
        try:
            server.start()
            server.peer.sendall(b"first token\n")
            if stop:
                time.sleep(0.05)
                server.stop()
            else:
                server.peer.shutdown(socket.SHUT_WR)
            assert server.done.wait(BOUND_S)
            assert _watchers() == []
        finally:
            server.close()


def test_a_proof_source_that_hangs_cannot_hold_the_call_past_its_check_bound():
    from local_agent.session import endpoint_client

    class Hanging(_Proof):
        def idle(self, timeout_s):
            assert timeout_s <= endpoint_client._CUT_CHECK_S
            time.sleep(timeout_s)            # a fetch that runs to its bound
            return None                       # and could not observe

    server = _Server(Hanging(lambda n: None))
    try:
        server.start()
        server.peer.sendall(b"first token\n")
        time.sleep(0.05)
        server.stop()                         # watcher starts its bounded check
        server.peer.shutdown(socket.SHUT_WR)  # the call ends during that check
        started = time.monotonic()
        assert server.done.wait(BOUND_S)
        assert time.monotonic() - started < endpoint_client._CUT_CHECK_S + 1.0
        assert "value" in server.outcome      # never cut: proof was unobservable
        assert _watchers() == []
    finally:
        server.close()
