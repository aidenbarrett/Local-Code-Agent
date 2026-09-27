#include <doctest/doctest.h>

#include <thread>

#include "fixture_backend.hpp"
#include "inference_scheduler.hpp"

using namespace lca;
using namespace std::chrono_literals;

namespace {

FixtureConfig fixture(std::optional<std::string> reply, std::chrono::milliseconds delay = 1ms,
                      std::uint64_t context = 64) {
    FixtureConfig c;
    c.model_id = "fixture";
    c.max_context_tokens = context;
    c.piece_delay = delay;
    c.default_reply = std::move(reply);
    return c;
}

InferenceJob job(const std::string& id, const std::string& prompt, std::shared_ptr<RequestChannel> channel) {
    InferenceJob j;
    j.request.request_id = id;
    j.request.prompt = prompt;
    j.channel = std::move(channel);
    return j;
}

struct Drained {
    std::optional<AdmittedFacts> admitted;
    std::string content;
    std::optional<FinishedFacts> finished;
    std::optional<FailureFacts> failure;
};

Drained drain(RequestChannel& channel) {
    Drained d;
    for (int i = 0; i < 400; ++i) {
        auto e = channel.pop(25ms);
        if (!e) continue;
        if (e->kind == ChannelEvent::Kind::Admitted) d.admitted = e->admitted;
        if (e->kind == ChannelEvent::Kind::Parts) {
            for (auto& p : e->parts) d.content += p.text;
        }
        if (e->kind == ChannelEvent::Kind::Finished) {
            d.finished = e->finished;
            return d;
        }
        if (e->kind == ChannelEvent::Kind::Failed) {
            d.failure = e->failure;
            return d;
        }
    }
    return d;
}

bool eventually(const std::function<bool()>& condition);

bool is_running(const InferenceScheduler& scheduler, const std::string& id) {
    const auto active = scheduler.active_requests();
    return !active.empty() && active[0].id == id && active[0].state == "running";
}

bool eventually(const std::function<bool()>& condition) {
    for (int i = 0; i < 400; ++i) {
        if (condition()) return true;
        std::this_thread::sleep_for(5ms);
    }
    return false;
}

}  // namespace

TEST_CASE("a request is admitted, streamed and finished") {
    FixtureBackend backend(fixture(std::string("one two three")), "test");
    InferenceScheduler scheduler(backend, {});
    auto channel = std::make_shared<RequestChannel>();
    REQUIRE(scheduler.submit(job("r1", "a b c", channel)));
    const auto d = drain(*channel);
    REQUIRE(d.admitted);
    CHECK(d.admitted->prompt_tokens == 3);
    CHECK(d.content == "one two three");
    REQUIRE(d.finished);
    CHECK(d.finished->outcome.finish == FinishReason::EndOfSequence);
    CHECK(d.finished->outcome.stats.completion_tokens > 0);
    CHECK(eventually([&] { return scheduler.active_requests().empty(); }));
    CHECK(scheduler.last_request()->request_id == "r1");
    CHECK(scheduler.counters().completed == 1);
}

TEST_CASE("max_new_tokens is clamped to the context room and reported as length") {
    FixtureBackend backend(fixture(std::nullopt, 0ms, 10), "test");
    InferenceScheduler scheduler(backend, {});
    auto channel = std::make_shared<RequestChannel>();
    auto j = job("r1", "a b c d e f", channel);
    j.requested_max_tokens = 100;
    REQUIRE(scheduler.submit(std::move(j)));
    const auto d = drain(*channel);
    REQUIRE(d.admitted);
    CHECK(d.admitted->max_new_tokens == 4);
    REQUIRE(d.finished);
    CHECK(d.finished->outcome.finish == FinishReason::Length);
    CHECK(d.finished->outcome.stats.completion_tokens == 4);
}

TEST_CASE("a prompt that fills the context is refused before generation") {
    FixtureBackend backend(fixture(std::string("x"), 0ms, 3), "test");
    InferenceScheduler scheduler(backend, {});
    auto channel = std::make_shared<RequestChannel>();
    REQUIRE(scheduler.submit(job("r1", "a b c", channel)));
    const auto d = drain(*channel);
    REQUIRE(d.failure);
    CHECK(d.failure->kind == BackendErrorKind::ContextExceeded);
    CHECK_FALSE(d.admitted);
    CHECK(scheduler.counters().rejected_context == 1);
}

