#include <doctest/doctest.h>

#include <atomic>
#include <thread>

#include "plugin_backend.hpp"

using namespace lca;

TEST_CASE("the reference C plugin loads through the ABI and describes itself") {
    PluginBackend backend(LCA_REFERENCE_PLUGIN_PATH,
                          R"({"model_id": "echo-test", "reply": "hello from C", "max_context_tokens": 99})");
    const auto& id = backend.identity();
    CHECK(id.kind == "plugin");
    CHECK(id.model_id == "echo-test");
    CHECK(id.runtime_name == "reference-echo");
    CHECK(id.max_context_tokens == 99);
    CHECK_FALSE(id.device_observed.has_value());
    CHECK_FALSE(id.compile_ms.has_value());
    CHECK(id.load_ms.has_value());  // measured by the endpoint when the plugin reports null
    CHECK(backend.count_prompt_tokens("one two  three") == 3);
}

TEST_CASE("plugin generation streams through the sink and reports stats") {
    PluginBackend backend(LCA_REFERENCE_PLUGIN_PATH, R"({"reply": "hello from C", "word_delay_ms": 0})");
    GenerationRequest req;
    req.prompt = "a b";
    req.max_new_tokens = 100;
    std::string text;
    CancelFlag cancel;
    const auto outcome = backend.generate(req, [&](std::string_view p) {
        text += p;
        return SinkAction::Continue;
    }, cancel);
    CHECK(text == "hello from C");
    CHECK(outcome.finish == FinishReason::EndOfSequence);
    CHECK(outcome.stats.completion_tokens == 3);
    CHECK(outcome.stats.prompt_tokens == std::optional<std::uint64_t>(2));
    CHECK_FALSE(outcome.stats.prefill_ms.has_value());  // the plugin reported -1: unknown stays unknown
}

TEST_CASE("plugin honours max_new_tokens, sink stop and cancellation") {
    PluginBackend backend(LCA_REFERENCE_PLUGIN_PATH, R"({"word_delay_ms": 1})");
    GenerationRequest req;
    req.prompt = "go";
    req.max_new_tokens = 5;
    CancelFlag none;
    auto sink = [](std::string_view) { return SinkAction::Continue; };
    CHECK(backend.generate(req, sink, none).finish == FinishReason::Length);

    req.max_new_tokens = 1000;
    int pieces = 0;
    const auto stopped = backend.generate(req, [&](std::string_view) {
        return ++pieces == 2 ? SinkAction::Stop : SinkAction::Continue;
    }, none);
    CHECK(stopped.finish == FinishReason::StoppedBySink);
    CHECK(pieces == 2);

    CancelFlag cancel;
    std::thread canceller([&] {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        cancel.request();
    });
    const auto cancelled = backend.generate(req, sink, cancel);
    canceller.join();
    CHECK(cancelled.finish == FinishReason::Cancelled);
}

TEST_CASE("an exception thrown by the sink crosses the C boundary safely") {
    PluginBackend backend(LCA_REFERENCE_PLUGIN_PATH, R"({"word_delay_ms": 0})");
    GenerationRequest req;
    req.prompt = "go";
    req.max_new_tokens = 10;
    CancelFlag none;
    CHECK_THROWS_WITH(backend.generate(req, [](std::string_view) -> SinkAction { throw std::runtime_error("boom"); },
                                       none),
                      "boom");
}

TEST_CASE("plugin load failures are explicit") {
    CHECK_THROWS_AS(PluginBackend("/nonexistent/libnothing.so", "{}"), BackendError);
    CHECK_THROWS_WITH_AS(PluginBackend(LCA_REFERENCE_PLUGIN_PATH, R"({"max_context_tokens": 0})"),
                         doctest::Contains("max_context_tokens must be positive"), BackendError);
}
