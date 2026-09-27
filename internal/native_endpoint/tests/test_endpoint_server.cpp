#include <doctest/doctest.h>

#include <httplib.h>

#include <thread>

#include "endpoint_server.hpp"
#include "fixture_backend.hpp"
#include "openai_protocol.hpp"

using namespace lca;
using namespace std::chrono_literals;

namespace {

FixtureConfig qualification_fixture() {
    FixtureConfig c;
    c.model_id = "fixture-model";
    c.max_context_tokens = 256;
    c.piece_delay = 1ms;
    c.rules.push_back({"Call echo_value",
                       "<tool_call>\n{\"name\": \"echo_value\", \"arguments\": {\"value\": \"READY\"}}\n</tool_call>"});
    c.rules.push_back({"Think first", "<think>\nweighing it\n</think>\n\nDecided."});
    c.rules.push_back({"Reply with exactly READY", "READY"});
    // A backend that streams special tokens verbatim, as the GenAI one does.
    c.rules.push_back({"Stream special tokens", "<tool_call>\n{\"name\": \"echo_value\", \"arguments\": {}}\n</tool_call><|im_end|>\nleaked"});
    c.rules.push_back({"End the turn", "done<|im_end|>\nnot part of the answer"});
    return c;  // no default reply: endless filler
}

struct RunningServer {
    explicit RunningServer(EndpointOptions options = {}, FixtureConfig config = qualification_fixture())
        : server(std::move(options), [config] { return std::make_unique<FixtureBackend>(config, "test"); }) {
        port = server.start();
        REQUIRE(server.wait_until_loaded(10s));
    }
    httplib::Client client() const {
        httplib::Client c("127.0.0.1", port);
        c.set_read_timeout(10, 0);
        return c;
    }
    EndpointServer server;
    int port = 0;
};

std::string chat_body(const std::string& user, bool stream, const std::string& extra = "") {
    std::string body = R"({"model": "fixture-model", "messages": [{"role": "user", "content": ")" + user +
                       R"("}], "stream": )" + (stream ? "true" : "false");
    if (stream) body += R"(, "stream_options": {"include_usage": true})";
    return body + extra + "}";
}

struct SseResult {
    std::vector<ordered_json> events;
    bool done = false;
    httplib::Error error = httplib::Error::Success;
    int status = 0;
};

// Streams a chat request. stop_after_content > 0 abandons the connection
// once that many content chunks have arrived.
SseResult stream_chat(const RunningServer& s, const std::string& body, int stop_after_content = 0,
                      const std::function<void()>& before_abort = {}) {
    SseResult out;
    std::string buffer;
    int content_chunks = 0;
    auto cli = s.client();
    auto res = cli.Post(
        "/v1/chat/completions", httplib::Headers{}, body.size(),
        [&](std::size_t offset, std::size_t length, httplib::DataSink& sink) {
            sink.write(body.data() + offset, length);
            return true;
        },
        "application/json",
        [&](const char* data, std::size_t len) {
            buffer.append(data, len);
            for (auto end = buffer.find("\n\n"); end != std::string::npos; end = buffer.find("\n\n")) {
                const std::string event = buffer.substr(0, end);
                buffer.erase(0, end + 2);
                if (event.rfind("data: ", 0) != 0) continue;
                const std::string payload = event.substr(6);
                if (payload == "[DONE]") {
                    out.done = true;
                    continue;
                }
                out.events.push_back(ordered_json::parse(payload));
                const auto& e = out.events.back();
                if (e.contains("choices") && !e["choices"].empty() &&
                    e["choices"][0]["delta"].contains("content") &&
                    !e["choices"][0]["delta"]["content"].get<std::string>().empty()) {
                    if (stop_after_content > 0 && ++content_chunks >= stop_after_content) {
                        if (before_abort) before_abort();
                        return false;
                    }
                }
            }
            return true;
        });
    out.error = res.error();
    if (res) out.status = res->status;
    return out;
}

