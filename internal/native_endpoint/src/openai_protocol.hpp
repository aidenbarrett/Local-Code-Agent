// OpenAI chat-completions wire protocol: request validation and response
// rendering. Nothing here touches sockets or backends.
//
// Validation fails closed: a field the endpoint cannot honour (n > 1,
// tool_choice "required", JSON response_format, penalties, logprobs) is a 400,
// never silently ignored, because an ignored setting changes the output
// without telling the caller.
#pragma once

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "chat_format.hpp"
#include "inference_backend.hpp"
#include "output_parser.hpp"

namespace lca {

struct ProtocolError {
    int http_status = 400;
    std::string type = "invalid_request_error";
    std::string code;
    std::string message;
    std::optional<std::string> param;
};

class ProtocolException : public std::runtime_error {
public:
    explicit ProtocolException(ProtocolError error)
        : std::runtime_error(error.message), error_(std::move(error)) {}
    const ProtocolError& error() const noexcept { return error_; }

private:
    ProtocolError error_;
};

struct ChatCompletionRequest {
    std::string model;
    std::vector<ChatMessage> messages;
    std::vector<ordered_json> tools;  // rendered only when tool_choice allows
    bool tools_enabled = false;
    bool stream = false;
    bool include_usage = false;
    std::optional<std::uint32_t> max_tokens;
    SamplingParams sampling;
    std::vector<std::string> stop;
    std::optional<bool> enable_thinking;
};

ChatCompletionRequest parse_chat_completion_request(const std::string& body);

ordered_json error_body(const ProtocolError& error);

struct ResponseContext {
    std::string completion_id;  // chatcmpl-...
    std::string model;
    std::string system_fingerprint;
    std::int64_t created = 0;
};

struct ResponseToolCall {
    std::string id;
    ParsedToolCall call;
};

struct UsageFacts {
    std::uint64_t prompt_tokens = 0;
    std::uint64_t completion_tokens = 0;
    std::optional<std::uint64_t> cached_prompt_tokens;
};

struct TimingFacts {
    std::optional<double> prefill_ms;
    std::optional<double> decode_ms;
    std::optional<std::uint64_t> cached_prompt_tokens;
};

// "stop" | "length" | "tool_calls"
std::string openai_finish_reason(FinishReason finish, bool produced_tool_calls);

ordered_json completion_response(const ResponseContext& ctx, const std::string& content,
                                 const std::string& reasoning,
                                 const std::vector<ResponseToolCall>& tool_calls,
                                 const std::string& finish_reason, const UsageFacts& usage,
                                 const TimingFacts& timings);

ordered_json chunk_role(const ResponseContext& ctx);
ordered_json chunk_content(const ResponseContext& ctx, const std::string& text);
ordered_json chunk_reasoning(const ResponseContext& ctx, const std::string& text);
ordered_json chunk_tool_call(const ResponseContext& ctx, std::size_t index,
                             const ResponseToolCall& call);
ordered_json chunk_finish(const ResponseContext& ctx, const std::string& finish_reason);
ordered_json chunk_usage(const ResponseContext& ctx, const UsageFacts& usage,
                         const TimingFacts& timings);

// JSON text that is always valid UTF-8 (invalid bytes become U+FFFD).
std::string to_wire(const ordered_json& value);

// "data: {...}\n\n"
std::string sse_event(const ordered_json& value);

}  // namespace lca
