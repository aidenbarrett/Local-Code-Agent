#include "openai_protocol.hpp"

#include <cmath>
#include <limits>

namespace lca {

namespace {

[[noreturn]] void reject(const std::string& message, const std::string& param = {},
                         const std::string& code = "invalid_value") {
    ProtocolError error;
    error.http_status = 400;
    error.code = code;
    error.message = message;
    if (!param.empty()) error.param = param;
    throw ProtocolException(std::move(error));
}

[[noreturn]] void unsupported(const std::string& message, const std::string& param) {
    reject(message, param, "unsupported_parameter");
}

bool present(const ordered_json& body, const char* key) {
    const auto it = body.find(key);
    return it != body.end() && !it->is_null();
}

std::string message_text(const ordered_json& content, const std::string& param) {
    if (content.is_null()) return {};
    if (content.is_string()) return content.get<std::string>();
    if (content.is_array()) {
        std::string text;
        for (const auto& part : content) {
            if (!part.is_object() || part.value("type", "") != "text" || !part.contains("text") ||
                !part["text"].is_string()) {
                unsupported("only text content parts are supported", param);
            }
            text += part["text"].get<std::string>();
        }
        return text;
    }
    reject("message content must be a string, an array of text parts or null", param);
}

double number_in(const ordered_json& body, const char* key, double lo, double hi,
                 bool lo_inclusive) {
    const auto& v = body.at(key);
    if (!v.is_number()) reject(std::string(key) + " must be a number", key);
    const double d = v.get<double>();
    if (!std::isfinite(d) || d > hi || d < lo || (!lo_inclusive && d == lo)) {
        reject(std::string(key) + " is out of range", key);
    }
    return d;
}

ChatMessage parse_message(const ordered_json& m, std::size_t index) {
    const std::string where = "messages[" + std::to_string(index) + "]";
    if (!m.is_object()) reject(where + " must be an object", where);
    if (!m.contains("role") || !m["role"].is_string()) reject(where + ".role is required", where);

    ChatMessage out;
    out.role = m["role"].get<std::string>();
    if (out.role == "developer") out.role = "system";
    if (out.role != "system" && out.role != "user" && out.role != "assistant" && out.role != "tool") {
        reject(where + ".role '" + out.role + "' is not supported", where + ".role");
    }
    out.content = message_text(m.contains("content") ? m["content"] : ordered_json(), where + ".content");

    if (out.role == "assistant") {
        if (present(m, "reasoning_content")) {
            if (!m["reasoning_content"].is_string()) {
                reject(where + ".reasoning_content must be a string", where);
            }
            out.reasoning_content = m["reasoning_content"].get<std::string>();
        }
        if (present(m, "tool_calls")) {
            const auto& calls = m["tool_calls"];
            if (!calls.is_array()) reject(where + ".tool_calls must be an array", where);
            for (std::size_t k = 0; k < calls.size(); ++k) {
                const auto& c = calls[k];
                const std::string cw = where + ".tool_calls[" + std::to_string(k) + "]";
                if (!c.is_object() || !c.contains("function") || !c["function"].is_object()) {
                    reject(cw + " must contain a function object", cw);
                }
                const auto& fn = c["function"];
                if (!fn.contains("name") || !fn["name"].is_string()) {
                    reject(cw + ".function.name is required", cw);
                }
                ChatToolCall call;
                call.id = c.value("id", "");
                call.name = fn["name"].get<std::string>();
                if (!fn.contains("arguments") || fn["arguments"].is_null()) {
                    call.arguments_text = "{}";
                } else if (fn["arguments"].is_string()) {
                    call.arguments_text = fn["arguments"].get<std::string>();
                } else if (fn["arguments"].is_object()) {
                    call.arguments_text = python_style_json(fn["arguments"]);
                } else {
                    reject(cw + ".function.arguments must be a string or object", cw);
                }
                out.tool_calls.push_back(std::move(call));
            }
        }
    }
    return out;
}

ordered_json chunk_base(const ResponseContext& ctx) {
    ordered_json chunk;
    chunk["id"] = ctx.completion_id;
    chunk["object"] = "chat.completion.chunk";
    chunk["created"] = ctx.created;
    chunk["model"] = ctx.model;
    chunk["system_fingerprint"] = ctx.system_fingerprint;
    return chunk;
}

ordered_json chunk_with_delta(const ResponseContext& ctx, ordered_json delta,
                              const ordered_json& finish_reason) {
    ordered_json choice;
    choice["index"] = 0;
    choice["delta"] = std::move(delta);
    choice["finish_reason"] = finish_reason;
    ordered_json chunk = chunk_base(ctx);
    chunk["choices"] = ordered_json::array({std::move(choice)});
    return chunk;
}

ordered_json tool_call_json(const ResponseToolCall& call) {
    ordered_json fn;
    fn["name"] = call.call.name;
    fn["arguments"] = call.call.arguments_json;
    ordered_json out;
    out["id"] = call.id;
    out["type"] = "function";
    out["function"] = std::move(fn);
    return out;
}

ordered_json usage_json(const UsageFacts& usage) {
    ordered_json out;
    out["prompt_tokens"] = usage.prompt_tokens;
    out["completion_tokens"] = usage.completion_tokens;
    out["total_tokens"] = usage.prompt_tokens + usage.completion_tokens;
    if (usage.cached_prompt_tokens) {
        out["prompt_tokens_details"] = {{"cached_tokens", *usage.cached_prompt_tokens}};
    }
    return out;
}

// llama.cpp-style "timings" block. Present only with backend-reported values;
// the agent's client records these as server-reported, never as its own clock.
void attach_timings(ordered_json& target, const TimingFacts& timings) {
    if (!timings.prefill_ms && !timings.decode_ms && !timings.cached_prompt_tokens) return;
    ordered_json t = ordered_json::object();
    if (timings.prefill_ms) t["prompt_ms"] = *timings.prefill_ms;
    if (timings.decode_ms) t["predicted_ms"] = *timings.decode_ms;
    if (timings.cached_prompt_tokens) t["cache_n"] = *timings.cached_prompt_tokens;
    target["timings"] = std::move(t);
}

}  // namespace

ChatCompletionRequest parse_chat_completion_request(const std::string& body_text) {
    auto body = ordered_json::parse(body_text, nullptr, /*allow_exceptions=*/false);
    if (body.is_discarded() || !body.is_object()) reject("request body must be a JSON object");

    ChatCompletionRequest req;
    if (!body.contains("model") || !body["model"].is_string()) reject("model is required", "model");
    req.model = body["model"].get<std::string>();

    if (!body.contains("messages") || !body["messages"].is_array() || body["messages"].empty()) {
        reject("messages must be a non-empty array", "messages");
    }
    for (std::size_t i = 0; i < body["messages"].size(); ++i) {
        req.messages.push_back(parse_message(body["messages"][i], i));
    }

    if (present(body, "tools")) {
        const auto& tools = body["tools"];
        if (!tools.is_array()) reject("tools must be an array", "tools");
        for (std::size_t i = 0; i < tools.size(); ++i) {
            const auto& t = tools[i];
            if (!t.is_object() || t.value("type", "") != "function" || !t.contains("function") ||
                !t["function"].is_object() || !t["function"].contains("name") ||
                !t["function"]["name"].is_string()) {
                reject("tools[" + std::to_string(i) + "] must be a function tool with a name", "tools");
            }
            req.tools.push_back(t);
        }
    }
    std::string tool_choice = "auto";
    if (present(body, "tool_choice")) {
        if (!body["tool_choice"].is_string()) {
            unsupported("tool_choice naming a specific function is not supported", "tool_choice");
        }
        tool_choice = body["tool_choice"].get<std::string>();
        if (tool_choice == "required") {
            unsupported("tool_choice 'required' is not supported", "tool_choice");
        }
        if (tool_choice != "auto" && tool_choice != "none") {
            reject("tool_choice must be 'auto' or 'none'", "tool_choice");
        }
    }
    req.tools_enabled = !req.tools.empty() && tool_choice == "auto";
    if (!req.tools_enabled) req.tools.clear();

    if (present(body, "n")) {
        if (!body["n"].is_number_integer() || body["n"].get<long long>() != 1) {
            unsupported("only n=1 is supported", "n");
        }
    }
    if (present(body, "stream")) {
        if (!body["stream"].is_boolean()) reject("stream must be a boolean", "stream");
        req.stream = body["stream"].get<bool>();
    }
    if (present(body, "stream_options")) {
        const auto& so = body["stream_options"];
        if (!so.is_object()) reject("stream_options must be an object", "stream_options");
        if (so.contains("include_usage")) {
            if (!so["include_usage"].is_boolean()) {
                reject("stream_options.include_usage must be a boolean", "stream_options");
            }
            req.include_usage = so["include_usage"].get<bool>();
        }
    }
    for (const char* key : {"max_completion_tokens", "max_tokens"}) {
        if (present(body, key)) {
            const auto& v = body[key];
            if (!v.is_number_integer() || v.get<long long>() < 1 ||
                v.get<long long>() > std::numeric_limits<std::uint32_t>::max()) {
                reject(std::string(key) + " must be a positive integer", key);
            }
            req.max_tokens = static_cast<std::uint32_t>(v.get<long long>());
            break;
        }
    }

    req.sampling.temperature = 1.0f;
    req.sampling.top_p = 1.0f;
    if (present(body, "temperature")) {
        req.sampling.temperature = static_cast<float>(number_in(body, "temperature", 0.0, 2.0, true));
    }
    if (present(body, "top_p")) {
        req.sampling.top_p = static_cast<float>(number_in(body, "top_p", 0.0, 1.0, false));
    }
    if (present(body, "top_k")) {
        if (!body["top_k"].is_number_integer()) reject("top_k must be an integer", "top_k");
        const auto k = body["top_k"].get<long long>();
        if (k > 0) {
            if (k > std::numeric_limits<std::int32_t>::max()) reject("top_k is out of range", "top_k");
            req.sampling.top_k = static_cast<std::int32_t>(k);
        }
    }
    if (present(body, "min_p")) {
        req.sampling.min_p = static_cast<float>(number_in(body, "min_p", 0.0, 1.0, true));
        if (req.sampling.min_p >= 1.0f) reject("min_p must be below 1", "min_p");
    }
    if (present(body, "seed")) {
        if (!body["seed"].is_number_integer()) reject("seed must be an integer", "seed");
        req.sampling.seed = static_cast<std::uint64_t>(body["seed"].get<long long>());
    }
    if (present(body, "stop")) {
        const auto& stop = body["stop"];
        if (stop.is_string()) {
            req.stop.push_back(stop.get<std::string>());
        } else if (stop.is_array()) {
            if (stop.size() > 16) reject("at most 16 stop strings are supported", "stop");
            for (const auto& s : stop) {
                if (!s.is_string()) reject("stop entries must be strings", "stop");
                req.stop.push_back(s.get<std::string>());
            }
        } else {
            reject("stop must be a string or an array of strings", "stop");
        }
    }
    if (present(body, "response_format")) {
        const auto& rf = body["response_format"];
        if (!rf.is_object() || rf.value("type", "") != "text") {
            unsupported("only response_format type 'text' is supported", "response_format");
        }
    }
    if (present(body, "logprobs") && body["logprobs"].is_boolean() && body["logprobs"].get<bool>()) {
        unsupported("logprobs are not supported", "logprobs");
    }
    for (const char* key : {"presence_penalty", "frequency_penalty"}) {
        if (present(body, key)) {
            if (!body[key].is_number()) reject(std::string(key) + " must be a number", key);
            if (body[key].get<double>() != 0.0) unsupported(std::string(key) + " is not supported", key);
        }
    }
    if (present(body, "logit_bias") && !(body["logit_bias"].is_object() && body["logit_bias"].empty())) {
        unsupported("logit_bias is not supported", "logit_bias");
    }
    if (present(body, "chat_template_kwargs")) {
        const auto& kw = body["chat_template_kwargs"];
        if (!kw.is_object()) reject("chat_template_kwargs must be an object", "chat_template_kwargs");
        if (kw.contains("enable_thinking")) {
            if (!kw["enable_thinking"].is_boolean()) {
                reject("chat_template_kwargs.enable_thinking must be a boolean", "chat_template_kwargs");
            }
            req.enable_thinking = kw["enable_thinking"].get<bool>();
        }
    }
    return req;
}

ordered_json error_body(const ProtocolError& error) {
    ordered_json e;
    e["message"] = error.message;
    e["type"] = error.type;
    e["param"] = error.param ? ordered_json(*error.param) : ordered_json();
    e["code"] = error.code.empty() ? ordered_json() : ordered_json(error.code);
    return ordered_json{{"error", e}};
}

std::string openai_finish_reason(FinishReason finish, bool produced_tool_calls) {
    if (finish == FinishReason::Length) return "length";
    if (produced_tool_calls) return "tool_calls";
    return "stop";
}

ordered_json completion_response(const ResponseContext& ctx, const std::string& content,
                                 const std::string& reasoning,
                                 const std::vector<ResponseToolCall>& tool_calls,
                                 const std::string& finish_reason, const UsageFacts& usage,
                                 const TimingFacts& timings) {
    ordered_json message;
    message["role"] = "assistant";
    message["content"] = (content.empty() && !tool_calls.empty()) ? ordered_json() : ordered_json(content);
    if (!reasoning.empty()) message["reasoning_content"] = reasoning;
    if (!tool_calls.empty()) {
        ordered_json calls = ordered_json::array();
        for (const auto& c : tool_calls) calls.push_back(tool_call_json(c));
        message["tool_calls"] = std::move(calls);
    }
    ordered_json choice;
    choice["index"] = 0;
    choice["message"] = std::move(message);
    choice["finish_reason"] = finish_reason;

    ordered_json out;
    out["id"] = ctx.completion_id;
    out["object"] = "chat.completion";
    out["created"] = ctx.created;
    out["model"] = ctx.model;
    out["system_fingerprint"] = ctx.system_fingerprint;
    out["choices"] = ordered_json::array({std::move(choice)});
    out["usage"] = usage_json(usage);
    attach_timings(out, timings);
    return out;
}

ordered_json chunk_role(const ResponseContext& ctx) {
    return chunk_with_delta(ctx, {{"role", "assistant"}, {"content", ""}}, nullptr);
}

ordered_json chunk_content(const ResponseContext& ctx, const std::string& text) {
    return chunk_with_delta(ctx, {{"content", text}}, nullptr);
}

ordered_json chunk_reasoning(const ResponseContext& ctx, const std::string& text) {
    return chunk_with_delta(ctx, {{"reasoning_content", text}}, nullptr);
}

ordered_json chunk_tool_call(const ResponseContext& ctx, std::size_t index,
                             const ResponseToolCall& call) {
    ordered_json entry = tool_call_json(call);
    ordered_json indexed;
    indexed["index"] = index;
    for (auto it = entry.begin(); it != entry.end(); ++it) indexed[it.key()] = it.value();
    return chunk_with_delta(ctx, {{"tool_calls", ordered_json::array({std::move(indexed)})}}, nullptr);
}

ordered_json chunk_finish(const ResponseContext& ctx, const std::string& finish_reason) {
    return chunk_with_delta(ctx, ordered_json::object(), finish_reason);
}

ordered_json chunk_usage(const ResponseContext& ctx, const UsageFacts& usage,
                         const TimingFacts& timings) {
    ordered_json chunk = chunk_base(ctx);
    chunk["choices"] = ordered_json::array();
    chunk["usage"] = usage_json(usage);
    attach_timings(chunk, timings);
    return chunk;
}

std::string to_wire(const ordered_json& value) {
    return value.dump(-1, ' ', false, ordered_json::error_handler_t::replace);
}

std::string sse_event(const ordered_json& value) { return "data: " + to_wire(value) + "\n\n"; }

}  // namespace lca