std::string streamed_content(const SseResult& r) {
    std::string text;
    for (const auto& e : r.events) {
        if (e.contains("choices") && !e["choices"].empty() && e["choices"][0]["delta"].contains("content")) {
            text += e["choices"][0]["delta"]["content"].get<std::string>();
        }
    }
    return text;
}

bool eventually(const std::function<bool()>& condition, std::chrono::milliseconds limit = 3000ms) {
    const auto deadline = std::chrono::steady_clock::now() + limit;
    while (std::chrono::steady_clock::now() < deadline) {
        if (condition()) return true;
        std::this_thread::sleep_for(5ms);
    }
    return false;
}

ordered_json get_json(const RunningServer& s, const std::string& path) {
    auto cli = s.client();
    auto res = cli.Get(path);
    REQUIRE(res);
    return ordered_json::parse(res->body);
}

}  // namespace

TEST_CASE("health, models and identity describe the served backend") {
    RunningServer s;
    auto cli = s.client();
    auto health = cli.Get("/health");
    REQUIRE(health);
    CHECK(health->status == 200);
    CHECK(ordered_json::parse(health->body)["status"] == "ready");

    const auto models = get_json(s, "/v1/models");
    CHECK(models["data"][0]["id"] == "fixture-model");
    CHECK(get_json(s, "/v3/models") == models);

    const auto id = get_json(s, "/identity");
    CHECK(id["server"]["name"] == "lca-endpoint");
    CHECK(id["runtime"]["name"] == "lca-fixture");
    CHECK(id["device"]["observed"].is_null());  // the fixture never claims hardware
    CHECK(id["model"]["id"] == "fixture-model");
    CHECK(id["limits"]["max_context_tokens"] == 256);
    CHECK(id["backend"]["kind"] == "fixture");
}

TEST_CASE("unary chat returns content, usage and a request id") {
    RunningServer s;
    auto cli = s.client();
    auto res = cli.Post("/v1/chat/completions", chat_body("Reply with exactly READY", false), "application/json");
    REQUIRE(res);
    CHECK(res->status == 200);
    CHECK(res->get_header_value("x-request-id").rfind("req-", 0) == 0);
    const auto body = ordered_json::parse(res->body);
    CHECK(body["object"] == "chat.completion");
    CHECK(body["choices"][0]["message"]["content"] == "READY");
    CHECK(body["choices"][0]["finish_reason"] == "stop");
    CHECK(body["usage"]["completion_tokens"].get<int>() > 0);
    CHECK(body["timings"].contains("prompt_ms"));
    CHECK(body["system_fingerprint"].get<std::string>().find("lca-fixture") != std::string::npos);

    auto v3 = cli.Post("/v3/chat/completions", chat_body("Reply with exactly READY", false), "application/json");
    REQUIRE(v3);
    CHECK(ordered_json::parse(v3->body)["choices"][0]["message"]["content"] == "READY");
}

TEST_CASE("streamed chat ends with finish, usage and [DONE]") {
    RunningServer s;
    const auto r = stream_chat(s, chat_body("Reply with exactly READY", true));
    CHECK(r.error == httplib::Error::Success);
    CHECK(r.done);
    CHECK(streamed_content(r) == "READY");
    REQUIRE(r.events.size() >= 3);
    CHECK(r.events.front()["choices"][0]["delta"]["role"] == "assistant");
    const auto& usage = r.events.back();
    CHECK(usage["choices"].empty());
    CHECK(usage["usage"]["prompt_tokens"].get<int>() > 0);
    const auto& finish = r.events[r.events.size() - 2];
    CHECK(finish["choices"][0]["finish_reason"] == "stop");
}

