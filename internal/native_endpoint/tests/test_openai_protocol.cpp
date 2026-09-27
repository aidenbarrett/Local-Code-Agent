#include <doctest/doctest.h>

#include "openai_protocol.hpp"

using lca::ChatCompletionRequest;
using lca::ordered_json;
using lca::parse_chat_completion_request;
using lca::ProtocolException;

namespace {

std::string code_of(const std::string& body) {
    try {
        parse_chat_completion_request(body);
    } catch (const ProtocolException& e) {
        return e.error().code;
    }
    return "accepted";
}

}  // namespace

TEST_CASE("the product client's request shape is accepted") {
    // What internal/local_agent/llm/client.py sends, extra_body merged.
    const auto req = parse_chat_completion_request(R"({
        "model": "m", "messages": [{"role": "developer", "content": "sys"},
                                   {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}],
        "temperature": 0.6, "top_p": 0.95, "max_tokens": 512, "top_k": 20, "min_p": 0,
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}], "tool_choice": "auto",
        "stream": true, "stream_options": {"include_usage": true},
        "chat_template_kwargs": {"enable_thinking": false}})");
    CHECK(req.model == "m");
    CHECK(req.messages[0].role == "system");
    CHECK(req.messages[1].content == "ab");
    CHECK(req.tools_enabled);
    CHECK(req.stream);
    CHECK(req.include_usage);
    CHECK(*req.max_tokens == 512);
    CHECK(*req.sampling.top_k == 20);
    CHECK(req.enable_thinking == std::optional<bool>(false));
}

TEST_CASE("assistant tool calls and tool results parse") {
    const auto req = parse_chat_completion_request(R"({"model": "m", "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": null, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{\"p\": 1}"}},
            {"id": "c2", "type": "function", "function": {"name": "cat", "arguments": {"p": 2}}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "out"}]})");
    REQUIRE(req.messages[1].tool_calls.size() == 2);
    CHECK(req.messages[1].tool_calls[0].arguments_text == "{\"p\": 1}");
    CHECK(req.messages[1].tool_calls[1].arguments_text == "{\"p\": 2}");
    CHECK(req.messages[2].role == "tool");
}

TEST_CASE("tool_choice none hides the tools") {
    const auto req = parse_chat_completion_request(
        R"({"model": "m", "messages": [{"role": "user", "content": "q"}], "tool_choice": "none",
            "tools": [{"type": "function", "function": {"name": "f"}}]})");
    CHECK_FALSE(req.tools_enabled);
    CHECK(req.tools.empty());
}

TEST_CASE("settings the endpoint cannot honour are refused, not ignored") {
    const std::string base = R"("model": "m", "messages": [{"role": "user", "content": "q"}])";
    CHECK(code_of("{" + base + ", \"n\": 2}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"tool_choice\": \"required\"}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"tool_choice\": {\"type\": \"function\"}}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"response_format\": {\"type\": \"json_object\"}}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"logprobs\": true}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"presence_penalty\": 0.5}") == "unsupported_parameter");
    CHECK(code_of("{" + base + ", \"presence_penalty\": 0}") == "accepted");
    CHECK(code_of("{" + base + ", \"response_format\": {\"type\": \"text\"}}") == "accepted");
}

TEST_CASE("malformed requests are 400s") {
    CHECK(code_of("not json") == "invalid_value");
    CHECK(code_of(R"({"messages": [{"role": "user", "content": "q"}]})") == "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": []})") == "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": [{"role": "robot", "content": "q"}]})") == "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": [{"role": "user", "content": "q"}], "temperature": 3})") ==
          "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": [{"role": "user", "content": "q"}], "top_p": 0})") ==
          "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": [{"role": "user", "content": "q"}], "max_tokens": 0})") ==
          "invalid_value");
    CHECK(code_of(R"({"model": "m", "messages": [{"role": "user", "content": [{"type": "image_url"}]}]})") ==
          "unsupported_parameter");
}

TEST_CASE("unary response: tool calls null the content, timings only when reported") {
    lca::ResponseContext ctx{"chatcmpl-x", "m", "fp", 1};
    lca::ParsedToolCall call{"ls", "{}"};
    const auto body = lca::completion_response(ctx, "", "", {{"call_1", call}}, "tool_calls", {10, 3, std::nullopt},
                                               lca::TimingFacts{});
    CHECK(body["choices"][0]["message"]["content"].is_null());
    CHECK(body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == "{}");
    CHECK(body["choices"][0]["finish_reason"] == "tool_calls");
    CHECK(body["usage"]["total_tokens"] == 13);
    CHECK_FALSE(body.contains("timings"));

    const auto timed = lca::completion_response(ctx, "hi", "", {}, "stop", {1, 1, 4},
                                                lca::TimingFacts{12.5, 30.0, 4});
    CHECK(timed["timings"]["prompt_ms"] == 12.5);
    CHECK(timed["timings"]["predicted_ms"] == 30.0);
    CHECK(timed["timings"]["cache_n"] == 4);
    CHECK(timed["usage"]["prompt_tokens_details"]["cached_tokens"] == 4);
}

TEST_CASE("stream chunks carry the OpenAI shapes") {
    lca::ResponseContext ctx{"chatcmpl-x", "m", "fp", 1};
    const auto usage = lca::chunk_usage(ctx, {5, 2, std::nullopt}, lca::TimingFacts{});
    CHECK(usage["choices"].empty());
    CHECK(usage["usage"]["completion_tokens"] == 2);
    const auto tool = lca::chunk_tool_call(ctx, 1, {"call_9", {"f", "{\"a\": 1}"}});
    CHECK(tool["choices"][0]["delta"]["tool_calls"][0]["index"] == 1);
    CHECK(tool["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_9");
    const auto finish = lca::chunk_finish(ctx, "length");
    CHECK(finish["choices"][0]["finish_reason"] == "length");
    CHECK(lca::sse_event(finish).rfind("data: {", 0) == 0);
}

TEST_CASE("wire JSON is valid UTF-8 even from invalid bytes") {
    ordered_json v = std::string("ok \xff");
    CHECK(lca::to_wire(v) == "\"ok \xEF\xBF\xBD\"");
}

TEST_CASE("finish reasons") {
    CHECK(lca::openai_finish_reason(lca::FinishReason::Length, true) == "length");
    CHECK(lca::openai_finish_reason(lca::FinishReason::EndOfSequence, true) == "tool_calls");
    CHECK(lca::openai_finish_reason(lca::FinishReason::StoppedBySink, false) == "stop");
}