TEST_CASE("a stop string ends generation and is not emitted") {
    FixtureBackend backend(fixture(std::nullopt), "test");  // endless filler
    InferenceScheduler scheduler(backend, {});
    auto channel = std::make_shared<RequestChannel>();
    auto j = job("r1", "go", channel);
    j.stop_strings = {"dolor"};
    j.requested_max_tokens = 1000;
    REQUIRE(scheduler.submit(std::move(j)));
    const auto d = drain(*channel);
    REQUIRE(d.finished);
    CHECK(d.finished->outcome.finish == FinishReason::StoppedBySink);
    CHECK(d.finished->stop_string_matched);
    CHECK(d.content == "lorem ipsum ");
}

TEST_CASE("cancelling a running request stops the backend and only then leaves the active set") {
    FixtureBackend backend(fixture(std::nullopt, 5ms), "test");
    InferenceScheduler scheduler(backend, {});
    auto channel = std::make_shared<RequestChannel>();
    auto j = job("r1", "go", channel);
    j.requested_max_tokens = 100000;
    REQUIRE(scheduler.submit(std::move(j)));
    REQUIRE(eventually([&] { return is_running(scheduler, "r1"); }));
    scheduler.cancel("r1", channel);
    const auto d = drain(*channel);
    REQUIRE(d.finished);
    CHECK(d.finished->outcome.finish == FinishReason::Cancelled);
    CHECK(eventually([&] { return scheduler.active_requests().empty(); }));
    CHECK(scheduler.counters().cancelled == 1);
}

TEST_CASE("queued requests wait, can be cancelled in place, and the queue is bounded") {
    FixtureBackend backend(fixture(std::nullopt, 5ms), "test");
    SchedulerOptions options;
    options.max_queued_requests = 1;
    InferenceScheduler scheduler(backend, options);

    auto running = std::make_shared<RequestChannel>();
    auto r = job("running", "go", running);
    r.requested_max_tokens = 100000;
    REQUIRE(scheduler.submit(std::move(r)));
    REQUIRE(eventually([&] { return is_running(scheduler, "running"); }));

    auto waiting = std::make_shared<RequestChannel>();
    REQUIRE(scheduler.submit(job("waiting", "go", waiting)));
    auto overflow = std::make_shared<RequestChannel>();
    CHECK_FALSE(scheduler.submit(job("overflow", "go", overflow)));
    CHECK(scheduler.counters().rejected_busy == 1);

    const auto active = scheduler.active_requests();
    REQUIRE(active.size() == 2);
    CHECK(active[1].id == "waiting");
    CHECK(active[1].state == "queued");

    scheduler.cancel("waiting", waiting);
    CHECK(scheduler.active_requests().size() == 1);  // removed immediately, not at the front of the queue
    const auto d = drain(*waiting);
    REQUIRE(d.finished);
    CHECK(d.finished->outcome.finish == FinishReason::Cancelled);

    scheduler.cancel("running", running);
    const auto stopped = drain(*running);
    REQUIRE(stopped.finished);
    CHECK(stopped.finished->outcome.finish == FinishReason::Cancelled);
}

TEST_CASE("shutdown cancels the running request and fails queued ones") {
    FixtureBackend backend(fixture(std::nullopt, 5ms), "test");
    InferenceScheduler scheduler(backend, {});
    auto running = std::make_shared<RequestChannel>();
    auto r = job("a", "go", running);
    r.requested_max_tokens = 100000;
    REQUIRE(scheduler.submit(std::move(r)));
    REQUIRE(eventually([&] { return is_running(scheduler, "a"); }));
    auto queued = std::make_shared<RequestChannel>();
    REQUIRE(scheduler.submit(job("b", "go", queued)));
    scheduler.shutdown();
    const auto first = drain(*running);
    REQUIRE(first.finished);
    CHECK(first.finished->outcome.finish == FinishReason::Cancelled);
    const auto second = drain(*queued);
    REQUIRE(second.failure);
    CHECK(second.failure->kind == BackendErrorKind::Unavailable);
    CHECK_FALSE(scheduler.submit(job("c", "go", std::make_shared<RequestChannel>())));
}