TEST_CASE("tool calls are structured when tools are offered") {
    RunningServer s;
    const std::string tools =
        R"(, "tools": [{"type": "function", "function": {"name": "echo_value", "parameters": {"type": "object"}}}])";
    auto cli = s.client();
    auto res = cli.Post("/v1/chat/completions", chat_body("Call echo_value with value READY.", false, tools),
                        "application/json");
    REQUIRE(res);
    const auto body = ordered_json::parse(res->body);
    CHECK(body["choices"][0]["finish_reason"] == "tool_calls");
    CHECK(body["choices"][0]["message"]["content"].is_null());
    const auto& call = body["choices"][0]["message"]["tool_calls"][0];
    CHECK(call["function"]["name"] == "echo_value");
    CHECK(ordered_json::parse(call["function"]["arguments"].get<std::string>())["value"] == "READY");

    const auto streamed = stream_chat(s, chat_body("Call echo_value with value READY.", true, tools));
    bool saw_call = false;
    for (const auto& e : streamed.events) {
        if (!e["choices"].empty() && e["choices"][0]["delta"].contains("tool_calls")) {
            saw_call = e["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "echo_value";
        }
    }
    CHECK(saw_call);
}

TEST_CASE("the template's end-of-turn marker ends the answer and is never emitted") {
    RunningServer s;
    auto cli = s.client();
    auto res = cli.Post("/v1/chat/completions", chat_body("End the turn", false), "application/json");
    REQUIRE(res);
    const auto body = ordered_json::parse(res->body);
    CHECK(body["choices"][0]["message"]["content"] == "done");
    CHECK(body["choices"][0]["finish_reason"] == "stop");

    const auto streamed = stream_chat(s, chat_body("End the turn", true));
    CHECK(streamed.done);
    CHECK(streamed_content(streamed) == "done");

    const std::string tools = R"(, "tools": [{"type": "function", "function": {"name": "echo_value"}}])";
    auto call = cli.Post("/v1/chat/completions", chat_body("Stream special tokens", false, tools), "application/json");
    REQUIRE(call);
    const auto call_body = ordered_json::parse(call->body);
    CHECK(call_body["choices"][0]["finish_reason"] == "tool_calls");
    CHECK(call_body["choices"][0]["message"]["content"].is_null());
    CHECK(call_body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "echo_value");
}

TEST_CASE("reasoning is separated from the answer") {
    RunningServer s;
    auto cli = s.client();
    auto res = cli.Post("/v1/chat/completions", chat_body("Think first", false), "application/json");
    REQUIRE(res);
    const auto body = ordered_json::parse(res->body);
    CHECK(body["choices"][0]["message"]["content"] == "Decided.");
    CHECK(body["choices"][0]["message"]["reasoning_content"] == "\nweighing it\n");
}

TEST_CASE("wrong model, bad request and oversized prompt fail cleanly") {
    RunningServer s;
    auto cli = s.client();
    auto wrong = cli.Post("/v1/chat/completions",
                          R"({"model": "other", "messages": [{"role": "user", "content": "q"}]})", "application/json");
    REQUIRE(wrong);
    CHECK(wrong->status == 404);
    CHECK(ordered_json::parse(wrong->body)["error"]["code"] == "model_not_found");

    auto bad = cli.Post("/v1/chat/completions", "{", "application/json");
    REQUIRE(bad);
    CHECK(bad->status == 400);

    std::string words;
    for (int i = 0; i < 400; ++i) words += "x ";
    for (bool stream : {false, true}) {
        auto big = cli.Post("/v1/chat/completions", chat_body(words, stream), "application/json");
        REQUIRE(big);
        CHECK(big->status == 400);
        CHECK(ordered_json::parse(big->body)["error"]["code"] == "context_length_exceeded");
    }
    CHECK(get_json(s, "/telemetry")["counters"]["rejected_context"] == 2);
}

TEST_CASE("disconnecting a stream stops inference, proven by the active-request probe") {
    RunningServer s;
    std::string seen_id;
    // No rule matches, so the fixture streams filler until cancelled. Like the
    // endpoint harness, prove the request is active before disconnecting.
    const auto r = stream_chat(s, chat_body("keep going", true, R"(, "max_tokens": 100000)"), 3, [&] {
        const auto active = get_json(s, "/debug/active-requests");
        REQUIRE(active["active_request_ids"].size() == 1);
        CHECK(active["requests"][0]["state"] == "running");
        seen_id = active["active_request_ids"][0].get<std::string>();
    });
    CHECK(r.error == httplib::Error::Canceled);
    CHECK_FALSE(r.done);
    REQUIRE_FALSE(seen_id.empty());
    CHECK(eventually([&] { return get_json(s, "/debug/active-requests")["active_request_ids"].empty(); }));
    const auto telemetry = get_json(s, "/telemetry");
    CHECK(telemetry["counters"]["cancelled"] == 1);
    CHECK(telemetry["last_request"]["request_id"] == seen_id);
    CHECK(telemetry["last_request"]["finish_reason"] == "cancelled");
}

TEST_CASE("telemetry reports startup and the last request with explicit sources") {
    RunningServer s;
    auto cli = s.client();
    REQUIRE(cli.Post("/v1/chat/completions", chat_body("Reply with exactly READY", false), "application/json"));
    const auto t = get_json(s, "/telemetry");
    CHECK(t["startup"]["backend_load_ms"].is_number());
    CHECK(t["startup"]["backend_compile_ms"].is_null());
    CHECK(t["last_request"]["backend_prefill_ms"].is_number());
    CHECK(t["last_request"]["endpoint_generate_ms"].is_number());
#if defined(__linux__) || defined(_WIN32)
    CHECK(t["process"]["rss_bytes"].get<std::uint64_t>() > 0);
#endif
}

TEST_CASE("an api key protects every route except health") {
    EndpointOptions options;
    options.api_key = "sekrit";
    RunningServer s(options);
    auto cli = s.client();
    CHECK(cli.Get("/health")->status == 200);
    CHECK(cli.Get("/v1/models")->status == 401);
    CHECK(cli.Get("/debug/active-requests")->status == 401);
    httplib::Headers auth{{"Authorization", "Bearer sekrit"}};
    CHECK(cli.Get("/v1/models", auth)->status == 200);
}

TEST_CASE("non-loopback binds are refused unless explicitly allowed") {
    EndpointOptions options;
    options.host = "0.0.0.0";
    EndpointServer server(options, [] { return std::make_unique<FixtureBackend>(FixtureConfig{}, "test"); });
    CHECK_THROWS_AS(server.start(), std::runtime_error);
}

TEST_CASE("a backend that fails to load leaves the endpoint reporting failure") {
    EndpointServer server(EndpointOptions{}, []() -> std::unique_ptr<InferenceBackend> {
        throw BackendError(BackendErrorKind::Unavailable, "no such model");
    });
    const int port = server.start();
    CHECK_FALSE(server.wait_until_loaded(5s));
    CHECK(server.load_error().value_or("").find("no such model") != std::string::npos);
    httplib::Client cli("127.0.0.1", port);
    auto health = cli.Get("/health");
    REQUIRE(health);
    CHECK(health->status == 503);
    CHECK(ordered_json::parse(health->body)["status"] == "failed");
    auto chat = cli.Post("/v1/chat/completions", chat_body("q", false), "application/json");
    REQUIRE(chat);
    CHECK(chat->status == 503);
    CHECK_FALSE(server.wait());
}

TEST_CASE("the endpoint answers while the backend is still loading") {
    FixtureConfig slow = qualification_fixture();
    slow.load_delay = 300ms;
    EndpointServer server(EndpointOptions{}, [slow] { return std::make_unique<FixtureBackend>(slow, "test"); });
    const int port = server.start();
    httplib::Client cli("127.0.0.1", port);
    auto early = cli.Get("/v1/models");
    REQUIRE(early);
    CHECK(early->status == 503);
    CHECK(ordered_json::parse(early->body)["error"]["code"] == "backend_loading");
    REQUIRE(server.wait_until_loaded(10s));
    CHECK(cli.Get("/v1/models")->status == 200);
}
